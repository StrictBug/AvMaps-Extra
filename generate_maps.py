import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
import argparse
import bz2
import cfgrib
import json
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from cartopy.io.shapereader import Reader, natural_earth
from cartopy.feature import ShapelyFeature
from shapely.geometry import Point
from shapely.prepared import prep
from shapely.ops import unary_union
from siphon.catalog import TDSCatalog
import xarray as xr
import numpy as np
from datetime import datetime, timedelta, timezone
import os
import shutil
import tempfile
import requests
import re
from concurrent.futures import ThreadPoolExecutor
from scipy.spatial import cKDTree
from scipy import ndimage
import pandas as pd

# Define the Australia domain
# SW corner: 47°57'S, 103°34'E
# NE corner: 22°33'S, 172°7'E
lat_min = -47.95  # 47°57'S
lat_max = -22.55  # 22°33'S
lon_min = 103.5667  # 103°34'E
lon_max = 172.1167  # 172°7'E

# Expand the data ingest domain slightly west of the displayed AU domain so
# the WA_SA regional view has complete source coverage without changing the
# visual extent of any domain.
data_lat_min = lat_min
data_lat_max = lat_max
data_lon_min = 101.0
data_lon_max = lon_max

REFERENCE_DOMAIN_ASPECT_RATIO = (lon_max - lon_min) / (lat_max - lat_min)
DOMAIN_PADDING_RATIO = 0.08
MIN_DOMAIN_PADDING_DEGREES = 0.75

DOMAIN_DEFINITIONS = {
    'AU': {
        'label': 'Australia',
        'fixed_extent': (lon_min, lon_max, lat_min, lat_max),
    },
    'WA_SA': {
        'label': 'WA-S/SA',
        'gaf_areas': ('WA-S', 'SA'),
    },
    'VIC_TAS': {
        'label': 'VIC/TAS',
        'gaf_areas': ('VIC', 'TAS'),
    },
    'NSW': {
        'label': 'NSW',
        'gaf_areas': ('NSW-E', 'NSW-W'),
    },
}
DEFAULT_DOMAIN_IDS = tuple(DOMAIN_DEFINITIONS.keys())

CATEGORY_PANEL_DIRS = {
    'Airmass': ('Airmass/FZL', 'Airmass/4C'),
}

LEGEND_CONFIG = {
    'Airmass': {
        'left': {
            'title': 'FZL',
            'fields': [
                {
                    'id': 'airmass-fzl-freezing-layer',
                    'label': 'Freezing layer',
                    'units': 'hatch',
                    'items': [
                        {'pattern': 'crosshatch', 'label': 'Multiple 0C crossings'},
                    ],
                },
                {
                    'id': 'airmass-fzl-icing',
                    'label': 'Icing severity',
                    'units': '%',
                    'items': [
                        {'color': '#00ccff', 'label': '95-97.5'},
                        {'color': '#00ffff', 'label': '97.5-100'},
                    ],
                },
                {
                    'id': 'airmass-fzl-thermals',
                    'label': 'Thermals',
                    'units': 'ft',
                    'items': [
                        {'color': '#ffcc00', 'label': '6000-10000'},
                        {'color': '#ff6600', 'label': '10000-20000+'},
                    ],
                },
                {
                    'id': 'airmass-fzl-hail',
                    'label': '1 hr small hail',
                    'units': 'mm/h',
                    'items': [
                        {'color': '#b2ebf2', 'label': '0.1-0.5'},
                        {'color': '#00bcd4', 'label': '2.5-5'},
                        {'color': '#0097a7', 'label': '5-10'},
                        {'color': '#006064', 'label': '10-20'},
                        {'color': '#00363a', 'label': '20+'},
                    ],
                },
                {
                    'id': 'airmass-fzl-freezing-fog',
                    'label': 'Freezing fog',
                    'units': 'category',
                    'items': [
                        {'color': '#ff0000', 'label': 'F1'},
                        {'color': '#ffaa7f', 'label': 'F2'},
                        {'color': '#ffff00', 'label': 'F3'},
                    ],
                },
            ],
        },
        'right': {
            'title': '4C isotherm',
            'fields': [
                {
                    'id': 'airmass-4c-height',
                    'label': '4C isotherm height',
                    'units': 'ft AMSL',
                    'items': [
                        {'color': '#99509f', 'label': '0-2000'},
                        {'color': '#36c6f4', 'label': '2000-4000'},
                        {'color': '#6fccdd', 'label': '4000-6000'},
                        {'color': '#69bd45', 'label': '6000-8000'},
                        {'color': '#cbdb2a', 'label': '8000-10000'},
                        {'color': '#f6eb14', 'label': '10000-12000'},
                        {'color': '#fbce08', 'label': '12000-14000'},
                        {'color': '#f8981d', 'label': '14000-16000'},
                        {'color': '#ed2024', 'label': '16000-18000'},
                        {'color': '#ed2891', 'label': '18000-20000+'},
                    ],
                },
                {
                    'id': 'airmass-4c-marginal-icing',
                    'label': 'Marginal icing',
                    'units': 'hatch',
                    'items': [
                        {'pattern': 'crosshatch', 'label': 'Max mean RH 92%+ in 0–4C or -5–0C'},
                    ],
                },
            ],
        },
    },
}

REMOVABLE_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
TAF_LABEL_DX = -0.12
TAF_LABEL_DY = 0.12

OUTPUT_IMAGE_EXT = 'jpg'
OUTPUT_IMAGE_FORMAT = 'jpeg'
OUTPUT_JPEG_QUALITY = 72
BASE_FIGURE_WIDTH_INCHES = 12
BASE_FIGURE_HEIGHT_INCHES = 10
OUTPUT_MAX_WIDTH_PX = 1280
OUTPUT_DPI = OUTPUT_MAX_WIDTH_PX / BASE_FIGURE_WIDTH_INCHES
# Must match savefig(bbox_inches='tight') padding used by save_output_figure.
SAVEFIG_PAD_INCHES = 0.1

# On-disk cache for downloaded ICON GRIB2 files.  Each entry is the raw
# decompressed bytes keyed by run_stamp + folder + filename so that repeated
# runs (e.g. a single-frame re-render) skip the network entirely.
ICON_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.icon_cache')

TAF_LOCATION_ROWS_CACHE = None
GAF_FEATURE_CACHE = None
GAF_AREA_GEOMETRY_CACHE = None
DOMAIN_PROFILE_CACHE = None
LAND_PREP_CACHE = None
OCEAN_MASK_CACHE = {}

# Empirical snow level regression (derived from user-provided calibration points):
# SL_m = b0 + b1*T850_C + b2*H850_m + b3*Thickness_m
SNOW_LEVEL_B0 = -16930.19336594584
SNOW_LEVEL_B1_T850 = 71.92961793293026
SNOW_LEVEL_B2_H850 = 0.5617611362518282
SNOW_LEVEL_B3_THICKNESS = 3.218874975055911


def safe_remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def measure_axes_image_fraction(fig, ax, pad_inches=SAVEFIG_PAD_INCHES):
    """Return axes box as fractions of the saved tight-cropped image (top-left origin).

    Includes the same pad_inches that savefig applies around bbox_inches='tight',
    otherwise click mapping drifts once maps are letterboxed/resized in the viewer.
    """
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    tight_bbox = fig.get_tightbbox(renderer)
    if tight_bbox is None:
        return {'left': 0.0, 'top': 0.0, 'width': 1.0, 'height': 1.0}

    tight_disp = tight_bbox.transformed(fig.dpi_scale_trans)
    ax_disp = ax.get_window_extent(renderer)
    pad_px = float(pad_inches) * float(fig.dpi)
    width = max(float(tight_disp.width) + 2.0 * pad_px, 1e-6)
    height = max(float(tight_disp.height) + 2.0 * pad_px, 1e-6)

    left = (pad_px + float(ax_disp.x0) - float(tight_disp.x0)) / width
    # Image y grows downward; matplotlib display y grows upward.
    top = (pad_px + float(tight_disp.y1) - float(ax_disp.y1)) / height
    axes_width = float(ax_disp.width) / width
    axes_height = float(ax_disp.height) / height

    return {
        'left': float(np.clip(left, 0.0, 1.0)),
        'top': float(np.clip(top, 0.0, 1.0)),
        'width': float(np.clip(axes_width, 0.0, 1.0)),
        'height': float(np.clip(axes_height, 0.0, 1.0)),
    }


def calibrate_domain_georef():
    """Build per-domain lon/lat extents and shared axes image fraction for click mapping."""
    sample_extent = get_domain_extent('AU')
    fig = plt.figure(figsize=(BASE_FIGURE_WIDTH_INCHES, BASE_FIGURE_HEIGHT_INCHES))
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_extent(
        [sample_extent[0], sample_extent[1], sample_extent[2], sample_extent[3]],
        crs=ccrs.PlateCarree(),
    )
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8)
    ax.set_title(
        'GFS Freezing Level\n'
        'Run: 2026-01-01 0000Z | Valid: 2026-01-01 0900Z'
    )
    axes_fraction = measure_axes_image_fraction(fig, ax)
    plt.close(fig)

    domains = {}
    for domain_id in normalize_domain_ids():
        lon_min, lon_max, lat_min, lat_max = get_domain_extent(domain_id)
        domains[domain_id] = {
            'extent': {
                'lonMin': float(lon_min),
                'lonMax': float(lon_max),
                'latMin': float(lat_min),
                'latMax': float(lat_max),
            },
            'axesFraction': axes_fraction,
        }
    return domains


def save_output_figure(fig, filepath):
    fig.savefig(
        filepath,
        dpi=OUTPUT_DPI,
        bbox_inches='tight',
        pad_inches=SAVEFIG_PAD_INCHES,
        format=OUTPUT_IMAGE_FORMAT,
        pil_kwargs={
            'quality': OUTPUT_JPEG_QUALITY,
            'optimize': True,
            'progressive': True,
        },
    )


def style_height_contour_labels(contour_set, labels, cmap, norm, darken_factor=0.55):
    """Keep each label's colormap hue, but darken RGB for readability."""
    label_texts = getattr(contour_set, 'labelTexts', None) or list(labels or [])
    label_values = getattr(contour_set, 'labelCValues', None)

    for index, label in enumerate(label_texts):
        if label_values is not None and index < len(label_values):
            base_rgba = cmap(norm(label_values[index]))
        else:
            base_rgba = label.get_color()
            if isinstance(base_rgba, str):
                from matplotlib.colors import to_rgba
                base_rgba = to_rgba(base_rgba)

        darker = (
            float(base_rgba[0]) * darken_factor,
            float(base_rgba[1]) * darken_factor,
            float(base_rgba[2]) * darken_factor,
            1.0,
        )
        label.set_color(darker)
        label.set_alpha(1.0)
        label.set_zorder(50.1)


def draw_height_isolines_with_labels(ax, longitude, latitude, height_ft, cmap, norm):
    """Draw 500 ft height isolines and label every second contour (4C/FZL shared scheme)."""
    contour_levels = np.arange(500, 18000, 500)
    label_levels = contour_levels[1::2]
    contour_set = ax.contour(
        longitude,
        latitude,
        height_ft,
        levels=contour_levels,
        cmap=cmap,
        linewidths=1.0,
        alpha=0.5,
        transform=ccrs.PlateCarree(),
        zorder=50,
    )
    labels = ax.clabel(contour_set, levels=label_levels, inline=True, fontsize=9, fmt='%d ft')
    style_height_contour_labels(contour_set, labels, cmap, norm)
    return contour_set


def publish_generated_frames(output_dir, generated_files, temp_dir):
    removed_count = 0
    for entry in os.listdir(output_dir):
        file_path = os.path.join(output_dir, entry)
        if os.path.isfile(file_path) and entry.lower().endswith(REMOVABLE_IMAGE_EXTS):
            os.remove(file_path)
            removed_count += 1

    for filename in sorted(generated_files):
        shutil.move(os.path.join(temp_dir, filename), os.path.join(output_dir, filename))

    print(f'Removed {removed_count} previously published frames from {output_dir}')
    print(f'Published {len(generated_files)} new frames to {output_dir}')


def get_taf_location_rows():
    global TAF_LOCATION_ROWS_CACHE
    if TAF_LOCATION_ROWS_CACHE is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        taf_file = os.path.join(script_dir, 'geo files', 'TAF lat long.csv')
        taf_data = pd.read_csv(taf_file, delimiter='\t')
        TAF_LOCATION_ROWS_CACHE = [
            (row.TAF, row.Latitude, row.Longitude)
            for row in taf_data.itertuples(index=False)
        ]
    return TAF_LOCATION_ROWS_CACHE


def get_gaf_feature():
    global GAF_FEATURE_CACHE
    if GAF_FEATURE_CACHE is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gaf_shp = os.path.join(script_dir, 'geo files', 'GAF_Boundaries.shp')
        reader = Reader(gaf_shp)
        GAF_FEATURE_CACHE = ShapelyFeature(
            reader.geometries(),
            ccrs.PlateCarree(),
            facecolor='none',
            edgecolor='black',
            linewidth=0.7,
        )
    return GAF_FEATURE_CACHE


def get_gaf_area_geometries():
    global GAF_AREA_GEOMETRY_CACHE
    if GAF_AREA_GEOMETRY_CACHE is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gaf_shp = os.path.join(script_dir, 'geo files', 'GAF_Boundaries.shp')
        reader = Reader(gaf_shp)
        GAF_AREA_GEOMETRY_CACHE = {
            record.attributes['GAF_AREA']: record.geometry
            for record in reader.records()
        }
    return GAF_AREA_GEOMETRY_CACHE


def normalize_domain_ids(domain_ids=None):
    if domain_ids is None or domain_ids == 'all':
        requested = list(DEFAULT_DOMAIN_IDS)
    elif isinstance(domain_ids, str):
        requested = [item.strip() for item in domain_ids.split(',') if item.strip()]
    else:
        requested = list(domain_ids)

    normalized = []
    seen = set()
    for domain_id in requested:
        if domain_id not in DOMAIN_DEFINITIONS:
            raise ValueError(f'Unknown domain id: {domain_id}')
        if domain_id in seen:
            continue
        normalized.append(domain_id)
        seen.add(domain_id)

    if not normalized:
        raise ValueError('At least one domain must be requested')

    return normalized


def _build_bounds_centered_extent(bounds):
    min_lon, min_lat, max_lon, max_lat = bounds

    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0

    lon_padding = max((max_lon - min_lon) * DOMAIN_PADDING_RATIO, MIN_DOMAIN_PADDING_DEGREES)
    lat_padding = max((max_lat - min_lat) * DOMAIN_PADDING_RATIO, MIN_DOMAIN_PADDING_DEGREES)

    half_lon_span = ((max_lon - min_lon) / 2.0) + lon_padding
    half_lat_span = ((max_lat - min_lat) / 2.0) + lat_padding

    current_ratio = half_lon_span / max(half_lat_span, 1e-6)
    if current_ratio < REFERENCE_DOMAIN_ASPECT_RATIO:
        half_lon_span = half_lat_span * REFERENCE_DOMAIN_ASPECT_RATIO
    else:
        half_lat_span = half_lon_span / REFERENCE_DOMAIN_ASPECT_RATIO

    return (
        center_lon - half_lon_span,
        center_lon + half_lon_span,
        center_lat - half_lat_span,
        center_lat + half_lat_span,
    )


def get_domain_profiles():
    global DOMAIN_PROFILE_CACHE
    if DOMAIN_PROFILE_CACHE is not None:
        return DOMAIN_PROFILE_CACHE

    gaf_geometries = get_gaf_area_geometries()
    profiles = {}

    for domain_id, config in DOMAIN_DEFINITIONS.items():
        fixed_extent = config.get('fixed_extent')
        if fixed_extent is not None:
            min_lon, max_lon, min_lat, max_lat = fixed_extent
            centroid_lon = (min_lon + max_lon) / 2.0
            centroid_lat = (min_lat + max_lat) / 2.0
            extent = fixed_extent
        else:
            missing_areas = [area for area in config['gaf_areas'] if area not in gaf_geometries]
            if missing_areas:
                raise RuntimeError(f'Missing GAF areas for {domain_id}: {", ".join(missing_areas)}')

            geometry_union = unary_union([gaf_geometries[area] for area in config['gaf_areas']])
            centroid_lon = geometry_union.centroid.x
            centroid_lat = geometry_union.centroid.y
            extent = _build_bounds_centered_extent(geometry_union.bounds)

        profiles[domain_id] = {
            'id': domain_id,
            'label': config['label'],
            'extent': extent,
            'centroid': (centroid_lon, centroid_lat),
            'gaf_areas': tuple(config.get('gaf_areas', ())),
        }

    DOMAIN_PROFILE_CACHE = profiles
    return DOMAIN_PROFILE_CACHE


def get_domain_extent(domain_id, layer_profile='bg'):
    profile = get_domain_profiles()[domain_id]
    return profile['extent']


def get_domain_root_relative_dir(domain_id):
    return os.path.join('images', 'domains', domain_id)


def get_panel_output_dir(domain_id, category, panel_name, output_root=None):
    base_root = output_root or os.path.join('images', 'domains')
    return os.path.join(base_root, domain_id, category, panel_name)


def resolve_panel_output_dirs(domain_ids, category, panel_name, output_dir=None, output_root=None):
    if output_dir is not None and output_root is not None:
        raise ValueError('output_dir and output_root cannot be used together')

    if output_dir is not None:
        if len(domain_ids) != 1:
            raise ValueError('A single explicit output_dir can only be used with one domain')
        return {domain_ids[0]: output_dir}

    return {
        domain_id: get_panel_output_dir(domain_id, category, panel_name, output_root=output_root)
        for domain_id in domain_ids
    }


def get_prepared_land_geometry():
    global LAND_PREP_CACHE
    if LAND_PREP_CACHE is None:
        print('Loading Natural Earth land geometry for ocean masking...')
        land_shp = natural_earth(resolution='10m', category='physical', name='land')
        land_reader = Reader(land_shp)
        land_union = unary_union(list(land_reader.geometries()))
        LAND_PREP_CACHE = prep(land_union)
    return LAND_PREP_CACHE


