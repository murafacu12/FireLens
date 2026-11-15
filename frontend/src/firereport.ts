// Fire details: the ground a fire burns on (land cover, terrain) and what Sentinel-2/3/5P saw
// (vegetation before, burn scar, ground temperature, smoke).
import { api, type Anomaly, type FireGround, type FireSatellites, type LandShare, type SpreadOutlook } from "./api";
import { esc, fmtNum } from "./util";

export const LAND_COLOUR: Record<string, string> = {
  tree_cover: "#2f7d32", shrubland: "#a3a83c", grassland: "#e3c45a", cropland: "#e08a3c", built_up: "#c0392b",
  bare: "#9e9e9e", snow_ice: "#e8f4fa", water: "#2c7fb8", wetland: "#4fb3a9", mangroves: "#1b5e20", moss_lichen: "#c5b4a0", no_data: "#555",
};
const pct = (x: number) => `${fmtNum(100 * x, 0)}%`;

/** A stacked share bar (land cover or burn severity) with a compact legend of the main parts. */
export function shareBar(parts: { key: string; label: string; share: number; colour: string }[], max = 4): string {
  const shown = parts.filter((p) => p.share >= 0.005);
  return `<div class="share-bar" role="img" aria-label="${esc(shown.map((p) => `${p.label} ${pct(p.share)}`).join(", "))}">
      ${shown.map((p) => `<span style="width:${(100 * p.share).toFixed(2)}%;background:${p.colour}" title="${esc(`${p.label} ${pct(p.share)}`)}"></span>`).join("")}
    </div>
    <div class="share-legend">${shown.slice(0, max).map((p) => `<span><i style="background:${p.colour}"></i>${esc(p.label)} ${pct(p.share)}</span>`).join("")}</div>`;
}

const landParts = (shares: LandShare[]) => shares.map((s) => ({ key: s.class, label: s.label, share: s.share, colour: LAND_COLOUR[s.class] ?? "#777" }));

function terrainText(t: NonNullable<FireGround["terrain"]>): string {
  const relief = t.elevation_max_m - t.elevation_min_m;
  const kind = t.slope_mean_deg < 2 ? "Flat" : t.slope_mean_deg < 8 ? "Rolling" : t.slope_mean_deg < 18 ? "Hilly" : "Steep, mountainous";
  const facing = t.slopes_face ? ` Most slopes face ${t.slopes_face}.` : "";
  const steep = t.steep_share >= 0.05 ? ` ${pct(t.steep_share)} of it is steeper than 20°, where fire runs much faster uphill.` : "";
  return `<b>${kind}</b>: ${fmtNum(t.elevation_min_m, 0)}–${fmtNum(t.elevation_max_m, 0)} m (relief ${fmtNum(relief, 0)} m), mean slope ${fmtNum(t.slope_mean_deg, 1)}°.${steep}${facing}`;
}

function renderGround(g: FireGround): string {
  const under = g.under_detections ?? [];
  const around = g.around ?? [];
  const main = under[0];
  const burnable = g.burnable_share_around ?? 0;
  return `
    <h3>Where it burns</h3>
    ${main ? `<p class="lead-line">Mostly <b>${esc(main.label.toLowerCase())}</b> (${pct(main.share)} of its detections)</p>${shareBar(landParts(under))}` : `<p class="hint">Land cover not available yet (map tiles downloading).</p>`}
    ${around.length ? `<p class="hint" style="margin-top:8px">Around the fire (the ground it could spread into): ${pct(burnable)} burnable vegetation.</p>${shareBar(landParts(around))}` : ""}
    ${g.terrain ? `<p class="hint" style="margin-top:8px">${terrainText(g.terrain)}</p>` : ""}
    <p class="hint">ESA WorldCover 2021 (read at ~150 m) and Copernicus DEM (90 m). Land cleared since 2021 shows its 2021 class.</p>`;
}

function ndviWord(v: number): string {
  return v < 0.2 ? "bare or very sparse" : v < 0.4 ? "sparse or dry vegetation" : v < 0.6 ? "moderately green" : "dense, green vegetation";
}
function ndmiWord(v: number): string {
  return v < -0.1 ? "very dry" : v < 0.05 ? "dry" : v < 0.2 ? "moderately moist" : "moist";
}

