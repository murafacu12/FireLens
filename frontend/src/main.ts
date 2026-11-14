import "./styles.css";

import { api, type AlertRuleIn, type CalendarDay } from "./api";
import { closeDetails, showDetection, showEvent, showPoint, wireDetails } from "./details";
import { Globe, type BaseLayerId, type PickResult } from "./globe";
import { $, daysAgo, esc, fmtNum, fmtTime, isoDay, toast } from "./util";

const filtersForm = $<HTMLFormElement>("#filters");
let picking = false;
let focusEventId: string | null = null;

const globe = new Globe($("#globe"), onPick);
if (import.meta.env.DEV) (window as unknown as { __globe: Globe }).__globe = globe; // debugging aid

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
  if (r.kind === "event") void openEvent(r.eventId);
  else if (r.kind === "detection") showDetection(r.detection);
  else showPoint(r.lat, r.lon);
}

function setPicking(on: boolean): void {
  picking = on;
  $("#picking-hint").classList.toggle("hidden", !on);
  $("#globe").classList.toggle("crosshair", on);
}

async function openEvent(id: string): Promise<void> {
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
function readFilters() {
  const f = new FormData(filtersForm);
  const s = (k: string) => (f.get(k) as string | null)?.trim() || undefined;
  const start = s("start");
  const end = s("end");
  const area: Record<string, string | undefined> = {};
  if (f.get("use_view")) {
    const bbox = globe.viewBbox();
    if (bbox) area.bbox = bbox.join(",");
  }
  if (s("lat") && s("lon") && s("radius_km")) {
    Object.assign(area, { lat: s("lat"), lon: s("lon"), radius_km: s("radius_km") });
  }
  const instruments = f.getAll("instrument") as string[];
  return {
    common: {
      start: start ? `${start}T00:00:00Z` : undefined,
      end: end ? `${end}T23:59:59Z` : undefined,
      ...area,
    },
    detections: {
      instrument: instruments.length === 2 ? undefined : instruments,
      satellite: (f.getAll("satellite") as string[]).length ? (f.getAll("satellite") as string[]) : undefined,
      min_confidence: s("min_confidence"),
      daynight: s("daynight"),
      min_frp: s("min_frp"),
      max_frp: s("max_frp"),
    },
    events: {
      instrument: instruments.length === 1 ? instruments[0] : undefined,
      min_area: s("min_area"),
      max_area: s("max_area"),
      min_detections: s("min_detections"),
      status: s("status"),
      min_frp: s("min_frp"),
    },
    noSensor: instruments.length === 0,
  };
}

function setRange(days: number): void {
  (filtersForm.elements.namedItem("start") as HTMLInputElement).value = isoDay(daysAgo(days - 1));
  (filtersForm.elements.namedItem("end") as HTMLInputElement).value = isoDay(new Date());
}

async function refresh(): Promise<void> {
  const f = readFilters();
  if (f.noSensor) {
    globe.showDetections([]);
    globe.showEvents([]);
    renderStats(0, 0);
    return;
  }
  try {
    // Focus mode shows one fire's complete history, regardless of the sidebar filters.
    const [dets, events] = await Promise.all([
      focusEventId
        ? api.detections({ event_id: focusEventId, limit: 50000 })
        : api.detections({ ...f.common, ...f.detections, limit: 20000 }),
      focusEventId ? Promise.resolve([]) : api.events({ ...f.common, ...f.events, limit: 5000 }),
    ]);
    globe.showDetections(dets);
    globe.showEvents(events);
    renderStats(dets.length, events.length);
    if (dets.length >= 20000) toast("Showing the 20,000 most recent detections — narrow the filters to see all.");
  } catch (err) {
    toast(`Failed to load data: ${(err as Error).message}`, "error");
  }
}

function renderStats(dets: number, events: number): void {
  $("#stats").innerHTML = `
    <div><b>${dets.toLocaleString()}</b><span>detections</span></div>
    <div><b>${events.toLocaleString()}</b><span>fire events</span></div>
    ${focusEventId ? `<button type="button" id="clear-focus" class="secondary small">Show all</button>` : ""}`;
  document.getElementById("clear-focus")?.addEventListener("click", () => {
    focusEventId = null;
    void refresh();
  });
}

filtersForm.addEventListener("submit", (e) => {
  e.preventDefault();
  focusEventId = null;
  const f = new FormData(filtersForm);
  const lat = Number(f.get("lat"));
  const lon = Number(f.get("lon"));
  const r = Number(f.get("radius_km"));
  globe.pin(f.get("lat") ? lat : null, f.get("lon") ? lon : null, r || undefined);
  void refresh();
});
filtersForm.addEventListener("reset", () => {
  window.setTimeout(() => {
    setRange(7);
    globe.pin(null, null);
    focusEventId = null;
    void refresh();
  });
});
$("#quick-ranges").addEventListener("click", (e) => {
  const days = (e.target as HTMLElement).dataset.days;
  if (!days) return;
  setRange(Number(days));
  void refresh();
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
  (filtersForm.elements.namedItem("start") as HTMLInputElement).value = day;
  (filtersForm.elements.namedItem("end") as HTMLInputElement).value = day;
  focusEventId = null;
  void refresh();
  $("#cal-day-title").textContent = `Fires on ${day}`;
  try {
    const events = await api.events({ start: `${day}T00:00:00Z`, end: `${day}T23:59:59Z`, sort: "area", limit: 15 });
    $("#cal-events").innerHTML = events.length
      ? events.map((ev) => `<li><a href="#" data-event="${esc(ev.id)}">
          <b>${fmtNum(ev.area_km2, 2)} km²</b> · ${ev.detection_count} det. · max ${fmtNum(ev.max_frp_mw, 0)} MW
          <small>${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)} · <span class="pill ${ev.status}">${ev.status}</span></small></a></li>`).join("")
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

// ---------------------------------------------------------------- boot
wireDetails((id) => void openEvent(id));
setRange(7);
(async () => {
  try {
    const h = await api.health();
    const badge = $("#mode-badge");
    badge.textContent = h.demo_mode ? "DEMO DATA" : "LIVE · FIRMS";
    badge.classList.toggle("demo", h.demo_mode);
    badge.title = h.demo_mode
      ? "Synthetic FIRMS-format data. Set FIRELENS_FIRMS_MAP_KEY on the backend for live NASA data."
      : `Last ingest ${h.last_ingest ? fmtTime(h.last_ingest) : "never"}`;
  } catch {
    toast("Backend not reachable on /api — is the API running on :8000?", "error");
  }
  await refresh();
  const rules = await api.rules().catch(() => []);
  globe.showAlertZones(rules);
})();