def get_ocean_mask(lons, lats):
    cache_key = (
        len(lats),
        len(lons),
        round(float(lats[0]), 4),
        round(float(lats[-1]), 4),
        round(float(lons[0]), 4),
        round(float(lons[-1]), 4),
    )
    cached_mask = OCEAN_MASK_CACHE.get(cache_key)
    if cached_mask is not None:
        return cached_mask

    print('Creating ocean mask from Natural Earth land data...')
    land_prep = get_prepared_land_geometry()
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    ocean_mask = np.zeros(lon_grid.shape, dtype=bool)

    for i in range(lon_grid.shape[0]):
        for j in range(lon_grid.shape[1]):
            if not land_prep.contains(Point(lon_grid[i, j], lat_grid[i, j])):
                ocean_mask[i, j] = True

    OCEAN_MASK_CACHE[cache_key] = ocean_mask
    print(f'Identified {np.sum(ocean_mask)} ocean grid points out of {ocean_mask.size} total')
    return ocean_mask


def count_image_files(directory):
    return sum(
        1
        for entry in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, entry))
        and entry.lower().endswith(REMOVABLE_IMAGE_EXTS)
    )


def extract_forecast_hour_from_filename(filename):
    match = re.search(r'_(\d{2,3})\.[^.]+$', filename)
    return int(match.group(1)) if match else None


def list_published_frames(relative_dir, base_dir):
    directory = os.path.join(base_dir, relative_dir)
    if not os.path.isdir(directory):
        return []

    frames = []
    normalized_dir = relative_dir.replace(os.sep, '/')
    for entry in sorted(os.listdir(directory)):
        file_path = os.path.join(directory, entry)
        if not os.path.isfile(file_path) or not entry.lower().endswith(REMOVABLE_IMAGE_EXTS):
            continue

        hour = extract_forecast_hour_from_filename(entry)
        if hour is None:
            continue

        frames.append({
            'hour': hour,
            'path': f'{normalized_dir}/{entry}',
        })

    return sorted(frames, key=lambda frame: frame['hour'])


def pair_published_frames(left_frames, right_frames):
    left_by_hour = {frame['hour']: frame['path'] for frame in left_frames}
    right_by_hour = {frame['hour']: frame['path'] for frame in right_frames}

    return [
        {
            'hour': hour,
            'leftPath': left_by_hour[hour],
            'rightPath': right_by_hour[hour],
        }
        for hour in sorted(set(left_by_hour) & set(right_by_hour))
    ]


def build_categories_manifest(root_relative_dir, base_dir):
    categories = {}

    for category, (left_subdir, right_subdir) in CATEGORY_PANEL_DIRS.items():
        categories[category] = pair_published_frames(
            list_published_frames(os.path.join(root_relative_dir, left_subdir), base_dir),
            list_published_frames(os.path.join(root_relative_dir, right_subdir), base_dir),
        )

    return categories


def write_frame_manifest(base_dir=None):
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    images_dir = os.path.join(base_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)

    georef_by_domain = calibrate_domain_georef()
    domains = {}
    for domain_id in normalize_domain_ids():
        domain_root = get_domain_root_relative_dir(domain_id)
        categories = build_categories_manifest(domain_root, base_dir)
        georef = georef_by_domain.get(domain_id, {})

        domains[domain_id] = {
            'label': DOMAIN_DEFINITIONS[domain_id]['label'],
            'categories': categories,
            'extent': georef.get('extent'),
            'axesFraction': georef.get('axesFraction'),
        }

    manifest = {
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'domainOrder': normalize_domain_ids(),
        'legends': LEGEND_CONFIG,
        'domains': domains,
    }

    manifest_path = os.path.join(images_dir, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as manifest_file:
        json.dump(manifest, manifest_file, indent=2)
        manifest_file.write('\n')

    print(f'Wrote frame manifest to {manifest_path}')
    return manifest_path


def swap_directory_into_place(source_dir, destination_dir):
    parent_dir = os.path.dirname(destination_dir)
    os.makedirs(parent_dir, exist_ok=True)

    backup_dir = tempfile.mkdtemp(
        prefix=f'.avmaps_backup_{os.path.basename(destination_dir)}_',
        dir=parent_dir,
    )
    os.rmdir(backup_dir)

    if os.path.exists(destination_dir):
        os.replace(destination_dir, backup_dir)

    try:
        os.replace(source_dir, destination_dir)
    except Exception:
        if os.path.exists(backup_dir) and not os.path.exists(destination_dir):
            os.replace(backup_dir, destination_dir)
        raise
    else:
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir)


def publish_staged_directories_atomically(staging_root, relative_dirs):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    print('\nAll requested layers generated successfully. Publishing staged run...')

    for relative_dir in relative_dirs:
        staged_dir = os.path.join(staging_root, relative_dir)
        live_dir = os.path.join(script_dir, relative_dir)

        if not os.path.isdir(staged_dir):
            raise RuntimeError(f'Missing staged output for {relative_dir}; aborting atomic publish')

        image_count = count_image_files(staged_dir)
        if image_count == 0:
            raise RuntimeError(f'No image frames staged for {relative_dir}; aborting atomic publish')

        swap_directory_into_place(staged_dir, live_dir)
        print(f'Published {image_count} staged frames to {live_dir}')

# Helper function to handle different GFS time dimension names
def get_time_index(data_var):
    """Get first time index, handling both 'time2' and 'validtime1' dimension names"""
    if 'time2' in data_var.dims:
        return data_var.isel(time2=0)
    elif 'validtime1' in data_var.dims:
        return data_var.isel(validtime1=0)
    else:
        # If neither, try to find the time dimension dynamically
        time_dims = [d for d in data_var.dims if 'time' in d.lower() or 'valid' in d.lower()]
        if time_dims:
            return data_var.isel({time_dims[0]: 0})
        return data_var  # Return as-is if no time dimension


def get_isobaric_field(data_var, target_pa):
    """Select an isobaric field at target pressure (Pa), falling back to nearest level."""
    return get_time_index(data_var.sel(isobaric=target_pa, method='nearest'))


def dewpoint_from_temperature_and_rh(temp_k, rh_percent):
    """Return dewpoint in Celsius from temperature (K) and relative humidity (%)."""
    temp_c = temp_k - 273.15
    rh_clipped = np.clip(rh_percent, 1e-6, 100.0)
    a = 17.625
    b = 243.04
    gamma = np.log(rh_clipped / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


def relative_humidity_from_temperature_and_dewpoint(temp_c, dewpoint_c):
    """Return relative humidity (%) from temperature and dewpoint in Celsius."""
    es = 6.112 * np.exp((17.67 * temp_c) / (temp_c + 243.5))
    e = 6.112 * np.exp((17.67 * dewpoint_c) / (dewpoint_c + 243.5))
    return np.clip(100.0 * e / np.maximum(es, 1e-6), 1e-6, 100.0)


def wet_bulb_temperature_from_temperature_and_rh(temp_c, rh_percent):
    """Return wet-bulb temperature (C) using the Stull approximation."""
    rh = np.clip(rh_percent, 1e-6, 100.0)
    return (
        temp_c * np.arctan(0.151977 * np.sqrt(rh + 8.313659))
        + np.arctan(temp_c + rh)
        - np.arctan(rh - 1.676331)
        + 0.00391838 * np.power(rh, 1.5) * np.arctan(0.023101 * rh)
        - 4.686035
    )


def mixing_ratio_from_dewpoint_and_pressure(td_c, pressure_hpa):
    """Return humidity mixing ratio (kg/kg) from dewpoint (C) and pressure (hPa)."""
    vapor_pressure_hpa = 6.112 * np.exp((17.67 * td_c) / (td_c + 243.5))
    vapor_pressure_hpa = np.minimum(vapor_pressure_hpa, pressure_hpa - 0.01)
    epsilon = 0.622
    return (epsilon * vapor_pressure_hpa) / (pressure_hpa - vapor_pressure_hpa)


def calculate_total_totals(data):
    """Return the Total Totals index in degrees Celsius."""
    temp_850 = get_isobaric_field(data['Temperature_isobaric'], 85000)
    temp_500 = get_isobaric_field(data['Temperature_isobaric'], 50000)
    rh_850 = get_isobaric_field(data['Relative_humidity_isobaric'], 85000)

    td_850_c = dewpoint_from_temperature_and_rh(temp_850.values, rh_850.values)
    t_850_c = temp_850.values - 273.15
    t_500_c = temp_500.values - 273.15
    return t_850_c + td_850_c - 2.0 * t_500_c


def _interp_level_to_pressure(field_3d, p_env_pa, target_p_pa):
    """Vectorized linear interpolation of a 3D field to a 2D target pressure surface.

    Parameters
    ----------
    field_3d    : ndarray (n_levels, nlat, nlon) — values at each pressure level
    p_env_pa    : 1D array (n_levels,)           — pressure in Pa, surface-first (descending)
    target_p_pa : ndarray (nlat, nlon)           — target pressure in Pa per grid point

    Returns
    -------
    result : ndarray (nlat, nlon) — NaN where target falls outside the profile
    """
    result = np.full(target_p_pa.shape, np.nan, dtype=float)
    for k in range(len(p_env_pa) - 1):
        p_lo = p_env_pa[k]       # higher pressure (lower altitude)
        p_hi = p_env_pa[k + 1]   # lower pressure  (higher altitude)
        in_bracket = (target_p_pa <= p_lo) & (target_p_pa >= p_hi)
        active = in_bracket & np.isnan(result)
        if not np.any(active):
            continue
        frac = np.where(active, (target_p_pa - p_lo) / (p_hi - p_lo + 1e-10), 0.0)
        interp = field_3d[k] + frac * (field_3d[k + 1] - field_3d[k])
        result = np.where(active, interp, result)
    return result


def _find_isotherm_pressure(t_env_c, p_env_pa, target_temp_c):
    """Vectorized: find the pressure (Pa) of the first upward crossing of target_temp_c.

    Parameters
    ----------
    t_env_c     : ndarray (n_levels, nlat, nlon) — env temperature in °C, surface-first
    p_env_pa    : 1D array (n_levels,)           — pressure in Pa, surface-first (descending)
    target_temp_c : float                        — isotherm temperature (e.g. -20)

    Returns
    -------
    isotherm_p_pa : ndarray (nlat, nlon) — NaN where isotherm not found in profile
    """
    result = np.full(t_env_c.shape[1:], np.nan, dtype=float)
    for k in range(len(p_env_pa) - 1):
        t_lo = t_env_c[k]      # warmer (surface side)
        t_hi = t_env_c[k + 1]  # colder (upper side)
        crossing = (t_lo >= target_temp_c) & (t_hi <= target_temp_c)
        active = crossing & np.isnan(result) & np.isfinite(t_lo) & np.isfinite(t_hi)
        if not np.any(active):
            continue
        dt = t_hi - t_lo
        frac = np.where(active, (target_temp_c - t_lo) / np.where(np.abs(dt) > 1e-6, dt, np.nan), 0.0)
        interp_p = p_env_pa[k] + frac * (p_env_pa[k + 1] - p_env_pa[k])
        result = np.where(active, interp_p, result)
    return result


# Physical constants for parcel lift
_Rd   = 287.05   # J kg-1 K-1  dry air gas constant
_Rv   = 461.5    # J kg-1 K-1  water vapour gas constant
_cpd  = 1005.7   # J kg-1 K-1  specific heat of dry air at constant pressure
_Lv   = 2.501e6  # J kg-1      latent heat of vaporisation (0°C reference)
_kappa = _Rd / _cpd  # Poisson exponent ≈ 0.286


def _saturation_mixing_ratio(t_k, p_pa):
    """Saturation mixing ratio (kg/kg) from temperature (K) and pressure (Pa)."""
    t_c = t_k - 273.15
    es_pa = 611.2 * np.exp((17.67 * t_c) / (t_c + 243.5))  # Buck equation, Pa
    es_pa = np.minimum(es_pa, p_pa - 1.0)
    return (_Rd / _Rv) * es_pa / (p_pa - es_pa)


def _moist_adiabatic_lapse_rate(t_k, p_pa):
    """Saturated adiabatic lapse rate dT/dp (K/Pa) — positive = warms as p increases.

    Derived from the pseudoadiabatic first-law balance:
        dT/dp = (Rd*T + Lv*ws) / (p * (cpd + Lv²*ws / (Rv*T²)))
    """
    ws = _saturation_mixing_ratio(t_k, p_pa)
    numer = _Rd * t_k + _Lv * ws
    denom = _cpd + (_Lv ** 2 * ws) / (_Rv * t_k ** 2)
    return numer / (denom * p_pa)


def calculate_min_lifted_index_minus20(data):
    """Return the minimum LI-20 across parcels lifted from surface and each
    isobaric level from 1000 hPa to 600 hPa.

    LI-20 = T_env(P_-20) - T_parcel(P_-20), where P_-20 is the pressure of
    the environmental -20°C isotherm.  Negative values indicate a parcel
    warmer than the environment at that level — convective instability.

    Algorithm (fully vectorized — no Python loop over grid columns):
      1. Analytically compute the LCL pressure and temperature for each
         starting level using Bolton (1980) formulae.
      2. Dry-adiabatic lift from the starting level to each model level below
         the LCL using potential-temperature conservation:
             T_parcel = T_start * (P / P_start)^kappa
      3. Moist-adiabatic lift above the LCL: step through each model level
         applying the saturated lapse rate dT/dp to the *full 2D grid* in one
         NumPy operation per level.
      4. Interpolate T_parcel and T_env to the -20°C isotherm pressure, then
         compute LI-20 and take the element-wise minimum across all starting
         levels.

    Returns a 2D ndarray (nlat, nlon) in °C.  NaN where the -20°C isotherm is
    not found within the column (e.g. very cold surface environments).
    """
    # -----------------------------------------------------------------------
    # 1. Build the environment profile
    #    - Full profile (surface → 300 hPa): used to locate the -20°C isotherm
    #    - Starting levels (surface → 600 hPa): the levels from which parcels
    #      are lifted (per the definition of min-LI-20)
    # -----------------------------------------------------------------------
    # Levels used for parcel starting points (surface → 600 hPa)
    start_levels_pa = [100000, 97500, 95000, 92500, 90000, 85000,
                       80000, 75000, 70000, 65000, 60000]
    # Extended levels above 600 hPa — needed to capture the -20°C isotherm,
    # which in Australia typically lies near 450–550 hPa.  These levels are
    # already fetched by include_ts_fields (400, 300, 250 hPa) and the base
    # fetch (500 hPa).
    upper_levels_pa = [50000, 40000, 30000]

    temp_iso = get_time_index(data['Temperature_isobaric'])
    rh_iso   = get_time_index(data['Relative_humidity_isobaric'])

    def _stack_levels(levels):
        t_stack, td_stack, p_stack = [], [], []
        for p_pa in levels:
            t_lev  = temp_iso.sel(isobaric=p_pa, method='nearest')
            rh_lev = rh_iso.sel(isobaric=p_pa, method='nearest')
            t_stack.append(t_lev.values)
            td_stack.append(dewpoint_from_temperature_and_rh(t_lev.values, rh_lev.values))
            p_stack.append(float(t_lev['isobaric'].values))
        return t_stack, td_stack, p_stack

    t2m_k  = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)).values
    td2m_c = (get_time_index(data['Dewpoint_temperature_height_above_ground'].sel(height_above_ground4=2)).values
              - 273.15)

    t_start_stack, td_start_stack, p_start_stack = _stack_levels(start_levels_pa)
    t_upper_stack, td_upper_stack, p_upper_stack = _stack_levels(upper_levels_pa)

    # Full environment profile: surface → 300 hPa (surface-first, descending pressure)
    p_env_pa = np.array([101325.0] + p_start_stack + p_upper_stack)
    t_env_k  = np.array([t2m_k]   + t_start_stack  + t_upper_stack)
    td_env_c = np.array([td2m_c]  + td_start_stack  + td_upper_stack)
    t_env_c  = t_env_k - 273.15

    # Number of starting levels (surface + start_levels_pa)
    n_start_lev = 1 + len(start_levels_pa)
    nlat, nlon  = t2m_k.shape

    # -----------------------------------------------------------------------
    # 2. Find the -20°C isotherm pressure in the environment
    # -----------------------------------------------------------------------
    isotherm_p_pa = _find_isotherm_pressure(t_env_c, p_env_pa, -20.0)
    # T_env at the -20°C level (≈ -20 by construction; interpolated exactly)
    t_env_at_isotherm = _interp_level_to_pressure(t_env_c, p_env_pa, isotherm_p_pa)

    # -----------------------------------------------------------------------
    # 3. Lift parcels from each starting level and compute LI-20
    # -----------------------------------------------------------------------
    min_li20 = np.full((nlat, nlon), np.nan)

    for start_idx in range(n_start_lev):
        p_start  = p_env_pa[start_idx]                  # scalar Pa
        t_start_k  = t_env_k[start_idx]                 # (nlat, nlon)
        td_start_c = td_env_c[start_idx]                # (nlat, nlon)

        # Only meaningful where the starting level is below the isotherm
        valid = np.isfinite(isotherm_p_pa) & (p_start > isotherm_p_pa)
        if not np.any(valid):
            continue

        # --- Analytical LCL (Bolton 1980) ---
        # LCL temperature
        t_lcl_k = 1.0 / (
            1.0 / np.maximum(td_start_c + 273.15 - 56.0, 1e-3)
            + np.log(np.maximum(t_start_k / np.maximum(td_start_c + 273.15, 1e-3), 1.0)) / 800.0
        ) + 56.0
        # LCL pressure via Poisson relation (potential temperature conserved dry-adiabatically)
        p_lcl_pa = p_start * np.power(np.maximum(t_lcl_k / t_start_k, 1e-6), 1.0 / _kappa)

        # --- Build parcel temperature profile over all levels above start ---
        # Phase 1: dry adiabatic — θ conservation
        #   T_parcel(P) = T_start * (P / p_start)^kappa   for P >= P_LCL
        # Phase 2: moist adiabatic — integrate dT/dp upward level by level
        #   T_parcel(k+1) = T_parcel(k) + dT/dp * (p[k+1] - p[k])

        # Initialise parcel temperature array over levels start_idx…n_lev-1
        levels_above = p_env_pa[start_idx:]   # (n_above,)
        n_above = len(levels_above)

        # Dry adiabat for all levels (used below LCL)
        t_dry_k = t_start_k[np.newaxis, :, :] * np.power(
            (levels_above / p_start)[:, np.newaxis, np.newaxis], _kappa
        )  # (n_above, nlat, nlon)

        # Moist adiabat: step up from the starting level
        t_parcel_k = np.copy(t_dry_k)  # initialise with dry profile
        # At the starting level the parcel = environment
        t_parcel_k[0] = t_start_k

        for k in range(n_above - 1):
            p_k   = levels_above[k]
            p_k1  = levels_above[k + 1]
            dp    = p_k1 - p_k   # negative (ascending)

            # Above LCL: use moist adiabat; at or below: keep dry adiabat value
            above_lcl = p_k <= p_lcl_pa  # (nlat, nlon)

            dT_moist = _moist_adiabatic_lapse_rate(t_parcel_k[k], p_k) * dp
            t_moist_k1 = t_parcel_k[k] + dT_moist

            # At transition level (first level above LCL): blend from dry→moist
            # by starting the moist integration from the LCL temperature
            # interpolated along the dry parcel to the LCL pressure.
            # For simplicity and numerical stability we switch at the first
            # level where the mid-point is above the LCL.
            p_mid = 0.5 * (p_k + p_k1)
            crossing_lcl = (p_mid <= p_lcl_pa) & (p_k > p_lcl_pa)
            # At the crossing level, restart from the LCL temperature on a
            # moist adiabat: T_lcl then integrate to p_k1
            dT_from_lcl = _moist_adiabatic_lapse_rate(t_lcl_k, p_lcl_pa) * (p_k1 - p_lcl_pa)
            t_moist_from_lcl = t_lcl_k + dT_from_lcl

            t_parcel_k[k + 1] = np.where(
                above_lcl, t_moist_k1,
                np.where(crossing_lcl, t_moist_from_lcl, t_dry_k[k + 1])
            )

        # --- Interpolate parcel temperature to -20°C isotherm pressure ---
        t_parcel_c = t_parcel_k - 273.15
        t_parcel_at_isotherm = _interp_level_to_pressure(t_parcel_c, levels_above, isotherm_p_pa)

        # --- Compute LI-20 and update running minimum ---
        li20 = t_env_at_isotherm - t_parcel_at_isotherm
        li20 = np.where(valid, li20, np.nan)

        min_li20 = np.where(
            np.isfinite(li20) & (np.isnan(min_li20) | (li20 < min_li20)),
            li20,
            min_li20,
        )

    print(f'Min LI-20 < 0 grid points: {np.sum(min_li20 < 0.0)}')
    return min_li20


