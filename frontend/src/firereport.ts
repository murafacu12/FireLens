// Fire details: the ground a fire burns on (land cover, terrain), what Sentinel-2/3/5P saw
// (vegetation before, burn scar, ground temperature, smoke) and the experimental spread outlook.
// Results are kept per fire, so changing the language or the view redraws without new requests.
import { api, type Anomaly, type FireGround, type FireSatellites, type LandShare, type SpreadOutlook } from "./api";
import { isSimple, t } from "./prefs";
import type { Key } from "./strings";
import { esc, fmtNum } from "./util";

export const LAND_COLOUR: Record<string, string> = {
  tree_cover: "#2f7d32", shrubland: "#a3a83c", grassland: "#e3c45a", cropland: "#e08a3c", built_up: "#c0392b",
  bare: "#9e9e9e", snow_ice: "#e8f4fa", water: "#2c7fb8", wetland: "#4fb3a9", mangroves: "#1b5e20", moss_lichen: "#c5b4a0", no_data: "#555",
};
const LAND_CLASSES = Object.keys(LAND_COLOUR);
/** Land-cover class name in the current language. */
export const landLabel = (cls: string) => (LAND_CLASSES.includes(cls) ? t(`lcl.${cls}` as Key) : cls);
/** What a fire on that land cover usually is ("" when there is nothing to say). */
export const landMeaning = (cls: string) => (["tree_cover", "shrubland", "grassland", "cropland", "built_up", "wetland", "mangroves", "bare", "water"].includes(cls) ? t(`lcm.${cls}` as Key) : "");

const pct = (x: number) => `${fmtNum(100 * x, 0)}%`;

/** A stacked share bar (land cover or burn severity) with a compact legend of the main parts. */
export function shareBar(parts: { key: string; label: string; share: number; colour: string }[], max = 4): string {
  const shown = parts.filter((p) => p.share >= 0.005);
  return `<div class="share-bar" role="img" aria-label="${esc(shown.map((p) => `${p.label} ${pct(p.share)}`).join(", "))}">
      ${shown.map((p) => `<span style="width:${(100 * p.share).toFixed(2)}%;background:${p.colour}" title="${esc(`${p.label} ${pct(p.share)}`)}"></span>`).join("")}
    </div>
    <div class="share-legend">${shown.slice(0, max).map((p) => `<span><i style="background:${p.colour}"></i>${esc(p.label)} ${pct(p.share)}</span>`).join("")}</div>`;
}

const landParts = (shares: LandShare[]) => shares.map((s) => ({ key: s.class, label: landLabel(s.class), share: s.share, colour: LAND_COLOUR[s.class] ?? "#777" }));

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const compassWord = (c: string) => t(`dir.${COMPASS.includes(c) ? c : "N"}` as Key);

function terrainKind(slope: number): string {
  return slope < 2 ? t("gr.flat") : slope < 8 ? t("gr.rolling") : slope < 18 ? t("gr.hilly") : t("gr.steep");
}

function terrainText(g: NonNullable<FireGround["terrain"]>): string {
  const relief = g.elevation_max_m - g.elevation_min_m;
  const facing = g.slopes_face ? ` ${t("gr.facing", { dir: compassWord(g.slopes_face) })}` : "";
  const steep = g.steep_share >= 0.05 ? ` ${t("gr.steepShare", { pct: pct(g.steep_share) })}` : "";
  return `<b>${terrainKind(g.slope_mean_deg)}</b>: ${t("gr.terrain", { min: fmtNum(g.elevation_min_m, 0), max: fmtNum(g.elevation_max_m, 0), relief: fmtNum(relief, 0), slope: fmtNum(g.slope_mean_deg, 1) })}${steep}${facing}`;
}

function renderGround(g: FireGround): string {
  const under = g.under_detections ?? [];
  const around = g.around ?? [];
  const main = under[0];
  const burnable = g.burnable_share_around ?? 0;
  if (isSimple()) {
    return `
      <h3>${t("gr.title")}</h3>
      ${main ? `<p class="lead-line">${t("gr.mostly", { what: esc(landLabel(main.class).toLowerCase()) })}</p>${shareBar(landParts(under), 3)}` : `<p class="hint">${t("gr.loading")}</p>`}
      ${g.terrain ? `<p class="hint" style="margin-top:8px"><b>${terrainKind(g.terrain.slope_mean_deg)}</b> · ${t("gr.sBurnable", { pct: pct(burnable) })}</p>` : ""}`;
  }
  return `
    <h3>${t("gr.title")}</h3>
    ${main ? `<p class="lead-line">${t("gr.mostlyShare", { what: esc(landLabel(main.class).toLowerCase()), pct: pct(main.share) })}</p>${shareBar(landParts(under))}` : `<p class="hint">${t("gr.loading")}</p>`}
    ${around.length ? `<p class="hint" style="margin-top:8px">${t("gr.around", { pct: pct(burnable) })}</p>${shareBar(landParts(around))}` : ""}
    ${g.terrain ? `<p class="hint" style="margin-top:8px">${terrainText(g.terrain)}</p>` : ""}
    <p class="hint">${t("gr.source")}</p>`;
}