function change(a: Anomaly, unit: string, digits: number, rel = false): string {
  if (!a.available || a.during == null) return "no clear view";
  if (a.before == null || a.change == null) return `${fmtNum(a.during, digits)} ${unit} during the fire`;
  const pctChange = rel && a.before ? ` (${a.change >= 0 ? "+" : ""}${fmtNum((100 * a.change) / Math.abs(a.before), 0)}%)` : "";
  return `${fmtNum(a.during, digits)} ${unit} during the fire vs ${fmtNum(a.before, digits)} in the month before: <b>${a.change >= 0 ? "+" : ""}${fmtNum(a.change, digits)}${pctChange}</b>`;
}

const SEVERITY: [string, string, string][] = [
  ["low", "Low", "#f2c14e"], ["moderate_low", "Moderate-low", "#e8833a"], ["moderate_high", "Moderate-high", "#c8452c"], ["high", "High", "#7a1f1f"],
];

function renderSatellites(s: FireSatellites): string {
  const b = s.burn_scar, v = s.vegetation_before, t = s.surface_temperature, co = s.smoke?.co, ai = s.smoke?.aerosol_index;
  const burned = b.available
    ? `<p class="lead-line"><b>${fmtNum(b.burned_km2 ?? 0, 2)} km²</b> burned${b.so_far ? " so far" : ""} <small>(moderate severity or worse, Sentinel-2)</small></p>
       ${shareBar(SEVERITY.map(([k, label, colour]) => ({ key: k, label, share: b.severity?.[k] ?? 0, colour })))}
       <p class="hint">Detected footprint (satellite fire pixels): ${fmtNum(s.detected_footprint_km2, 2)} km². Shares are of the whole box around the fire${b.clear_fraction != null && b.clear_fraction < 0.9 ? `; ${pct(1 - b.clear_fraction)} of it was hidden by clouds` : ""}.</p>`
    : `<p class="hint">${b.reason === "too_recent" ? "Too recent: no image after the fire yet." : b.reason === "no_clear_pair" ? "No cloud-free pair of images before and after the fire yet." : `Not available${b.error ? ` (${esc(b.error)})` : ""}.`}</p>`;
  const aiText = ai?.available && ai.during != null
    ? ai.peak != null && ai.peak >= 1 ? `aerosol index peaked at <b>${fmtNum(ai.peak, 1)}</b> on ${esc(ai.peak_day ?? "")}: an absorbing smoke plume` : `aerosol index ${fmtNum(ai.during, 1)}: no clear absorbing plume over the area`
    : "aerosol index: no clear view";
  return `
    <h3>Burn scar</h3>
    ${burned}
    <h3>Before the fire</h3>
    <p class="hint">${v.available && v.ndvi != null ? `Vegetation: NDVI <b>${fmtNum(v.ndvi, 2)}</b> (${ndviWord(v.ndvi)}), moisture index NDMI <b>${fmtNum(v.ndmi ?? 0, 2)}</b> (${ndmiWord(v.ndmi ?? 0)}), Sentinel-2, last cloud-free image of the 30 days before.` : "No cloud-free Sentinel-2 image in the 30 days before."}</p>
    <h3>Ground temperature</h3>
    <p class="hint">${change(t, "°C", 1)} <small>(Sentinel-3, daytime pass, warmest tenth of the area)</small></p>
    <h3>Smoke</h3>
    <p class="hint">Carbon monoxide: ${co ? change(co, "×10¹⁸ molec/cm²", 2, true) : "no data"}; ${aiText}. <small>(Sentinel-5P over ~100 km around the fire: wind carries smoke away, and other fires nearby add to it.)</small></p>`;
}

// ---------------------------------------------------------------- spread outlook

// On as EXPERIMENTAL: the backtest found the zones no better than a circle of the same area
// (docs/FIRE_SPREAD.md); the panel says so next to every outlook.
const SPREAD_ENABLED = true;

type SpreadDrawer = (grid: { west: number; north: number; step: number; rows: number; cols: number; hours: Uint8Array } | null) => void;
let drawSpread: SpreadDrawer = () => undefined;
/** main.ts hands over the globe's drawing function. */
export function setSpreadDrawer(fn: SpreadDrawer): void {
  drawSpread = fn;
}
export function clearSpread(): void {
  drawSpread(null);
}

const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];
const SHORT = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const toward = (deg: number) => COMPASS[Math.round(((deg % 360) + 360) % 360 / 45) % 8];

