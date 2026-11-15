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

## Limits

* No baseline from previous years yet, so "unusual" is relative to the selected period only.
* The worldwide database only goes back as far as it has been loaded; before 14 Nov 2026 it held
  South America only.
* About 1 s per request on the current database; results are not cached.
