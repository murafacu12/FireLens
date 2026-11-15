# MODIS vs VIIRS: how the two sensors see the same fires

`GET /api/analysis/sensors?bbox=w,s,e,n&hours=168` (or `start`/`end`, plus `window_minutes`, 15–240,
default 120), and **Analysis → MODIS vs VIIRS** in the app. Read-only, no new tables.
Code: `backend/app/services/sensor_compare.py`, `frontend/src/analysis.ts`.

## The problem it solves carefully

FireLens stores fire detections, not satellite swath footprints. So "VIIRS saw a fire MODIS missed"
can simply mean MODIS was not imaging that place at that moment. Comparing raw detection counts
mixes sensor differences with orbit timing, and is misleading.

## Method

1. **Coincident scenes.** A detection is compared only if the other sensor demonstrably imaged the
   same place within the time window: the other sensor has at least one detection within 50 km of
   the centre of the ~11 km (0.1°) cell holding the detection. Detections outside scenes are left out.
2. **Matching.** Inside a scene, a detection is *matched* when the other sensor has a detection whose
   pixel footprint is within link distance: `r_a + r_b + 1 km`, where `r` is half the pixel diagonal.
   This is the same pixel-touching rule the fire-event engine uses.
3. **Fire power of the same fire.** Matched pixels from one MODIS overpass and one VIIRS overpass are
   joined into connected *coincident fire clusters*. On each side, FRP is summed over that cluster's
   pixels from that single pass: an instantaneous whole-cluster fire power. It is never summed over
   time. FIRMS stamps each 5–6 min granule separately, so granules less than 20 min apart count as
   one overpass.
4. **Baseline.** The same procedure runs on two *identical* sensors, VIIRS NOAA-20 vs NOAA-21, which
   are ~50 min apart. Their disagreement can only come from time, cloud, viewing angle and
   geolocation. The gap between MODIS–VIIRS agreement and this baseline is what the sensors
   themselves account for.
5. **Fire events.** For events started in the period: seen by both sensors, VIIRS only, MODIS only,
   and which sensor detected first. This is not limited to coincident scenes, so it reflects
   overpass timing as much as sensitivity.

Subjects are the detections inside the period. Detections up to one window outside it serve only as
partners, so fires near the edges of the period are not counted as missed.

## Orbits in 2026 (measured on FireLens data)

| Satellite | Sensor | Local solar time of daytime pass |
|---|---|---|
| NOAA-20, NOAA-21 | VIIRS | ~13:30 (NOAA-21 ~50 min after NOAA-20) |
| Suomi NPP | VIIRS | (FIRMS feed currently empty) |
| Aqua | MODIS | **~15:00–16:00**: drifted from 13:30 since orbit maintenance ended |
| Terra | MODIS | ~08:30 / 20:30 (drifted earlier) |

Aqua is typically **~100 min** after a VIIRS pass over the same place: only 1 % of MODIS detections
have a VIIRS view within 60 min, 73 % within 120 min and 99 % within 180 min. Hence the 120 min
default. **Terra never coincides with VIIRS**, so it is left out of the scene comparison (the count
is reported) and only counts at the event level.

## First results (7 days to 15 Nov 2026, NRT data)

| | Brazilian Cerrado (12°×12°) | South America |
|---|---|---|
| MODIS (Aqua) detections in scenes, matched by VIIRS | 40 % | 46 % |
| VIIRS detections in scenes, matched by MODIS | 48 % | 40 % |
| **Baseline**, VIIRS NOAA-20 ↔ NOAA-21 | ~54–58 % | **~60 %** |
| Coincident fire clusters | ~40 | 1,683 |
| VIIRS ÷ MODIS fire power, median (middle half) | 0.78 (0.43–1.4) | 0.88 (0.43–1.72) |
| Median time between the two views | 95 min | 100 min |
| Unmatched VIIRS pixels vs matched (median FRP) | 7.5 vs 8.4 MW | 6.6 vs 9.3 MW |

How to read it:

* **Most disagreement is not about the sensors.** Even identical sensors 50 min apart agree on only
  ~60 % of detections. Fires flare up and die down, cloud and smoke move, and the viewing angle
  changes. MODIS–VIIRS agreement (40–46 %) is lower than that, at twice the time gap.
* **VIIRS sees smaller fires.** The VIIRS pixels MODIS doesn't match are weaker in the median, as
  expected from 375 m against 1 km pixels.
* **Fire power is of the same order**, with wide scatter. The fire changed in the ~100 min between
  the views, and the sensors differ in resolution and saturation. FireLens shows them side by side
  and never mixes them.
* **Event level:** most fire events are seen by VIIRS only. That reflects both sensitivity (small
  fires) and the extra overpasses of two VIIRS satellites.

## Limits

* No swath footprints: the scene criterion (another detection within 50 km) can only confirm
  imaging where *some* fire was detected. Fire-free scenes cannot be compared, and partial cloud
  inside a scene goes unnoticed.
* The time gap mixes fire evolution with sensor differences. The baseline separates them only
  approximately, since its gap is ~50 min instead of ~100 min.
* Near-real-time data. Results may shift slightly when the archive (SP) versions arrive.
* Cost: about 1 s for a region and 10–15 s for a continent. Results are cached until new data
  arrives. Areas with more than 400,000 detections in the period are refused: zoom in or shorten
  the period.