def calculate_isotherm_height_ft(data, target_temp_c):
    """Return height (ft AMSL) of the first dry-bulb isotherm crossing at target_temp_c."""
    temp_isobaric = get_time_index(data['Temperature_isobaric']) - 273.15
    hgt_isobaric = get_time_index(data['Geopotential_height_isobaric'])

    pressures = temp_isobaric['isobaric'].values
    sort_idx = np.argsort(pressures)[::-1]  # Surface-near first

    temp_surface = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)) - 273.15
    hgt_surface = get_time_index(data['Geopotential_height_surface'])

    temp_stack = np.concatenate([temp_surface.values[np.newaxis, :, :], temp_isobaric.values[sort_idx, :, :]], axis=0)
    hgt_stack = np.concatenate([hgt_surface.values[np.newaxis, :, :], hgt_isobaric.values[sort_idx, :, :]], axis=0)

    isotherm_height_m = np.full(temp_surface.values.shape, np.nan, dtype=float)

    # If the surface is already at/below the target temperature, the isotherm is at the surface.
    surface_at_or_below = temp_stack[0] <= target_temp_c
    isotherm_height_m[surface_at_or_below] = hgt_stack[0][surface_at_or_below]

    for k in range(temp_stack.shape[0] - 1):
        t_lo = temp_stack[k]
        t_hi = temp_stack[k + 1]
        z_lo = hgt_stack[k]
        z_hi = hgt_stack[k + 1]

        crossing = ((t_lo >= target_temp_c) & (t_hi <= target_temp_c)) | ((t_lo <= target_temp_c) & (t_hi >= target_temp_c))
        crossing &= np.isfinite(t_lo) & np.isfinite(t_hi) & np.isfinite(z_lo) & np.isfinite(z_hi)
        crossing &= np.abs(t_hi - t_lo) > 1e-6

        unresolved = ~np.isfinite(isotherm_height_m)
        use = unresolved & crossing
        if not np.any(use):
            continue

        frac = (target_temp_c - t_lo) / (t_hi - t_lo)
        z_cross = z_lo + frac * (z_hi - z_lo)
        isotherm_height_m[use] = z_cross[use]

    return isotherm_height_m * 3.28084


def calculate_freezing_level_height_ft(data):
    """Return freezing level height (ft AMSL), interpolated from available levels."""
    return calculate_isotherm_height_ft(data, 0.0)


def calculate_freezing_layer_mask(data):
    """Return mask where a column has multiple 0C crossings (a freezing layer aloft)."""
    temp_isobaric_c = get_time_index(data['Temperature_isobaric']) - 273.15

    pressures = temp_isobaric_c['isobaric'].values
    sort_idx = np.argsort(pressures)[::-1]  # Surface-near first

    temp_surface_c = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)) - 273.15
    temp_stack = np.concatenate(
        [temp_surface_c.values[np.newaxis, :, :], temp_isobaric_c.values[sort_idx, :, :]],
        axis=0,
    )

    t_lo = temp_stack[:-1]
    t_hi = temp_stack[1:]

    crossings = ((t_lo <= 0.0) & (t_hi > 0.0)) | ((t_lo >= 0.0) & (t_hi < 0.0))
    crossings &= np.isfinite(t_lo) & np.isfinite(t_hi)

    crossing_count = np.sum(crossings, axis=0)
    return crossing_count >= 2


def calculate_wet_bulb_freezing_level_height_ft(data):
    """Return wet-bulb freezing level height (ft AMSL), interpolated from profile levels."""
    temp_isobaric_c = get_time_index(data['Temperature_isobaric']) - 273.15
    rh_isobaric = get_time_index(data['Relative_humidity_isobaric'])
    hgt_isobaric = get_time_index(data['Geopotential_height_isobaric'])

    tw_isobaric_c = wet_bulb_temperature_from_temperature_and_rh(
        temp_isobaric_c.values,
        rh_isobaric.values,
    )

    pressures = temp_isobaric_c['isobaric'].values
    sort_idx = np.argsort(pressures)[::-1]  # Surface-near first

    temp_2m_c = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)) - 273.15
    dewpoint_2m_c = get_time_index(data['Dewpoint_temperature_height_above_ground'].sel(height_above_ground4=2)) - 273.15
    rh_2m = relative_humidity_from_temperature_and_dewpoint(temp_2m_c.values, dewpoint_2m_c.values)
    tw_surface_c = wet_bulb_temperature_from_temperature_and_rh(temp_2m_c.values, rh_2m)
    hgt_surface = get_time_index(data['Geopotential_height_surface'])

    tw_stack = np.concatenate([tw_surface_c[np.newaxis, :, :], tw_isobaric_c[sort_idx, :, :]], axis=0)
    hgt_stack = np.concatenate([hgt_surface.values[np.newaxis, :, :], hgt_isobaric.values[sort_idx, :, :]], axis=0)

    wbfl_m = np.full(temp_2m_c.values.shape, np.nan, dtype=float)

    surface_below_freezing = tw_stack[0] <= 0.0
    wbfl_m[surface_below_freezing] = hgt_stack[0][surface_below_freezing]

    for k in range(tw_stack.shape[0] - 1):
        tw_lo = tw_stack[k]
        tw_hi = tw_stack[k + 1]
        z_lo = hgt_stack[k]
        z_hi = hgt_stack[k + 1]

        crossing = ((tw_lo >= 0.0) & (tw_hi <= 0.0)) | ((tw_lo <= 0.0) & (tw_hi >= 0.0))
        crossing &= np.isfinite(tw_lo) & np.isfinite(tw_hi) & np.isfinite(z_lo) & np.isfinite(z_hi)
        crossing &= np.abs(tw_hi - tw_lo) > 1e-6

        unresolved = ~np.isfinite(wbfl_m)
        use = unresolved & crossing
        if not np.any(use):
            continue

        frac = (0.0 - tw_lo) / (tw_hi - tw_lo)
        z_cross = z_lo + frac * (z_hi - z_lo)
        wbfl_m[use] = z_cross[use]

    return wbfl_m * 3.28084


def calculate_snow_precipitation_1h(data):
    """Calculate 1-hour precipitation where estimated snow level is less than 600m above terrain.
    
    Returns 1-hour total precipitation (mm) masked to grid points where
    estimated snow level is lower than 600m above the terrain elevation.
    """
    # Get estimated snow level in meters from calibrated regression.
    snow_level_m = calculate_estimated_snow_level_m(data)
    
    # Get terrain height in meters (convert from geopotential)
    hgt_surface = get_time_index(data['Geopotential_height_surface'])
    terrain_m = hgt_surface.values
    
    # Get 1-hour total precipitation (convert from rate in mm/s to mm/hr)
    try:
        precip_rate = get_time_index(data['Precipitation_rate_surface']).values
        precip_1h = precip_rate * 3600  # Convert mm/s to mm/hr
    except (KeyError, AttributeError):
        print('Warning: 1-hour precipitation field not available; snow precipitation layer skipped.')
        return None
    
    # Mask precipitation to only where snow level < 600m above terrain (snow will precipitate).
    snow_precip = np.where((snow_level_m < (terrain_m + 600)) & np.isfinite(precip_1h), precip_1h, np.nan)
    
    return snow_precip


def calculate_layer_mean_relative_humidity(data, temp_min_c, temp_max_c):
    """Return mean RH (%) across isobaric levels where temp_min_c <= T <= temp_max_c."""
    temp_isobaric_c = get_time_index(data['Temperature_isobaric']).values - 273.15
    rh_isobaric = get_time_index(data['Relative_humidity_isobaric']).values

    in_layer = (
        (temp_isobaric_c <= temp_max_c)
        & (temp_isobaric_c >= temp_min_c)
        & np.isfinite(rh_isobaric)
    )

    rh_sum = np.sum(np.where(in_layer, rh_isobaric, 0.0), axis=0)
    rh_count = np.sum(in_layer, axis=0)

    mean_rh = np.full(rh_sum.shape, np.nan, dtype=float)
    valid = rh_count > 0
    mean_rh[valid] = rh_sum[valid] / rh_count[valid]
    return mean_rh


def calculate_icing_relative_humidity(data):
    """Return mean RH (%) between the 0C and -15C isotherm layer."""
    return calculate_layer_mean_relative_humidity(data, -15.0, 0.0)


def calculate_marginal_icing_relative_humidity(data):
    """Return max of mean RH (%) in the 0C–4C and -5C–0C isotherm layers."""
    rh_0_to_4c = calculate_layer_mean_relative_humidity(data, 0.0, 4.0)
    rh_m5_to_0c = calculate_layer_mean_relative_humidity(data, -5.0, 0.0)
    return np.fmax(rh_0_to_4c, rh_m5_to_0c)


def clean_binary_mask(mask, min_cells=8, open_iterations=1):
    """Remove tiny speckles from a boolean mask (opening + connected-component size filter)."""
    cleaned = np.asarray(mask, dtype=bool)
    if not cleaned.any():
        return cleaned

    if open_iterations > 0:
        cleaned = ndimage.binary_opening(cleaned, iterations=int(open_iterations))

    if min_cells > 1 and cleaned.any():
        labeled, feature_count = ndimage.label(cleaned)
        if feature_count:
            counts = np.bincount(labeled.ravel())
            keep = counts >= int(min_cells)
            keep[0] = False
            cleaned = keep[labeled]

    return cleaned


def calculate_estimated_snow_level_m(data):
    """Return estimated snow level height (m AMSL) from T850/H850/thickness regression."""
    t850_c = get_isobaric_field(data['Temperature_isobaric'], 85000).values - 273.15
    h850_m = get_isobaric_field(data['Geopotential_height_isobaric'], 85000).values
    h1000_m = get_isobaric_field(data['Geopotential_height_isobaric'], 100000).values
    h500_m = get_isobaric_field(data['Geopotential_height_isobaric'], 50000).values
    thickness_m = h500_m - h1000_m

    snow_level_m = (
        SNOW_LEVEL_B0
        + (SNOW_LEVEL_B1_T850 * t850_c)
        + (SNOW_LEVEL_B2_H850 * h850_m)
        + (SNOW_LEVEL_B3_THICKNESS * thickness_m)
    )
    return np.where(snow_level_m >= 0.0, snow_level_m, np.nan)


def get_max_geometric_vertical_velocity(data, pressure_levels_pa):
    """Return the maximum geometric vertical velocity (m/s) across the requested levels."""
    level_values = [
        get_isobaric_field(data['Geometric_vertical_velocity_isobaric'], level).values
        for level in pressure_levels_pa
    ]
    return np.max(np.stack(level_values, axis=0), axis=0)


def calculate_max_wind_below_850hpa_kt(data):
    """Return the maximum wind speed (kt) from the surface up to and including 850 hPa."""
    u_isobaric = get_time_index(data['u-component_of_wind_isobaric'])
    v_isobaric = get_time_index(data['v-component_of_wind_isobaric'])

    pressures = u_isobaric['isobaric'].values
    below_850_mask = pressures >= 85000

    speed_layers = []
    if np.any(below_850_mask):
        isobaric_speed = np.sqrt(
            u_isobaric.values[below_850_mask, :, :] ** 2
            + v_isobaric.values[below_850_mask, :, :] ** 2
        )
        speed_layers.append(isobaric_speed)

    u_10m = get_time_index(data['u-component_of_wind_height_above_ground'].sel(height_above_ground2=10))
    v_10m = get_time_index(data['v-component_of_wind_height_above_ground'].sel(height_above_ground2=10))
    surface_speed = np.sqrt(u_10m.values ** 2 + v_10m.values ** 2)[np.newaxis, :, :]
    speed_layers.append(surface_speed)

    max_speed_ms = np.nanmax(np.concatenate(speed_layers, axis=0), axis=0)
    return max_speed_ms * 1.94384


def calculate_shear_turbulence_category(data):
    """Return categorical shear turbulence class from the surface to available levels up to 800 hPa.

    Classes: 0 none, 1 moderate, 2 moderate/severe, 3 severe.
    """
    u_surface = get_time_index(data['u-component_of_wind_height_above_ground'].sel(height_above_ground2=10)).values
    v_surface = get_time_index(data['v-component_of_wind_height_above_ground'].sel(height_above_ground2=10)).values

    u_isobaric = get_time_index(data['u-component_of_wind_isobaric'])
    v_isobaric = get_time_index(data['v-component_of_wind_isobaric'])

    pressures = u_isobaric['isobaric'].values
    up_to_800_mask = pressures >= 80000

    if not np.any(up_to_800_mask):
        return np.zeros_like(u_surface, dtype=np.int8)

    u_upper = u_isobaric.values[up_to_800_mask, :, :]
    v_upper = v_isobaric.values[up_to_800_mask, :, :]

    upper_speed_kt = np.sqrt(u_upper ** 2 + v_upper ** 2) * 1.94384
    shear_mag_kt = np.sqrt(
        (u_upper - u_surface[np.newaxis, :, :]) ** 2
        + (v_upper - v_surface[np.newaxis, :, :]) ** 2
    ) * 1.94384

    category = np.zeros_like(shear_mag_kt, dtype=np.int8)

    strong_upper = upper_speed_kt > 45.0
    very_strong_upper = upper_speed_kt > 65.0

    category = np.where(strong_upper & (shear_mag_kt >= 27.5) & (shear_mag_kt < 50.0), 1, category)
    category = np.where(strong_upper & (shear_mag_kt >= 50.0) & (shear_mag_kt < 60.0), 2, category)
    category = np.where(strong_upper & (shear_mag_kt >= 60.0), 3, category)

    category = np.where(very_strong_upper & (shear_mag_kt >= 25.0) & (shear_mag_kt < 40.0), 1, category)
    category = np.where(very_strong_upper & (shear_mag_kt >= 40.0) & (shear_mag_kt < 45.0), 2, category)
    category = np.where(very_strong_upper & (shear_mag_kt >= 45.0), 3, category)

    max_category = np.max(category, axis=0)
    print(
        f"Shear turbulence grid points - MOD: {np.sum(max_category == 1)}, "
        f"MOD/SEV: {np.sum(max_category == 2)}, SEV: {np.sum(max_category == 3)}"
    )
    return max_category


def calculate_lee_turbulence_category(data):
    """Return categorical lee turbulence class for downhill flow within 4000 ft AGL up to 800 hPa.

    Classes: 0 none, 1 moderate, 2 moderate/severe, 3 severe.
    """
    terrain_m = get_time_index(data['Geopotential_height_surface']).values
    hgt_isobaric = get_time_index(data['Geopotential_height_isobaric'])
    u_isobaric = get_time_index(data['u-component_of_wind_isobaric'])
    v_isobaric = get_time_index(data['v-component_of_wind_isobaric'])

    pressures = u_isobaric['isobaric'].values
    up_to_800_mask = pressures >= 80000

    if not np.any(up_to_800_mask):
        return np.zeros_like(terrain_m, dtype=np.int8)

    lats = data.latitude.values
    lons = data.longitude.values

    dterrain_dlat = np.gradient(terrain_m, lats, axis=0)
    dterrain_dlon = np.gradient(terrain_m, lons, axis=1)

    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = np.maximum(np.cos(np.deg2rad(lats)), 1e-6)[:, np.newaxis] * 111320.0

    terrain_grad_y = dterrain_dlat / meters_per_deg_lat
    terrain_grad_x = dterrain_dlon / meters_per_deg_lon

    upper_heights_m = hgt_isobaric.values[up_to_800_mask, :, :]
    height_agl_m = upper_heights_m - terrain_m[np.newaxis, :, :]
    within_4000ft_agl = np.isfinite(height_agl_m) & (height_agl_m >= 0.0) & (height_agl_m <= 1219.2)

    u_upper = u_isobaric.values[up_to_800_mask, :, :]
    v_upper = v_isobaric.values[up_to_800_mask, :, :]
    upper_speed_ms = np.sqrt(u_upper ** 2 + v_upper ** 2)
    upper_speed_kt = upper_speed_ms * 1.94384

    wind_unit_x = np.divide(u_upper, upper_speed_ms, out=np.zeros_like(u_upper), where=upper_speed_ms > 1e-6)
    wind_unit_y = np.divide(v_upper, upper_speed_ms, out=np.zeros_like(v_upper), where=upper_speed_ms > 1e-6)

    directional_derivative = (
        terrain_grad_x[np.newaxis, :, :] * wind_unit_x
        + terrain_grad_y[np.newaxis, :, :] * wind_unit_y
    )
    downhill_flow = directional_derivative < -0.0025

    lee_mask = within_4000ft_agl & downhill_flow
    category = np.zeros_like(upper_speed_kt, dtype=np.int8)
    category = np.where(lee_mask & (upper_speed_kt >= 30.0) & (upper_speed_kt < 40.0), 1, category)
    category = np.where(lee_mask & (upper_speed_kt >= 40.0) & (upper_speed_kt < 45.0), 2, category)
    category = np.where(lee_mask & (upper_speed_kt >= 45.0), 3, category)

    max_category = np.max(category, axis=0)
    print(
        f"Lee turbulence grid points - MOD: {np.sum(max_category == 1)}, "
        f"MOD/SEV: {np.sum(max_category == 2)}, SEV: {np.sum(max_category == 3)}"
    )
    return max_category


