# AvMaps Extra

Experimental two-panel GitHub Pages site that shows:

- **Left:** GFS freezing level (FZL)
- **Right:** dry-bulb 4°C isotherm height (ft AMSL)

The main multi-map product remains [AvMaps](https://github.com/StrictBug/AvMaps) at `https://strictbug.github.io/AvMaps/`. This repo is intentionally separate and does not modify AvMaps.

## Live site

`https://strictbug.github.io/AvMaps-Extra/`

## What it shows

- One category pair: FZL + 4°C isotherm height
- Domains: `AU`, `WA_SA`, `VIC_TAS`, `NSW`
- Forecast hours 9–48 (hourly CI refresh)
- Toggle the balloon icon, then click either map for a right-hand Skew-T / log-P (Open-Meteo GFS)

## Local generation

```bash
python generate_maps.py --layer all --start-hour 9 --end-hour 12 --domains AU
```

Selective layers:

```bash
python generate_maps.py --layer airmass_fzl --start-hour 9 --end-hour 12 --domains AU
python generate_maps.py --layer airmass_4c --start-hour 9 --end-hour 12 --domains AU
```

## Deploy

GitHub Actions builds frames from GFS and deploys to GitHub Pages on an hourly schedule, on push to `main`, or via manual `workflow_dispatch` with optional `force`.