function renderSpread(s: SpreadOutlook): string {
  const w = s.weather;
  const maxWind = Math.max(...w.map((x) => x.wind_kmh));
  const bt = s.backtest;
  const tested = bt
    ? bt.passed
      ? `Tested on ${bt.fires} past fires: zones like these held <b>${fmtNum(100 * bt.capture_24h, 0)}%</b> of where the fires spread next day, against ${fmtNum(100 * bt.circle_capture_24h, 0)}% for a circle of the same area that ignores wind, terrain and fuel.`
      : `<b>Not validated.</b> Tested on ${bt.fires} past fires, zones like these held ${fmtNum(100 * bt.capture_24h, 0)}% of where the fires spread next day, while a simple circle of the same size held ${fmtNum(100 * bt.circle_capture_24h, 0)}%: so far it does no better than distance alone.`
    : "";
  return `
    <h3>Spread outlook, next 24 h ${s.experimental ? `<span class="pill warning">Experimental</span>` : ""}</h3>
    <div class="spread-legend"><span><i style="background:#fff"></i>now</span><span><i style="background:#c81e1e"></i>≤ 6 h</span><span><i style="background:#f06e1e"></i>6–12 h</span><span><i style="background:#fac83c"></i>12–24 h</span></div>
    <p class="lead-line">${s.head ? `Could run up to <b>${fmtNum(s.head.distance_km, 1)} km</b> toward the <b>${toward(s.head.bearing_deg)}</b>` : "Little room to spread"}</p>
    <dl class="kv">
      <dt>Reachable area</dt><dd>${fmtNum(s.area_km2["6h"], 1)} km² in 6 h · ${fmtNum(s.area_km2["12h"], 1)} in 12 h · ${fmtNum(s.area_km2["24h"], 1)} in 24 h</dd>
      <dt>Wind</dt><dd>${w.map((x) => `${x.wind_kmh} km/h → ${SHORT[Math.round(((x.toward_deg % 360) + 360) % 360 / 45) % 8]}`).join(" · ")} <small>(each 6 h, blowing toward)</small></dd>
      <dt>Driest air</dt><dd>${Math.min(...w.map((x) => x.rh_min))}% relative humidity${w.some((x) => x.rain_mm >= 2) ? " · rain expected" : ""}</dd>
    </dl>
    <p class="hint">${tested}
      An estimate from land cover, terrain and the wind forecast${maxWind > 40 ? " (strong wind: spotting can jump far ahead)" : ""}, not a fire-behaviour simulation: fuel load, curing and the fire's own winds are unknown. Use it as where to look, never as a safe zone.</p>`;
}

/** Fill the report slot of the fire-details panel. `burning`: the fire has a current front. */
export function mountFireReport(slot: HTMLElement, eventId: string, burning = false): void {
  clearSpread();
  slot.innerHTML = `${burning && SPREAD_ENABLED ? `<div class="spread-slot"><button type="button" data-spread>Spread outlook: next 24 h on the map (experimental)</button></div>` : ""}
    <div class="ground-slot"><h3>Where it burns</h3><p class="hint">Reading land cover and terrain…</p></div>
    <div class="sat-slot"><button type="button" class="secondary" data-sat>Satellite report: burn scar, vegetation, heat, smoke</button>
    <p class="hint">Sentinel-2, -3 and -5P (Copernicus). Takes ~20 s the first time for each fire.</p></div>`;
  const groundSlot = slot.querySelector<HTMLElement>(".ground-slot")!;
  api.eventGround(eventId).then(
    (g) => (groundSlot.innerHTML = renderGround(g)),
    (err: Error) => (groundSlot.innerHTML = `<h3>Where it burns</h3><p class="hint">Not available: ${esc(err.message)}</p>`),
  );
  const spreadSlot = slot.querySelector<HTMLElement>(".spread-slot");
  spreadSlot?.querySelector("[data-spread]")!.addEventListener("click", () => {
    spreadSlot.innerHTML = `<p class="hint">Computing where it could spread (land cover, terrain, wind forecast)…</p>`;
    api.eventSpread(eventId).then(
      (s) => {
        const bin = atob(s.grid.hours_b64);
        const hours = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) hours[i] = bin.charCodeAt(i);
        drawSpread({ ...s.grid, hours });
        spreadSlot.innerHTML = renderSpread(s);
      },
      (err: Error) => (spreadSlot.innerHTML = `<p class="banner warn">Spread outlook unavailable: ${esc(err.message)}</p>`),
    );
  });
  const satSlot = slot.querySelector<HTMLElement>(".sat-slot")!;
  satSlot.querySelector("[data-sat]")!.addEventListener("click", () => {
    satSlot.innerHTML = `<p class="hint">Asking Sentinel-2, -3 and -5P… (~20 s)</p>`;
    api.eventSatellites(eventId).then(
      (s) => (satSlot.innerHTML = renderSatellites(s)),
      (err: Error) => (satSlot.innerHTML = `<p class="banner warn">Satellite report failed: ${esc(err.message)}</p>`),
    );
  });
}

