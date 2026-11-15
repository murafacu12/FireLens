# Fire spread outlook: where a burning fire could go in the next 24 h

`GET /api/events/{id}/spread` (code in `app/services/spread.py`) works for fires with a detection
in the last day. On the map it draws the cells the fire could reach within 6, 12 and 24 h.

## Model

A minimum-travel-time model on a ~150 m grid, 20 km around the fire.

**Ignition:** the fire's detections of the last 12 h, its current front.

**Travel time** between neighbouring cells is distance ÷ rate of spread. The neighbours are the 8
around plus the 8 "knight moves", so spread is not forced onto 8 directions. The rate is the
product of:

| Factor | From | Form |
|---|---|---|
| Fuel | ESA WorldCover class | m/min with no wind on flat ground: grass 1.2, crops 0.9, shrubs 0.6, forest 0.3, wetland 0.3, towns 0.05, bare 0.02; water and snow do not burn |
| Wind | Open-Meteo hourly forecast, 10 m | head rate × (1 + 0.15·U^1.2); elliptical shape with the length-to-breadth ratio of Alexander (1985); rate in each direction from the ellipse, ignition at its rear focus |
| Slope | Copernicus DEM | uphill × e^(0.069·slope°) (about double every 10°); downhill × e^(0.035·slope°), at least 0.5 |
| Dryness | minimum relative humidity | × e^(−0.03·(RH − 30)), between 0.2 and 2; × 0.2 with ≥ 2 mm of rain |

**Weather:** the 24 h run as four 6 h steps, each with its own mean wind (speed, and direction as
a vector mean), driest humidity and rain.

**Overall pace:** a single `SCALE` factor, calibrated in the backtest below.

## What it does not know

- **Fuel load, curing and moisture of the fuel itself.** The class is a 2021 land-cover map, not a
  fuel map.
- **The fire's own winds and spotting.** Embers can start fires ahead of the front in strong wind.
- **Firefighting, roads and fresh firebreaks.**
- **Fire behaviour itself.** It is not a FARSITE or Prometheus simulation.

It is a "where to look" layer. The panel shows its measured hit rate next to every outlook and
says it is never a safe zone.

## Backtest (decides whether it is shown at all)

`python -m ml.backtest_spread -n 300`. Reads the production database only, and caches the weather
in `data/training/spread_weather/`.

**Setup**, for each sampled fire (last ~80 days, at least 4 passes, 30 h observed, 15 VIIRS
detections), at the first moment T that qualifies:
- **front:** its VIIRS detections in the 12 h before T;
- **targets:** its VIIRS detections in the next 24 h on new ground, more than 0.75 km from every
  detection up to T. This is where the fire actually spread;
- **weather:** that day's hourly weather.

**Variants compared at equal area.** Each one's cells are ranked by arrival time (or distance).
The best N are taken, where N is the area the full model reaches in 24 h. So a bigger zone can
never win just by being bigger:
- **full:** fuel + wind + slope + humidity;
- **wind:** same wind and humidity, uniform fuel, flat ground;
- **circle:** plain distance from the front.

**Score:** the share of target detections inside the zone, averaged over fires, with paired
bootstrap 95% confidence intervals.

**Rule:** the outlook appears in the app only if the full model beats the circle at the chosen
scale, with a confidence interval above zero.

## Results (15 Nov 2026, 300 fires from the last ~80 days, worldwide)

Share of the next day's new-ground detections inside the zone, at equal area (95% CI from a
paired bootstrap):

| SCALE | Zone (median) | Full | Wind only | Circle | Full − circle |
|---|---|---|---|---|---|
| 0.1 | 0.4 km² | 0.2% | 0.3% | 0.0% | +0.2 pts (0.0 … +0.4) |
| 0.25 | 1.5 km² | 4.1% | 3.0% | 3.7% | +0.4 pts (−1.3 … +1.8) |
| 0.5 | 4.4 km² | 16.7% | 15.1% | 18.5% | −1.8 pts (−5.0 … +1.2) |
| 1 | 12.8 km² | 41.4% | 42.5% | 44.5% | −3.1 pts (−6.7 … +0.5) |
| 2 | 39.8 km² | 65.7% | 69.8% | 71.3% | −5.6 pts (−9.3 … −2.0) |

