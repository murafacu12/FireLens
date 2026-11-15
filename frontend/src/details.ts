import { api, type EventHistoryEntry, type EventPass, type FireDetection, type FireEvent, type FireEventDetail, type RiskForecast } from "./api";
import { clearSpread, forgetFireReport, mountFireReport } from "./firereport";
import { powerLevel, powerWord } from "./guide";
import { isSimple, locale, onPrefsChange, t } from "./prefs";
import type { Key } from "./strings";
import { $, esc, fmtNum, fmtTime, toast } from "./util";

const STATIC_LABEL: Record<string, Key> = {
  volcano: "d.volcano",
  industrial: "d.industrial",
  offshore: "d.offshore",
};

const LEVEL_LABEL: Record<string, Key> = {
  low: "risk.low", moderate: "risk.moderate", high: "risk.high", very_high: "risk.very_high", extreme: "risk.extreme",
};
const LEVEL_COLOUR: Record<string, string> = {
  low: "#6fcf7f", moderate: "#f5e663", high: "#ffbe5c", very_high: "#ff8f6b", extreme: "#ff7ac6",
};

// what the panel shows now, so a language or view change can draw it again
type Shown =
  | { kind: "event"; ev: FireEventDetail; onFocus: (id: string) => void }
  | { kind: "detection"; d: FireDetection }
  | { kind: "point"; lat: number; lon: number }
  | null;
let shown: Shown = null;
const risks = new Map<string, RiskForecast>(); // forecasts already loaded, by "lat,lon"

let onShownChange: (eventId: string | null) => void = () => {};
/** Called with the id of the fire the panel shows, or null (another kind of details, or closed). */
export function onDetailsChange(fn: (eventId: string | null) => void): void {
  onShownChange = fn;
}

function panel(html: string): void {
  $("#details-body").innerHTML = html;
  $("#details").classList.remove("hidden");
  onShownChange(shown?.kind === "event" ? shown.ev.id : null);
}

export function closeDetails(): void {
  $("#details").classList.add("hidden");
  shown = null;
  onShownChange(null);
  forgetFireReport();
  clearSpread();
}

function riskButton(lat: number, lon: number): string {
  const key = `${lat},${lon}`;
  const r = risks.get(key);
  return `<button type="button" class="secondary" data-risk="${key}">${t("risk.button")}</button><div class="risk-slot">${r ? renderRisk(r) : ""}</div>`;
}