def calculate_mountain_wave_intensity(data, min_pressure_pa=25000):
    """Return the maximum magnitude of geometric vertical velocity (m/s) from available levels down to 250 hPa."""
    if 'Geometric_vertical_velocity_isobaric' not in data:
        print('Warning: Geometric vertical velocity field not available; MTW layer skipped.')
        return None

    wz_isobaric = get_time_index(data['Geometric_vertical_velocity_isobaric'])
    pressures = wz_isobaric['isobaric'].values
    valid_mask = pressures >= min_pressure_pa

    if not np.any(valid_mask):
        print('Warning: No isobaric vertical velocity levels available down to 250 hPa; MTW layer skipped.')
        return None

    max_abs_wz = np.nanmax(np.abs(wz_isobaric.values[valid_mask, :, :]), axis=0)
    print(f'MTW intensity grid points >= 0.2 m/s: {np.sum(max_abs_wz >= 0.2)}')
    return max_abs_wz


def get_gfs_convective_precip_accumulation(forecast_hour, init_time):
    """Fetch accumulated convective precipitation (kg/m^2 ~= mm) for one forecast hour."""
    run_date = init_time.strftime('%Y%m%d')
    run_cycle = init_time.strftime('%H')
    grib_file = f'gfs.t{run_cycle}z.pgrb2.0p25.f{forecast_hour:03d}'

    params = {
        'file': grib_file,
        'dir': f'/gfs.{run_date}/{run_cycle}/atmos',
        'subregion': '',
        'leftlon': str(data_lon_min),
        'rightlon': str(data_lon_max),
        'toplat': str(data_lat_max),
        'bottomlat': str(data_lat_min),
        'var_ACPCP': 'on',
        'lev_surface': 'on',
    }

    response = requests.get(
        'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl',
        params=params,
        timeout=240,
    )
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix='.grib2', delete=False) as temp_grib:
        temp_grib.write(response.content)
        temp_grib_path = temp_grib.name

    try:
        ds_surface_accum = xr.open_dataset(
            temp_grib_path,
            engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'surface', 'stepType': 'accum'}, 'indexpath': ''},
        )
        ds_surface_accum = ds_surface_accum.load()
        return ds_surface_accum['acpcp'].values
    finally:
        safe_remove_file(temp_grib_path)


def derive_incremental_accumulation(current_values, previous_values=None):
    """Return one-step accumulation, handling source fields that periodically reset."""
    current_values = np.asarray(current_values)
    if previous_values is None:
        return np.maximum(current_values, 0.0)

    previous_values = np.asarray(previous_values)
    tolerance = 1e-6

    # Some upstream accumulated fields restart from zero at bucket boundaries.
    # When that happens, the current field already represents the latest bucket,
    # so subtracting the previous hour would wipe out most of the signal.
    if np.nanmax(current_values) + tolerance < np.nanmax(previous_values):
        return np.maximum(current_values, 0.0)

    return np.maximum(current_values - previous_values, 0.0)


def get_day_night_grid(valid_time, lons, lats):
    """Return solar-zenith proxy and night mask for a given UTC valid time."""
    day_of_year = valid_time.timetuple().tm_yday
    utc_hour = valid_time.hour + valid_time.minute / 60 + valid_time.second / 3600

    # Fractional year in radians (NOAA solar approximation)
    gamma = 2 * np.pi / 365 * (day_of_year - 1 + (utc_hour - 12) / 24)

    # Solar declination (radians)
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )

    # Equation of time (minutes)
    eq_time = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )

    lon_grid, lat_grid = np.meshgrid(lons, lats)
    true_solar_time = (utc_hour * 60 + eq_time + 4 * lon_grid) % 1440
    hour_angle = np.where(true_solar_time / 4 < 0, true_solar_time / 4 + 180, true_solar_time / 4 - 180)

    lat_rad = np.deg2rad(lat_grid)
    ha_rad = np.deg2rad(hour_angle)
    cos_zenith = np.sin(lat_rad) * np.sin(decl) + np.cos(lat_rad) * np.cos(decl) * np.cos(ha_rad)
    night_mask = cos_zenith < 0

    return cos_zenith, night_mask

# Function to get elevation data from GFS surface geopotential
def get_elevation_data(data=None):
    """Extract elevation from GFS/ICON surface geopotential height."""
    print("Extracting elevation from model surface geopotential...")
    geop_surface = get_time_index(data['Geopotential_height_surface'])
    
    print(f"Raw geopotential min/max: {geop_surface.values.min():.0f} / {geop_surface.values.max():.0f}")
    print(f"Variable units: {geop_surface.attrs.get('units', 'unknown')}")
    
    # GFS/ICON output surface geopotential in gpm (geopotential meters)
    # which is approximately equal to meters of elevation above sea level
    elevation = geop_surface.values
    
    lons = data.longitude.values
    lats = data.latitude.values
    
    print(f"Elevation range: {elevation.min():.0f}m / {elevation.max():.0f}m")
    
    return elevation, lons, lats

# Function to get latest GFS data
def get_latest_gfs_dataset():
    # Access GFS 0.25 degree catalog
    catalog_url = 'https://thredds.ucar.edu/thredds/catalog/grib/NCEP/GFS/Global_0p25deg/catalog.xml'
    catalog = TDSCatalog(catalog_url)

    # Get latest dataset
    latest_dataset = catalog.datasets[2]  # Latest Collection
    opendap_url = latest_dataset.access_urls['OPENDAP']
    file_part = opendap_url.split('/')[-1]  # e.g., GFS_Global_0p25deg_20260407_1800.grib2
    date_str = file_part.split('_')[3]
    hour_str = file_part.split('_')[4][:2]
    init_time = datetime.strptime(date_str + hour_str, '%Y%m%d%H').replace(tzinfo=timezone.utc)

    return latest_dataset, init_time


def get_gfs_data(forecast_hour=9, latest_dataset=None, init_time=None, include_ts_fields=False, prev_acpcp_values=None, include_airmass_fields=False):
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    if forecast_hour < 0 or forecast_hour > 120:
        raise ValueError('Raw hourly NOMADS GFS is only available for forecast hours 0-120')

    run_date = init_time.strftime('%Y%m%d')
    run_cycle = init_time.strftime('%H')
    grib_file = f'gfs.t{run_cycle}z.pgrb2.0p25.f{forecast_hour:03d}'

    params = {
        'file': grib_file,
        'dir': f'/gfs.{run_date}/{run_cycle}/atmos',
        'subregion': '',
        'leftlon': str(data_lon_min),
        'rightlon': str(data_lon_max),
        'toplat': str(data_lat_max),
        'bottomlat': str(data_lat_min),
        'var_PRMSL': 'on',
        'var_HGT': 'on',
        'var_PRATE': 'on',
        'var_UGRD': 'on',
        'var_VGRD': 'on',
        'var_RH': 'on',
        'var_TMP': 'on',
        'var_DPT': 'on',
        'var_CAPE': 'on',
        'var_PRES': 'on',
        'lev_mean_sea_level': 'on',
        'lev_surface': 'on',
        'lev_1000_mb': 'on',
        'lev_975_mb': 'on',
        'lev_950_mb': 'on',
        'lev_900_mb': 'on',
        'lev_850_mb': 'on',
        'lev_500_mb': 'on',
        'lev_2_m_above_ground': 'on',
        'lev_10_m_above_ground': 'on',
    }

    if include_ts_fields:
        params.update({
            'var_ACPCP': 'on',
            'var_DZDT': 'on',
            'lev_925_mb': 'on',
            'lev_800_mb': 'on',
            'lev_750_mb': 'on',
            'lev_700_mb': 'on',
            'lev_650_mb': 'on',
            'lev_600_mb': 'on',
            'lev_400_mb': 'on',
            'lev_300_mb': 'on',
            'lev_250_mb': 'on',
        })

    if include_airmass_fields:
        params.update({
            'var_HPBL': 'on',
            'var_ACPCP': 'on',
            'lev_700_mb': 'on',
        })

    response = requests.get(
        'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl',
        params=params,
        timeout=240,
    )
    response.raise_for_status()

    if len(response.content) < 1000:
        raise RuntimeError(
            f'NOMADS returned an unexpectedly small payload for {grib_file}. '
            'The requested run/hour may not be available yet.'
        )

    with tempfile.NamedTemporaryFile(suffix='.grib2', delete=False) as temp_grib:
        temp_grib.write(response.content)
        temp_grib_path = temp_grib.name

    try:
        ds_msl = xr.open_dataset(
            temp_grib_path,
            engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'meanSea'}, 'indexpath': ''},
        )
        ds_surface = xr.open_dataset(
            temp_grib_path,
            engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'surface', 'stepType': 'instant'}, 'indexpath': ''},
        )
        ds_surface_accum = None
        if include_ts_fields and forecast_hour > 0:
            ds_surface_accum = xr.open_dataset(
                temp_grib_path,
                engine='cfgrib',
                backend_kwargs={'filter_by_keys': {'typeOfLevel': 'surface', 'stepType': 'accum'}, 'indexpath': ''},
            )
        ds_isobaric = xr.open_dataset(
            temp_grib_path,
            engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'isobaricInhPa'}, 'indexpath': ''},
        )
        ds_2m = xr.open_dataset(
            temp_grib_path,
            engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'heightAboveGround', 'level': 2}, 'indexpath': ''},
        )
        ds_10m = xr.open_dataset(
            temp_grib_path,
            engine='cfgrib',
            backend_kwargs={'filter_by_keys': {'typeOfLevel': 'heightAboveGround', 'level': 10}, 'indexpath': ''},
        )

        ds_msl = ds_msl.load()
        ds_surface = ds_surface.load()
        if ds_surface_accum is not None:
            ds_surface_accum = ds_surface_accum.load()
        ds_isobaric = ds_isobaric.load()
        ds_2m = ds_2m.load()
        ds_10m = ds_10m.load()

        # Avoid merge conflicts between 2m and 10m scalar height coordinates.
        ds_2m = ds_2m.drop_vars('heightAboveGround', errors='ignore')
        ds_10m = ds_10m.drop_vars('heightAboveGround', errors='ignore')

        # Convert isobaric level coordinate from hPa to Pa to match existing plotting code.
        isobaric_pa = (ds_isobaric['isobaricInhPa'] * 100).astype(np.int32)
        ds_isobaric = ds_isobaric.assign_coords(isobaricInhPa=isobaric_pa)
        ds_isobaric = ds_isobaric.rename({'isobaricInhPa': 'isobaric'})

        u10 = ds_10m['u10'].expand_dims({'height_above_ground2': [10]})
        v10 = ds_10m['v10'].expand_dims({'height_above_ground2': [10]})
        t2m = ds_2m['t2m'].expand_dims({'height_above_ground3': [2]})
        d2m = ds_2m['d2m'].expand_dims({'height_above_ground4': [2]})

        dataset_vars = {
            'MSLP_Eta_model_reduction_msl': ds_msl['prmsl'],
            'Geopotential_height_isobaric': ds_isobaric['gh'],
            'Geopotential_height_surface': ds_surface['orog'],
            'Precipitation_rate_surface': ds_surface['prate'],
            'u-component_of_wind_height_above_ground': u10,
            'v-component_of_wind_height_above_ground': v10,
            'Relative_humidity_isobaric': ds_isobaric['r'],
            'Temperature_isobaric': ds_isobaric['t'],
            'Temperature_height_above_ground': t2m,
            'Dewpoint_temperature_height_above_ground': d2m,
        }

        if 'cape' in ds_surface.data_vars:
            dataset_vars['Convective_available_potential_energy_surface'] = ds_surface['cape']
        if 'pres' in ds_surface.data_vars:
            dataset_vars['Pressure_surface'] = ds_surface['pres']

        if 'u' in ds_isobaric.data_vars:
            dataset_vars['u-component_of_wind_isobaric'] = ds_isobaric['u']
        if 'v' in ds_isobaric.data_vars:
            dataset_vars['v-component_of_wind_isobaric'] = ds_isobaric['v']

        if 'wz' in ds_isobaric.data_vars:
            dataset_vars['Geometric_vertical_velocity_isobaric'] = ds_isobaric['wz']

        if include_airmass_fields:
            # NCEP HPBL decodes as 'unknown' in cfgrib (no standard name mapping).
            # Scan all sub-datasets from the same GRIB2 file to find it.
            all_surface_ds = cfgrib.open_datasets(
                temp_grib_path,
                backend_kwargs={
                    'filter_by_keys': {'typeOfLevel': 'surface', 'stepType': 'instant'},
                    'indexpath': '',
                },
            )
            for _ds in all_surface_ds:
                if 'unknown' in _ds.data_vars:
                    _ds = _ds.load()
                    dataset_vars['Planetary_boundary_layer_height_surface'] = _ds['unknown']
                    break

        if include_ts_fields:
            if forecast_hour == 0:
                acpcp_1h = np.zeros_like(ds_surface['prate'].values)
                current_acpcp_values = np.zeros_like(ds_surface['prate'].values)
            else:
                current_acpcp_values = ds_surface_accum['acpcp'].values
                if prev_acpcp_values is None:
                    prev_acpcp_values = get_gfs_convective_precip_accumulation(forecast_hour - 1, init_time)
                acpcp_1h = derive_incremental_accumulation(current_acpcp_values, prev_acpcp_values)

            dataset_vars['Convective_precipitation_1h_surface'] = (
                ('latitude', 'longitude'),
                acpcp_1h,
            )
        else:
            current_acpcp_values = None

        ds = xr.Dataset(
            dataset_vars
        )

        ds = ds.load()
        return ds, init_time, current_acpcp_values
    finally:
        safe_remove_file(temp_grib_path)


def get_latest_icon_run(target_time=None):
    base_url = 'https://opendata.dwd.de/weather/nwp/icon/grib'
    run_candidates = []

    for cycle in ['00', '06', '12', '18']:
        listing_url = f'{base_url}/{cycle}/pmsl/'
        response = requests.get(listing_url, timeout=60)
        if response.status_code != 200:
            continue

        files = re.findall(r'href="([^"]+_PMSL\.grib2\.bz2)"', response.text)
        if not files:
            continue

        run_match = re.search(r'_(\d{10})_\d{3}_PMSL\.grib2\.bz2$', files[0])
        if not run_match:
            continue

        run_stamp = run_match.group(1)
        run_dt = datetime.strptime(run_stamp, '%Y%m%d%H').replace(tzinfo=timezone.utc)
        run_candidates.append((run_dt, cycle, run_stamp))

    if not run_candidates:
        raise RuntimeError('No ICON runs found on DWD open data')

    if target_time is not None:
        matching = [item for item in run_candidates if item[0] == target_time]
        if matching:
            run_dt, cycle, run_stamp = matching[0]
        else:
            older_or_equal = [item for item in run_candidates if item[0] <= target_time]
            if older_or_equal:
                run_dt, cycle, run_stamp = max(older_or_equal, key=lambda item: item[0])
            else:
                run_dt, cycle, run_stamp = min(run_candidates, key=lambda item: item[0])
    else:
        run_dt, cycle, run_stamp = max(run_candidates, key=lambda item: item[0])

    return {
        'base_url': base_url,
        'cycle': cycle,
        'run_stamp': run_stamp,
        'init_time': run_dt,
    }


def evict_stale_icon_cache(current_run_stamp):
    """Remove all cache entries that do not belong to current_run_stamp."""
    if not os.path.isdir(ICON_CACHE_DIR):
        return
    removed = 0
    freed_bytes = 0
    for entry in os.listdir(ICON_CACHE_DIR):
        if not entry.startswith(current_run_stamp + '_'):
            file_path = os.path.join(ICON_CACHE_DIR, entry)
            try:
                freed_bytes += os.path.getsize(file_path)
                os.remove(file_path)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f'Evicted {removed} stale ICON cache entries ({freed_bytes / 1024 / 1024:.1f} MB freed)')


def _download_icon_grib2(icon_run, folder, filename):
    """Download and bz2-decompress one ICON GRIB2 file, using a local cache."""
    cache_path = os.path.join(ICON_CACHE_DIR, f"{icon_run['run_stamp']}_{folder}_{filename}")
    if os.path.exists(cache_path):
        with open(cache_path, 'rb') as fh:
            return fh.read()

    url = f"{icon_run['base_url']}/{icon_run['cycle']}/{folder}/{filename}"
    response = requests.get(url, timeout=240)
    response.raise_for_status()
    raw = bz2.decompress(response.content)

    os.makedirs(ICON_CACHE_DIR, exist_ok=True)
    with open(cache_path, 'wb') as fh:
        fh.write(raw)

    return raw


def _load_icon_field(icon_run, folder, filename):
    raw_grib = _download_icon_grib2(icon_run, folder, filename)
    with tempfile.NamedTemporaryFile(suffix='.grib2', delete=False) as temp_grib:
        temp_grib.write(raw_grib)
        temp_path = temp_grib.name

    try:
        ds = xr.open_dataset(temp_path, engine='cfgrib', backend_kwargs={'indexpath': ''})
        ds = ds.load()
    finally:
        safe_remove_file(temp_path)

    var_name = list(ds.data_vars)[0]
    return ds[var_name], ds