**Verdict:** the model never beats the circle, and at the larger zones it is significantly worse.
Under the rule above it stays out of the app: `spread.ENABLED = False`, the API answers 404 and
the button is hidden. The code and the backtest are kept.

**Why** (diagnosed on the same 300 fires):
- **Weak wind effect in this sample.** 57.7% of new detections lie downwind (within 90° of where
  the 24 h mean wind blows; 50% = no effect). With a mean wind of at least 15 km/h it is 61%, but
  only 18 of the 300 fires had such wind. Most of the sample is tropical fires in light wind.
- **Much of the next-day "spread" is not propagation.** New detections are a median 1.8 km from
  the nearest front pixel, and 26.6% are more than 3 km away. Those are new ignitions
  (agricultural burning, land clearing), fires merging and jumps. No wind and fuel model
  predicts where people light the next fire. "Near the current fire, in any direction" captures
  them better, which is what the circle does.
- **Fuel does not discriminate.** 97.7% of the new detections are on burnable land, so the land
  cover hardly narrows down where the fire goes.

## Second attempt: a model learned from FireLens's history (15 Nov 2026)

`ml/spread_cases.py`, `ml/fetch_spread_weather.py` and `ml/train_spread.py`.

**Data**
- 4,064 fire-days from 2,773 fires.
- Hourly weather from Meteomatics (wind, gusts, humidity, rain, temperature), cached in
  `data/training/spread/`.

**Model**
- Gradient boosting on per-cell features:
  - distance to the front, and to the ground the fire already burned;
  - alignment with the wind, with the fire's own movement over the previous day, and with the
    uphill direction;
  - land cover, slope, other fires in the last 30 days;
  - the day's weather and the Fosberg index.
- Strict time split: trained on fire-days before 13 Nov (1,692), tested on 13–14 Nov (2,372
  fire-days, 1,726 fires).

**Fair baseline: the ring.** The plain circle wastes its smallest areas on the burned ground
itself, where a target can never be (targets are new ground by definition). So the comparison is
against a ring: distance from the front, with the burned ground excluded.

| Zone | Learned | Circle | Ring | Learned − ring (95% CI, bootstrap over fires) |
|---|---|---|---|---|
| 1 km² | 8.5% | 0.0% | 8.6% | −0.1 pts (−0.7 … +0.5) |
| 2 km² | 15.9% | 0.7% | 17.0% | −1.2 pts (−1.8 … −0.5) |
| 5 km² | 34.0% | 12.3% | 35.4% | −1.5 pts (−2.1 … −0.9) |
| 10 km² | 52.2% | 33.7% | 53.0% | −0.8 pts (−1.5 … −0.2) |
| 20 km² | 70.7% | 60.8% | 69.7% | +1.0 pts (+0.4 … +1.6) |
| 40 km² | 85.1% | 80.1% | 81.9% | +3.2 pts (+2.7 … +3.7) |

**Verdict: no meaningful gain over the ring.** Slightly worse for small zones, slightly better
for large ones.

What decides where a fire shows up next day is how far it is from the current front, outside the
ground already burned. With better weather (Meteomatics), the fire's own movement and land use,
the wind and fuel signals still add almost nothing at this scale.

The ring itself is a useful, honest product:
- it catches 35% of the next day's new fire in 5 km², 53% in 10 km² and 82% in 40 km²;
- those are measured hit rates that can be shown next to it.

**What could still be tried** (tested the same way):
- **Physical spread only where it applies:** fires in continuous forest, shrub or grass with
  wind ≥ 15 km/h. That is a small share of fires, so testing it needs a larger sample.
- **Geostationary fire detections** (GOES, Meteosat, every 10–15 minutes) would show spread
  almost continuously instead of at a few satellite passes a day.
