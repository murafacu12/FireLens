import "./styles.css";

import { api, type AlertRuleIn, type BackfillJob, type CalendarDay, type Health, type Params } from "./api";
import { runAnalysis, runSensors, wireAnalysis } from "./analysis";
import { closeDetails, setActiveHours, showDetection, showEvent, showPoint, statePill, wireDetails } from "./details";
import { setSpreadDrawer } from "./firereport";
import { Globe, type BaseLayerId, type PickResult } from "./globe";
import { helpButton, mountGuide, mountLegend, showMapTip, wireHelp } from "./guide";
import { revealMap, wireLayout } from "./layout";
import { $, daysAgo, esc, fmtNum, fmtTime, isoDay, toast } from "./util";

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
    api.detection(r.id).then(showDetection, (err: Error) => toast(`Could not load detection: ${err.message}`, "error"));
  }
  else showPoint(r.lat, r.lon);
}

function setPicking(on: boolean): void {
  picking = on;
  if (on) revealMap(); // on phones the sheet would cover the globe the user must tap
  $("#picking-hint").classList.toggle("hidden", !on);
  $("#globe").classList.toggle("crosshair", on);
}

async function openEvent(id: string): Promise<void> {
  revealMap();
  try {
    const ev = await api.event(id);
    showEvent(ev, (eventId) => {
      focusEventId = eventId;
      void refresh();
    });
    globe.flyToBox(ev.west, ev.south, ev.east, ev.north);
  } catch (err) {
    toast(`Could not load event: ${(err as Error).message}`, "error");
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
  btn.textContent = "Fetching from NASA…";
  try {
    const report = (await api.ingest({})) as { stored: number; sources: { source: string; error: string | null }[] };
    const failed = report.sources.filter((s) => s.error).map((s) => s.source);
    toast(`NASA FIRMS: ${report.stored.toLocaleString()} detections refreshed${failed.length ? ` (failed: ${failed.join(", ")})` : ""}.`,
      failed.length ? "error" : "info");
    await loadHealth();
    await syncEpoch();
  } catch (err) {
    toast(`Fetch failed: ${(err as Error).message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Fetch newest from NASA now";
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
    el.textContent = "No detections loaded yet.";
    return;
  }
  const ageMin = Math.max(0, Math.round((Date.now() - new Date(health.latest_detection_at).getTime()) / 60_000));
  const age = ageMin < 60 ? `${ageMin} min` : `${Math.floor(ageMin / 60)} h ${ageMin % 60} min`;
  el.innerHTML = `Latest satellite data: <b>${age} ago</b> <small>(${esc(fmtTime(health.latest_detection_at))})</small> ${helpButton("freshness")}`;
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

const fmtDay = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

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
    ? (total === 1 ? `${fmtDay(start)} isn't` : "These days aren't")
    : `${missing.length} of ${total} days aren't`;
  if (!cov.can_load) {
    return showBanner(`${what} loaded for this area. Loading history needs a FIRMS MAP_KEY (see Calendar → Fire history).`, "warn");
  }
  if (!view || view[0] > view[2] || viewSpanDeg(view) > MAX_LOAD_SPAN_DEG) {
    return showBanner(`${what} fully loaded for this area. Zoom in to a region (under ${MAX_LOAD_SPAN_DEG}° across) and FireLens will load it from the NASA archive.`, "warn");
  }
  const first = missing[0];
  const last = missing[missing.length - 1];
  const daysToLoad = Math.round((Date.parse(last) - Date.parse(first)) / 86_400_000) + 1;
  if (daysToLoad > MAX_LOAD_DAYS) {
    return showBanner(`${what} loaded for this area. Pick ${MAX_LOAD_DAYS} days or fewer to load them automatically, or:`, "warn", true);
  }
  const box = snapBbox(view);
  const key = `${first}|${last}|${box}`;
  if (attemptedLoads.has(key)) {
    // tried already: NASA may not have these days for every satellite; don't loop
    return showBanner(`${what} complete for this area in the NASA archive (already requested).`, "warn", true);
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
    showBanner(`Could not load from NASA: ${esc(msg)}`, "warn", true);
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
      return showBanner(`Lost track of the NASA load: ${esc((err as Error).message)}`, "warn", true);
    }
    const r = job.request;
    const range = r.start === r.end ? fmtDay(r.start) : `${fmtDay(r.start)} – ${fmtDay(r.end)}`;
    if (job.status === "queued" || job.status === "running") {
      banner.className = "banner";
      $(".banner-text", banner).innerHTML =
        `Loading <b>${esc(range)}</b> from the NASA archive… ${job.done_chunks}/${job.total_chunks || "?"} requests · ` +
        `${job.detections.toLocaleString()} detections`;
      $(".progress", banner).classList.remove("hidden");
      $(".bar", banner).style.width = `${Math.round(job.progress * 100)}%`;
      $("#coverage-load").classList.add("hidden");
      window.setTimeout(tick, 2000);
      return;
    }
    coverageJobId = null;
    if (job.status === "done") {
      const partial = job.failed_chunks ? ` (${job.failed_chunks} request(s) failed)` : "";
      showBanner(`Loaded ${esc(range)}: ${job.detections.toLocaleString()} detections${partial}.`, job.failed_chunks ? "warn" : "ok");
      keepBannerUntil = Date.now() + 8000;
      window.setTimeout(() => void checkCoverage(), 8200);
      void renderCalendar();
      await refresh(); // re-checks coverage; anything NASA didn't have stays flagged, without looping
    } else {
      showBanner(`NASA load ${esc(job.status)}: ${esc(job.errors.at(-1) ?? "no data returned")}`, "warn", true);
    }
  };
  void tick();
}

$("#coverage-load").addEventListener("click", async () => {
  const view = globe.viewBbox();
  if (!view || view[0] > view[2] || viewSpanDeg(view) > MAX_LOAD_SPAN_DEG) {
    return toast(`Zoom in to a region under ${MAX_LOAD_SPAN_DEG}° across first.`, "error");
  }
  const start = dateInput("start").value;
  const end = dateInput("end").value;
  const days = Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000) + 1;
  if (days > 400) return toast("Pick 400 days or fewer.", "error");
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
  toast(`Failed to load map tiles: ${msg}`, "error");
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
    ? `Loading ${st.loading} tile${st.loading > 1 ? "s" : ""}…`
    : currentLod.kind === "cells"
      ? `Grouped in ${fmtCell(currentLod.cellDeg!)} cells · zoom in for individual fires`
      : st.truncated ? "Very dense area: showing the strongest detections per tile" : "";
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
    toast(`Failed to load data: ${(err as Error).message}`, "error");
  }
}

function renderStats(dets: number, events: number, note = "", approx = false): void {
  $("#stats").innerHTML = `
    <div><b>${approx && dets ? "≈ " : ""}${dets.toLocaleString()}</b><span>detections in view</span></div>
    <div><b>${events.toLocaleString()}</b><span>fire events</span></div>
    ${focusEventId ? `<button type="button" id="clear-focus" class="secondary small">Show all</button>` : ""}
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

/** What is currently narrowing the map, as removable chips under the stats. */
function renderActiveFilters(): void {
  const f = new FormData(filtersForm);
  const v = (k: string) => ((f.get(k) as string | null) ?? "").trim();
  const chips: [string, string[]][] = [];
  const conf = v("min_confidence");
  if (conf) chips.push([conf === "high" ? "High confidence only" : "Nominal and high confidence", ["min_confidence"]]);
  const ins = f.getAll("instrument");
  if (ins.length === 1) chips.push([`${ins[0]} only`, ["instrument"]]);
  if (ins.length === 0) chips.push(["No sensor selected", ["instrument"]]);
  const sats = f.getAll("satellite") as string[];
  if (sats.length) chips.push([`Satellites: ${sats.join(", ")}`, ["satellite"]]);
  if (v("daynight")) chips.push([v("daynight") === "D" ? "Day passes" : "Night passes", ["daynight"]]);
  if (v("min_frp") || v("max_frp")) chips.push([`Fire power ${v("min_frp") ? `≥ ${v("min_frp")}` : ""}${v("min_frp") && v("max_frp") ? " and " : ""}${v("max_frp") ? `≤ ${v("max_frp")}` : ""} MW`, ["min_frp", "max_frp"]]);
  if (v("min_area") || v("max_area")) chips.push([`Footprint ${v("min_area") ? `≥ ${v("min_area")}` : ""}${v("min_area") && v("max_area") ? " and " : ""}${v("max_area") ? `≤ ${v("max_area")}` : ""} km²`, ["min_area", "max_area"]]);
  if (v("min_detections")) chips.push([`Events with ≥ ${v("min_detections")} detections`, ["min_detections"]]);
  if (v("status")) chips.push([v("status") === "active" ? "Active fires only" : "No recent detection only", ["status"]]);
  if (v("lat") && v("lon") && v("radius_km")) chips.push([`Within ${v("radius_km")} km of ${Number(v("lat")).toFixed(2)}, ${Number(v("lon")).toFixed(2)}`, ["lat", "lon", "radius_km"]]);
  const box = $("#active-filters");
  box.classList.toggle("hidden", chips.length === 0);
  box.innerHTML = chips.length
    ? `<span class="af-label">Filters:</span>${chips.map(([label, fields]) =>
        `<button type="button" class="af-chip" data-clear="${fields.join(",")}" title="Remove this filter">${esc(label)} <span aria-hidden="true">✕</span></button>`).join("")}
       <button type="button" class="af-clear">Clear all</button>`
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
    ? `the last ${latestHours >= 48 ? `${latestHours / 24} days` : `${latestHours} h`}`
    : dateInput("start").value === dateInput("end").value ? dateInput("start").value : `${dateInput("start").value} to ${dateInput("end").value}`;
  $("#analysis-scope").textContent = `Period: ${period} (change it under Filters → Time). Area: what the globe shows when you press the button.`;
}

let analysisKind: "stats" | "sensors" = "stats";
$("#analysis-kind").addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest<HTMLButtonElement>("[data-kind]");
  if (!btn) return;
  analysisKind = btn.dataset.kind as typeof analysisKind;
  document.querySelectorAll("#analysis-kind button").forEach((b) => b.classList.toggle("active", b === btn));
  $("#analysis-body").innerHTML = "";
});
$("#analysis-run").addEventListener("click", () => {
  describeAnalysisScope();
  const run = analysisKind === "stats" ? runAnalysis : runSensors;
  void run(globe.visibleBbox(), analysisTime());
});
wireAnalysis((id) => void openEvent(id));

// ---------------------------------------------------------------- calendar
let calMonth = new Date(Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), 1));

async function renderCalendar(): Promise<void> {
  const y = calMonth.getUTCFullYear();
  const m = calMonth.getUTCMonth();
  const first = new Date(Date.UTC(y, m, 1));
  const last = new Date(Date.UTC(y, m + 1, 0));
  $("#cal-title").textContent = first.toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
  let days: CalendarDay[] = [];
  try {
    days = await api.calendar({ start: isoDay(first), end: isoDay(last) });
  } catch (err) {
    toast(`Calendar failed: ${(err as Error).message}`, "error");
  }
  const max = Math.max(1, ...days.map((d) => d.detections));
  const offset = (first.getUTCDay() + 6) % 7; // weeks start on Monday
  const today = isoDay(new Date());
  const cells = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((d) => `<div class="dow">${d}</div>`);
  for (let i = 0; i < offset; i++) cells.push("<div></div>");
  for (const d of days) {
    const t = d.detections ? 0.15 + 0.85 * (Math.log(1 + d.detections) / Math.log(1 + max)) : 0;
    cells.push(`<button type="button" class="day${d.date === today ? " today" : ""}" data-date="${d.date}"
      style="--heat:${t.toFixed(3)}" title="${d.detections} detections · ${d.events_started} new events · max FRP ${fmtNum(d.max_frp_mw)} MW">
      <span>${Number(d.date.slice(8))}</span>${d.events_started ? `<small>${d.events_started}</small>` : ""}</button>`);
  }
  $("#calendar").innerHTML = cells.join("");
}

$("#calendar").addEventListener("click", async (e) => {
  const day = (e.target as HTMLElement).closest<HTMLElement>("[data-date]")?.dataset.date;
  if (!day) return;
  setHistoryRange(day, day);
  setTimeMode("history");
  $("#cal-day-title").textContent = `Fires on ${day}`;
  try {
    const events = await api.events({ start: `${day}T00:00:00Z`, end: `${day}T23:59:59Z`, sort: "area", limit: 15 });
    $("#cal-events").innerHTML = events.length
      ? events.map((ev) => `<li><a href="#" data-event="${esc(ev.id)}">
          <b>${fmtNum(ev.area_km2, 2)} km²</b> · ${ev.detection_count} det. · max ${fmtNum(ev.max_frp_mw, 0)} MW
          <small>${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)} · ${statePill(ev)}</small></a></li>`).join("")
      : '<li class="empty">No fire events that day.</li>';
  } catch (err) {
    toast((err as Error).message, "error");
  }
});
$("#cal-events").addEventListener("click", (e) => {
  const id = (e.target as HTMLElement).closest<HTMLElement>("[data-event]")?.dataset.event;
  if (id) {
    e.preventDefault();
    void openEvent(id);
  }
});
$("#cal-prev").addEventListener("click", () => {
  calMonth = new Date(Date.UTC(calMonth.getUTCFullYear(), calMonth.getUTCMonth() - 1, 1));
  void renderCalendar();
});
$("#cal-next").addEventListener("click", () => {
  calMonth = new Date(Date.UTC(calMonth.getUTCFullYear(), calMonth.getUTCMonth() + 1, 1));
  void renderCalendar();
});

// ---------------------------------------------------------------- alerts
async function renderAlerts(): Promise<void> {
  try {
    const [rules, alerts] = await Promise.all([api.rules(), api.alerts()]);
    globe.showAlertZones(rules);
    $("#rules").innerHTML = rules.length
      ? rules.map((r) => `<li><div><b>${esc(r.name)}</b>
          <small>${r.radius_km ? `${fmtNum(r.radius_km, 0)} km around ${r.center_lat?.toFixed(2)}, ${r.center_lon?.toFixed(2)}` : "box zone"}
          · conf ≥ ${esc(r.min_confidence)} · area ≥ ${r.min_area_km2} km²${r.webhook_url ? " · webhook" : ""}</small></div>
          <button type="button" class="icon" data-delete-rule="${r.id}" aria-label="Delete rule">🗑</button></li>`).join("")
      : '<li class="empty">No rules yet.</li>';
    $("#alerts").innerHTML = alerts.length
      ? alerts.map((a) => `<li class="sev-${a.severity}"><a href="#" data-event="${esc(a.event_id)}">
          <span class="pill ${a.severity}">${a.severity}</span> ${esc(a.message)}
          <small>${esc(fmtTime(a.triggered_at))}</small></a></li>`).join("")
      : '<li class="empty">No alerts. Rules fire on active events after each ingestion.</li>';
  } catch (err) {
    toast(`Alerts failed: ${(err as Error).message}`, "error");
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
    if (!bbox) return toast("Point the camera at the Earth to define the zone.", "error");
    const [w, s, e, n] = bbox;
    const width = e >= w ? e - w : e - w + 360; // view may cross the antimeridian
    if (width > MAX_ZONE_DEG || n - s > MAX_ZONE_DEG) {
      return toast(`Zoom in to the area to watch (view is ${Math.round(width)}° wide, max ${MAX_ZONE_DEG}°), or set a point + radius in Filters.`, "error");
    }
    [rule.west, rule.south, rule.east, rule.north] = bbox;
  }
  try {
    await api.createRule(rule);
    form.reset();
    toast("Rule created. It is evaluated on every ingestion.");
    await renderAlerts();
  } catch (err) {
    toast(`Could not create rule: ${(err as Error).message}`, "error");
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
    coverage.textContent = "Demo mode: history uses real NASA data only. Start the API without FIRELENS_DEMO_MODE.";
  } else if (!enabled) {
    coverage.innerHTML = `Showing real FIRMS data for <b>${esc(health.public_region.replaceAll("_", " "))}</b> from the public files
      (last 7 days, refreshed hourly). To load older fires, add a free
      <a href="https://firms.modaps.eosdis.nasa.gov/api/map_key/" target="_blank" rel="noopener">FIRMS MAP_KEY</a>
      as <code>FIRELENS_FIRMS_MAP_KEY</code> in <code>.env</code> and restart the API.`;
  } else {
    coverage.textContent = "Loads NASA's science-quality archive (SP) where available and near-real-time data for recent months.";
  }
}

function renderJob(job: BackfillJob): void {
  const box = $("#history-job");
  box.classList.remove("hidden");
  $(".bar", box).style.width = `${Math.round(job.progress * 100)}%`;
  const running = job.status === "queued" || job.status === "running";
  const parts = [
    `<b>${esc(job.status)}</b> · ${job.done_chunks}/${job.total_chunks || "?"} requests · ${job.detections.toLocaleString()} detections`,
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
        const partial = job.failed_chunks ? ` (${job.failed_chunks} of ${job.total_chunks} requests failed)` : "";
        toast(`History loaded: ${job.detections.toLocaleString()} detections${partial}.`, job.failed_chunks ? "error" : "info");
      } else if (job.status === "failed") {
        toast(`History load failed: ${job.errors.at(-1) ?? "no data returned"}`, "error");
      }
      await Promise.all([renderCalendar(), refresh()]);
    } catch (err) {
      toast(`Lost track of the history job: ${(err as Error).message}`, "error");
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
    if (!bbox || bbox[0] >= bbox[2]) return toast("Point the camera at the area to load (it can't cross the 180° meridian).", "error");
    body.bbox = bbox.join(",");
  } else {
    body.country = String(f.get("country") || "ARG");
  }
  try {
    const job = await api.backfill(body);
    renderJob(job);
    watchJob(job.id);
    // land the calendar on the month being loaded
    calMonth = new Date(Date.UTC(Number(body.start.slice(0, 4)), Number(body.start.slice(5, 7)) - 1, 1));
  } catch (err) {
    toast(`Could not start history load: ${(err as Error).message}`, "error");
  }
});
$("#history-cancel").addEventListener("click", async () => {
  const jobs = await api.jobs().catch(() => [] as BackfillJob[]);
  const running = jobs.find((j) => j.status === "running" || j.status === "queued");
  if (running) renderJob(await api.cancelJob(running.id));
});

// ---------------------------------------------------------------- boot
const SOURCE_BADGE: Record<Health["data_source"], [string, string]> = {
  demo: ["DEMO DATA", "Synthetic FIRMS-format data (FIRELENS_DEMO_MODE=true)."],
  firms_public: ["LIVE · FIRMS", "Real NASA FIRMS data from the public files (last 7 days). Add a MAP_KEY for history."],
  firms_api: ["LIVE · FIRMS + ARCHIVE", "Real NASA FIRMS data; MAP_KEY configured, history available."],
};

async function loadHealth(): Promise<void> {
  health = await api.health();
  if (health.event_active_hours) setActiveHours(health.event_active_hours);
  const badge = $("#mode-badge");
  const [label, title] = SOURCE_BADGE[health.data_source];
  badge.textContent = health.seeding ? "LOADING REAL DATA…" : label;
  badge.classList.toggle("demo", health.data_source === "demo");
  badge.classList.toggle("seeding", health.seeding);
  badge.title = `${title} Last ingest: ${health.last_ingest ? fmtTime(health.last_ingest) : "never"}.`;
  renderHistoryAvailability();
  renderFreshness();
}

wireDetails((id) => void openEvent(id));
wireHelp();
wireLayout(() => globe.resize());
setSpreadDrawer((grid) => void globe.showSpread(grid));
mountLegend($("#globe"));
mountGuide($("#globe"), $("#guide-open"));
globe.onHover(showMapTip);
renderActiveFilters();
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
    toast("Backend not reachable on /api — is the API running on :8000?", "error");
  }
  await refresh();
  booted = true;
  const rules = await api.rules().catch(() => []);
  globe.showAlertZones(rules);
})();