function ndviWord(v: number): string {
  return v < 0.2 ? t("veg.bare") : v < 0.4 ? t("veg.sparse") : v < 0.6 ? t("veg.moderate") : t("veg.dense");
}
function ndmiWord(v: number): string {
  return v < -0.1 ? t("veg.veryDry") : v < 0.05 ? t("veg.dry") : v < 0.2 ? t("veg.moist") : t("veg.wet");
}

function change(a: Anomaly, unit: string, digits: number, rel = false): string {
  if (!a.available || a.during == null) return t("sat.noView");
  if (a.before == null || a.change == null) return t("sat.during", { v: `${fmtNum(a.during, digits)} ${unit}` });
  const pctChange = rel && a.before ? ` (${a.change >= 0 ? "+" : ""}${fmtNum((100 * a.change) / Math.abs(a.before), 0)}%)` : "";
  return t("sat.vsBefore", { during: `${fmtNum(a.during, digits)} ${unit}`, before: fmtNum(a.before, digits), change: `${a.change >= 0 ? "+" : ""}${fmtNum(a.change, digits)}${pctChange}` });
}

const SEVERITY: [string, Key, string][] = [
  ["low", "sev.low", "#f2c14e"], ["moderate_low", "sev.modLow", "#e8833a"], ["moderate_high", "sev.modHigh", "#c8452c"], ["high", "sev.high", "#7a1f1f"],
];

function burnReason(b: FireSatellites["burn_scar"]): string {
  return b.reason === "too_recent" ? t("sat.tooRecent") : b.reason === "no_clear_pair" ? t("sat.noPair") : t("sat.na", { why: b.error ? ` (${esc(b.error)})` : "" });
}

function renderSatSimple(s: FireSatellites): string {
  const b = s.burn_scar, v = s.vegetation_before, tmp = s.surface_temperature, co = s.smoke?.co, ai = s.smoke?.aerosol_index;
  const plume = ai?.available && ai.peak != null && ai.peak >= 1;
  const coPct = co?.available && co.before && co.change != null ? (100 * co.change) / Math.abs(co.before) : null;
  const tile = (k: string, value: string, sub: string, accent = false) =>
    `<div class="s-tile${accent ? " accent" : ""}"><div class="k">${k}</div><div class="v">${value}</div><div class="sub">${sub}</div></div>`;
  return `
    <h3>${t("sat.sTitle")}</h3>
    <div class="s-tiles">
      ${tile(t("sat.sBurned"), b.available ? `${fmtNum(b.burned_km2 ?? 0, b.burned_km2 != null && b.burned_km2 < 10 ? 1 : 0)}<small>km²</small>` : "–", b.available ? (b.so_far ? t("sat.sSoFar") : t("sat.sScar")) : burnReason(b), true)}
      ${tile(t("sat.sSmoke"), plume ? t("sat.sPlume") : coPct != null && coPct > 10 ? t("sat.sMore") : t("sat.sLittle"), coPct != null ? t("sat.sCo", { pct: `${coPct >= 0 ? "+" : ""}${fmtNum(coPct, 0)}%` }) : t("sat.noView"))}
      ${tile(t("sat.sHeat"), tmp.available && tmp.change != null ? `${tmp.change >= 0 ? "+" : ""}${fmtNum(tmp.change, 1)}<small>°C</small>` : "–", tmp.available && tmp.during != null ? t("sat.sGround", { c: fmtNum(tmp.during, 0) }) : t("sat.noView"))}
      ${tile(t("sat.sVeg"), v.available && v.ndmi != null ? esc(ndmiWord(v.ndmi)) : "–", v.available && v.ndvi != null ? esc(ndviWord(v.ndvi)) : t("sat.noView"))}
    </div>
    ${b.available && b.severity ? shareBar(SEVERITY.map(([k, label, colour]) => ({ key: k, label: t(label), share: b.severity?.[k] ?? 0, colour })), 4) : ""}
    <p class="hint">${t("sat.sSource")}</p>`;
}

