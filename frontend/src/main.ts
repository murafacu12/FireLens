import "./styles.css";

import { api, type AlertRuleIn, type BackfillJob, type CalendarDay, type Health, type Params } from "./api";
import { clearAnalysis, runAnalysis, runSensors, wireAnalysis } from "./analysis";
import { closeDetails, onDetailsChange, setActiveHours, showDetection, showEvent, showPoint, statePill, wireDetails } from "./details";
import { setSpreadDrawer } from "./firereport";
import { Globe, type BaseLayerId, type PickResult } from "./globe";
import { helpButton, mountGuide, mountLegend, showMapTip, wireHelp } from "./guide";
import { revealMap, wireLayout } from "./layout";
import { $, daysAgo, esc, fmtNum, fmtTime, isoDay, toast } from "./util";
import { mountGuideTab } from "./about";
import { addDays, periodSummary, renderHeatmap, weekStart, WEEKS } from "./heatmap";
import { isSimple, locale, onPrefsChange, t, tn, wirePrefs } from "./prefs";

const filtersForm = $<HTMLFormElement>("#filters");
const MAX_ZONE_DEG = 40; // an alert zone taken from a zoomed-out view would cover half the planet
const MAX_POINTS = 50_000; // focus mode: one fire's complete history
let picking = false;
let focusEventId: string | null = null;

const globe = new Globe($("#globe"), onPick);
if (import.meta.env.DEV) (window as unknown as { __globe: Globe }).__globe = globe; // debugging aid
let booted = false; // camera-driven updates start after the first load, so startup queries run once
// Moving or zooming only asks for tiles the camera hasn't loaded yet (see tiles.ts); nothing else reloads.
globe.onCameraSettled(() => booted && updateView());

// ---------------------------------------------------------------- picking
function onPick(r: PickResult): void {
  if (picking && r.kind === "point") {
    setPicking(false);
    (filtersForm.elements.namedItem("lat") as HTMLInputElement).value = r.lat.toFixed(4);
    (filtersForm.elements.namedItem("lon") as HTMLInputElement).value = r.lon.toFixed(4);
    const radius = filtersForm.elements.namedItem("radius_km") as HTMLInputElement;
    if (!radius.value) radius.value = "50";
    globe.pin(r.lat, r.lon, Number(radius.value));
    return;
  }
  if (r.kind === "cell") {
    // Zoom into the bubble; the camera-settled refresh then shows finer cells or the fires themselves.
    const h = r.cellDeg / 2;
    globe.flyToBox(r.lon - h, r.lat - h, r.lon + h, r.lat + h);
  } else if (r.kind === "event") void openEvent(r.eventId);
  else if (r.kind === "detection") {
    api.detection(r.id).then(showDetection, (err: Error) => toast(t("toast.detFailed", { msg: err.message }), "error"));
  }
  else showPoint(r.lat, r.lon);
}

function setPicking(on: boolean): void {
  picking = on;
  if (on) revealMap(); // on phones the sheet would cover the globe the user must tap
  $("#picking-hint").classList.toggle("hidden", !on);
  $("#globe").classList.toggle("crosshair", on);
}

// the outline of the fire open in the details panel; cleared when the panel shows anything else
let perimeterOf: string | null = null;
onDetailsChange((id) => {
  if (id !== perimeterOf) {
    perimeterOf = null;
    globe.showPerimeter(null);
  }
});

async function openEvent(id: string): Promise<void> {
  revealMap();
  try {
    const ev = await api.event(id);
    showEvent(ev, (eventId) => {
      focusEventId = eventId;
      void refresh();
    });
    perimeterOf = ev.id;
    globe.showPerimeter(ev.perimeter);
    globe.flyToBox(ev.west, ev.south, ev.east, ev.north);
  } catch (err) {
    toast(t("toast.evFailed", { msg: (err as Error).message }), "error");
  }
}

// ---------------------------------------------------------------- filters
/** The tile query: everything that changes what a tile contains. The view (bbox) is not part of it —
 * tiles have fixed positions — and neither are the sensors: tiles carry MODIS and VIIRS apart. */
function tileParams(): Params {
  const f = new FormData(filtersForm);
  const s = (k: string) => (f.get(k) as string | null)?.trim() || undefined;
  const satellites = f.getAll("satellite") as string[];
  const time: Params = timeMode === "latest" ? { hours: latestHours } : timeRange();
  return {
    ...time,
    ...(s("lat") && s("lon") && s("radius_km") ? { lat: s("lat"), lon: s("lon"), radius_km: s("radius_km") } : {}),
    satellite: satellites.length ? satellites : undefined,
    min_confidence: s("min_confidence"),
    daynight: s("daynight"),
    min_frp: s("min_frp"),
    max_frp: s("max_frp"),
    include_static: $<HTMLInputElement>("#lyr-static").checked || undefined,
    min_area: s("min_area"),
    max_area: s("max_area"),
    min_detections: s("min_detections"),
    status: s("status"),
  };
}

function sensors() {
  const on = new FormData(filtersForm).getAll("instrument");
  return { viirs: on.includes("VIIRS"), modis: on.includes("MODIS") };
}

// ---------------------------------------------------------------- time: Latest vs History
type TimeMode = "latest" | "history";
let timeMode: TimeMode = "latest";
let latestHours = 24;
const AUTO_REFRESH_MS = 5 * 60_000; // Latest mode re-reads the database; the API pulls NASA hourly
const MAX_LOAD_DAYS = 31; // History auto-loads at most this many missing days at once
const MAX_LOAD_SPAN_DEG = 60; // ...for a view at most this wide (a region, not a hemisphere)

const dateInput = (name: "start" | "end") => filtersForm.elements.namedItem(name) as HTMLInputElement;

/** Query window for the map: a rolling window back from now, or the selected calendar days (UTC). */
function timeRange(): { start?: string; end?: string } {
  if (timeMode === "latest") return { start: new Date(Date.now() - latestHours * 3_600_000).toISOString() };
  const s = dateInput("start").value;
  const e = dateInput("end").value;
  return { start: s ? `${s}T00:00:00Z` : undefined, end: e ? `${e}T23:59:59Z` : undefined };
}

function setHistoryRange(start: string, end: string): void {
  dateInput("start").value = start;
  dateInput("end").value = end < start ? start : end;
}

function setTimeMode(mode: TimeMode): void {
  timeMode = mode;
  document.querySelectorAll<HTMLButtonElement>("[data-mode]").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.setAttribute("aria-selected", String(b.dataset.mode === mode));
  });
  $("#mode-latest").classList.toggle("hidden", mode !== "latest");
  $("#mode-history").classList.toggle("hidden", mode !== "history");
  if (mode === "history" && !dateInput("start").value) setHistoryRange(isoDay(daysAgo(1)), isoDay(daysAgo(1)));
  if (mode === "latest") showBanner("", "hidden");
  focusEventId = null;
  void refresh();
}

