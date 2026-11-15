# Analysis engine: fire statistics and trends

`GET /api/analysis/summary?bbox=w,s,e,n&hours=168` (or `start`/`end`), and the **Analysis** tab in the
app, which analyses the area the globe shows for the period chosen under Filters → Time.

It is read-only and adds no tables: it reads the map pyramid (`fire_cells`), `detections` and
`fire_events`. Code: `backend/app/services/analysis.py`, `frontend/src/analysis.ts`.

## What it reports

| Section | Content |
|---|---|
| Totals | fire detections and fire events started, each with the change against the previous period of the same length |
| Detections per day | VIIRS and MODIS stacked, with unusually high days marked |
| Trend | second half of the period against the first (by hours): rising / falling (±25 %) / stable |
| Time of day | detections per hour, in local solar time for views up to 45° wide, otherwise UTC |
| By sensor | detections, strongest pixel and mean pixel FRP, for VIIRS and MODIS separately |
| Confidence, evidence | shares of high / nominal / low detections, and of multi-pass / single-pass / single-detection events |
| Top fires | the five largest detected footprints and the five strongest fires (peak whole-fire FRP in one pass) |

## How it counts

* Areas up to 3° across are counted exactly from `detections`.
* Wider areas use the pyramid: cells whose centre is in the area, at the finest level with at most
  256 cells across. The total is approximate at the edges of the area. `method` says which was used.
* Non-fire heat sources (gas flares, volcanoes, platforms) are excluded.
* Times snap to whole UTC hours.

## Rules that keep it honest

* **Coverage first.** Each period reports how many of its days are fully loaded for the area. If
  the period is incomplete, the totals carry a warning, and no trend or unusual day is stated:
  days that are not loaded look like quiet days. The comparison with the previous period is only
  given when both periods are fully loaded.
* **An unusual day** is one above the median by 3 robust standard deviations (MAD-based, with a
  square-root floor so very regular counts don't make everything unusual). It needs 7 or more days.
* **Detections are not fires.** One fire seen by several satellites or passes gives several
  detections. Counts measure observed fire activity, not the number of fires or burned area.
* **FRP is never summed** over time or across sensors.
* **Time of day** shows when satellites passed and saw fire. Each satellite crosses a place at
  fixed local times, so the chart is not a fire diurnal cycle.

## Seasonal baseline: compared with past seasons

`GET /api/analysis/baseline` (same `bbox`, `hours` or `start`/`end` as the summary) answers "is this
normal for the time of year?". The code is in `app/services/baseline.py`.

**What is compared**
* The period's whole UTC days, against the same calendar days of each earlier year (up to 10).
  29 February maps to the 28th. Days that ended less than 6 hours ago are left out, because late
  NRT data is still arriving. A period with no whole day ("last 24 h") gets no comparison.
* Each satellite group is compared only with itself:
  * VIIRS NOAA-20 (flying since 2018) is the headline when it has enough seasons;
  * MODIS (Terra + Aqua) is shown underneath.
* NOAA-21 is never counted, because it only flies since 2023. Adding it would make recent years
  look fierier just because more satellites watch.
* MODIS caveat: both satellites have been drifting to later overpass times since about 2020–2022.
* Every confidence level is counted, and non-vegetation sources are excluded on both sides.

**The same area every year**
* The archive is stored per 0.5° cell and day. The view is snapped outward to whole cells, then
  cut to the part every compared year's archive covers.
* The current period is counted over exactly that area, which is reported as `area` and
  `area_fraction`. Below half of the view: no comparison.
* A year the archive does not cover is left out, never read as a quiet year.

**Where the current period's numbers come from**
* For each day: from the archive where it covers that day and the whole area (the same product as
  the past seasons); otherwise from the live database where that day is loaded.
* Days that neither has are reported in `current_days.missing`. If there are any, the verdict is
  `current_incomplete`.
* The live database learns gas-flare sites from far fewer labelled rows than the archive. Live
  detections on a site the archive labelled non-vegetation are therefore also left out.
* Checked on Argentina, 1–13 Nov 2026, NOAA-20 and MODIS: archive and live counts are identical
  for every day. Without the site filter, live MODIS was 10% higher.

**Verdict**
* With only a handful of seasons, the year-to-year range is the yardstick, not a ratio:
  * `above` / `below`: outside the range of every past season by more than counting noise
    (2·√n of that bound);
  * `well_above` / `well_below`: in addition, at least 1.5× (at most 0.67×) the median season;
  * otherwise `within_range`.
* No verdict with fewer than 2 past seasons, or when both the median season and the current
  period have fewer than 10 detections.
* `rank` 1 means more than every past season.

**Storage and data**
* `data/baseline.db` is a separate SQLite file, opened read-only by the API. The production
  database is never touched. Its location is set by `FIRELENS_BASELINE_DB_PATH`.
* It holds:
  * `baseline_cells`: detections per group × day × 0.5° cell;
  * `baseline_sources`: which days and area each import fully covers;
  * `baseline_static_sites`: archive-labelled non-vegetation sites;
  * `baseline_chunks`: finished download requests.
* Built by:
  * `python -m ml.build_baseline data/training/arg.db`: Argentina, 15 Nov 2023 – 13 Nov 2026,
    from the training archive;
  * `python -m ml.download_baseline`: worldwide, 2021–2025 (5 past seasons), VIIRS NOAA-20 +
    MODIS, from the FIRMS archive API. It is resumable and keeps no raw rows. FIRMS rate-limits
    worldwide requests to about 25 per 10 minutes, so the full run takes about 5 hours and
    roughly 500 MB. Starting in 2019 would add 2 seasons and about 40% to the size.
* An import replaces the cell-days it covers, so re-running or overlapping imports never double
  count.

## Limits

* "Unusual days" are relative to the selected period. Comparison with earlier years is the
  seasonal baseline above.
* The worldwide database only goes back as far as it has been loaded; before 14 Nov 2026 it held
  South America only.
* Summary: 15–380 ms on the current database, from a small area to the whole world over a year.
  Results are cached until new data arrives.

The second analysis, MODIS vs VIIRS, is described in [SENSOR_COMPARISON.md](SENSOR_COMPARISON.md).