function renderSatellites(s: FireSatellites): string {
  if (isSimple()) return renderSatSimple(s);
  const b = s.burn_scar, v = s.vegetation_before, tmp = s.surface_temperature, co = s.smoke?.co, ai = s.smoke?.aerosol_index;
  const burned = b.available
    ? `<p class="lead-line">${t("sat.burned", { km: fmtNum(b.burned_km2 ?? 0, 2), soFar: b.so_far ? t("sat.soFar") : "" })} <small>${t("sat.burnedNote")}</small></p>
       ${shareBar(SEVERITY.map(([k, label, colour]) => ({ key: k, label: t(label), share: b.severity?.[k] ?? 0, colour })))}
       <p class="hint">${t("sat.footprint", { km: fmtNum(s.detected_footprint_km2, 2) })}${b.clear_fraction != null && b.clear_fraction < 0.9 ? t("sat.clouds", { pct: pct(1 - b.clear_fraction) }) : ""}.</p>`
    : `<p class="hint">${burnReason(b)}</p>`;
  const aiText = ai?.available && ai.during != null
    ? ai.peak != null && ai.peak >= 1 ? t("sat.aiPlume", { v: fmtNum(ai.peak, 1), day: esc(ai.peak_day ?? "") }) : t("sat.aiNone", { v: fmtNum(ai.during, 1) })
    : t("sat.aiNoView");
  return `
    <h3>${t("sat.burnTitle")}</h3>
    ${burned}
    <h3>${t("sat.beforeTitle")}</h3>
    <p class="hint">${v.available && v.ndvi != null ? t("sat.veg", { ndvi: fmtNum(v.ndvi, 2), ndviWord: ndviWord(v.ndvi), ndmi: fmtNum(v.ndmi ?? 0, 2), ndmiWord: ndmiWord(v.ndmi ?? 0) }) : t("sat.noS2")}</p>
    <h3>${t("sat.tempTitle")}</h3>
    <p class="hint">${change(tmp, "°C", 1)} <small>${t("sat.tempNote")}</small></p>
    <h3>${t("sat.smokeTitle")}</h3>
    <p class="hint">${t("sat.co")} ${co ? change(co, "×10¹⁸ molec/cm²", 2, true) : t("sat.noData")}; ${aiText}. <small>${t("sat.smokeNote")}</small></p>`;
}

// ---------------------------------------------------------------- spread outlook

type SpreadDrawer = (grid: { west: number; north: number; step: number; rows: number; cols: number; hours: Uint8Array } | null) => void;
let drawSpread: SpreadDrawer = () => undefined;
/** main.ts hands over the globe's drawing function. */
export function setSpreadDrawer(fn: SpreadDrawer): void {
  drawSpread = fn;
}
export function clearSpread(): void {
  drawSpread(null);
}

// On as EXPERIMENTAL: the backtest found the zones no better than a circle of the same area
// (docs/FIRE_SPREAD.md); the panel says so next to every outlook.
const SPREAD_ENABLED = true;
const toward = (deg: number) => compassWord(COMPASS[Math.round(((deg % 360) + 360) % 360 / 45) % 8]);

function renderSpread(s: SpreadOutlook): string {
  const w = s.weather;
  const maxWind = Math.max(...w.map((x) => x.wind_kmh));
  const bt = s.backtest;
  const tested = bt
    ? bt.passed
      ? t("sp.tested", { n: bt.fires, cap: fmtNum(100 * bt.capture_24h, 0), circle: fmtNum(100 * bt.circle_capture_24h, 0) })
      : t("sp.notValidated", { n: bt.fires, cap: fmtNum(100 * bt.capture_24h, 0), circle: fmtNum(100 * bt.circle_capture_24h, 0) })
    : "";
  const legend = `<div class="spread-legend"><span><i style="background:#fff"></i>${t("sp.now")}</span><span><i style="background:#c81e1e"></i>≤ 6 h</span><span><i style="background:#f06e1e"></i>6–12 h</span><span><i style="background:#fac83c"></i>12–24 h</span></div>`;
  const head = `<p class="lead-line">${s.head ? t("sp.head", { km: fmtNum(s.head.distance_km, 1), dir: toward(s.head.bearing_deg) }) : t("sp.little")}</p>`;
  const title = `<h3>${t("sp.title")} ${s.experimental ? `<span class="pill warning">${t("sp.experimental")}</span>` : ""}</h3>`;
  if (isSimple()) {
    return `${title}${legend}${head}
      <p class="hint">${t("sp.sArea", { a: fmtNum(s.area_km2["24h"], 1) })} ${bt && !bt.passed ? `<b>${t("sp.sNotValidated")}</b>` : ""} ${t("sp.sWhere")}</p>`;
  }
  return `${title}${legend}${head}
    <dl class="kv">
      <dt>${t("sp.reach")}</dt><dd>${t("sp.reachVal", { a6: fmtNum(s.area_km2["6h"], 1), a12: fmtNum(s.area_km2["12h"], 1), a24: fmtNum(s.area_km2["24h"], 1) })}</dd>
      <dt>${t("sp.wind")}</dt><dd>${w.map((x) => `${x.wind_kmh} km/h → ${COMPASS[Math.round(((x.toward_deg % 360) + 360) % 360 / 45) % 8]}`).join(" · ")} <small>${t("sp.windNote")}</small></dd>
      <dt>${t("sp.driest")}</dt><dd>${t("sp.rh", { rh: Math.min(...w.map((x) => x.rh_min)) })}${w.some((x) => x.rain_mm >= 2) ? ` · ${t("sp.rain")}` : ""}</dd>
    </dl>
    <p class="hint">${tested} ${t("sp.disclaimer")}${maxWind > 40 ? ` ${t("sp.strongWind")}` : ""} ${t("sp.never")}</p>`;
}

