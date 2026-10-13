#!/usr/bin/env python3
"""Automatically refresh AvMaps Extra when a fully available GFS run is ready."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from generate_maps import (  # noqa: E402
    data_lat_max,
    data_lat_min,
    data_lon_max,
    data_lon_min,
    generate_all_layers_atomically,
    get_latest_gfs_dataset,
)

GFS_FILTER_URL = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl'
MIN_GFS_READY_BYTES = 1000
PUBLISHED_MANIFEST_URL = 'https://strictbug.github.io/AvMaps-Extra/images/manifest.json'


def extract_run_stamp_from_path(path: str | None) -> str | None:
    if not path:
        return None

    match = re.search(r'_(\d{8}_\d{2})_\d{2,3}\.[^.]+$', Path(path).name)
    return match.group(1) if match else None


def load_manifest_payload() -> dict | None:
    manifest_path = REPO_ROOT / 'images' / 'manifest.json'
    if manifest_path.exists():
        with manifest_path.open('r', encoding='utf-8') as manifest_file:
            return json.load(manifest_file)

    try:
        response = requests.get(PUBLISHED_MANIFEST_URL, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def read_published_run_stamp() -> str | None:
    manifest = load_manifest_payload()
    if not manifest:
        return None

    frames = manifest.get('domains', {}).get('AU', {}).get('categories', {}).get('Airmass') or []
    if not frames:
        return None

    first_frame = frames[0]
    return (
        extract_run_stamp_from_path(first_frame.get('leftPath'))
        or extract_run_stamp_from_path(first_frame.get('rightPath'))
    )


def gfs_hour_is_ready(init_time, forecast_hour: int) -> bool:
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
        'lev_mean_sea_level': 'on',
    }

    response = requests.get(GFS_FILTER_URL, params=params, timeout=120)
    response.raise_for_status()
    return len(response.content) >= MIN_GFS_READY_BYTES


def write_github_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return

    with open(path, 'a', encoding='utf-8') as output_file:
        for key, value in values.items():
            output_file.write(f'{key}={value}\n')


def latest_gfs_run_is_ready(start_hour: int, end_hour: int) -> tuple[bool, dict[str, str | None]]:
    published = read_published_run_stamp()
    status: dict[str, str | None] = {
        'published_gfs': published,
        'latest_gfs': None,
    }

    _, gfs_init_time = get_latest_gfs_dataset()
    latest_gfs_stamp = gfs_init_time.strftime('%Y%m%d_%H')
    status['latest_gfs'] = latest_gfs_stamp

    gfs_hours_to_check = {end_hour}
    if start_hour > 0:
        gfs_hours_to_check.add(start_hour - 1)

    for hour in sorted(gfs_hours_to_check):
        ready = gfs_hour_is_ready(gfs_init_time, hour)
        print(f'GFS readiness for {latest_gfs_stamp} hour {hour:02d}: {ready}')
        if not ready:
            return False, status

    return True, status


def main() -> int:
    parser = argparse.ArgumentParser(description='Automatically update AvMaps Extra when a fully available GFS run is ready.')
    parser.add_argument('--model', choices=['gfs'], default='gfs')
    parser.add_argument('--start-hour', type=int, default=9)
    parser.add_argument('--end-hour', type=int, default=48)
    parser.add_argument('--check-only', action='store_true', help='Only report readiness; do not generate maps.')
    parser.add_argument('--force', action='store_true', help='Generate even if the latest ready run already appears to be published.')
    parser.add_argument('--github-output', help='Optional GitHub Actions output file path.')
    args = parser.parse_args()

    ready, status = latest_gfs_run_is_ready(args.start_hour, args.end_hour)

    github_output = {
        'published_gfs': status['published_gfs'] or '',
        'latest_gfs': status['latest_gfs'] or '',
    }

    print(f"Published GFS run: {status['published_gfs']}")
    print(f"Latest ready GFS run: {status['latest_gfs']}")

    if not ready:
        print('The latest model run is not fully available yet. No update will be published.')
        write_github_output(args.github_output, {
            **github_output,
            'should_deploy': 'false',
            'reason': 'model_run_not_ready',
        })
        return 0

    already_current = status['published_gfs'] == status['latest_gfs']
    if already_current and not args.force:
        print('The site is already showing the latest fully available run. No update is needed.')
        write_github_output(args.github_output, {
            **github_output,
            'should_deploy': 'false',
            'reason': 'already_current',
        })
        return 0

    if args.check_only:
        print('A newer fully available run is ready for publishing.')
        write_github_output(args.github_output, {
            **github_output,
            'should_deploy': 'false',
            'reason': 'ready_but_check_only',
        })
        return 0

    print('Starting atomic AvMaps Extra publish...')
    generate_all_layers_atomically(
        list(range(args.start_hour, args.end_hour + 1)),
        model='gfs',
    )
    print('Automatic update finished successfully.')
    write_github_output(args.github_output, {
        **github_output,
        'should_deploy': 'true',
        'reason': 'site_published',
    })
    return 0


if __name__ == '__main__':
    # Use os._exit so CI is not marked failed by native eccodes/eckit teardown
    # crashes after the site has already been published successfully.
    os._exit(main())