def _load_icon_static_remap(icon_run):
    clat_file = f"icon_global_icosahedral_time-invariant_{icon_run['run_stamp']}_CLAT.grib2.bz2"
    clon_file = f"icon_global_icosahedral_time-invariant_{icon_run['run_stamp']}_CLON.grib2.bz2"
    hsurf_file = f"icon_global_icosahedral_time-invariant_{icon_run['run_stamp']}_HSURF.grib2.bz2"

    clat_da, _ = _load_icon_field(icon_run, 'clat', clat_file)
    clon_da, _ = _load_icon_field(icon_run, 'clon', clon_file)
    hsurf_da, _ = _load_icon_field(icon_run, 'hsurf', hsurf_file)

    clat = clat_da.values
    clon = clon_da.values

    # ICON longitudes are in [-180, 180], convert to [0, 360) to match the plotting domain.
    clon_0360 = np.where(clon < 0, clon + 360.0, clon)

    domain_mask = (
        (clat >= data_lat_min - 1.0)
        & (clat <= data_lat_max + 1.0)
        & (clon_0360 >= data_lon_min - 1.0)
        & (clon_0360 <= data_lon_max + 1.0)
    )

    clat_sel = clat[domain_mask]
    clon_sel = clon_0360[domain_mask]
    hsurf_sel = hsurf_da.values[domain_mask]

    target_lons = np.arange(data_lon_min, data_lon_max + 0.0001, 0.25)
    target_lats = np.arange(data_lat_min, data_lat_max + 0.0001, 0.25)
    lon_grid, lat_grid = np.meshgrid(target_lons, target_lats)

    points = np.column_stack((clon_sel, clat_sel))
    tree = cKDTree(points)
    target_points = np.column_stack((lon_grid.ravel(), lat_grid.ravel()))
    _, nn_indices = tree.query(target_points)

    return {
        'mask': domain_mask,
        'lons': target_lons,
        'lats': target_lats,
        'lon_grid': lon_grid,
        'lat_grid': lat_grid,
        'nn_indices': nn_indices,
        'hsurf_grid': hsurf_sel[nn_indices].reshape(lat_grid.shape),
    }


def _regrid_icon_to_domain(icon_field_values, remap_state):
    values = icon_field_values[remap_state['mask']]
    return values[remap_state['nn_indices']].reshape(remap_state['lat_grid'].shape)


def get_icon_data(forecast_hour, icon_run, remap_state, prev_tp_values=None):
    """Fetch and regrid all ICON fields for one forecast hour.

    All per-field downloads and GRIB parses are dispatched in parallel via a
    thread pool.  Pass the raw TOT_PREC numpy array from the previous call as
    *prev_tp_values* to avoid re-downloading the prior hour's accumulation.

    Returns (ds, init_time, tp_raw_values) where tp_raw_values can be passed
    as prev_tp_values on the next consecutive forecast hour.
    """
    if forecast_hour < 0 or forecast_hour > 72:
        raise ValueError('ICON hourly generation is configured for forecast hours 0-72')

    run_stamp = icon_run['run_stamp']
    fh = f'{forecast_hour:03d}'
    pressure_levels_hpa = [1000, 950, 900, 850, 500]

    # Build the full list of (task_key, folder, filename) for this hour.
    fetch_tasks = [
        ('pmsl',    'pmsl',     f'icon_global_icosahedral_single-level_{run_stamp}_{fh}_PMSL.grib2.bz2'),
        ('u_10m',   'u_10m',    f'icon_global_icosahedral_single-level_{run_stamp}_{fh}_U_10M.grib2.bz2'),
        ('v_10m',   'v_10m',    f'icon_global_icosahedral_single-level_{run_stamp}_{fh}_V_10M.grib2.bz2'),
        ('t_2m',    't_2m',     f'icon_global_icosahedral_single-level_{run_stamp}_{fh}_T_2M.grib2.bz2'),
        ('td_2m',   'td_2m',    f'icon_global_icosahedral_single-level_{run_stamp}_{fh}_TD_2M.grib2.bz2'),
        ('tot_prec','tot_prec', f'icon_global_icosahedral_single-level_{run_stamp}_{fh}_TOT_PREC.grib2.bz2'),
    ]
    for level_hpa in pressure_levels_hpa:
        fetch_tasks.append((
            f'fi_{level_hpa}', 'fi',
            f'icon_global_icosahedral_pressure-level_{run_stamp}_{fh}_{level_hpa}_FI.grib2.bz2',
        ))
        fetch_tasks.append((
            f'rh_{level_hpa}', 'relhum',
            f'icon_global_icosahedral_pressure-level_{run_stamp}_{fh}_{level_hpa}_RELHUM.grib2.bz2',
        ))

    # Also fetch previous-hour TOT_PREC when it is not already available in memory.
    need_prev_tp = forecast_hour > 0 and prev_tp_values is None
    if need_prev_tp:
        prev_fh = f'{forecast_hour - 1:03d}'
        fetch_tasks.append((
            'tot_prec_prev', 'tot_prec',
            f'icon_global_icosahedral_single-level_{run_stamp}_{prev_fh}_TOT_PREC.grib2.bz2',
        ))

    def _fetch(task):
        key, folder, filename = task
        da, _ = _load_icon_field(icon_run, folder, filename)
        return key, da

    with ThreadPoolExecutor(max_workers=min(len(fetch_tasks), 8)) as pool:
        fetched = dict(pool.map(_fetch, fetch_tasks))

    pmsl_da  = fetched['pmsl']
    u10_da   = fetched['u_10m']
    v10_da   = fetched['v_10m']
    t2m_da   = fetched['t_2m']
    td2m_da  = fetched['td_2m']
    tp_da    = fetched['tot_prec']

    fi_levels = {level: fetched[f'fi_{level}']  for level in pressure_levels_hpa}
    rh_levels = {level: fetched[f'rh_{level}']  for level in pressure_levels_hpa}

    # Convert ICON accumulated precipitation to an hourly rate (mm/s).
    if forecast_hour == 0:
        tp_rate_raw = np.zeros_like(tp_da.values)
    elif prev_tp_values is not None:
        tp_rate_raw = np.maximum(tp_da.values - prev_tp_values, 0.0) / 3600.0
    else:
        tp_rate_raw = np.maximum(tp_da.values - fetched['tot_prec_prev'].values, 0.0) / 3600.0

    pmsl_grid = _regrid_icon_to_domain(pmsl_da.values, remap_state)
    u10_grid = _regrid_icon_to_domain(u10_da.values, remap_state)
    v10_grid = _regrid_icon_to_domain(v10_da.values, remap_state)
    t2m_grid = _regrid_icon_to_domain(t2m_da.values, remap_state)
    td2m_grid = _regrid_icon_to_domain(td2m_da.values, remap_state)
    tp_rate_grid = _regrid_icon_to_domain(tp_rate_raw, remap_state)

    fi_grids = {
        int(level * 100): _regrid_icon_to_domain(fi_levels[level].values / 9.80665, remap_state)
        for level in pressure_levels_hpa
    }
    rh_grids = {
        int(level * 100): _regrid_icon_to_domain(rh_levels[level].values, remap_state)
        for level in pressure_levels_hpa
    }

    isobaric_levels_pa = np.array(sorted(fi_grids.keys()), dtype=np.int32)
    geopotential_height = np.stack([fi_grids[level] for level in isobaric_levels_pa], axis=0)
    rel_humidity = np.stack([rh_grids[level] for level in isobaric_levels_pa], axis=0)

    ds = xr.Dataset(
        {
            'MSLP_Eta_model_reduction_msl': (('latitude', 'longitude'), pmsl_grid),
            'Geopotential_height_isobaric': (('isobaric', 'latitude', 'longitude'), geopotential_height),
            'Geopotential_height_surface': (('latitude', 'longitude'), remap_state['hsurf_grid']),
            'Precipitation_rate_surface': (('latitude', 'longitude'), tp_rate_grid),
            'u-component_of_wind_height_above_ground': (
                ('height_above_ground2', 'latitude', 'longitude'),
                u10_grid[np.newaxis, :, :],
            ),
            'v-component_of_wind_height_above_ground': (
                ('height_above_ground2', 'latitude', 'longitude'),
                v10_grid[np.newaxis, :, :],
            ),
            'Relative_humidity_isobaric': (('isobaric', 'latitude', 'longitude'), rel_humidity),
            'Temperature_height_above_ground': (
                ('height_above_ground3', 'latitude', 'longitude'),
                t2m_grid[np.newaxis, :, :],
            ),
            'Dewpoint_temperature_height_above_ground': (
                ('height_above_ground4', 'latitude', 'longitude'),
                td2m_grid[np.newaxis, :, :],
            ),
        },
        coords={
            'longitude': remap_state['lons'],
            'latitude': remap_state['lats'],
            'isobaric': isobaric_levels_pa,
            'height_above_ground2': [10],
            'height_above_ground3': [2],
            'height_above_ground4': [2],
        },
    )

    return ds, icon_run['init_time'], tp_da.values