document.querySelectorAll<HTMLButtonElement>("[data-mode]").forEach((b) =>
  b.addEventListener("click", () => setTimeMode(b.dataset.mode as TimeMode)),
);
$("#latest-window").addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-hours]");
  if (!btn) return;
  latestHours = Number(btn.dataset.hours);
  document.querySelectorAll("#latest-window button").forEach((b) => b.classList.toggle("active", b === btn));
  void refresh();
});
$("#fetch-latest").addEventListener("click", async (e) => {
  const btn = e.currentTarget as HTMLButtonElement;
  btn.disabled = true;
  btn.textContent = t("f.fetching");
  try {
    const report = (await api.ingest({})) as { stored: number; sources: { source: string; error: string | null }[] };
    const failed = report.sources.filter((s) => s.error).map((s) => s.source);
    toast(t("toast.fetched", { n: report.stored.toLocaleString(locale()), failed: failed.length ? t("toast.fetchedFailed", { list: failed.join(", ") }) : "" }),
      failed.length ? "error" : "info");
    await loadHealth();
    await syncEpoch();
  } catch (err) {
    toast(t("toast.fetchFailed", { msg: (err as Error).message }), "error");
  } finally {
    btn.disabled = false;
    btn.textContent = t("f.fetch");
  }
});

function shiftHistory(direction: 1 | -1): void {
  const s = new Date(`${dateInput("start").value}T00:00:00Z`);
  const e = new Date(`${dateInput("end").value}T00:00:00Z`);
  const span = Math.round((e.getTime() - s.getTime()) / 86_400_000) + 1;
  const ns = new Date(s.getTime() + direction * span * 86_400_000);
  const ne = new Date(e.getTime() + direction * span * 86_400_000);
  if (ne > new Date()) return; // no future days
  setHistoryRange(isoDay(ns), isoDay(ne));
  void refresh();
}
$("#day-prev").addEventListener("click", () => shiftHistory(-1));
$("#day-next").addEventListener("click", () => shiftHistory(1));
$("#history-presets").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-ago]");
  if (!b) return;
  const ago = Number(b.dataset.ago);
  const span = Number(b.dataset.span);
  setHistoryRange(isoDay(daysAgo(ago)), isoDay(daysAgo(ago - span + 1)));
  void refresh();
});
for (const name of ["start", "end"] as const) {
  dateInput(name).max = isoDay(new Date());
  dateInput(name).addEventListener("change", () => {
    if (dateInput("end").value < dateInput("start").value) dateInput("end").value = dateInput("start").value;
    void refresh();
  });
}

function renderFreshness(): void {
  const el = $("#freshness");
  if (!health?.latest_detection_at) {
    el.textContent = t("f.noData");
    return;
  }
  const ageMin = Math.max(0, Math.round((Date.now() - new Date(health.latest_detection_at).getTime()) / 60_000));
  el.innerHTML = `${t("f.latestData", { age: ageText(ageMin), when: esc(fmtTime(health.latest_detection_at)) })} ${helpButton("freshness")}`;
}
function ageText(min: number): string {
  return min < 60 ? t("f.ageMin", { m: min }) : t("f.ageH", { h: Math.floor(min / 60), m: min % 60 });
}
window.setInterval(renderFreshness, 30_000);
window.setInterval(async () => {
  if (!booted || timeMode !== "latest" || document.hidden) return;
  await loadHealth().catch(() => undefined);
  await syncEpoch(); // tiles are re-checked only if new data arrived or the hour rolled over
}, AUTO_REFRESH_MS);

// ---------------------------------------------------------------- history: load missing days automatically
const attemptedLoads = new Set<string>();
let coverageJobId: string | null = null;
let keepBannerUntil = 0; // let a "Loaded …" confirmation stay readable after the follow-up re-check

function showBanner(text: string, kind: "info" | "warn" | "ok" | "hidden", withButton = false): void {
  const banner = $("#coverage-banner");
  banner.className = `banner ${kind === "hidden" ? "hidden" : kind === "info" ? "" : kind}`;
  $(".banner-text", banner).innerHTML = text;
  $("#coverage-load").classList.toggle("hidden", !withButton);
  $(".progress", banner).classList.add("hidden");
}

function viewSpanDeg(b: [number, number, number, number]): number {
  const width = b[2] >= b[0] ? b[2] - b[0] : 360 - (b[0] - b[2]);
  return Math.max(width, b[3] - b[1]);
}

/** Round the view outwards to 0.5 deg so nearby pans reuse the same coverage records. */
function snapBbox(b: [number, number, number, number]): string {
  const f = (x: number) => Math.floor(x * 2) / 2;
  const c = (x: number) => Math.ceil(x * 2) / 2;
  return [Math.max(-180, f(b[0])), Math.max(-90, f(b[1])), Math.min(180, c(b[2])), Math.min(90, c(b[3]))].join(",");
}