function kv(rows: [string, string][]): string {
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

let activeHours = 24;
/** Hours without detection after which an event counts as inactive (from /api/health). */
export function setActiveHours(h: number): void {
  activeHours = h;
}

/** Lifecycle wording: no detection is not proof a fire is out (cloud, smoke, canopy, revisit gaps). */
export function stateLabel(ev: Pick<FireEvent, "lifecycle_state" | "status">): string {
  switch (ev.lifecycle_state ?? ev.status) {
    case "active": return t("d.active");
    case "reactivated": return t("d.reactivated");
    case "archived": return t("d.archived");
    default: return t("d.noDetection", { h: activeHours });
  }
}
export const statePill = (ev: Pick<FireEvent, "lifecycle_state" | "status">) =>
  `<span class="pill ${ev.lifecycle_state ?? ev.status}">${esc(stateLabel(ev))}</span>`;

const EVIDENCE_LABEL: Record<FireEvent["evidence"], Key> = {
  single_detection: "an.evOne",
  single_pass: "an.evPass",
  multi_pass: "an.evMulti",
};
const TREND_LABEL: Record<FireEvent["intensity_trend"], Key> = {
  rising: "d.trendRising",
  falling: "d.trendFalling",
  stable: "d.trendStable",
  insufficient_data: "d.trendNone",
};
const TREND_SIMPLE: Record<FireEvent["intensity_trend"], [Key, string]> = {
  rising: ["d.sRising", "up"],
  falling: ["d.sFalling", "down"],
  stable: ["d.sStable", "flat"],
  insufficient_data: ["d.sTooEarly", "none"],
};

/** Position (0-1) of a fire power on the legend's log scale (1 MW .. 2,000 MW). */
const gaugePos = (mw: number | null | undefined) => Math.min(1, Math.max(0, Math.log10(Math.max(1, mw ?? 1)) / Math.log10(2000)));

function powerGauge(mw: number | null | undefined): string {
  return `<div class="gauge"><i style="left:${(100 * gaugePos(mw)).toFixed(1)}%"></i></div>
    <div class="gauge-labels"><span>${t("power.small")}</span><span>${t("power.moderate")}</span><span>${t("power.intense")}</span><span>${t("power.extreme")}</span></div>`;
}

function duration(hours: number | null | undefined): string {
  const h = hours ?? 0;
  return h < 48 ? `${fmtNum(h, 0)}<small>h</small>` : `${fmtNum(h / 24, 1)}<small>${t("d.days")}</small>`;
}

function eventSimple(ev: FireEventDetail): string {
  const [trendKey, tone] = TREND_SIMPLE[ev.intensity_trend] ?? TREND_SIMPLE.insufficient_data;
  const tile = (k: string, v: string, sub: string, accent = false) =>
    `<div class="s-tile${accent ? " accent" : ""}"><div class="k">${k}</div><div class="v">${v}</div><div class="sub">${sub}</div></div>`;
  return `
    <h2>${t("d.fire")} ${statePill(ev)}</h2>
    <p class="hint">${t("d.sSince", { when: esc(fmtTime(ev.start_at)) })} · ${ev.latitude.toFixed(3)}, ${ev.longitude.toFixed(3)}</p>
    <div class="s-tiles">
      ${tile(t("d.sSize"), `${fmtNum(ev.area_km2, ev.area_km2 < 10 ? 1 : 0)}<small>km²</small>`, t("d.sSizeSub"), true)}
      ${tile(t("d.sStrength"), esc(powerWord(ev.peak_pass_frp_mw)), t("d.sStrengthSub", { mw: fmtNum(ev.peak_pass_frp_mw, 0) }))}
      ${tile(t("d.sObserved"), duration(ev.observed_hours), t("d.sPasses", { n: ev.pass_count }))}
      ${tile(t("d.sDetections"), ev.detection_count.toLocaleString(locale()), `VIIRS ${ev.viirs_count} · MODIS ${ev.modis_count}`)}
    </div>
    ${ev.perimeter ? `<p class="hint perimeter-hint"><span class="perimeter-swatch"></span>${t("d.sPerimeter", { km2: fmtNum(ev.perimeter.area_km2, ev.perimeter.area_km2 < 10 ? 1 : 0) })}</p>` : ""}
    ${powerGauge(ev.peak_pass_frp_mw)}
    <div class="s-card"><div class="s-big ${tone}"><span class="dot-state"></span>${t(trendKey)}</div>
      <p>${t(`d.sLevel.${powerLevel(ev.peak_pass_frp_mw)}` as Key)}</p></div>`;
}

function eventAdvanced(ev: FireEventDetail): string {
  const sats = Object.entries(ev.satellite_counts ?? {}).map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
  const meanFrp = [
    ev.mean_frp_viirs_mw != null ? `VIIRS ${fmtNum(ev.mean_frp_viirs_mw, 1)} MW` : "",
    ev.mean_frp_modis_mw != null ? `MODIS ${fmtNum(ev.mean_frp_modis_mw, 1)} MW` : "",
  ].filter(Boolean).join(" · ");
  const growth = ev.footprint_growth_km2_per_day != null
    ? `${fmtNum(ev.footprint_growth_km2_per_day, 2)} km²/${t("d.day")}${ev.footprint_growth_se != null ? ` ± ${fmtNum(ev.footprint_growth_se, 2)}` : ""}`
    : "";
  return `
    <h2>${t("d.event")} ${statePill(ev)}</h2>
    <p class="mono">${esc(ev.id)}</p>
    ${ev.resolved_from ? `<p class="hint">${t("d.resolved", { id: `<span class="mono">${esc(ev.resolved_from)}</span>` })}</p>` : ""}
    ${kv([
      [t("d.location"), `${ev.latitude.toFixed(4)}, ${ev.longitude.toFixed(4)} <small>${t("d.frpCentre")}</small>`],
      [t("d.first"), esc(fmtTime(ev.start_at))],
      [t("d.last"), esc(fmtTime(ev.end_at))],
      [t("d.span"), `${fmtNum(ev.observed_hours, 0)} h <small>${t("d.spanNote")}</small>`],
      [t("d.evidence"), `${t(EVIDENCE_LABEL[ev.evidence] ?? "an.evOne")} <small>(${t(ev.pass_count === 1 ? "d.pass_one" : "d.pass_other", { n: ev.pass_count })})</small>`],
      [t("d.detections"), `${ev.detection_count} <small>(VIIRS ${ev.viirs_count} · MODIS ${ev.modis_count})</small>`],
      [t("d.satellites"), sats ? `<small>${sats}</small>` : "–"],
      [t("d.peak"), `${fmtNum(ev.peak_pass_frp_mw)} MW <small>${t("d.peakNote", { sensor: esc(ev.peak_pass_sensor ?? "") })}</small>`],
      [t("d.maxPixel"), `${fmtNum(ev.max_frp_mw)} MW`],
      [t("d.meanPixel"), meanFrp ? `<small>${meanFrp}</small>` : "–"],
      [t("d.trend"), esc(t(TREND_LABEL[ev.intensity_trend] ?? "d.trendNone"))],
      [t("d.footprint"), ev.area_basis === "modis_1km"
        ? `≈ ${fmtNum(ev.area_km2, 1)} km² <small>${t("d.modisPx")}</small>`
        : `${fmtNum(ev.area_km2, 2)} km² <small>${t("d.viirsPx")}</small>`],
      ...(ev.perimeter ? [[t("d.perimeter"), `${fmtNum(ev.perimeter.area_km2, 2)} km² <small>${t("d.perimeterNote", { n: ev.perimeter.rings.length, km: fmtNum(ev.perimeter.gap_km, 2) })}</small>`] as [string, string]] : []),
      [t("d.hull"), `${fmtNum(ev.detection_hull_km2, 2)} km² <small>${t("d.hullNote", { km: fmtNum(ev.extent_km, 1) })}</small>`],
      ...(growth ? [[t("d.growth"), `${growth} <small>${t("d.growthNote")}</small>`] as [string, string]] : []),
      [t("d.burned"), `<small>${t("d.burnedNote")}</small>`],
      [t("d.maxConf"), `${ev.max_confidence_pct}%`],
    ])}`;
}

export function showEvent(ev: FireEventDetail, onFocusDetections: (id: string) => void): void {
  if (shown?.kind !== "event" || shown.ev.id !== ev.id) forgetFireReport();
  shown = { kind: "event", ev, onFocus: onFocusDetections };
  panel(`
    ${isSimple() ? eventSimple(ev) : eventAdvanced(ev)}
    <div class="frp-timeline"><p class="hint">${t("d.loadingPasses")}</p></div>
    <p class="hint adv">${t("d.footnote")}</p>
    <div class="fire-report"></div>
    <details class="history adv"><summary>${t("d.history")}</summary><div class="history-body">${t("d.loading")}</div></details>
    <div class="actions">
      <button type="button" id="focus-dets" class="secondary">${t("d.focus")}</button>
    </div>
    ${riskButton(ev.latitude, ev.longitude)}
  `);
  $("#focus-dets").addEventListener("click", () => onFocusDetections(ev.id));
  // a current front to spread from: detected in the last day (as the API requires)
  mountFireReport($(".fire-report"), ev.id, Date.now() - new Date(ev.end_at).getTime() < 24 * 3_600_000);
  api.eventTimeline(ev.id).then(
    (passes) => {
      const el = document.querySelector(".frp-timeline");
      if (el) el.innerHTML = frpChart(passes);
    },
    () => document.querySelector(".frp-timeline")?.replaceChildren(),
  );
  $<HTMLDetailsElement>("details.history").addEventListener("toggle", function onOpen(e) {
    const box = e.currentTarget as HTMLDetailsElement;
    if (!box.open) return;
    box.removeEventListener("toggle", onOpen);
    api.eventHistory(ev.id).then(
      (h) => ($(".history-body").innerHTML = historyList(h)),
      (err: Error) => ($(".history-body").textContent = t("d.historyFailed", { msg: err.message })),
    );
  });
}

/** Whole-fire FRP per overpass; VIIRS and MODIS drawn as separate series (not cross-calibrated). */
function frpChart(passes: EventPass[]): string {
  if (passes.length < 2) return "";
  const W = 360, H = 76, pad = 5;
  const t0 = new Date(passes[0].acquired_at).getTime();
  const t1 = new Date(passes[passes.length - 1].acquired_at).getTime();
  const max = Math.max(...passes.map((p) => p.frp_sum), 1);
  const x = (p: EventPass) => pad + ((new Date(p.acquired_at).getTime() - t0) / Math.max(1, t1 - t0)) * (W - 2 * pad);
  const y = (p: EventPass) => H - pad - (p.frp_sum / max) * (H - 2 * pad);
  const series = (ins: string, cls: string) => {
    const pts = passes.filter((p) => p.instrument === ins);
    if (!pts.length) return "";
    const line = pts.length > 1 ? `<polyline class="${cls}" points="${pts.map((p) => `${x(p).toFixed(1)},${y(p).toFixed(1)}`).join(" ")}" />` : "";
    return line + pts.map((p) => `<circle class="${cls}" cx="${x(p).toFixed(1)}" cy="${y(p).toFixed(1)}" r="2.8"><title>${esc(p.satellite)} ${esc(fmtTime(p.acquired_at))}: ${fmtNum(p.frp_sum)} MW (${p.pixels} px)</title></circle>`).join("");
  };
  const title = isSimple()
    ? `${t("d.sPowerTitle")} <small>(<span class="viirs">VIIRS</span> · <span class="modis">MODIS</span>)</small>`
    : `${t("d.powerTitle")} <small>(${t("d.powerMax", { mw: fmtNum(max) })} · <span class="viirs">VIIRS</span> · <span class="modis">MODIS</span>)</small>`;
  return `<div class="spark-title">${title}</div>
    <svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(t("d.powerTitle"))}">${series("VIIRS", "viirs")}${series("MODIS", "modis")}</svg>`;
}

const CHANGE_LABEL: Record<string, Key> = {
  created: "h.created", grown: "h.grown", updated: "h.updated", state: "h.state",
  merged: "h.merged", split: "h.split", renamed: "h.renamed", removed: "h.removed",
};

function historyList(entries: EventHistoryEntry[]): string {
  if (!entries.length) return t("d.noChanges");
  return `<ol class="history-list">${entries.map((h) => {
    const snap = h.snapshot as { detection_count?: number; area_km2?: number };
    const what = [
      h.change === "state" || h.from_state !== h.to_state ? `${esc(h.from_state ?? "–")} → ${esc(h.to_state ?? "–")}` : "",
      snap.detection_count != null ? `${snap.detection_count} ${t("c.det")}` : "",
      snap.area_km2 != null ? `${fmtNum(snap.area_km2, 2)} km²` : "",
      h.related_id ? `<span class="mono">${esc(h.related_id)}</span>` : "",
      h.reason ? `<small>${esc(h.reason.replaceAll("_", " "))}</small>` : "",
    ].filter(Boolean).join(" · ");
    const label = CHANGE_LABEL[h.change] ? t(CHANGE_LABEL[h.change]) : h.change;
    return `<li><b>${esc(label)}</b> <small>${esc(fmtTime(h.at))}${h.event_id !== entries[entries.length - 1].event_id ? ` · ${esc(h.event_id)}` : ""}</small><br>${what}</li>`;
  }).join("")}</ol>`;
}

export function showDetection(d: FireDetection): void {
  forgetFireReport();
  clearSpread();
  shown = { kind: "detection", d };
  const staticRow: [string, string][] = d.static_kind ? [[t("d.notWildfire"), `<span class="pill warning">${esc(t(STATIC_LABEL[d.static_kind] ?? "d.industrial"))}</span>
        <small>${d.detection_type != null ? t("d.labelled") : t("d.onSite")}</small>`]] : [];
  if (isSimple()) {
    panel(`
      <h2>${t("d.detectionTitle", { sensor: esc(d.instrument) })}</h2>
      <p class="hint">${esc(fmtTime(d.acquired_at))} · ${esc(d.satellite)}</p>
      <div class="s-tiles">
        <div class="s-tile accent"><div class="k">${t("d.sStrength")}</div><div class="v">${esc(powerWord(d.frp_mw))}</div><div class="sub">${fmtNum(d.frp_mw)} MW</div></div>
        <div class="s-tile"><div class="k">${t("f.confidence")}</div><div class="v">${esc(t(d.confidence_level === "high" ? "al.high" : d.confidence_level === "low" ? "al.low" : "al.nominal"))}</div><div class="sub">${d.confidence_pct}%</div></div>
      </div>
      ${powerGauge(d.frp_mw)}
      ${staticRow.length ? kv(staticRow) : ""}
      ${d.event_id ? `<div class="actions"><button type="button" class="secondary" data-event="${esc(d.event_id)}">${t("d.openFire")}</button></div>` : ""}
      ${riskButton(d.latitude, d.longitude)}
    `);
    return;
  }
  panel(`
    <h2>${t("d.detectionTitle", { sensor: esc(d.instrument) })}</h2>
    ${kv([
      [t("d.satellite"), `${esc(d.satellite)} <small>(${esc(d.source)})</small>`],
      [t("d.acquired"), esc(fmtTime(d.acquired_at))],
      [t("d.location"), `${d.latitude.toFixed(5)}, ${d.longitude.toFixed(5)}`],
      ["FRP", `${fmtNum(d.frp_mw)} MW`],
      [t("f.confidence"), `${esc(d.confidence_level)} · ${d.confidence_pct}% <small>(raw "${esc(d.confidence_raw)}")</small>`],
      [t("d.brightness"), `${fmtNum(d.brightness_k)} K${d.brightness_saturated ? ` <span class="pill warning">${t("d.saturated")}</span>` : ""}`],
      [t("d.brightness11"), `${fmtNum(d.brightness_secondary_k)} K`],
      [t("d.pixel"), `${d.scan_km} × ${d.track_km} km (${fmtNum(d.pixel_area_km2, 3)} km²)`],
      [t("f.daynight"), d.daynight === "N" ? t("f.night") : d.daynight === "D" ? t("f.day") : "–"],
      [t("d.processing"), esc(d.processing_level)],
      [t("d.event"), d.event_id ? `<a href="#" data-event="${esc(d.event_id)}">${esc(d.event_id)}</a>` : "–"],
      ...staticRow,
    ])}
    ${riskButton(d.latitude, d.longitude)}
  `);
}

export function showPoint(lat: number, lon: number): void {
  forgetFireReport();
  clearSpread();
  shown = { kind: "point", lat, lon };
  panel(`
    <h2>${t("d.place")}</h2>
    ${kv([[t("d.coords"), `${lat.toFixed(4)}, ${lon.toFixed(4)}`]])}
    ${riskButton(lat, lon)}
  `);
}

const dayName = (iso: string, long = false) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(locale(), { weekday: long ? "long" : "short", day: "2-digit", timeZone: "UTC" });

function renderRisk(r: RiskForecast): string {
  const water = r.surface === "water" || r.surface === "open_ocean"
    ? `<p class="hint"><b>${t(r.surface === "open_ocean" ? "risk.ocean" : "risk.water")}</b> ${t("risk.waterNote", { pct: fmtNum((r.water_fraction ?? 1) * 100, 0) })}</p>`
    : "";
  if (isSimple()) {
    const worst = [...r.days].sort((a, b) => b.score - a.score)[0];
    return `
      <div class="week-strip">${r.days.map((d) => `<div style="background:${LEVEL_COLOUR[d.level] ?? "#777"}" title="${esc(`${dayName(d.date)} · ${t(LEVEL_LABEL[d.level])}`)}">${esc(dayName(d.date).split(" ")[0])}<small>${esc(t(LEVEL_LABEL[d.level]))}</small></div>`).join("")}</div>
      ${worst ? `<p class="hint">${t("risk.worst", { level: `<b>${esc(t(LEVEL_LABEL[worst.level]))}</b>`, day: esc(dayName(worst.date, true)) })}</p>` : ""}
      ${water}
      <p class="hint">${t("risk.sNote")}</p>`;
  }
  const rows = r.days
    .map(
      (d) => `<tr>
        <td>${esc(dayName(d.date))}</td>
        <td><span class="pill risk-${d.level}">${esc(t(LEVEL_LABEL[d.level]))}</span></td>
        <td>${fmtNum(d.score, 0)}</td>
        <td>${fmtNum(d.temperature_max_c, 0)}°</td>
        <td>${fmtNum(d.humidity_min_pct, 0)}%</td>
        <td>${fmtNum(d.wind_max_ms, 0)}</td>
        <td>${d.dry_days}</td>
      </tr>`,
    )
    .join("");
  return `
    <table class="risk">
      <thead><tr><th>${t("risk.day")}</th><th>${t("risk.danger")}</th><th>${t("risk.score")}</th><th>Tmax</th><th>RHmin</th><th>${t("risk.wind")}</th><th>${t("risk.dryDays")}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${water}
    <p class="hint">${t("risk.activity", { n: r.nearby_detections_7d, prev: r.nearby_detections_prev_7d, trend: `<b>${esc(r.activity_trend)}</b>` })}
      ${r.ndvi != null ? ` · Sentinel-2 NDVI ${r.ndvi.toFixed(2)}` : ""}</p>
    <p class="hint">${esc(r.model)} — ${esc(r.disclaimer)} ${t("risk.weather")}</p>`;
}

function redraw(): void {
  if (!shown || $("#details").classList.contains("hidden")) return;
  if (shown.kind === "event") showEvent(shown.ev, shown.onFocus);
  else if (shown.kind === "detection") showDetection(shown.d);
  else showPoint(shown.lat, shown.lon);
}

/** Delegated handlers for buttons rendered inside the details panel. */
export function wireDetails(openEvent: (id: string) => void): void {
  $("#details-close").addEventListener("click", closeDetails);
  onPrefsChange(redraw);
  $("#details").addEventListener("click", async (e) => {
    const target = e.target as HTMLElement;
    const ev = target.closest<HTMLElement>("[data-event]");
    if (ev) {
      e.preventDefault();
      openEvent(ev.dataset.event!);
      return;
    }
    const btn = target.closest<HTMLButtonElement>("[data-risk]");
    if (!btn) return;
    const key = btn.dataset.risk!;
    const [lat, lon] = key.split(",").map(Number);
    const slot = btn.nextElementSibling as HTMLElement;
    btn.disabled = true;
    slot.innerHTML = `<p class="hint">${t("risk.loading")}</p>`;
    try {
      const r = await api.risk(lat, lon);
      risks.set(key, r);
      slot.innerHTML = renderRisk(r);
    } catch (err) {
      slot.innerHTML = "";
      toast(t("risk.failed", { msg: (err as Error).message }), "error");
    } finally {
      btn.disabled = false;
    }
  });
}