// ---------------------------------------------------------------- the report slot

type Loadable<T> = { state: "idle" } | { state: "loading" } | { state: "ok"; data: T } | { state: "error"; msg: string };
interface Report {
  eventId: string;
  burning: boolean;
  ground: Loadable<FireGround>;
  sat: Loadable<FireSatellites>;
  spread: Loadable<SpreadOutlook>;
}
let report: Report | null = null;
let slotEl: HTMLElement | null = null;

function draw(): void {
  const slot = slotEl;
  const r = report;
  if (!slot || !r || !slot.isConnected) return;
  const g = r.ground;
  const ground = g.state === "ok" ? renderGround(g.data) : g.state === "error"
    ? `<h3>${t("gr.title")}</h3><p class="hint">${t("gr.na", { msg: esc(g.msg) })}</p>`
    : `<h3>${t("gr.title")}</h3><p class="hint">${t("gr.reading")}</p>`;
  const s = r.sat;
  const sat = s.state === "ok" ? renderSatellites(s.data) : s.state === "loading"
    ? `<p class="hint">${t("sat.asking")}</p>`
    : s.state === "error" ? `<p class="banner warn">${t("sat.failed", { msg: esc(s.msg) })}</p>`
    : `<button type="button" class="secondary" data-sat>${t("sat.button")}</button><p class="hint">${t("sat.buttonHint")}</p>`;
  const sp = r.spread;
  const spread = !r.burning || !SPREAD_ENABLED ? "" : sp.state === "ok" ? renderSpread(sp.data) : sp.state === "loading"
    ? `<p class="hint">${t("sp.computing")}</p>`
    : sp.state === "error" ? `<p class="banner warn">${t("sp.failed", { msg: esc(sp.msg) })}</p>`
    : `<button type="button" data-spread>${t("sp.button")}</button>`;
  slot.innerHTML = `${spread ? `<div class="spread-slot">${spread}</div>` : ""}<div class="ground-slot">${ground}</div><div class="sat-slot">${sat}</div>`;
  slot.querySelector("[data-sat]")?.addEventListener("click", () => loadSat(r));
  slot.querySelector("[data-spread]")?.addEventListener("click", () => loadSpread(r));
}

function loadSat(r: Report): void {
  r.sat = { state: "loading" };
  draw();
  api.eventSatellites(r.eventId).then(
    (data) => (r.sat = { state: "ok", data }),
    (err: Error) => (r.sat = { state: "error", msg: err.message }),
  ).finally(() => report === r && draw());
}

function loadSpread(r: Report): void {
  r.spread = { state: "loading" };
  draw();
  api.eventSpread(r.eventId).then(
    (data) => {
      r.spread = { state: "ok", data };
      const bin = atob(data.grid.hours_b64);
      const hours = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) hours[i] = bin.charCodeAt(i);
      if (report === r) drawSpread({ ...data.grid, hours });
    },
    (err: Error) => (r.spread = { state: "error", msg: err.message }),
  ).finally(() => report === r && draw());
}

/** Fill the report slot of the fire-details panel. `burning`: the fire has a current front.
 * Mounting the same fire again (language or view change) reuses what was already loaded. */
export function mountFireReport(slot: HTMLElement, eventId: string, burning = false): void {
  slotEl = slot;
  if (report?.eventId === eventId) return draw();
  clearSpread();
  const r: Report = { eventId, burning, ground: { state: "loading" }, sat: { state: "idle" }, spread: { state: "idle" } };
  report = r;
  draw();
  api.eventGround(eventId).then(
    (data) => (r.ground = { state: "ok", data }),
    (err: Error) => (r.ground = { state: "error", msg: err.message }),
  ).finally(() => report === r && draw());
}

/** The details panel closed or now shows something else. */
export function forgetFireReport(): void {
  report = null;
  slotEl = null;
}