const fmtDay = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString(locale(), { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

async function checkCoverage(): Promise<void> {
  if (timeMode !== "history" || focusEventId) return showBanner("", "hidden");
  if (coverageJobId) return; // a load is running; its watcher owns the banner
  const start = dateInput("start").value;
  const end = dateInput("end").value;
  if (!start || !end) return;
  const view = globe.viewBbox();
  let cov;
  try {
    cov = await api.coverage({ start, end, bbox: view ? view.join(",") : undefined });
  } catch {
    return;
  }
  const missing = cov.missing;
  if (!missing.length) {
    if (Date.now() < keepBannerUntil) return;
    return showBanner("", "hidden");
  }
  const total = cov.days.length;
  const what = missing.length === total
    ? (total === 1 ? t("hb.dayNot", { day: fmtDay(start) }) : t("hb.daysNot"))
    : t("hb.someNot", { n: missing.length, total });
  if (!cov.can_load) {
    return showBanner(t("hb.needKey", { what }), "warn");
  }
  if (!view || view[0] > view[2] || viewSpanDeg(view) > MAX_LOAD_SPAN_DEG) {
    return showBanner(t("hb.zoomIn", { what, deg: MAX_LOAD_SPAN_DEG }), "warn");
  }
  const first = missing[0];
  const last = missing[missing.length - 1];
  const daysToLoad = Math.round((Date.parse(last) - Date.parse(first)) / 86_400_000) + 1;
  if (daysToLoad > MAX_LOAD_DAYS) {
    return showBanner(t("hb.tooMany", { what, n: MAX_LOAD_DAYS }), "warn", true);
  }
  const box = snapBbox(view);
  const key = `${first}|${last}|${box}`;
  if (attemptedLoads.has(key)) {
    // tried already: NASA may not have these days for every satellite; don't loop
    return showBanner(t("hb.tried", { what }), "warn", true);
  }
  attemptedLoads.add(key);
  await startCoverageLoad(first, last, box);
}

async function startCoverageLoad(start: string, end: string, bbox: string): Promise<void> {
  try {
    const job = await api.backfill({ start, end, bbox });
    followCoverageJob(job.id);
  } catch (err) {
    const msg = (err as Error).message;
    if (msg.startsWith("409")) {
      // another load is running (e.g. from the Calendar tab): follow it, then re-check
      const running = (await api.jobs().catch(() => [] as BackfillJob[])).find((j) => j.status === "running" || j.status === "queued");
      if (running) return followCoverageJob(running.id);
    }
    showBanner(t("hb.failed", { msg: esc(msg) }), "warn", true);
  }
}

function followCoverageJob(id: string): void {
  coverageJobId = id;
  const banner = $("#coverage-banner");
  const tick = async () => {
    let job: BackfillJob;
    try {
      job = await api.job(id);
    } catch (err) {
      coverageJobId = null;
      return showBanner(t("hb.lost", { msg: esc((err as Error).message) }), "warn", true);
    }
    const r = job.request;
    const range = r.start === r.end ? fmtDay(r.start) : `${fmtDay(r.start)} – ${fmtDay(r.end)}`;
    if (job.status === "queued" || job.status === "running") {
      banner.className = "banner";
      $(".banner-text", banner).innerHTML = t("hb.loading", {
        range: esc(range), done: job.done_chunks, total: job.total_chunks || "?", n: job.detections.toLocaleString(locale()) });
      $(".progress", banner).classList.remove("hidden");
      $(".bar", banner).style.width = `${Math.round(job.progress * 100)}%`;
      $("#coverage-load").classList.add("hidden");
      window.setTimeout(tick, 2000);
      return;
    }
    coverageJobId = null;
    if (job.status === "done") {
      const partial = job.failed_chunks ? t("hb.partial", { n: job.failed_chunks }) : "";
      showBanner(t("hb.loaded", { range: esc(range), n: job.detections.toLocaleString(locale()), partial }), job.failed_chunks ? "warn" : "ok");
      keepBannerUntil = Date.now() + 8000;
      window.setTimeout(() => void checkCoverage(), 8200);
      void renderCalendar();
      await refresh(); // re-checks coverage; anything NASA didn't have stays flagged, without looping
    } else {
      showBanner(t("hb.status", { status: esc(job.status), msg: esc(job.errors.at(-1) ?? t("hb.noData")) }), "warn", true);
    }
  };
  void tick();
}

$("#coverage-load").addEventListener("click", async () => {
  const view = globe.viewBbox();
  if (!view || view[0] > view[2] || viewSpanDeg(view) > MAX_LOAD_SPAN_DEG) {
    return toast(t("hb.zoomFirst", { deg: MAX_LOAD_SPAN_DEG }), "error");
  }
  const start = dateInput("start").value;
  const end = dateInput("end").value;
  const days = Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000) + 1;
  if (days > 400) return toast(t("hb.max400"), "error");
  await startCoverageLoad(start, end, snapBbox(view));
});

const NO_DETECTIONS = { count: 0, id: [], lat: [], lon: [], frp: [], conf: [], viirs: [], t: [] };
let refreshSeq = 0; // a newer refresh discards older responses
let eventsInView = 0;

/** Server data epoch: tiles validated in an older epoch are re-checked (ETag) when shown. */
async function syncEpoch(): Promise<void> {
  try {
    const meta = await api.tilesMeta();
    globe.tiles.setEpoch(timeMode === "latest" ? `${meta.data_version}:${meta.hour}` : `${meta.data_version}`);
  } catch {
    /* keep the current tiles */
  }
}

let errorShown = 0;
globe.tiles.onError = (msg) => {
  if (Date.now() - errorShown < 10_000) return; // one toast for a burst of failed tiles
  errorShown = Date.now();
  toast(t("toast.tiles", { msg }), "error");
};
let statsTimer: number | undefined;
globe.tiles.onChange = () => {
  // a timer, not requestAnimationFrame: rAF is paused in background tabs and the stats went stale
  window.clearTimeout(statsTimer);
  statsTimer = window.setTimeout(renderTileStats, 50);
};
let currentLod: ReturnType<typeof globe.lod> | null = null;

function renderTileStats(): void {
  if (focusEventId) return;
  const bbox = globe.focusBbox();
  if (!bbox || !currentLod) return renderStats(0, eventsInView);
  const st = globe.tiles.stats(bbox);
  const note = st.loading
    ? tn("stats.loadingTiles", st.loading)
    : currentLod.kind === "cells"
      ? t("stats.grouped", { cell: fmtCell(currentLod.cellDeg!) })
      : st.truncated ? t("stats.dense") : "";
  renderStats(st.detections, eventsInView, note, currentLod.kind === "cells");
}

const fmtCell = (deg: number) => (deg >= 1 ? `${+deg.toFixed(2)}°` : `${Math.round(deg * 111)} km`);

/** Camera moved or zoomed: pick the level of detail and show its tiles. Only tiles never loaded
 * for the current filters are requested. */
function updateView(): void {
  if (focusEventId) return;
  const bbox = globe.focusBbox();
  if (!bbox) return;
  // what the tiles already on screen say about this area decides between dots and bubbles
  const known = globe.tiles.stats(bbox);
  currentLod = globe.lod(bbox, known.detections > 0 || globe.tiles.shownCount ? known.detections : null);
  globe.tiles.view(currentLod, bbox);
  void countEvents(bbox);
  void checkCoverage();
}

async function countEvents(bbox: [number, number, number, number]): Promise<void> {
  const seq = refreshSeq;
  const p = tileParams();
  const { hours, ...rest } = p;
  const time = hours ? { start: new Date(Date.now() - Number(hours) * 3_600_000).toISOString() } : {};
  try {
    const sens = sensors();
    if (!sens.viirs && !sens.modis) eventsInView = 0;
    else {
      const instrument = sens.viirs && sens.modis ? undefined : sens.viirs ? "VIIRS" : "MODIS";
      const r = await api.eventsCount({ ...rest, ...time, bbox: bbox.join(","), instrument, satellite: undefined, daynight: undefined, include_static: undefined, max_frp: undefined });
      if (seq !== refreshSeq) return;
      eventsInView = r.count;
    }
    renderTileStats();
  } catch {
    /* the count is informative only */
  }
}

/** Filters, time window or mode changed: switch the tile query (tiles of earlier queries stay cached). */
async function refresh(): Promise<void> {
  const seq = ++refreshSeq;
  try {
    if (focusEventId) {
      // Focus mode shows one fire's complete history, regardless of the view and filters.
      globe.tiles.hideAll();
      const dets = await api.detectionsCompact({ event_id: focusEventId, limit: MAX_POINTS });
      if (seq !== refreshSeq) return;
      globe.showDetections(dets);
      globe.showEvents([]);
      renderStats(dets.count, 1);
      return;
    }
    globe.showDetections(NO_DETECTIONS);
    globe.showEvents([]);
    globe.tiles.setQuery(tileParams());
    globe.tiles.setSensors(sensors());
    await syncEpoch();
    if (seq !== refreshSeq) return;
    updateView();
  } catch (err) {
    toast(t("toast.data", { msg: (err as Error).message }), "error");
  }
}

let lastStats: [number, number, string, boolean] = [0, 0, "", false];
function renderStats(dets: number, events: number, note = "", approx = false): void {
  lastStats = [dets, events, note, approx];
  $("#stats").innerHTML = `
    <div><b>${approx && dets ? "≈ " : ""}${dets.toLocaleString(locale())}</b><span>${t("stats.detections")}</span></div>
    <div><b>${events.toLocaleString(locale())}</b><span>${t("stats.events")}</span></div>
    ${focusEventId ? `<button type="button" id="clear-focus" class="secondary small">${t("stats.showAll")}</button>` : ""}
    ${note ? `<p class="stats-note">${esc(note)}</p>` : ""}`;
  document.getElementById("clear-focus")?.addEventListener("click", () => {
    focusEventId = null;
    void refresh();
  });
}

filtersForm.querySelectorAll<HTMLInputElement>('input[name="instrument"]').forEach((box) =>
  box.addEventListener("change", () => {
    globe.tiles.setSensors(sensors()); // redraw from memory: tiles carry MODIS and VIIRS separately
    const bbox = globe.viewBbox();
    if (bbox && !focusEventId) void countEvents(bbox);
  }),
);

// Filters apply as they change (number fields after a short pause in typing).
function applyFilters(): void {
  focusEventId = null;
  const f = new FormData(filtersForm);
  const lat = Number(f.get("lat"));
  const lon = Number(f.get("lon"));
  const r = Number(f.get("radius_km"));
  globe.pin(f.get("lat") ? lat : null, f.get("lon") ? lon : null, r || undefined);
  renderActiveFilters();
  void refresh();
}
const HANDLED_ELSEWHERE = new Set(["instrument", "start", "end"]);
let typingTimer: number | undefined;
filtersForm.addEventListener("submit", (e) => {
  e.preventDefault();
  applyFilters();
});
filtersForm.addEventListener("change", (e) => {
  const el = e.target as HTMLInputElement;
  if (HANDLED_ELSEWHERE.has(el.name)) return renderActiveFilters();
  window.clearTimeout(typingTimer);
  applyFilters();
});
filtersForm.addEventListener("input", (e) => {
  const el = e.target as HTMLInputElement;
  if (el.type !== "number") return;
  window.clearTimeout(typingTimer);
  typingTimer = window.setTimeout(applyFilters, 600);
});
filtersForm.addEventListener("reset", () => {
  window.setTimeout(() => {
    globe.pin(null, null);
    renderActiveFilters();
    setTimeMode("latest");
  });
});

// "Hide low-confidence detections": a shortcut for the Confidence filter (Nominal and high).
const hideLow = $<HTMLInputElement>("#hide-low");
hideLow.addEventListener("change", (e) => {
  e.stopPropagation(); // not a filter field itself: the select change below applies it
  const sel = filtersForm.querySelector<HTMLSelectElement>('select[name="min_confidence"]')!;
  sel.value = hideLow.checked ? "nominal" : "";
  sel.dispatchEvent(new Event("change", { bubbles: true }));
});

/** What is currently narrowing the map, as removable chips under the stats. */
function renderActiveFilters(): void {
  const f = new FormData(filtersForm);
  const v = (k: string) => ((f.get(k) as string | null) ?? "").trim();
  hideLow.checked = v("min_confidence") !== ""; // in step with the select, the chips and Reset
  const chips: [string, string[]][] = [];
  const conf = v("min_confidence");
  const range = (lo: string, hi: string) => `${lo ? `≥ ${lo}` : ""}${lo && hi ? t("chip.and") : ""}${hi ? `≤ ${hi}` : ""}`;
  if (conf) chips.push([conf === "high" ? t("chip.high") : t("chip.nominal"), ["min_confidence"]]);
  const ins = f.getAll("instrument");
  if (ins.length === 1) chips.push([t("chip.only", { what: String(ins[0]) }), ["instrument"]]);
  if (ins.length === 0) chips.push([t("chip.noSensor"), ["instrument"]]);
  const sats = f.getAll("satellite") as string[];
  if (sats.length) chips.push([t("chip.sats", { list: sats.join(", ") }), ["satellite"]]);
  if (v("daynight")) chips.push([v("daynight") === "D" ? t("chip.day") : t("chip.night"), ["daynight"]]);
  if (v("min_frp") || v("max_frp")) chips.push([t("chip.frp", { range: range(v("min_frp"), v("max_frp")) }), ["min_frp", "max_frp"]]);
  if (v("min_area") || v("max_area")) chips.push([t("chip.area", { range: range(v("min_area"), v("max_area")) }), ["min_area", "max_area"]]);
  if (v("min_detections")) chips.push([t("chip.minDet", { n: v("min_detections") }), ["min_detections"]]);
  if (v("status")) chips.push([v("status") === "active" ? t("chip.active") : t("chip.inactive"), ["status"]]);
  if (v("lat") && v("lon") && v("radius_km")) chips.push([t("chip.near", { r: v("radius_km"), lat: Number(v("lat")).toFixed(2), lon: Number(v("lon")).toFixed(2) }), ["lat", "lon", "radius_km"]]);
  const box = $("#active-filters");
  box.classList.toggle("hidden", chips.length === 0);
  box.innerHTML = chips.length
    ? `<span class="af-label">${t("chip.label")}</span>${chips.map(([label, fields]) =>
        `<button type="button" class="af-chip" data-clear="${fields.join(",")}" title="${esc(t("chip.remove"))}">${esc(label)} <span aria-hidden="true">✕</span></button>`).join("")}
       <button type="button" class="af-clear">${t("chip.clear")}</button>`
    : "";
}
$("#active-filters").addEventListener("click", (e) => {
  const t = e.target as HTMLElement;
  if (t.closest(".af-clear")) return filtersForm.reset();
  const fields = t.closest<HTMLElement>("[data-clear]")?.dataset.clear?.split(",");
  if (!fields) return;
  for (const name of fields) {
    filtersForm.querySelectorAll<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`).forEach((el) => {
      if (el instanceof HTMLInputElement && el.type === "checkbox") el.checked = name === "instrument";
      else if (el instanceof HTMLSelectElement) el.value = "";
      else el.value = "";
    });
  }
  if (fields.includes("instrument")) {
    filtersForm.querySelector<HTMLInputElement>('input[name="instrument"]')?.dispatchEvent(new Event("change", { bubbles: true }));
  }
  applyFilters();
});
$("#pick-point").addEventListener("click", () => setPicking(true));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    setPicking(false);
    closeDetails();
  }
});

// ---------------------------------------------------------------- tabs & layers
document.querySelectorAll<HTMLButtonElement>(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
    if (tab.dataset.tab === "calendar") void renderCalendar();
    if (tab.dataset.tab === "analysis") describeAnalysisScope();
    if (tab.dataset.tab === "alerts") void renderAlerts();
    if (tab.dataset.tab === "guide") mountGuideTab($("#tab-guide"));
  }),
);
document.querySelectorAll<HTMLInputElement>('input[name="base"]').forEach((r) =>
  r.addEventListener("change", () => globe.setBaseLayer(r.value as BaseLayerId)),
);
const gibsDate = $<HTMLInputElement>("#gibs-date");
gibsDate.value = isoDay(daysAgo(1));
gibsDate.max = isoDay(new Date());
gibsDate.addEventListener("change", () => gibsDate.value && globe.setGibsDate(gibsDate.value));
const toggle = (id: string, fn: (v: boolean) => void) =>
  $<HTMLInputElement>(id).addEventListener("change", (e) => fn((e.target as HTMLInputElement).checked));
toggle("#lyr-detections", (v) => globe.setDetectionsVisible(v));
toggle("#lyr-events", (v) => globe.setEventsVisible(v));
toggle("#lyr-zones", (v) => globe.setZonesVisible(v));
toggle("#lyr-ndvi", (v) => globe.setNdviVisible(v));
toggle("#lyr-static", () => void refresh());

// ---------------------------------------------------------------- analysis
/** The analysis uses the period chosen under Filters -> Time and the area the camera shows. */
function analysisTime(): Params {
  if (timeMode === "latest") return { hours: latestHours };
  const { start, end } = timeRange();
  // History days are inclusive: the period ends at the start of the next day
  return { start, end: end ? new Date(new Date(end).getTime() + 1000).toISOString() : undefined };
}

function describeAnalysisScope(): void {
  const period = timeMode === "latest"
    ? t("a.periodLast", { span: latestHours >= 48 ? t("a.periodDays", { n: latestHours / 24 }) : t("a.periodHours", { n: latestHours }) })
    : dateInput("start").value === dateInput("end").value ? dateInput("start").value : t("a.periodRange", { from: dateInput("start").value, to: dateInput("end").value });
  $("#analysis-scope").textContent = t("a.scope", { period });
}

let analysisKind: "stats" | "sensors" = "stats";
$("#analysis-kind").addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-kind]");
  if (!btn) return;
  analysisKind = btn.dataset.kind as typeof analysisKind;
  document.querySelectorAll("#analysis-kind button").forEach((b) => b.classList.toggle("active", b === btn));
  clearAnalysis();
  $("#analysis-body").innerHTML = "";
});
$("#analysis-run").addEventListener("click", () => {
  describeAnalysisScope();
  // the MODIS vs VIIRS comparison is part of the advanced view
  const run = analysisKind === "sensors" && !isSimple() ? runSensors : runAnalysis;
  void run(globe.visibleBbox(), analysisTime());
});
wireAnalysis((id) => void openEvent(id));

// ---------------------------------------------------------------- calendar
// A contribution-style heatmap of WEEKS weeks (see heatmap.ts); the last column is the current week.
// Scope: the whole world, or the area the globe showed when "This area" was chosen. Selection: one
// day (click) or a period (drag across days); it sets the map's History period.
let calSel: { from: string; to: string } | null = null;
const latestFirst = () => addDays(weekStart(new Date()), -7 * (WEEKS - 1));
let calFirst = latestFirst();
let calDays: CalendarDay[] = []; // what the heatmap shows, kept to redraw on language changes
let calNoData = new Set<string>(); // days with no data loaded (worldwide: no detection at all; area: coverage)
let calScope: "world" | "area" = "world";
let calBox: [number, number, number, number] | null = null;
let calSeq = 0;

const longDay = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString(locale(), { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
const shortDay = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString(locale(), { day: "numeric", month: "short", timeZone: "UTC" });
const fmtCompact = (n: number) => n.toLocaleString(locale(), { notation: n >= 10_000 ? "compact" : "standard", maximumFractionDigits: 1 });
const daysBetween = (a: string, b: string) => Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000) + 1;

function drawCalendar(): void {
  const last = addDays(calFirst, 7 * WEEKS - 1);
  const fmt = (d: Date, year: boolean) => d.toLocaleDateString(locale(), { month: "short", ...(year ? { year: "numeric" } : {}), timeZone: "UTC" });
  $("#cal-title").textContent = `${fmt(calFirst, calFirst.getUTCFullYear() !== last.getUTCFullYear())} – ${fmt(last, true)}`;
  $<HTMLButtonElement>("#cal-next").disabled = calFirst.getTime() >= latestFirst().getTime();
  document.querySelectorAll<HTMLButtonElement>("#cal-scope [data-scope]").forEach((b) => b.classList.toggle("active", b.dataset.scope === calScope));
  const areaLine = $("#cal-area");
  areaLine.classList.toggle("hidden", calScope !== "area");
  if (calScope === "area" && calBox) {
    areaLine.innerHTML = `${esc(t("hm.areaIs", { area: boxLabel(calBox) }))} <button type="button" class="more-link" id="cal-use-view">${t("hm.useView")}</button>`;
  }
  $("#cal-hint").textContent = t(calScope === "area" ? "c.hintArea" : "c.hint");
  $("#calendar").innerHTML = renderHeatmap({ first: calFirst, days: calDays, selected: calSel, noData: calNoData });
  drawSelection();
  const s = periodSummary(calDays);
  $("#cal-summary").innerHTML = `
    <h3>${t("hm.periodTitle")}</h3>
    <div class="s-tiles three">
      <div class="s-tile accent"><div class="k">${t("hm.total")}</div><div class="v">${fmtCompact(s.total)}</div><div class="sub">${t("hm.totalSub")}</div></div>
      <div class="s-tile"><div class="k">${t("hm.newFires")}</div><div class="v">${fmtCompact(s.fires)}</div><div class="sub">${t("hm.newFiresSub")}</div></div>
      <div class="s-tile"><div class="k">${t("hm.active")}</div><div class="v">${s.active}</div><div class="sub">${t("hm.activeSub", { n: calDays.length })}</div></div>
    </div>
    ${s.busiest ? `<button type="button" class="more-link" data-date="${s.busiest.date}">${t("hm.busiest", { day: longDay(s.busiest.date), n: s.busiest.detections.toLocaleString(locale()) })} →</button>` : ""}`;
}

/** The card for the selected day or period: its totals, actions and largest fires. */
function drawSelection(): void {
  const box = $("#cal-selection");
  if (!calSel) {
    box.innerHTML = "";
    $("#cal-day-title").textContent = "";
    $("#cal-events").innerHTML = "";
    return;
  }
  const { from, to } = calSel;
  const inSel = calDays.filter((d) => d.date >= from && d.date <= to);
  const s = periodSummary(inSel);
  const n = daysBetween(from, to);
  const noData = inSel.filter((d) => calNoData.has(d.date)).length;
  box.innerHTML = `
    <div class="s-card cal-sel">
      <div class="cal-sel-head"><div><div class="s-big">${from === to ? esc(longDay(from)) : `${esc(shortDay(from))} – ${esc(longDay(to))}`}</div>
        <p>${esc(tn("hm.days", n))}${calScope === "area" ? ` · ${esc(t("hm.inArea"))}` : ""}</p></div>
        <button type="button" class="icon" id="cal-clear" aria-label="${esc(t("hm.clear"))}" title="${esc(t("hm.clear"))}">×</button></div>
      <div class="s-tiles three">
        <div class="s-tile accent"><div class="k">${t("hm.total")}</div><div class="v">${fmtCompact(s.total)}</div></div>
        <div class="s-tile"><div class="k">${t("hm.newFires")}</div><div class="v">${fmtCompact(s.fires)}</div></div>
        <div class="s-tile"><div class="k">${t("hm.peakTile")}</div><div class="v">${s.busiest ? `${fmtNum(Math.max(...inSel.map((d) => d.max_frp_mw ?? 0)), 0)}<small>MW</small>` : "–"}</div></div>
      </div>
      ${noData ? `<p class="hint">${esc(tn("hm.selNoData", noData))}</p>` : ""}
      <div class="actions"><button type="button" class="primary-wide" id="cal-analyse"><svg class="ic"><use href="#i-analysis" /></svg>${t("hm.analyse")}</button></div>
      <p class="hint">${t("hm.onMap")}</p>
    </div>`;
  $("#cal-day-title").textContent = from === to ? t("c.dayTitle", { day: longDay(from) }) : t("hm.rangeTitle");
}

function boxLabel(b: number[]): string {
  const lon = (x: number) => `${Math.abs(x).toFixed(1)}°${x < 0 ? t("an.W") : t("an.E")}`;
  const lat = (y: number) => `${Math.abs(y).toFixed(1)}°${y < 0 ? t("an.S") : t("an.N")}`;
  return t("an.box", { w: lon(b[0]), e: lon(b[2]), s: lat(b[1]), n: lat(b[3]) });
}

async function renderCalendar(): Promise<void> {
  const mine = ++calSeq;
  const last = addDays(calFirst, 7 * WEEKS - 1);
  const today = new Date();
  const start = isoDay(calFirst);
  const end = isoDay(last < today ? last : today);
  const bbox = calScope === "area" && calBox ? calBox.join(",") : undefined;
  try {
    const [days, cov] = await Promise.all([
      api.calendar({ start, end, bbox }),
      bbox ? api.coverage({ start, end, bbox }) : Promise.resolve(null),
    ]);
    if (mine !== calSeq) return;
    calDays = days;
    // worldwide, satellites always see fire somewhere: a zero is a day that is not loaded;
    // in an area a zero can be a quiet day, so the coverage records decide
    calNoData = cov
      ? new Set(cov.days.filter((d) => d.status !== "loaded").map((d) => d.date))
      : new Set(days.filter((d) => d.detections === 0).map((d) => d.date));
  } catch (err) {
    if (mine !== calSeq) return;
    calDays = [];
    calNoData = new Set();
    toast(t("c.calFailed", { msg: (err as Error).message }), "error");
  }
  drawCalendar();
}

/** Show a day or a period: on the map (History mode), in the card, with its largest fires. */
async function selectCalendar(from: string, to: string): Promise<void> {
  if (to < from) [from, to] = [to, from];
  calSel = { from, to };
  setHistoryRange(from, to);
  setTimeMode("history");
  drawCalendar();
  const mine = ++calSeq;
  try {
    const events = await api.events({ start: `${from}T00:00:00Z`, end: `${to}T23:59:59Z`, sort: "area", limit: 15,
      bbox: calScope === "area" && calBox ? calBox.join(",") : undefined });
    if (mine !== calSeq) return;
    $("#cal-events").innerHTML = events.length
      ? events.map((ev) => `<li><a href="#" data-event="${esc(ev.id)}">
          <b>${fmtNum(ev.area_km2, 2)} km²</b> · ${ev.detection_count} ${t("c.det")} · max ${fmtNum(ev.max_frp_mw, 0)} MW
          <small>${esc(shortDay(ev.start_at.slice(0, 10)))} · ${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)} · ${statePill(ev)}</small></a></li>`).join("")
      : `<li class="empty">${t("c.noEvents")}</li>`;
  } catch (err) {
    toast((err as Error).message, "error");
  }
}

function setCalendarScope(scope: "world" | "area"): void {
  if (scope === "area") {
    const view = globe.viewBbox();
    // a view of (nearly) the whole globe is the worldwide calendar
    if (!view || (view[2] - view[0] + 360) % 360 > 300 || view[3] - view[1] > 150) {
      toast(t("hm.zoomIn"));
      scope = "world";
    } else {
      calBox = view;
    }
  }
  calScope = scope;
  calSel = null;
  void renderCalendar();
}

// hover: the same tooltip as the charts
$("#calendar").addEventListener("mousemove", (e) => {
  const tip = $("#chart-tip");
  const cell = (e.target as Element).closest<SVGElement>("[data-tip]");
  if (!cell || drag) return tip.classList.add("hidden");
  const [head, ...rows] = (cell.dataset.tip ?? "").split("|");
  tip.innerHTML = `<b>${esc(head)}</b>${rows.map((r) => `<div>${esc(r)}</div>`).join("")}`;
  tip.classList.remove("hidden");
  tip.style.left = `${Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${e.clientY + 14}px`;
});
$("#calendar").addEventListener("mouseleave", () => $("#chart-tip").classList.add("hidden"));

// select: press on a day, drag over others (mouse or finger), release; a press without a drag is one day
let drag: { from: string; to: string } | null = null;
const dayAt = (x: number, y: number) => document.elementFromPoint(x, y)?.closest<SVGElement>(".heatmap [data-date]")?.dataset.date ?? null;
function markDrag(): void {
  if (!drag) return;
  const [a, b] = drag.from <= drag.to ? [drag.from, drag.to] : [drag.to, drag.from];
  document.querySelectorAll<SVGElement>(".heatmap [data-date]").forEach((r) => {
    const d = r.dataset.date!;
    r.classList.toggle("sel", d >= a && d <= b);
  });
}
$("#calendar").addEventListener("pointerdown", (e) => {
  const d = dayAt(e.clientX, e.clientY);
  if (!d) return;
  e.preventDefault();
  drag = { from: d, to: d };
  markDrag();
  $("#chart-tip").classList.add("hidden");
});
window.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const d = dayAt(e.clientX, e.clientY);
  if (d && d !== drag.to) {
    drag.to = d;
    markDrag();
  }
});
window.addEventListener("pointerup", () => {
  if (!drag) return;
  const { from, to } = drag;
  drag = null;
  void selectCalendar(from, to);
});

$("#cal-summary").addEventListener("click", (e) => {
  const day = (e.target as HTMLElement).closest<HTMLElement>("[data-date]")?.dataset.date;
  if (day) void selectCalendar(day, day);
});
$("#cal-selection").addEventListener("click", (e) => {
  const el = e.target as HTMLElement;
  if (el.closest("#cal-clear")) {
    calSel = null;
    drawCalendar();
  } else if (el.closest("#cal-analyse")) {
    document.querySelector<HTMLButtonElement>('.tab[data-tab="analysis"]')?.click();
    $<HTMLButtonElement>("#analysis-run").click();
  }
});
$("#cal-area").addEventListener("click", (e) => {
  if ((e.target as HTMLElement).closest("#cal-use-view")) setCalendarScope("area");
});
$("#cal-scope").addEventListener("click", (e) => {
  const s = (e.target as HTMLElement).closest<HTMLElement>("[data-scope]")?.dataset.scope;
  if (s === "world" || s === "area") setCalendarScope(s);
});
$("#cal-events").addEventListener("click", (e) => {
  const id = (e.target as HTMLElement).closest<HTMLElement>("[data-event]")?.dataset.event;
  if (id) {
    e.preventDefault();
    void openEvent(id);
  }
});
$("#cal-prev").addEventListener("click", () => {
  calFirst = addDays(calFirst, -7 * WEEKS);
  void renderCalendar();
});
$("#cal-next").addEventListener("click", () => {
  const next = addDays(calFirst, 7 * WEEKS);
  calFirst = next.getTime() > latestFirst().getTime() ? latestFirst() : next;
  void renderCalendar();
});

// ---------------------------------------------------------------- alerts
async function renderAlerts(): Promise<void> {
  try {
    const [rules, alerts] = await Promise.all([api.rules(), api.alerts()]);
    globe.showAlertZones(rules);
    $("#rules").innerHTML = rules.length
      ? rules.map((r) => `<li><div><b>${esc(r.name)}</b>
          <small>${r.radius_km ? t("al.around", { r: fmtNum(r.radius_km, 0), lat: r.center_lat?.toFixed(2) ?? "", lon: r.center_lon?.toFixed(2) ?? "" }) : t("al.box")}
          · ${t("al.ruleMeta", { c: esc(r.min_confidence), a: r.min_area_km2 })}${r.webhook_url ? " · webhook" : ""}</small></div>
          <button type="button" class="icon" data-delete-rule="${r.id}" aria-label="${esc(t("al.delete"))}">🗑</button></li>`).join("")
      : `<li class="empty">${t("al.noRules")}</li>`;
    $("#alerts").innerHTML = alerts.length
      ? alerts.map((a) => `<li class="sev-${a.severity}"><a href="#" data-event="${esc(a.event_id)}">
          <span class="pill ${a.severity}">${esc(t(`sev.${a.severity}` as "sev.info"))}</span> ${esc(a.message)}
          <small>${esc(fmtTime(a.triggered_at))}</small></a></li>`).join("")
      : `<li class="empty">${t("al.noAlerts")}</li>`;
  } catch (err) {
    toast(t("al.failed", { msg: (err as Error).message }), "error");
  }
}

$<HTMLFormElement>("#rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target as HTMLFormElement;
  const f = new FormData(form);
  const ff = new FormData(filtersForm);
  const rule: AlertRuleIn = {
    name: String(f.get("name")),
    min_confidence: f.get("min_confidence") as AlertRuleIn["min_confidence"],
    min_area_km2: Number(f.get("min_area_km2") || 0),
    min_frp_mw: Number(f.get("min_frp_mw") || 0),
    min_detections: Number(f.get("min_detections") || 1),
    webhook_url: (f.get("webhook_url") as string) || null,
    enabled: true,
  };
  if (ff.get("lat") && ff.get("lon") && ff.get("radius_km")) {
    Object.assign(rule, { center_lat: Number(ff.get("lat")), center_lon: Number(ff.get("lon")), radius_km: Number(ff.get("radius_km")) });
  } else {
    const bbox = globe.viewBbox();
    if (!bbox) return toast(t("al.pointCamera"), "error");
    const [w, s, e, n] = bbox;
    const width = e >= w ? e - w : e - w + 360; // view may cross the antimeridian
    if (width > MAX_ZONE_DEG || n - s > MAX_ZONE_DEG) {
      return toast(t("al.zoomIn", { w: Math.round(width), max: MAX_ZONE_DEG }), "error");
    }
    [rule.west, rule.south, rule.east, rule.north] = bbox;
  }
  try {
    await api.createRule(rule);
    form.reset();
    toast(t("al.created"));
    await renderAlerts();
  } catch (err) {
    toast(t("al.createFailed", { msg: (err as Error).message }), "error");
  }
});
$("#rules").addEventListener("click", async (e) => {
  const id = (e.target as HTMLElement).closest<HTMLElement>("[data-delete-rule]")?.dataset.deleteRule;
  if (!id) return;
  await api.deleteRule(Number(id));
  await renderAlerts();
});
$("#alerts").addEventListener("click", (e) => {
  const id = (e.target as HTMLElement).closest<HTMLElement>("[data-event]")?.dataset.event;
  if (id) {
    e.preventDefault();
    void openEvent(id);
  }
});

// ---------------------------------------------------------------- history (FIRMS archive)
const historyForm = $<HTMLFormElement>("#history-form");
let health: Health | null = null;
let jobTimer: number | undefined;

async function loadCountries(): Promise<void> {
  const select = historyForm.elements.namedItem("country") as HTMLSelectElement;
  const countries = await api.countries().catch(() => []);
  if (!countries.length) return;
  select.innerHTML = countries
    .map((c) => `<option value="${esc(c.code)}"${c.code === "ARG" ? " selected" : ""}>${esc(c.name)}</option>`)
    .join("");
}

function renderHistoryAvailability(): void {
  const coverage = $("#history-coverage");
  const enabled = !!health?.history_enabled;
  $("#history-controls").classList.toggle("hidden", !enabled);
  if (!health) return;
  if (health.data_source === "demo") {
    coverage.textContent = t("c.demo");
  } else if (!enabled) {
    coverage.innerHTML = t("c.noKey", { region: esc(health.public_region.replaceAll("_", " ")) });
  } else {
    coverage.textContent = t("c.withKey");
  }
}

function renderJob(job: BackfillJob): void {
  const box = $("#history-job");
  box.classList.remove("hidden");
  $(".bar", box).style.width = `${Math.round(job.progress * 100)}%`;
  const running = job.status === "queued" || job.status === "running";
  const parts = [
    t("c.jobLine", { status: esc(job.status), done: job.done_chunks, total: job.total_chunks || "?", n: job.detections.toLocaleString(locale()) }),
    job.current ? esc(job.current) : "",
    ...job.notes.map(esc),
    ...job.errors.slice(-3).map((e) => `<span class="err">${esc(e)}</span>`),
  ].filter(Boolean);
  $(".job-text", box).innerHTML = parts.join("<br>");
  $("#history-cancel").classList.toggle("hidden", !running);
  $<HTMLButtonElement>("#history-start").disabled = running;
}

function watchJob(id: string): void {
  window.clearTimeout(jobTimer);
  const tick = async () => {
    try {
      const job = await api.job(id);
      renderJob(job);
      if (job.status === "queued" || job.status === "running") {
        jobTimer = window.setTimeout(tick, 2000);
        return;
      }
      if (job.status === "done") {
        const partial = job.failed_chunks ? t("c.partial", { failed: job.failed_chunks, total: job.total_chunks }) : "";
        toast(t("c.loaded", { n: job.detections.toLocaleString(locale()), partial }), job.failed_chunks ? "error" : "info");
      } else if (job.status === "failed") {
        toast(t("c.failed", { msg: job.errors.at(-1) ?? t("hb.noData") }), "error");
      }
      await Promise.all([renderCalendar(), refresh()]);
    } catch (err) {
      toast(t("c.lost", { msg: (err as Error).message }), "error");
    }
  };
  void tick();
}

historyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(historyForm);
  const body: { start: string; end: string; bbox?: string; country?: string } = {
    start: String(f.get("start")),
    end: String(f.get("end")),
  };
  if (f.get("scope") === "view") {
    const bbox = globe.viewBbox();
    if (!bbox || bbox[0] >= bbox[2]) return toast(t("c.pointCamera"), "error");
    body.bbox = bbox.join(",");
  } else {
    body.country = String(f.get("country") || "ARG");
  }
  try {
    const job = await api.backfill(body);
    renderJob(job);
    watchJob(job.id);
    // land the calendar on the period being loaded (it starts in the first column)
    const loadedFirst = weekStart(new Date(`${body.start}T00:00:00Z`));
    calFirst = loadedFirst.getTime() > latestFirst().getTime() ? latestFirst() : loadedFirst;
  } catch (err) {
    toast(t("c.startFailed", { msg: (err as Error).message }), "error");
  }
});
$("#history-cancel").addEventListener("click", async () => {
  const jobs = await api.jobs().catch(() => [] as BackfillJob[]);
  const running = jobs.find((j) => j.status === "running" || j.status === "queued");
  if (running) renderJob(await api.cancelJob(running.id));
});

// ---------------------------------------------------------------- boot
const SOURCE_BADGE: Record<Health["data_source"], ["badge.demo" | "badge.public" | "badge.api", "badge.demoTitle" | "badge.publicTitle" | "badge.apiTitle"]> = {
  demo: ["badge.demo", "badge.demoTitle"],
  firms_public: ["badge.public", "badge.publicTitle"],
  firms_api: ["badge.api", "badge.apiTitle"],
};

async function loadHealth(): Promise<void> {
  health = await api.health();
  if (health.event_active_hours) setActiveHours(health.event_active_hours);
  renderBadge();
  renderHistoryAvailability();
  renderFreshness();
}

function renderBadge(): void {
  if (!health) return;
  const badge = $("#mode-badge");
  const [label, title] = SOURCE_BADGE[health.data_source];
  badge.textContent = health.seeding ? t("badge.loading") : t(label);
  badge.classList.toggle("demo", health.data_source === "demo");
  badge.classList.toggle("seeding", health.seeding);
  badge.title = `${t(title)} ${t("badge.lastIngest", { when: health.last_ingest ? fmtTime(health.last_ingest) : t("badge.never") })}`;
}

wirePrefs();
wireDetails((id) => void openEvent(id));
wireHelp();
wireLayout(() => globe.resize());
setSpreadDrawer((grid) => void globe.showSpread(grid));
mountLegend($("#globe"));
mountGuide($("#globe"), $("#guide-open"));
globe.onHover(showMapTip);
renderActiveFilters();
// language or view changed: everything this file draws is drawn again from what it last showed
/** The globe's zoom buttons are drawn by globe.ts; only their labels follow the language. */
function labelZoomButtons(): void {
  document.querySelectorAll<HTMLElement>("[data-zoom]").forEach((b) => {
    const label = t(b.dataset.zoom === "in" ? "app.zoomIn" : "app.zoomOut");
    b.title = label;
    b.setAttribute("aria-label", label);
  });
}
labelZoomButtons();
onPrefsChange(() => {
  labelZoomButtons();
  renderStats(...lastStats);
  renderTileStats();
  renderActiveFilters();
  renderFreshness();
  renderBadge();
  renderHistoryAvailability();
  describeAnalysisScope();
  const tab = document.querySelector<HTMLElement>(".tab.active")?.dataset.tab;
  if (tab === "calendar") drawCalendar();
  if (tab === "alerts") void renderAlerts();
  if (tab === "guide") mountGuideTab($("#tab-guide"));
});
describeAnalysisScope();
(historyForm.elements.namedItem("end") as HTMLInputElement).value = isoDay(daysAgo(1));
(historyForm.elements.namedItem("start") as HTMLInputElement).value = isoDay(daysAgo(30));
(async () => {
  try {
    await loadHealth();
    // First start on real data: the API downloads the last 7 days in the background.
    while ((health as Health | null)?.seeding) {
      await new Promise((r) => setTimeout(r, 3000));
      await loadHealth();
    }
    const running = (await api.jobs().catch(() => [] as BackfillJob[])).find((j) => j.status === "running" || j.status === "queued");
    if (running) watchJob(running.id);
    if ((health as Health | null)?.history_enabled) await loadCountries();
  } catch {
    toast(t("toast.backend"), "error");
  }
  await refresh();
  booted = true;
  const rules = await api.rules().catch(() => []);
  globe.showAlertZones(rules);
})();
