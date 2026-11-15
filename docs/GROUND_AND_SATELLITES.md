# Ground and satellite context: land cover, terrain, Sentinel-2/3/5P

FireLens's detections come from MODIS and VIIRS. This adds three kinds of context:
- what the fire burns on;
- the terrain around it;
- what the Copernicus Sentinel satellites saw (burn scar, vegetation, ground heat, smoke).

The production database is never modified. Everything is computed on demand and cached in
separate files under `data/`.

## Data sources

| What | Source | Resolution | Access | Cost |
|---|---|---|---|---|
| Land cover | ESA WorldCover 2021 v200 | 10 m, read at ~150 m | public cloud GeoTIFFs (s3://esa-worldcover) | none; ~1 MB per 3° tile, cached in `data/geo/worldcover` |
| Elevation, slope | Copernicus DEM GLO-90 | 90 m | public cloud GeoTIFFs (s3://copernicus-dem-90m) | none; ~1.5 MB per 1° tile, cached in `data/geo/dem90` |
| Vegetation, moisture, burn scar | Sentinel-2 L2A | 10–20 m, ~5 days | Copernicus Data Space (Sentinel Hub Statistical API) | processing units |
| Ground temperature | Sentinel-3 SLSTR L2 LST, daytime pass | 1 km, ~daily | same | processing units |
| Smoke | Sentinel-5P TROPOMI (CO, UV aerosol index) | ~5.5 × 3.5 km, daily | same | processing units |

The Copernicus measurements use the existing OAuth client (`FIRELENS_COPERNICUS_CLIENT_ID` and
`FIRELENS_COPERNICUS_CLIENT_SECRET`).

Not used, and why:
- **Copernicus DEM through Sentinel Hub:** this account gets 403. The same DEM is read from its
  public bucket instead.
- **Sentinel-3 FRP product:** downloads need a personal Copernicus login. The user chose to skip it.
- **ERA5 and the official Fire Weather Index:** they need an ECMWF account. Not now.

## What is burning (Analysis panel)

`GET /api/analysis/landcover`, with the same area and period as the summary.

- Every detection gets the WorldCover class at its centre.
- The 1/16 overview is about 150 m. On the Sierras de Córdoba tile its class shares are within
  0.6 percentage points of the full 10 m map.
- A VIIRS pixel is 375 m and a MODIS pixel 1 km, so over mixed land the class is that of the
  best location estimate. The shares across many detections are what matter.
- WorldCover is a 2021 map: land cleared since then (which often burns) shows its 2021 class.
- On first use the land-cover tiles download:
  - the API waits at most 8 s, then reports `coverage` < 1 and keeps downloading in the background;
  - such partial results are not cached;
  - `python -m ml.prefetch_geodata` downloads every tile under the database's detections ahead of
    time (about 1,200 tiles, ~1 GB, ~40 min).

## Per-fire report (fire details)

**`GET /api/events/{id}/ground`** — no Copernicus quota:
- land cover under the fire's detections, and around it (the event box plus a margin of 1 km or
  25% of its size, whichever is larger);
- the share of burnable vegetation around it;
- terrain: elevation range, mean and 90th-percentile slope, the share steeper than 20°, and which
  way the slopes face (slope-weighted circular mean of the downhill direction; omitted when the
  slopes face every way).

**`GET /api/events/{id}/satellites`** — Copernicus quota, cached per event version.

| Measurement | How | Reading it |
|---|---|---|
| Burn scar | dNBR = NBR(latest clear image before the fire) − NBR(latest clear image after it). Burned = dNBR ≥ 0.27 | USGS severity classes. Misses light burns under canopy and grass that greens up fast. For fires still burning it is "so far" |
| Vegetation before | NDVI and NDMI of the latest clear Sentinel-2 image in the 30 days before | NDVI ≈ greenness; NDMI < ~0 = dry vegetation |
| Ground temperature | Sentinel-3 daytime LST, warmest tenth of the area per day, during the fire vs the 30 days before | Skin temperature of the ground, not the air. Clouds read as cold, hence the warmest tenth |
| Smoke | Sentinel-5P CO column and UV aerosol index over ~100 km around the fire, during vs the 30 days before | Wind carries plumes away and nearby fires add to them, so it describes the region's smoke, not only this fire's |

**Caching** (in `data/enrichment.db`):
- reports are cached per event version;
- a fire that is still burning is refreshed once a day, as new images arrive;
- a finished fire without a clear post-fire image is retried for up to 45 days.

The first report for a fire takes about 20 s, with the four measurements run in parallel.

**Checked on a real fire** (evt_fbaf9b88f07b, Paraguayan Chaco, 16–17 Aug 2026):
- forest on flat ground;
- 5.0 km² burned at moderate severity or worse, against a detected footprint of 15.9 km²
  (whole satellite pixels);
- ground +5.5 °C and carbon monoxide +25% on the fire day.

## Fire spread outlook

See [FIRE_SPREAD.md](FIRE_SPREAD.md).