# Function to plot the map
def plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='bg', domain_id='AU'):
    fig = plt.figure(figsize=(BASE_FIGURE_WIDTH_INCHES, BASE_FIGURE_HEIGHT_INCHES))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    ts_flash_profile = layer_profile == 'ts_flash'
    ts_severe_profile = layer_profile == 'ts_severe'
    bg_profile = layer_profile == 'bg'
    turb_mtw_profile = layer_profile == 'turb_mtw'
    turb_wind_profile = layer_profile == 'turb_wind'
    turb_profile = turb_mtw_profile or turb_wind_profile
    bg_or_turb_profile = bg_profile or turb_profile
    airmass_fzl_profile = layer_profile == 'airmass_fzl'
    airmass_4c_profile = layer_profile == 'airmass_4c'
    airmass_snow_profile = layer_profile == 'airmass_snow'
    airmass_profile = airmass_fzl_profile or airmass_4c_profile or airmass_snow_profile

    plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max = get_domain_extent(
        domain_id,
        layer_profile=layer_profile,
    )

    # Keep Thunderstorms profile layering unchanged while restoring the requested Fog & Low Cloud/Turbulence stack.
    if bg_or_turb_profile:
        z_coast = 9
        z_borders = 9
        z_gaf = 9
        z_mslp = 2
        z_fog = 3
        z_low_cloud = 4
        z_drizzle = 5
        z_bg_precip = 6
        z_thickness = 7
        z_bg_barbs = 8
        z_taf_points = 10
        z_taf_text = 11
    else:
        z_coast = 220
        z_borders = 220
        z_gaf = 221
        z_mslp = 5
        z_thickness = 6
        z_low_cloud = 2
        z_fog = 3
        z_drizzle = 4
        z_bg_precip = 7
        z_bg_barbs = 6
        z_taf_points = 100
        z_taf_text = 101
    
    # Load TAF data early for plotting later
    taf_lons = []
    taf_lats = []
    taf_names = []
    try:
        for name, lat, lon in get_taf_location_rows():
            # Only include TAF points within the domain bounds
            # Also account for label offset so text remains inside the domain.
            if (plot_lat_min <= lat <= plot_lat_max and plot_lon_min <= lon <= plot_lon_max and
                lon + TAF_LABEL_DX >= plot_lon_min and lat + TAF_LABEL_DY <= plot_lat_max):
                taf_names.append(name)
                taf_lats.append(lat)
                taf_lons.append(lon)
        print(f"Loaded {len(taf_names)} TAF locations within domain")
    except Exception as e:
        print(f"Warning: Could not load TAF locations: {e}")
    
    # Add land/border boundaries on top of all weather layers.
    ax.add_feature(cfeature.COASTLINE, linewidth=0.8, zorder=z_coast)
    ax.add_feature(cfeature.BORDERS, linewidth=0.7, zorder=z_borders)
    
    # Add GAF boundaries from shapefile
    ax.add_feature(get_gaf_feature(), zorder=z_gaf)
    
    # Set extent
    ax.set_extent([plot_lon_min, plot_lon_max, plot_lat_min, plot_lat_max], crs=ccrs.PlateCarree())
    
    # Plot topography shading beneath all weather layers.
    if bg_or_turb_profile or ts_flash_profile or ts_severe_profile or airmass_profile:
        print("Plotting topography...")
        elevation, elev_lons, elev_lats = get_elevation_data(data)
        lon_grid, lat_grid = np.meshgrid(elev_lons, elev_lats)

        # Elevation levels and colors from XML
        elev_levels = [-1000, -15, 0, 50, 75, 150, 250, 350, 500, 1000, 1500, 2000, 2200]
        elev_colors = [
            (234/255.0, 244/255.0, 255/255.0),  # -1000 - ocean blue (#eaf4ff)
            (234/255.0, 244/255.0, 255/255.0),  # -15 - ocean blue (#eaf4ff)
            (230/255.0, 230/255.0, 190/255.0),  # 0 - intermediate tan (#e6e6be)
            (230/255.0, 230/255.0, 190/255.0),  # 50 - intermediate tan (#e6e6be)
            (230/255.0, 230/255.0, 190/255.0),  # 75 - intermediate tan (#e6e6be)
            (210/255.0, 210/255.0, 157/255.0),  # 150 - tan (#d2d29d)
            (170/255.0, 170/255.0, 127/255.0),  # 250 - darker tan
            (255/255.0, 255/255.0, 0/255.0),    # 350 - yellow
            (255/255.0, 102/255.0, 0/255.0),    # 500 - orange
            (153/255.0, 51/255.0, 0/255.0),     # 1000 - dark brown
            (0/255.0, 204/255.0, 255/255.0),    # 1500 - cyan
            (192/255.0, 192/255.0, 192/255.0),  # 2000 - gray
            (255/255.0, 255/255.0, 255/255.0)   # 2200+ - white
        ]

        elev_cmap = ListedColormap(elev_colors)
        elev_norm = BoundaryNorm(elev_levels, elev_cmap.N, clip=True)

        # Create a modified elevation array where ocean areas are set to -1000.
        ocean_mask = get_ocean_mask(elev_lons, elev_lats)
        elev_masked = elevation.copy()
        elev_masked[ocean_mask] = -1000

        cs_topo = ax.contourf(
            lon_grid,
            lat_grid,
            elev_masked,
            levels=elev_levels,
            cmap=elev_cmap,
            norm=elev_norm,
            alpha=0.3,
            extend='both',
            transform=ccrs.PlateCarree(),
            zorder=0)
    
    if turb_mtw_profile:
        mtw_intensity = calculate_mountain_wave_intensity(data)
        if mtw_intensity is not None:
            mtw_masked = np.where(mtw_intensity >= 0.2, mtw_intensity, np.nan)
            mtw_levels = [0.2, 0.4, 0.6]
            mtw_colors = ['#ff9900', '#ff0000']
            mtw_cmap = ListedColormap(mtw_colors)
            mtw_cmap.set_over('#ff00ff')
            mtw_norm = BoundaryNorm(mtw_levels, mtw_cmap.N, clip=False)

            ax.contourf(
                data.longitude,
                data.latitude,
                mtw_masked,
                levels=mtw_levels,
                cmap=mtw_cmap,
                norm=mtw_norm,
                alpha=0.9,
                extend='max',
                transform=ccrs.PlateCarree(),
                zorder=250,
            )

    if turb_wind_profile:
        max_wind_kt = calculate_max_wind_below_850hpa_kt(data)
        wind_masked = np.where(max_wind_kt >= 25.0, max_wind_kt, np.nan)
        wind_levels = [25, 30, 35, 40, 45, 50, 55, 60, 70, 80]
        wind_colors = [
            '#00ff00',
            '#006800',
            '#00b4ff',
            '#0064ff',
            '#0000ff',
            '#ff5555',
            '#ff0000',
            '#e60000',
            '#be0000',
        ]
        wind_cmap = ListedColormap(wind_colors)
        wind_cmap.set_over('#960000')
        wind_norm = BoundaryNorm(wind_levels, wind_cmap.N, clip=False)

        ax.contourf(
            data.longitude,
            data.latitude,
            wind_masked,
            levels=wind_levels,
            cmap=wind_cmap,
            norm=wind_norm,
            alpha=0.5,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=5.5,
        )

        shear_turbulence = calculate_shear_turbulence_category(data)
        shear_turbulence_masked = np.ma.masked_where(shear_turbulence == 0, shear_turbulence)
        shear_turbulence_cmap = ListedColormap(['#ffcc00', '#ff6600', '#ff0000'])
        shear_turbulence_levels = [0.5, 1.5, 2.5, 3.5]
        shear_turbulence_norm = BoundaryNorm(shear_turbulence_levels, shear_turbulence_cmap.N, clip=True)

        ax.contourf(
            data.longitude,
            data.latitude,
            shear_turbulence_masked,
            levels=shear_turbulence_levels,
            cmap=shear_turbulence_cmap,
            norm=shear_turbulence_norm,
            alpha=0.9,
            transform=ccrs.PlateCarree(),
            zorder=230,
        )

        ax.contour(
            data.longitude,
            data.latitude,
            shear_turbulence_masked,
            levels=shear_turbulence_levels[1:-1],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=230.05,
        )

        shear_patch_mask = (shear_turbulence > 0).astype(float)
        ax.contour(
            data.longitude,
            data.latitude,
            shear_patch_mask,
            levels=[0.5],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=230.1,
        )

        lee_turbulence = calculate_lee_turbulence_category(data)
        lee_turbulence_masked = np.ma.masked_where(lee_turbulence == 0, lee_turbulence)
        lee_turbulence_cmap = ListedColormap(['#ffcc00', '#ff6600', '#ff0000'])
        lee_turbulence_levels = [0.5, 1.5, 2.5, 3.5]
        lee_turbulence_norm = BoundaryNorm(lee_turbulence_levels, lee_turbulence_cmap.N, clip=True)

        ax.contourf(
            data.longitude,
            data.latitude,
            lee_turbulence_masked,
            levels=lee_turbulence_levels,
            cmap=lee_turbulence_cmap,
            norm=lee_turbulence_norm,
            alpha=0.9,
            transform=ccrs.PlateCarree(),
            zorder=231,
        )

        ax.contour(
            data.longitude,
            data.latitude,
            lee_turbulence_masked,
            levels=lee_turbulence_levels[1:-1],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=231.05,
        )

        lee_patch_mask = (lee_turbulence > 0).astype(float)
        ax.contour(
            data.longitude,
            data.latitude,
            lee_patch_mask,
            levels=[0.5],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=231.1,
        )

    # Plot MSLP except for Airmass profiles.
    if not airmass_profile:
        mslp_alpha = 0.2 if turb_profile else (0.5 if bg_profile else 0.3)
        mslp = get_time_index(data['MSLP_Eta_model_reduction_msl']) / 100  # Convert to hPa
        cs_mslp = ax.contour(
            data.longitude,
            data.latitude,
            mslp,
            levels=np.arange(980, 1040, 4),
            colors='black',
            linewidths=1,
            alpha=mslp_alpha,
            zorder=z_mslp,
        )
        mslp_labels = ax.clabel(cs_mslp, inline=True, fontsize=8)
        for label in mslp_labels:
            label.set_alpha(mslp_alpha)

    if ts_severe_profile:
        # Plot 250 hPa isotachs for severe storm potential context.
        u_250 = get_isobaric_field(data['u-component_of_wind_isobaric'], 25000)
        v_250 = get_isobaric_field(data['v-component_of_wind_isobaric'], 25000)
        wind_250_kt = np.sqrt(u_250.values**2 + v_250.values**2) * 1.94384
        isotach_masked = np.where(wind_250_kt >= 80.0, wind_250_kt, np.nan)

        isotach_levels = [80, 100, 120, 140, 160, 180, 300]
        isotach_colors = ['#000080', '#ffff00', '#ff6600', '#ff0000', '#800000', '#ff00ff']
        isotach_cmap = ListedColormap(isotach_colors)
        isotach_norm = BoundaryNorm(isotach_levels, isotach_cmap.N, clip=False)

        ax.contourf(
            data.longitude,
            data.latitude,
            isotach_masked,
            levels=isotach_levels,
            cmap=isotach_cmap,
            norm=isotach_norm,
            alpha=0.5,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=6,
        )
    
    if layer_profile == 'bg':
        # Plot thickness (1000-500 hPa)
        hgt_1000 = get_isobaric_field(data['Geopotential_height_isobaric'], 100000) / 10  # Convert to dam
        hgt_500 = get_isobaric_field(data['Geopotential_height_isobaric'], 50000) / 10
        thickness = hgt_500 - hgt_1000

        # Define major and minor levels
        major_levels = np.arange(480, 602, 18)
        all_minor_levels = np.arange(480, 602, 6)

        # Plot only minor lines that are NOT major lines
        major_levels_set = set(major_levels)
        minor_only_levels = np.array([x for x in all_minor_levels if x not in major_levels_set])

        cs_thickness_minor = ax.contour(data.longitude, data.latitude, thickness, levels=minor_only_levels,
                 colors='red', linewidths=1, linestyles='dashed', alpha=0.5, zorder=z_thickness)

        # Plot major interval lines (spacing of 18, blue)
        cs_thickness_major = ax.contour(data.longitude, data.latitude, thickness, levels=major_levels,
                 colors='blue', linewidths=1, linestyles='dashed', alpha=0.5, zorder=z_thickness)
        ax.clabel(cs_thickness_major, inline=True, fontsize=8, fmt='%d')

        # Plot low cloud layer (maxRH at 1000, 975, 950 hPa)
        rh_1000 = get_isobaric_field(data['Relative_humidity_isobaric'], 100000)
        rh_975 = get_isobaric_field(data['Relative_humidity_isobaric'], 97500)
        rh_950 = get_isobaric_field(data['Relative_humidity_isobaric'], 95000)

        # Calculate maximum RH across the three levels
        max_rh = np.maximum(np.maximum(rh_1000.values, rh_975.values), rh_950.values)

        # Create custom colormap for low clouds
        cloud_colors = ['#ffffff', '#c86400', '#aa5500', '#6d3600']
        cloud_levels = [85, 90, 95, 97.5, 100]
        cloud_cmap = ListedColormap(cloud_colors)
        cloud_norm = BoundaryNorm(cloud_levels, cloud_cmap.N, clip=True)

        cs_cloud = ax.contourf(
            data.longitude,
            data.latitude,
            max_rh,
            levels=cloud_levels,
            cmap=cloud_cmap,
            norm=cloud_norm,
            alpha=0.6,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=z_low_cloud)

        # Plot drizzle layer (average RH at 950, 900, 850 hPa)
        rh_950_drizzle = get_isobaric_field(data['Relative_humidity_isobaric'], 95000)
        rh_900 = get_isobaric_field(data['Relative_humidity_isobaric'], 90000)
        rh_850 = get_isobaric_field(data['Relative_humidity_isobaric'], 85000)

        # Calculate average RH across the three levels
        avg_rh_drizzle = (rh_950_drizzle.values + rh_900.values + rh_850.values) / 3.0

        # Mask out values below 92.5%
        drizzle_masked = np.where(avg_rh_drizzle >= 92.5, avg_rh_drizzle, np.nan)

        # Create colormap for drizzle (green only where RH >= 95%)
        drizzle_levels = [92.5, 95, 100]
        drizzle_colors = [(1.0, 1.0, 1.0, 0.0), (0.0, 1.0, 0.0, 1.0)]  # Transparent white, opaque green
        drizzle_cmap = ListedColormap(drizzle_colors)
        drizzle_norm = BoundaryNorm(drizzle_levels, drizzle_cmap.N, clip=True)

        cs_drizzle = ax.contourf(
            data.longitude,
            data.latitude,
            drizzle_masked,
            levels=drizzle_levels,
            cmap=drizzle_cmap,
            norm=drizzle_norm,
            alpha=0.6,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=z_drizzle)

    if airmass_4c_profile:
        isotherm_height_ft = calculate_isotherm_height_ft(data, 4.0)
        isotherm_height_ft = np.where(isotherm_height_ft >= 0, isotherm_height_ft, np.nan)

        iso_levels = [0, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000, 18000, 20000]
        iso_colors = [
            '#99509f',
            '#36c6f4',
            '#6fccdd',
            '#69bd45',
            '#cbdb2a',
            '#f6eb14',
            '#fbce08',
            '#f8981d',
            '#ed2024',
            '#ed2891',
        ]
        iso_cmap = ListedColormap(iso_colors)
        iso_norm = BoundaryNorm(iso_levels, iso_cmap.N, clip=False)

        ax.contourf(
            data.longitude,
            data.latitude,
            isotherm_height_ft,
            levels=iso_levels,
            cmap=iso_cmap,
            norm=iso_norm,
            alpha=0.1,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=4.8,
        )

        draw_height_isolines_with_labels(
            ax, data.longitude, data.latitude, isotherm_height_ft, iso_cmap, iso_norm
        )

        # Marginal icing: max of mean RH in 0–4C and -5–0C as hatched fill + dashed outline.
        marginal_icing_rh = calculate_marginal_icing_relative_humidity(data)
        marginal_icing_bool = np.isfinite(marginal_icing_rh) & (marginal_icing_rh >= 92.0)
        # Drop single-grid speckles so only coherent risk areas remain.
        marginal_icing_mask = clean_binary_mask(
            marginal_icing_bool,
            min_cells=8,
            open_iterations=1,
        ).astype(float)
        hatch_overlay = ax.contourf(
            data.longitude,
            data.latitude,
            marginal_icing_mask,
            levels=[0.5, 1.5],
            colors=[(0.0, 0.0, 0.0, 0.0)],
            hatches=['///'],
            transform=ccrs.PlateCarree(),
            zorder=55,
        )
        hatch_overlay.set_facecolors([(0.0, 0.0, 0.0, 0.0)])
        hatch_overlay.set_edgecolors([(0.0, 0.35, 0.55, 0.55)])
        # Suppress solid polygon borders from contourf while keeping hatch strokes.
        hatch_overlay.set_linewidth(0.0)

        ax.contour(
            data.longitude,
            data.latitude,
            marginal_icing_mask,
            levels=[0.5],
            colors=[(0.0, 0.25, 0.45, 0.4)],
            linewidths=1.1,
            linestyles=[(0, (5, 3))],
            transform=ccrs.PlateCarree(),
            zorder=55.1,
        )

    if airmass_fzl_profile:
        freezing_level_ft = calculate_freezing_level_height_ft(data)
        freezing_level_ft = np.where(freezing_level_ft >= 0, freezing_level_ft, np.nan)

        fzl_levels = [0, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000, 18000, 20000]
        fzl_colors = [
            '#99509f',
            '#36c6f4',
            '#6fccdd',
            '#69bd45',
            '#cbdb2a',
            '#f6eb14',
            '#fbce08',
            '#f8981d',
            '#ed2024',
            '#ed2891',
        ]
        fzl_cmap = ListedColormap(fzl_colors)
        fzl_norm = BoundaryNorm(fzl_levels, fzl_cmap.N, clip=False)

        # Keep the freezing-level fill beneath all FZL model overlays.
        ax.contourf(
            data.longitude,
            data.latitude,
            freezing_level_ft,
            levels=fzl_levels,
            cmap=fzl_cmap,
            norm=fzl_norm,
            alpha=0.1,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=4.8,
        )

        # Same isoline/label scheme as the 4C isotherm height map.
        draw_height_isolines_with_labels(
            ax, data.longitude, data.latitude, freezing_level_ft, fzl_cmap, fzl_norm
        )

        # Diagonal cross-hatching where a freezing layer exists
        # (multiple 0C crossings in the vertical column).
        freezing_layer_mask = calculate_freezing_layer_mask(data).astype(float)
        hatch_overlay = ax.contourf(
            data.longitude,
            data.latitude,
            freezing_layer_mask,
            levels=[0.5, 1.5],
            colors=[(0.0, 0.0, 0.0, 0.0)],
            hatches=['xx'],
            transform=ccrs.PlateCarree(),
            zorder=55,
        )
        hatch_overlay.set_facecolors([(0.0, 0.0, 0.0, 0.0)])
        hatch_overlay.set_edgecolors([(0.0, 0.0, 0.0, 0.5)])

        # Icing layer: mean RH between 0C and -15C, using provided XML color bins.
        icing_rh = calculate_icing_relative_humidity(data)
        icing_masked = np.where(icing_rh >= 95.0, icing_rh, np.nan)
        icing_levels = [0, 95, 97.5, 100]
        icing_colors = [
            (1.0, 1.0, 1.0, 0.0),
            (0.0, 204.0 / 255.0, 1.0, 1.0),
            (0.0, 1.0, 1.0, 1.0),
        ]
        icing_cmap = ListedColormap(icing_colors)
        icing_norm = BoundaryNorm(icing_levels, icing_cmap.N, clip=True)

        ax.contourf(
            data.longitude,
            data.latitude,
            icing_masked,
            levels=icing_levels,
            cmap=icing_cmap,
            norm=icing_norm,
            alpha=0.78,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=5.8,
        )

        # Dashed bin outlines between icing increments.
        ax.contour(
            data.longitude,
            data.latitude,
            icing_masked,
            levels=icing_levels[1:-1],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=5.85,
        )

        # Dashed outer boundary of the entire icing patch.
        icing_patch_mask = np.isfinite(icing_masked).astype(float)
        ax.contour(
            data.longitude,
            data.latitude,
            icing_patch_mask,
            levels=[0.5],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=5.9,
        )

        # Thermals layer: PBL height (ft) masked where 2m temp <= 30C, with XML palette.
        if 'Planetary_boundary_layer_height_surface' in data:
            temp_2m_raw = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)) - 273.15
            pblh_ft = get_time_index(data['Planetary_boundary_layer_height_surface']) * 3.28084
            thermals_ft = np.where(temp_2m_raw.values > 30.0, pblh_ft.values, np.nan)

            thermal_levels = [0, 6000, 10000, 20000]
            thermal_colors = [
                (1.0, 1.0, 1.0, 0.0),
                '#ffcc00',
                '#ff6600',
            ]
            thermal_cmap = ListedColormap(thermal_colors)
            thermal_norm = BoundaryNorm(thermal_levels, thermal_cmap.N, clip=False)

            ax.contourf(
                data.longitude,
                data.latitude,
                thermals_ft,
                levels=thermal_levels,
                cmap=thermal_cmap,
                norm=thermal_norm,
                alpha=0.75,
                extend='max',
                transform=ccrs.PlateCarree(),
                zorder=6,
            )
        else:
            print('Warning: HPBL field not available in dataset; thermals layer skipped.')

        # Cold pool hail layer: 1hr convective precip where 700hPa temp < -9°C
        if 'Convective_precipitation_1h_surface' in data:
            t700_c = get_isobaric_field(data['Temperature_isobaric'], 70000).values - 273.15
            cprecip = data['Convective_precipitation_1h_surface'].values
            hail_masked = np.where((t700_c < -9.0) & (cprecip >= 0.1), cprecip, np.nan)

            hail_levels = [0, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 40.0]
            hail_colors = [
                '#e0f7fa',
                '#b2ebf2',
                '#80deea',
                '#4dd0e1',
                '#00bcd4',
                '#0097a7',
                '#006064',
                '#00363a',
            ]
            hail_cmap = ListedColormap(hail_colors)
            hail_norm = BoundaryNorm(hail_levels, hail_cmap.N, clip=False)

            ax.contourf(
                data.longitude,
                data.latitude,
                hail_masked,
                levels=hail_levels,
                cmap=hail_cmap,
                norm=hail_norm,
                alpha=0.85,
                extend='max',
                transform=ccrs.PlateCarree(),
                zorder=7,
            )

            # Dashed bin outlines between hail increments.
            ax.contour(
                data.longitude,
                data.latitude,
                hail_masked,
                levels=hail_levels[1:-1],
                colors=[(0.08, 0.08, 0.08, 1.0)],
                linewidths=0.35,
                linestyles='dashed',
                transform=ccrs.PlateCarree(),
                zorder=7.1,
            )

            # Dashed outer boundary of the entire hail patch.
            hail_patch_mask = np.isfinite(hail_masked).astype(float)
            ax.contour(
                data.longitude,
                data.latitude,
                hail_patch_mask,
                levels=[0.5],
                colors=[(0.08, 0.08, 0.08, 1.0)],
                linewidths=0.35,
                linestyles='dashed',
                transform=ccrs.PlateCarree(),
                zorder=7.15,
            )
        else:
            print('Warning: Convective precipitation field not available; cold pool hail layer skipped.')

    if airmass_snow_profile:
        snow_level_m = calculate_estimated_snow_level_m(data)
        snow_level_ft = snow_level_m * 3.28084

        # Snow level isolines every 500 ft, colored by provided XIBL threshold bins.
        snow_level_contour_levels_ft = np.arange(0, 20000 + 500, 500)
        snow_level_bin_edges_ft = [0, 2500, 4000, 5000, 6500, 8000, 9800, 20000]
        snow_level_bin_colors = [
            '#c80000',  # 0-2500
            '#ff0000',  # 2500-4000
            '#ff5555',  # 4000-5000
            '#0000ff',  # 5000-6500
            '#0064ff',  # 6500-8000
            '#00c800',  # 8000-9800
            '#000000',  # 9800-20000
        ]

        snow_level_line_colors = []
        for level_ft in snow_level_contour_levels_ft:
            color_idx = len(snow_level_bin_colors) - 1
            for edge_idx in range(len(snow_level_bin_edges_ft) - 1):
                lower = snow_level_bin_edges_ft[edge_idx]
                upper = snow_level_bin_edges_ft[edge_idx + 1]
                if lower <= level_ft < upper:
                    color_idx = edge_idx
                    break
            snow_level_line_colors.append(snow_level_bin_colors[color_idx])

        snow_level_fill_cmap = ListedColormap(snow_level_bin_colors)
        snow_level_fill_norm = BoundaryNorm(snow_level_bin_edges_ft, snow_level_fill_cmap.N, clip=False)
        ax.contourf(
            data.longitude,
            data.latitude,
            snow_level_ft,
            levels=snow_level_bin_edges_ft,
            cmap=snow_level_fill_cmap,
            norm=snow_level_fill_norm,
            alpha=0.02,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=6.5,
        )

        cs_snow_level = ax.contour(
            data.longitude,
            data.latitude,
            snow_level_ft,
            levels=snow_level_contour_levels_ft,
            colors=snow_level_line_colors,
            linewidths=1.2,
            alpha=0.3,
            transform=ccrs.PlateCarree(),
            zorder=6.6,
        )
        ax.clabel(cs_snow_level, inline=True, fontsize=7, fmt='%d ft')
        
        # Plot 1-hour snow precipitation where WBFL < terrain height
        snow_precip = calculate_snow_precipitation_1h(data)
        if snow_precip is not None:
            # High-visibility glacial palette: bright blue at low values to white at high values
            snow_colors = [
                '#00b7ff',  # bright glacial blue
                '#2cc7ff',
                '#54d6ff',
                '#7ee3ff',
                '#a8eeff',
                '#cdf6ff',
                '#e9fcff',
                '#ffffff',  # bright white
            ]
            snow_cmap = ListedColormap(snow_colors)
            
            # Precipitation levels in mm (0.1, 0.2, 0.5, 1, 2, 4, 8 mm/hr)
            snow_precip_levels = [0.1, 0.2, 0.5, 1.0, 2.0, 4.0, 8.0]
            
            cf_snow = ax.contourf(
                data.longitude,
                data.latitude,
                snow_precip,
                levels=snow_precip_levels + [100],  # Add upper limit for contourf
                cmap=snow_cmap,
                alpha=0.82,
                transform=ccrs.PlateCarree(),
                zorder=7.0,
            )
            
            # Add contour lines for visibility
            cs_snow = ax.contour(
                data.longitude,
                data.latitude,
                snow_precip,
                levels=snow_precip_levels,
                colors='white',
                linewidths=0.5,
                alpha=0.6,
                transform=ccrs.PlateCarree(),
                zorder=7.05,
            )
    
    # Calculate 10 m surface wind once for both fog logic and wind barbs
    u_wind = get_time_index(data['u-component_of_wind_height_above_ground'].sel(height_above_ground2=10))
    v_wind = get_time_index(data['v-component_of_wind_height_above_ground'].sel(height_above_ground2=10))

    shear_u_ms = None
    shear_v_ms = None
    if ts_severe_profile:
        u_500 = get_isobaric_field(data['u-component_of_wind_isobaric'], 50000)
        v_500 = get_isobaric_field(data['v-component_of_wind_isobaric'], 50000)
        shear_u_ms = u_500.values - u_wind.values
        shear_v_ms = v_500.values - v_wind.values
        shear_mag_ms = np.sqrt(shear_u_ms**2 + shear_v_ms**2)

        if 'Convective_available_potential_energy_surface' in data:
            sbcape = get_time_index(data['Convective_available_potential_energy_surface'])
        else:
            print('Warning: CAPE field missing; using zeros for SigHail')
            sbcape = xr.zeros_like(u_wind)

        if 'Pressure_surface' in data:
            surface_pressure_hpa = get_time_index(data['Pressure_surface']) / 100.0
        else:
            print('Warning: Surface pressure missing; using MSLP proxy for SigHail mixing ratio')
            surface_pressure_hpa = get_time_index(data['MSLP_Eta_model_reduction_msl']) / 100.0
        td2m_c = get_time_index(data['Dewpoint_temperature_height_above_ground'].sel(height_above_ground4=2)) - 273.15
        mixing_ratio = mixing_ratio_from_dewpoint_and_pressure(td2m_c.values, surface_pressure_hpa.values)

        t700_c = get_isobaric_field(data['Temperature_isobaric'], 70000).values - 273.15
        t500_c = get_isobaric_field(data['Temperature_isobaric'], 50000).values - 273.15
        z700_km = get_isobaric_field(data['Geopotential_height_isobaric'], 70000).values / 1000.0
        z500_km = get_isobaric_field(data['Geopotential_height_isobaric'], 50000).values / 1000.0

        layer_dz_km = z700_km - z500_km
        layer_dz_km = np.where(np.abs(layer_dz_km) < 1e-6, np.nan, layer_dz_km)

        sighail = (
            sbcape.values
            * mixing_ratio
            * 1000.0
            * ((t700_c - t500_c) / layer_dz_km)
            * t500_c
            * shear_mag_ms
        ) / 44000000.0

        sighail_levels = [
            0.0, 0.1, 0.2, 0.3, 0.4, 0.5,
            0.6, 0.7, 0.8, 0.9, 1.0, 1.1,
            1.2, 1.3, 1.4, 1.5, 1.6, 1.7,
            1.8, 1.9, 2.0, 2.2, 2.4, 2.6,
            2.8, 3.0, 3.5, 4.0, 5.0,
        ]
        sighail_interval_colors = [
            (255, 255, 255, 0),
            (97, 0, 97, 120),
            (119, 0, 135, 120),
            (128, 0, 206, 120),
            (0, 127, 254, 120),
            (0, 168, 254, 120),
            (0, 208, 254, 120),
            (0, 245, 254, 120),
            (0, 254, 179, 120),
            (0, 254, 70, 120),
            (104, 254, 0, 120),
            (165, 254, 0, 120),
            (193, 254, 0, 120),
            (221, 254, 0, 120),
            (248, 254, 0, 120),
            (254, 231, 0, 120),
            (254, 202, 0, 120),
            (254, 171, 0, 120),
            (254, 140, 0, 120),
            (254, 107, 0, 120),
            (254, 70, 0, 120),
            (254, 0, 0, 120),
            (254, 77, 77, 120),
            (254, 109, 109, 120),
            (254, 132, 132, 120),
            (254, 171, 171, 120),
            (254, 210, 210, 120),
            (254, 223, 223, 120),
        ]
        sighail_over_color = (254, 254, 254, 120)
        sighail_colors_norm = [
            (r / 255.0, g / 255.0, b / 255.0, a / 255.0)
            for r, g, b, a in sighail_interval_colors
        ]
        sighail_cmap = ListedColormap(sighail_colors_norm)
        sighail_cmap.set_over(tuple(channel / 255.0 for channel in sighail_over_color))
        sighail_norm = BoundaryNorm(sighail_levels, sighail_cmap.N, clip=False)

        ax.contourf(
            data.longitude,
            data.latitude,
            sighail,
            levels=sighail_levels,
            cmap=sighail_cmap,
            norm=sighail_norm,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=6.1,
        )
    
    if layer_profile == 'bg':
        # Plot fog layer using 2 m temperature/dewpoint spread and light surface winds
        temp_2m = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)) - 273.15
        dewpoint_2m = get_time_index(data['Dewpoint_temperature_height_above_ground'].sel(height_above_ground4=2)) - 273.15
        wind_speed_kt = np.sqrt(u_wind.values**2 + v_wind.values**2) * 1.94384  # m/s to kt
        temp_dew_spread = temp_2m.values - dewpoint_2m.values

        fog = np.zeros_like(temp_dew_spread, dtype=int)
        fog[(wind_speed_kt < 5) & (temp_dew_spread < 1.0)] = 1
        fog[(wind_speed_kt < 3) & (temp_dew_spread < 0.5)] = 2
        fog[(wind_speed_kt < 1) & (temp_dew_spread < 0.1)] = 3

        print(f"Fog grid points - F1: {np.sum(fog == 1)}, F2: {np.sum(fog == 2)}, F3: {np.sum(fog == 3)}")

        fog_masked = np.ma.masked_where(fog == 0, fog)
        fog_cmap = ListedColormap(['#ff0000', '#ffaa7f', '#ffff00'])
        fog_levels = [0.5, 1.5, 2.5, 3.5]
        fog_norm = BoundaryNorm(fog_levels, fog_cmap.N, clip=True)

        cs_fog = ax.contourf(
            data.longitude,
            data.latitude,
            fog_masked,
            levels=fog_levels,
            cmap=fog_cmap,
            norm=fog_norm,
            alpha=0.6,
            transform=ccrs.PlateCarree(),
            zorder=z_fog)

    if airmass_fzl_profile:
        # Plot freezing fog using Fog & Low Cloud thresholds but only where surface temp < 0C.
        temp_2m = get_time_index(data['Temperature_height_above_ground'].sel(height_above_ground3=2)) - 273.15
        dewpoint_2m = get_time_index(data['Dewpoint_temperature_height_above_ground'].sel(height_above_ground4=2)) - 273.15
        wind_speed_kt = np.sqrt(u_wind.values**2 + v_wind.values**2) * 1.94384  # m/s to kt
        temp_dew_spread = temp_2m.values - dewpoint_2m.values

        fog = np.zeros_like(temp_dew_spread, dtype=int)
        fog[(wind_speed_kt < 5) & (temp_dew_spread < 1.0)] = 1
        fog[(wind_speed_kt < 3) & (temp_dew_spread < 0.5)] = 2
        fog[(wind_speed_kt < 1) & (temp_dew_spread < 0.1)] = 3
        freezing_fog = np.where(temp_2m.values < 0.0, fog, 0)

        print(
            f"Freezing fog grid points - F1: {np.sum(freezing_fog == 1)}, "
            f"F2: {np.sum(freezing_fog == 2)}, F3: {np.sum(freezing_fog == 3)}"
        )

        freezing_fog_masked = np.ma.masked_where(freezing_fog == 0, freezing_fog)
        fog_cmap = ListedColormap(['#ff0000', '#ffaa7f', '#ffff00'])
        fog_levels = [0.5, 1.5, 2.5, 3.5]
        fog_norm = BoundaryNorm(fog_levels, fog_cmap.N, clip=True)

        ax.contourf(
            data.longitude,
            data.latitude,
            freezing_fog_masked,
            levels=fog_levels,
            cmap=fog_cmap,
            norm=fog_norm,
            alpha=1.0,
            transform=ccrs.PlateCarree(),
            zorder=7.2,
        )
    
    # Plot 1hr precipitation / Thunderstorms flash density field
    if ts_flash_profile:
        base_precip = get_time_index(data['Precipitation_rate_surface']) * 3600  # Convert to mm/hr
        base_precip_masked = base_precip.where(base_precip >= 0.1, np.nan)
        base_precip_levels = [0, 0.1, 0.2, 0.5, 1, 2, 5, 7.5, 10, 15, 20, 25, 30, 35, 40, 45, 50]
        base_precip_colors = [
            (255, 255, 255, 255),
            (240, 255, 150, 255),
            (240, 255, 60, 255),
            (255, 255, 0, 255),
            (200, 255, 0, 255),
            (150, 255, 0, 255),
            (0, 150, 0, 255),
            (0, 175, 128, 255),
            (0, 200, 255, 255),
            (0, 150, 255, 255),
            (0, 0, 255, 255),
            (0, 0, 255, 255),
            (255, 100, 0, 255),
            (255, 50, 0, 255),
            (255, 0, 0, 255),
            (200, 0, 0, 255),
            (50, 0, 0, 255),
        ]
        base_precip_colors_norm = [(r / 255.0, g / 255.0, b / 255.0, a / 255.0) for r, g, b, a in base_precip_colors]
        base_precip_cmap = ListedColormap(base_precip_colors_norm)
        base_precip_norm = BoundaryNorm(base_precip_levels, base_precip_cmap.N, clip=True)

        ax.contourf(
            data.longitude,
            data.latitude,
            base_precip_masked,
            levels=base_precip_levels,
            cmap=base_precip_cmap,
            norm=base_precip_norm,
            alpha=0.3,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=7)

        total_totals = calculate_total_totals(data)
        max_wz = get_max_geometric_vertical_velocity(data, [60000, 50000, 40000, 30000, 25000])
        convective_precip = data['Convective_precipitation_1h_surface']
        min_li20 = calculate_min_lifted_index_minus20(data)
        ts_mask = (min_li20 < 0.0) & (max_wz > 0.1)

        print(f'Min LI-20 < 0 grid points: {np.sum(min_li20 < 0.0)}')
        print(f'Max geometric vertical velocity > 0.1 m/s grid points: {np.sum(max_wz > 0.1)}')
        print(f'Combined Thunderstorms mask grid points: {np.sum(ts_mask)}')

        precip_masked = np.where(ts_mask & (convective_precip.values >= 0.1), convective_precip.values, np.nan)
        precip_levels = [0, 0.1, 0.3, 0.5, 1.0, 2.5, 5.0, 7.5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 100, 300]
        precip_colors = [
            (255, 255, 255, 0),      # 0-0.1mm (transparent)
            (0, 255, 0, 255),        # 0.1-0.3mm (#00FF00)
            (128, 255, 0, 255),      # 0.3-0.5mm (#80FF00)
            (255, 251, 0, 255),      # 0.5-1.0mm (#FFFB00)
            (255, 240, 0, 255),      # 1.0-2.5mm (#FFF000)
            (255, 218, 0, 255),      # 2.5-5.0mm (#FFDA00)
            (255, 153, 0, 255),      # 5.0-7.5mm (#FF9900)
            (255, 115, 0, 255),      # 7.5-10mm (#FF7300)
            (255, 76, 0, 255),       # 10-15mm (#FF4C00)
            (255, 0, 0, 255),        # 15-20mm (#FF0000)
            (255, 32, 110, 255),     # 20-25mm (existing palette)
            (255, 32, 121, 255),     # 25-30mm (existing palette)
            (255, 34, 159, 255),     # 30-35mm (existing palette)
            (255, 30, 184, 255),     # 35-40mm (existing palette)
            (255, 32, 214, 255),     # 40-45mm (existing palette)
            (236, 32, 255, 255),     # 45-50mm (existing palette)
            (192, 32, 255, 255),     # 50-55mm (existing palette)
            (144, 33, 255, 255),     # 55-60mm (existing palette)
            (101, 29, 255, 255),     # 60-100mm (existing palette)
            (28, 20, 255, 255),      # 100-300mm (existing palette)
            (14, 255, 235, 255),     # 300+mm (existing palette)
        ]
        precip_alpha = 1.0
        precip_zorder = 8
    elif ts_severe_profile:
        precip_masked = None
        precip_levels = None
        precip_colors = None
        precip_alpha = None
        precip_zorder = None
    elif airmass_profile:
        precip_masked = None
        precip_levels = None
        precip_colors = None
        precip_alpha = None
        precip_zorder = None
    elif turb_profile:
        precip_masked = None
        precip_levels = None
        precip_colors = None
        precip_alpha = None
        precip_zorder = None
    else:
        precip = get_time_index(data['Precipitation_rate_surface']) * 3600  # Convert to mm/hr
        precip_masked = precip.where(precip >= 0.1, np.nan)
        precip_levels = [0, 0.1, 0.2, 0.5, 1, 2, 5, 7.5, 10, 15, 20, 25, 30, 35, 40, 45, 50]
        precip_colors = [
            (255, 255, 255, 255),
            (240, 255, 150, 255),
            (240, 255, 60, 255),
            (255, 255, 0, 255),
            (200, 255, 0, 255),
            (150, 255, 0, 255),
            (0, 150, 0, 255),
            (0, 175, 128, 255),
            (0, 200, 255, 255),
            (0, 150, 255, 255),
            (0, 0, 255, 255),
            (0, 0, 255, 255),
            (255, 100, 0, 255),
            (255, 50, 0, 255),
            (255, 0, 0, 255),
            (200, 0, 0, 255),
            (50, 0, 0, 255),
        ]
        precip_alpha = 0.6
        precip_zorder = z_bg_precip

    if precip_masked is not None:
        precip_colors_norm = [(r / 255.0, g / 255.0, b / 255.0, a / 255.0) for r, g, b, a in precip_colors]
        precip_cmap = ListedColormap(precip_colors_norm)
        precip_norm = BoundaryNorm(precip_levels, precip_cmap.N, clip=True)

        cs_precip = ax.contourf(
            data.longitude,
            data.latitude,
            precip_masked,
            levels=precip_levels,
            cmap=precip_cmap,
            norm=precip_norm,
            alpha=precip_alpha,
            extend='max',
            transform=ccrs.PlateCarree(),
            zorder=precip_zorder)

    if ts_flash_profile:
        # Add subtle dotted bin outlines to separate Thunderstorms increments from background layers.
        outline_levels = precip_levels[1:-1]
        ax.contour(
            data.longitude,
            data.latitude,
            precip_masked,
            levels=outline_levels,
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=precip_zorder + 0.1,
        )

        # Draw the outer patch boundary so dashed lines appear on the edge of colored Thunderstorms areas too.
        patch_mask = np.isfinite(precip_masked).astype(float)
        ax.contour(
            data.longitude,
            data.latitude,
            patch_mask,
            levels=[0.5],
            colors=[(0.08, 0.08, 0.08, 1.0)],
            linewidths=0.35,
            linestyles='dashed',
            transform=ccrs.PlateCarree(),
            zorder=precip_zorder + 0.15,
        )
    
    # Plot SFC winds unless explicitly disabled for the current profile.
    if not ts_severe_profile and not airmass_profile:
        barb_alpha = 0.2 if turb_profile else (0.5 if bg_or_turb_profile else 0.3)
        ax.barbs(data.longitude[::10], data.latitude[::10], u_wind.values[::10, ::10], v_wind.values[::10, ::10], 
                 length=5, linewidth=0.5, color='#800000', alpha=barb_alpha, zorder=(z_bg_barbs if bg_or_turb_profile else 6))
    else:
        if ts_severe_profile:
            # Plot surface-to-500 hPa shear vectors as barbs in kt.
            shear_u_kt = shear_u_ms * 1.94384
            shear_v_kt = shear_v_ms * 1.94384

            ax.barbs(
                data.longitude[::10],
                data.latitude[::10],
                shear_u_kt[::10, ::10],
                shear_v_kt[::10, ::10],
                length=5,
                linewidth=0.5,
                color='#2b0000',
                alpha=0.5,
                zorder=6.2,
            )
    
    # Plot TAF locations with filtering based on text bounding boxes
    if taf_lons:  # Only plot if TAF data was loaded successfully
        # Plot tiny dots to mark TAF locations
        ax.scatter(taf_lons, taf_lats, s=2, c='black', marker='o', zorder=z_taf_points, transform=ccrs.PlateCarree())
        
        # Track which points to keep
        points_to_keep = []
        
        # Create text objects for all TAF names
        text_objects = []
        for idx, (lon, lat, name) in enumerate(zip(taf_lons, taf_lats, taf_names)):
            text_obj = ax.text(lon + TAF_LABEL_DX, lat + TAF_LABEL_DY, name, fontsize=5, ha='right', va='bottom', 
                              zorder=z_taf_text, transform=ccrs.PlateCarree())
            text_objects.append((text_obj, lon, lat, name, idx))
        
        # Draw canvas to compute text extents
        fig.canvas.draw()
        
        # Check which text objects have bounding boxes completely within domain
        for text_obj, lon, lat, name, idx in text_objects:
            # Get text bounding box in display coordinates
            bbox_display = text_obj.get_window_extent(renderer=fig.canvas.get_renderer())
            
            # Transform bounding box corners to data coordinates
            # Get the transform from display to data coordinates
            trans_display_to_data = ax.transData.inverted()
            bbox_data = bbox_display.transformed(trans_display_to_data)
            
            # Check if all corners of bounding box are within domain bounds
            x_min, y_min = bbox_data.xmin, bbox_data.ymin
            x_max, y_max = bbox_data.xmax, bbox_data.ymax
            
            # If any corner is outside domain, remove the text
            if not (plot_lon_min <= x_min and x_max <= plot_lon_max and 
                    plot_lat_min <= y_min and y_max <= plot_lat_max):
                text_obj.remove()
                print(f"Removing {name}: bbox outside domain (lon: {x_min:.2f}-{x_max:.2f}, lat: {y_min:.2f}-{y_max:.2f})")
            else:
                points_to_keep.append(idx)
        
        print(f"Removed {len(text_objects) - len(points_to_keep)} TAF points with labels outside domain")

    # Add day/night terminator and night shading above all other layers
    valid_time = init_time + timedelta(hours=forecast_hour)
    cos_zenith, night_mask = get_day_night_grid(valid_time, data.longitude.values, data.latitude.values)
    print(f"Night grid points: {np.sum(night_mask)} out of {night_mask.size}")

    night_overlay = np.where(night_mask, 1.0, np.nan)
    ax.contourf(
        data.longitude,
        data.latitude,
        night_overlay,
        levels=[0.5, 1.5],
        colors=[(0.5, 0.5, 0.5, 0.3)],
        transform=ccrs.PlateCarree(),
        zorder=300,
    )
    
    title_prefix_map = {
        ('GFS', 'bg'): 'GFS Fog & Low Cloud',
        ('ICON', 'bg'): 'ICON Fog & Low Cloud',
        ('GFS', 'ts_flash'): 'GFS Thunderstorms',
        ('GFS', 'ts_severe'): 'GFS Storm Dynamics',
        ('GFS', 'airmass_fzl'): 'GFS Freezing Level',
        ('GFS', 'airmass_4c'): 'GFS 4C isotherm height',
        ('GFS', 'airmass_snow'): 'GFS Snow Level',
        ('GFS', 'turb_mtw'): 'GFS Mountain Waves',
        ('GFS', 'turb_wind'): 'GFS Low Level Turbulence',
    }

    title_prefix = title_prefix_map.get((model_name, layer_profile), f'{model_name} Forecast')

    # Add title
    title = (
        f'{title_prefix}\n'
        f'Run: {init_time.strftime("%Y-%m-%d %H%MZ")} | Valid: '
        f'{valid_time.strftime("%Y-%m-%d %H%MZ")}'
    )
    ax.set_title(title)
    
    return fig

def generate_gfs_bg_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'BG', 'US', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    # Generate the full new run in a temporary OS directory so the published folder stays stable
    with tempfile.TemporaryDirectory(prefix='avmaps_bg_us_') as staging_root:
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir

        print(f'Generating {len(forecast_hours)} Fog & Low Cloud forecast frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')

        for forecast_hour in forecast_hours:
            print(f'\nGenerating frame +{forecast_hour}hrs...')

            # Get raw hourly data from NOMADS GFS 0.25 files.
            data, init_time, _ = get_gfs_data(forecast_hour, latest_dataset, init_time)
            print('Using raw hourly GFS 0.25 model fields')

            # Plot map
            filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='GFS', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'Fog & Low Cloud map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} Fog & Low Cloud frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def generate_gfs_ts_flash_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'TS', 'Flash density', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    with tempfile.TemporaryDirectory(prefix='avmaps_ts_flash_') as staging_root:
        print(f'Generating {len(forecast_hours)} Thunderstorms flash-density frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir
        prev_acpcp_values = None
        last_generated_hour = None

        for forecast_hour in forecast_hours:
            print(f'\nGenerating Thunderstorms flash-density frame +{forecast_hour}hrs...')

            use_prev = prev_acpcp_values is not None and last_generated_hour == forecast_hour - 1
            data, init_time, prev_acpcp_values = get_gfs_data(
                forecast_hour,
                latest_dataset,
                init_time,
                include_ts_fields=True,
                prev_acpcp_values=(prev_acpcp_values if use_prev else None),
            )
            last_generated_hour = forecast_hour
            print('Using raw hourly GFS convective precipitation, Total Totals, and geometric vertical velocity fields')

            filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='ts_flash', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'Thunderstorms flash-density map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} Thunderstorms flash-density frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def generate_gfs_ts_severe_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'TS', 'Severe storm potential', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    with tempfile.TemporaryDirectory(prefix='avmaps_ts_severe_') as staging_root:
        print(f'Generating {len(forecast_hours)} Thunderstorms severe-potential frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir
        prev_acpcp_values = None
        last_generated_hour = None

        for forecast_hour in forecast_hours:
            print(f'\nGenerating Thunderstorms severe-potential frame +{forecast_hour}hrs...')

            use_prev = prev_acpcp_values is not None and last_generated_hour == forecast_hour - 1
            data, init_time, prev_acpcp_values = get_gfs_data(
                forecast_hour,
                latest_dataset,
                init_time,
                include_ts_fields=True,
                prev_acpcp_values=(prev_acpcp_values if use_prev else None),
            )
            last_generated_hour = forecast_hour
            print('Using raw hourly GFS convective precipitation, Total Totals, and geometric vertical velocity fields')

            filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='ts_severe', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'Thunderstorms severe-potential map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} Thunderstorms severe-potential frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def generate_gfs_airmass_4c_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'Airmass', '4C', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    with tempfile.TemporaryDirectory(prefix='avmaps_airmass_4c_') as staging_root:
        print(f'Generating {len(forecast_hours)} Airmass/4C frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir

        for forecast_hour in forecast_hours:
            print(f'\nGenerating Airmass/4C frame +{forecast_hour}hrs...')
            data, init_time, _ = get_gfs_data(
                forecast_hour,
                latest_dataset,
                init_time,
                include_ts_fields=False,
                include_airmass_fields=True,
            )
            print('Using GFS airmass fields for 4C isotherm height')

            filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='airmass_4c', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'Airmass/4C map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} Airmass/4C frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def generate_gfs_airmass_fzl_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'Airmass', 'FZL', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    with tempfile.TemporaryDirectory(prefix='avmaps_airmass_fzl_') as staging_root:
        print(f'Generating {len(forecast_hours)} Airmass/FZL frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir
        prev_acpcp_values = None
        last_generated_hour = None

        for forecast_hour in forecast_hours:
            print(f'\nGenerating Airmass/FZL frame +{forecast_hour}hrs...')

            use_prev = prev_acpcp_values is not None and last_generated_hour == forecast_hour - 1
            data, init_time, prev_acpcp_values = get_gfs_data(
                forecast_hour,
                latest_dataset,
                init_time,
                include_ts_fields=True,
                include_airmass_fields=True,
                prev_acpcp_values=(prev_acpcp_values if use_prev else None),
            )
            last_generated_hour = forecast_hour
            print('Using raw hourly GFS convective precipitation and airmass fields')

            filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='airmass_fzl', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'Airmass/FZL map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} Airmass/FZL frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def generate_gfs_airmass_snow_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'Airmass', 'Snow level', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)
    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    with tempfile.TemporaryDirectory(prefix='avmaps_airmass_snow_') as staging_root:
        print(f'Generating {len(forecast_hours)} Airmass/Snow level frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir

        for forecast_hour in forecast_hours:
            print(f'\nGenerating Airmass/Snow level frame +{forecast_hour}hrs...')
            data, init_time, _ = get_gfs_data(
                forecast_hour,
                latest_dataset,
                init_time,
                include_ts_fields=False,
                include_airmass_fields=True,
            )
            print('Using raw hourly GFS precipitation and airmass fields')

            filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='airmass_snow', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'Airmass/Snow level map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} Airmass/Snow level frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def _generate_gfs_turb_frames_internal(
    forecast_hours,
    include_mtw=True,
    include_wind=True,
    mtw_output_dir=None,
    wind_output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    if not include_mtw and not include_wind:
        print('No turbulence outputs requested; nothing to do.')
        return

    domain_ids = normalize_domain_ids(domain_ids)

    mtw_output_dirs = {}
    if include_mtw:
        mtw_output_dirs = resolve_panel_output_dirs(domain_ids, 'Turb', 'MTW', output_dir=mtw_output_dir, output_root=output_root)
        for domain_output_dir in mtw_output_dirs.values():
            os.makedirs(domain_output_dir, exist_ok=True)

    wind_output_dirs = {}
    if include_wind:
        wind_output_dirs = resolve_panel_output_dirs(domain_ids, 'Turb', 'Wind', output_dir=wind_output_dir, output_root=output_root)
        for domain_output_dir in wind_output_dirs.values():
            os.makedirs(domain_output_dir, exist_ok=True)

    if latest_dataset is None or init_time is None:
        latest_dataset, init_time = get_latest_gfs_dataset()

    with tempfile.TemporaryDirectory(prefix='avmaps_turb_mtw_') as mtw_staging_root, tempfile.TemporaryDirectory(prefix='avmaps_turb_wind_') as wind_staging_root:
        targets = []
        if include_mtw:
            targets.append('MTW')
        if include_wind:
            targets.append('Wind')

        print(
            f"Generating {len(forecast_hours)} Turbulence {'/'.join(targets)} frames for domains {', '.join(domain_ids)} in temporary workspaces: "
            f'{mtw_staging_root} and {wind_staging_root}'
        )

        mtw_temp_dirs = {}
        mtw_generated_files = {domain_id: [] for domain_id in domain_ids}
        if include_mtw:
            for domain_id in domain_ids:
                temp_dir = os.path.join(mtw_staging_root, domain_id)
                os.makedirs(temp_dir, exist_ok=True)
                mtw_temp_dirs[domain_id] = temp_dir

        wind_temp_dirs = {}
        wind_generated_files = {domain_id: [] for domain_id in domain_ids}
        if include_wind:
            for domain_id in domain_ids:
                temp_dir = os.path.join(wind_staging_root, domain_id)
                os.makedirs(temp_dir, exist_ok=True)
                wind_temp_dirs[domain_id] = temp_dir

        for forecast_hour in forecast_hours:
            print(f'\nGenerating Turbulence frame +{forecast_hour}hrs...')

            data, init_time, _ = get_gfs_data(
                forecast_hour,
                latest_dataset,
                init_time,
                include_ts_fields=include_mtw,
            )
            print('Using raw hourly GFS 0.25 model fields for Turbulence maps')

            if include_mtw:
                mtw_filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
                for domain_id in domain_ids:
                    mtw_fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='turb_mtw', domain_id=domain_id)
                    mtw_temp_filepath = os.path.join(mtw_temp_dirs[domain_id], mtw_filename)
                    save_output_figure(mtw_fig, mtw_temp_filepath)
                    plt.close(mtw_fig)
                    mtw_generated_files[domain_id].append(mtw_filename)
                    print(f'Turbulence/MTW map staged for {domain_id} at {mtw_temp_filepath}')

            if include_wind:
                wind_filename = f'GFS_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
                for domain_id in domain_ids:
                    wind_fig = plot_map(data, init_time, forecast_hour, model_name='GFS', layer_profile='turb_wind', domain_id=domain_id)
                    wind_temp_filepath = os.path.join(wind_temp_dirs[domain_id], wind_filename)
                    save_output_figure(wind_fig, wind_temp_filepath)
                    plt.close(wind_fig)
                    wind_generated_files[domain_id].append(wind_filename)
                    print(f'Turbulence/Wind map staged for {domain_id} at {wind_temp_filepath}')

        if include_mtw:
            for domain_id in domain_ids:
                print(f'\nFinished building {len(mtw_generated_files[domain_id])} Turbulence/MTW frames for {domain_id}. Publishing to {mtw_output_dirs[domain_id]}...')
                publish_generated_frames(mtw_output_dirs[domain_id], mtw_generated_files[domain_id], mtw_temp_dirs[domain_id])

        if include_wind:
            for domain_id in domain_ids:
                print(f'Finished building {len(wind_generated_files[domain_id])} Turbulence/Wind frames for {domain_id}. Publishing to {wind_output_dirs[domain_id]}...')
                publish_generated_frames(wind_output_dirs[domain_id], wind_generated_files[domain_id], wind_temp_dirs[domain_id])

        if update_manifest:
            write_frame_manifest()


def generate_gfs_turb_mtw_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    _generate_gfs_turb_frames_internal(
        forecast_hours,
        include_mtw=True,
        include_wind=False,
        mtw_output_dir=output_dir,
        latest_dataset=latest_dataset,
        init_time=init_time,
        update_manifest=update_manifest,
        domain_ids=domain_ids,
        output_root=output_root,
    )


def generate_gfs_turb_wind_frames(
    forecast_hours,
    output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    _generate_gfs_turb_frames_internal(
        forecast_hours,
        include_mtw=False,
        include_wind=True,
        wind_output_dir=output_dir,
        latest_dataset=latest_dataset,
        init_time=init_time,
        update_manifest=update_manifest,
        domain_ids=domain_ids,
        output_root=output_root,
    )


def generate_gfs_turb_frames(
    forecast_hours,
    mtw_output_dir=None,
    wind_output_dir=None,
    latest_dataset=None,
    init_time=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    _generate_gfs_turb_frames_internal(
        forecast_hours,
        include_mtw=True,
        include_wind=True,
        mtw_output_dir=mtw_output_dir,
        wind_output_dir=wind_output_dir,
        latest_dataset=latest_dataset,
        init_time=init_time,
        update_manifest=update_manifest,
        domain_ids=domain_ids,
        output_root=output_root,
    )


def generate_icon_bg_frames(
    forecast_hours,
    preferred_run_time=None,
    output_dir=None,
    icon_run=None,
    remap_state=None,
    update_manifest=True,
    domain_ids=None,
    output_root=None,
):
    domain_ids = normalize_domain_ids(domain_ids)
    output_dirs = resolve_panel_output_dirs(domain_ids, 'BG', 'ICON', output_dir=output_dir, output_root=output_root)
    for domain_output_dir in output_dirs.values():
        os.makedirs(domain_output_dir, exist_ok=True)

    if icon_run is None:
        icon_run = get_latest_icon_run(target_time=preferred_run_time)
    init_time = icon_run['init_time']
    evict_stale_icon_cache(icon_run['run_stamp'])
    if remap_state is None:
        remap_state = _load_icon_static_remap(icon_run)

    with tempfile.TemporaryDirectory(prefix='avmaps_bg_icon_') as staging_root:
        print(f'Generating {len(forecast_hours)} ICON forecast frames for domains {", ".join(domain_ids)} in temporary workspace: {staging_root}')
        temp_dirs = {}
        generated_files_by_domain = {domain_id: [] for domain_id in domain_ids}
        for domain_id in domain_ids:
            temp_dir = os.path.join(staging_root, domain_id)
            os.makedirs(temp_dir, exist_ok=True)
            temp_dirs[domain_id] = temp_dir

        prev_tp_values = None
        last_generated_hour = None
        for forecast_hour in forecast_hours:
            print(f'\nGenerating ICON frame +{forecast_hour}hrs...')

            # Pass the in-memory TOT_PREC from the previous iteration to avoid
            # a redundant network fetch (or cache read) for the prior hour.
            use_prev = (prev_tp_values is not None
                        and last_generated_hour == forecast_hour - 1)
            data, init_time, prev_tp_values = get_icon_data(
                forecast_hour, icon_run, remap_state,
                prev_tp_values=(prev_tp_values if use_prev else None),
            )
            last_generated_hour = forecast_hour
            print('Using raw hourly ICON model fields')

            filename = f'ICON_{init_time.strftime("%Y%m%d_%H")}_{forecast_hour:02d}.{OUTPUT_IMAGE_EXT}'
            for domain_id in domain_ids:
                fig = plot_map(data, init_time, forecast_hour, model_name='ICON', domain_id=domain_id)
                temp_filepath = os.path.join(temp_dirs[domain_id], filename)
                save_output_figure(fig, temp_filepath)
                plt.close(fig)
                generated_files_by_domain[domain_id].append(filename)
                print(f'ICON map staged for {domain_id} at {temp_filepath}')

        for domain_id in domain_ids:
            print(f'\nFinished building {len(generated_files_by_domain[domain_id])} ICON frames for {domain_id}. Publishing to {output_dirs[domain_id]}...')
            publish_generated_frames(output_dirs[domain_id], generated_files_by_domain[domain_id], temp_dirs[domain_id])
        if update_manifest:
            write_frame_manifest()


def generate_all_layers_atomically(forecast_hours, model='gfs', domain_ids=None):
    """Atomically publish the AvMaps Extra FZL + 4C isotherm site."""
    if model not in ('gfs', 'both'):
        raise ValueError('AvMaps Extra only supports GFS generation')

    script_dir = os.path.dirname(os.path.abspath(__file__))
    staged_targets = []
    domain_ids = normalize_domain_ids(domain_ids)

    gfs_latest_dataset, gfs_init_time = get_latest_gfs_dataset()
    print(f"Locked GFS run for atomic publish: {gfs_init_time.strftime('%Y-%m-%d %H%MZ')}")

    with tempfile.TemporaryDirectory(prefix='.avmaps_atomic_publish_', dir=script_dir) as staging_root:
        print(f'Building staged AvMaps Extra run in {staging_root}')
        stage_output_root = os.path.join(staging_root, 'images', 'domains')

        generate_gfs_airmass_fzl_frames(
            forecast_hours,
            latest_dataset=gfs_latest_dataset,
            init_time=gfs_init_time,
            update_manifest=False,
            domain_ids=domain_ids,
            output_root=stage_output_root,
        )
        staged_targets.extend([os.path.join('images', 'domains', domain_id, 'Airmass', 'FZL') for domain_id in domain_ids])

        generate_gfs_airmass_4c_frames(
            forecast_hours,
            latest_dataset=gfs_latest_dataset,
            init_time=gfs_init_time,
            update_manifest=False,
            domain_ids=domain_ids,
            output_root=stage_output_root,
        )
        staged_targets.extend([os.path.join('images', 'domains', domain_id, 'Airmass', '4C') for domain_id in domain_ids])

        publish_staged_directories_atomically(staging_root, staged_targets)
        write_frame_manifest(script_dir)
        print(f'Atomic publish complete. Updated {len(staged_targets)} directories under {script_dir}/images')


def main():
    parser = argparse.ArgumentParser(description='Generate AvMaps Extra FZL and 4C isotherm height frames from GFS data.')
    parser.add_argument('--model', choices=['gfs', 'both'], default='gfs')
    parser.add_argument('--layer', choices=['airmass_fzl', 'airmass_4c', 'all'], default='all')
    parser.add_argument('--start-hour', type=int, default=9)
    parser.add_argument('--end-hour', type=int, default=48)
    parser.add_argument('--domains', default='all', help='Comma-separated domain ids, or all. Current ids: AU,WA_SA,VIC_TAS,NSW')
    args = parser.parse_args()

    forecast_hours = list(range(args.start_hour, args.end_hour + 1))
    domain_ids = normalize_domain_ids(args.domains)

    if args.layer == 'all':
        generate_all_layers_atomically(forecast_hours, model=args.model, domain_ids=domain_ids)
        return

    if args.model not in ('gfs', 'both'):
        raise ValueError('AvMaps Extra layers are currently supported for GFS only')

    if args.layer == 'airmass_fzl':
        generate_gfs_airmass_fzl_frames(forecast_hours, domain_ids=domain_ids)
        return

    generate_gfs_airmass_4c_frames(forecast_hours, domain_ids=domain_ids)


if __name__ == '__main__':
    main()
