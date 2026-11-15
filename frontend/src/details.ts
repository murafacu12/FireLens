import { api, type EventHistoryEntry, type EventPass, type FireDetection, type FireEvent, type FireEventDetail, type RiskForecast } from "./api";
import { clearSpread, mountFireReport } from "./firereport";
import { $, esc, fmtNum, fmtTime, toast } from "./util";

const STATIC_LABEL: Record<string, string> = {
  volcano: "Volcano",
  industrial: "Industrial / gas flare",
  offshore: "Offshore platform",
};

const LEVEL_LABEL: Record<string, string> = {
  low: "Low", moderate: "Moderate", high: "High", very_high: "Very high", extreme: "Extreme",
};

function panel(html: string): void {
  $("#details-body").innerHTML = html;
  $("#details").classList.remove("hidden");
}

export function closeDetails(): void {
  $("#details").classList.add("hidden");
  clearSpread();
}

function riskButton(lat: number, lon: number): string {
  return `<button type="button" class="secondary" data-risk="${lat},${lon}">Fire-danger forecast here</button><div class="risk-slot"></div>`;
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
    case "active": return "Active";
    case "reactivated": return "Reactivated";
    case "archived": return "Archived · no detection > 30 d";
    default: return `No detection > ${activeHours} h`;
  }
}
export const statePill = (ev: Pick<FireEvent, "lifecycle_state" | "status">) =>
  `<span class="pill ${ev.lifecycle_state ?? ev.status}">${esc(stateLabel(ev))}</span>`;

const EVIDENCE_LABEL: Record<FireEvent["evidence"], string> = {
  single_detection: "One detection",
  single_pass: "One satellite pass",
  multi_pass: "Seen on several passes",
};
const TREND_LABEL: Record<FireEvent["intensity_trend"], string> = {
  rising: "Rising (latest pass ≥ 2× recent passes)",
  falling: "Falling (latest pass ≤ ½ recent passes)",
  stable: "No significant change",
  insufficient_data: "Not enough passes to tell",
};

export function showEvent(ev: FireEventDetail, onFocusDetections: (id: string) => void): void {
  const sats = Object.entries(ev.satellite_counts ?? {}).map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
  const meanFrp = [
    ev.mean_frp_viirs_mw != null ? `VIIRS ${fmtNum(ev.mean_frp_viirs_mw, 1)} MW` : "",
    ev.mean_frp_modis_mw != null ? `MODIS ${fmtNum(ev.mean_frp_modis_mw, 1)} MW` : "",
  ].filter(Boolean).join(" · ");
  const growth = ev.footprint_growth_km2_per_day != null
    ? `${fmtNum(ev.footprint_growth_km2_per_day, 2)} km²/day${ev.footprint_growth_se != null ? ` ± ${fmtNum(ev.footprint_growth_se, 2)}` : ""}`
    : "";
  panel(`
    <h2>Fire event ${statePill(ev)}</h2>
    <p class="mono">${esc(ev.id)}</p>
    ${ev.resolved_from ? `<p class="hint">Event <span class="mono">${esc(ev.resolved_from)}</span> is now part of this event (merged or renamed as new detections arrived).</p>` : ""}
    ${kv([
      ["Location", `${ev.latitude.toFixed(4)}, ${ev.longitude.toFixed(4)} <small>(FRP-weighted centre)</small>`],
      ["First detection", esc(fmtTime(ev.start_at))],
      ["Last detection", esc(fmtTime(ev.end_at))],
      ["Observed span", `${fmtNum(ev.observed_hours, 0)} h <small>(first to last detection, not fire duration)</small>`],
      ["Evidence", `${EVIDENCE_LABEL[ev.evidence] ?? esc(ev.evidence)} <small>(${ev.pass_count} pass${ev.pass_count === 1 ? "" : "es"})</small>`],
      ["Detections", `${ev.detection_count} <small>(VIIRS ${ev.viirs_count} · MODIS ${ev.modis_count})</small>`],
      ["Satellites", sats ? `<small>${sats}</small>` : "–"],
      ["Peak fire power", `${fmtNum(ev.peak_pass_frp_mw)} MW <small>(whole fire, one ${esc(ev.peak_pass_sensor ?? "")} pass)</small>`],
      ["Max pixel FRP", `${fmtNum(ev.max_frp_mw)} MW`],
      ["Mean pixel FRP", meanFrp ? `<small>${meanFrp}</small>` : "–"],
      ["Intensity trend", esc(TREND_LABEL[ev.intensity_trend] ?? ev.intensity_trend)],
      ["Detected footprint", ev.area_basis === "modis_1km"
        ? `≈ ${fmtNum(ev.area_km2, 1)} km² <small>(MODIS 1 km pixels, coarse)</small>`
        : `${fmtNum(ev.area_km2, 2)} km² <small>(VIIRS 375 m pixels)</small>`],
      ["Detections span", `${fmtNum(ev.detection_hull_km2, 2)} km² <small>(hull of detection centres) · ${fmtNum(ev.extent_km, 1)} km across</small>`],
      ...(growth ? [["Footprint growth", `${growth} <small>(apparent, from detections)</small>`] as [string, string]] : []),
      ["Burned area", "<small>See the satellite report below (Sentinel-2 burn scar)</small>"],
      ["Max confidence", `${ev.max_confidence_pct}%`],
    ])}
    <div class="frp-timeline"><p class="hint">Loading fire power per satellite pass…</p></div>
    <p class="hint">Detected footprint = the satellite pixels that saw fire over the event's life, at the sensor's
      nominal resolution. Neither it nor the detections span is burned area. "No detection" can mean cloud, smoke or
      a missed pass, not that the fire is out.</p>
    <div class="fire-report"></div>
    <details class="history"><summary>Change history</summary><div class="history-body">Loading…</div></details>
    <div class="actions">
      <button type="button" id="focus-dets">Show only this event's detections</button>
    </div>
    ${riskButton(ev.latitude, ev.longitude)}
  `);
  $("#focus-dets").addEventListener("click", () => onFocusDetections(ev.id));
  // a current front to spread from: detected in the last day (as the API requires)
  mountFireReport($(".fire-report"), ev.id, Date.now() - new Date(ev.end_at).getTime() < 24 * 3_600_000);
  api.eventTimeline(ev.id).then(
    (passes) => ($(".frp-timeline").innerHTML = frpChart(passes)),
    () => ($(".frp-timeline").innerHTML = ""),
  );
  $<HTMLDetailsElement>("details.history").addEventListener("toggle", function onOpen(e) {
    const box = e.currentTarget as HTMLDetailsElement;
    if (!box.open) return;
    box.removeEventListener("toggle", onOpen);
    api.eventHistory(ev.id).then(
      (h) => ($(".history-body").innerHTML = historyList(h)),
      (err: Error) => ($(".history-body").textContent = `Could not load history: ${err.message}`),
    );
  });
}

/** Whole-fire FRP per overpass; VIIRS and MODIS drawn as separate series (not cross-calibrated). */
function frpChart(passes: EventPass[]): string {
  if (passes.length < 2) return "";
  const W = 300, H = 70, pad = 4;
  const t0 = new Date(passes[0].acquired_at).getTime();
  const t1 = new Date(passes[passes.length - 1].acquired_at).getTime();
  const max = Math.max(...passes.map((p) => p.frp_sum), 1);
  const x = (p: EventPass) => pad + ((new Date(p.acquired_at).getTime() - t0) / Math.max(1, t1 - t0)) * (W - 2 * pad);
  const y = (p: EventPass) => H - pad - (p.frp_sum / max) * (H - 2 * pad);
  const series = (ins: string, cls: string) => {
    const pts = passes.filter((p) => p.instrument === ins);
    if (!pts.length) return "";
    const line = pts.length > 1 ? `<polyline class="${cls}" points="${pts.map((p) => `${x(p).toFixed(1)},${y(p).toFixed(1)}`).join(" ")}" />` : "";
    return line + pts.map((p) => `<circle class="${cls}" cx="${x(p).toFixed(1)}" cy="${y(p).toFixed(1)}" r="2.5"><title>${esc(p.satellite)} ${esc(fmtTime(p.acquired_at))}: ${fmtNum(p.frp_sum)} MW (${p.pixels} px)</title></circle>`).join("");
  };
  return `<div class="spark-title">Fire power per satellite pass <small>(max ${fmtNum(max)} MW · <span class="viirs">VIIRS</span> · <span class="modis">MODIS</span>)</small></div>
    <svg class="spark" viewBox="0 0 ${W} ${H}" role="img" aria-label="Fire radiative power per overpass">${series("VIIRS", "viirs")}${series("MODIS", "modis")}</svg>`;
}

const CHANGE_LABEL: Record<string, string> = {
  created: "First detected", grown: "New detections", updated: "Updated", state: "State changed",
  merged: "Merged", split: "Split", renamed: "Renamed", removed: "Removed (not a fire)",
};

function historyList(entries: EventHistoryEntry[]): string {
  if (!entries.length) return "No recorded changes.";
  return `<ol class="history-list">${entries.map((h) => {
    const snap = h.snapshot as { detection_count?: number; area_km2?: number };
    const what = [
      h.change === "state" || h.from_state !== h.to_state ? `${esc(h.from_state ?? "–")} → ${esc(h.to_state ?? "–")}` : "",
      snap.detection_count != null ? `${snap.detection_count} det.` : "",
      snap.area_km2 != null ? `${fmtNum(snap.area_km2, 2)} km²` : "",
      h.related_id ? `<span class="mono">${esc(h.related_id)}</span>` : "",
      h.reason ? `<small>${esc(h.reason.replaceAll("_", " "))}</small>` : "",
    ].filter(Boolean).join(" · ");
    return `<li><b>${esc(CHANGE_LABEL[h.change] ?? h.change)}</b> <small>${esc(fmtTime(h.at))}${h.event_id !== entries[entries.length - 1].event_id ? ` · ${esc(h.event_id)}` : ""}</small><br>${what}</li>`;
  }).join("")}</ol>`;
}

export function showDetection(d: FireDetection): void {
  panel(`
    <h2>${esc(d.instrument)} detection</h2>
    ${kv([
      ["Satellite", `${esc(d.satellite)} <small>(${esc(d.source)})</small>`],
      ["Acquired", esc(fmtTime(d.acquired_at))],
      ["Location", `${d.latitude.toFixed(5)}, ${d.longitude.toFixed(5)}`],
      ["FRP", `${fmtNum(d.frp_mw)} MW`],
      ["Confidence", `${esc(d.confidence_level)} · ${d.confidence_pct}% <small>(raw "${esc(d.confidence_raw)}")</small>`],
      ["Brightness", `${fmtNum(d.brightness_k)} K${d.brightness_saturated ? ' <span class="pill warning">saturated</span>' : ""}`],
      ["Brightness 11 µm", `${fmtNum(d.brightness_secondary_k)} K`],
      ["Pixel", `${d.scan_km} × ${d.track_km} km (${fmtNum(d.pixel_area_km2, 3)} km²)`],
      ["Day/night", d.daynight === "N" ? "Night" : d.daynight === "D" ? "Day" : "–"],
      ["Processing", esc(d.processing_level)],
      ["Event", d.event_id ? `<a href="#" data-event="${esc(d.event_id)}">${esc(d.event_id)}</a>` : "–"],
      ...(d.static_kind ? [["Not a wildfire", `<span class="pill warning">${esc(STATIC_LABEL[d.static_kind])}</span>
        <small>${d.detection_type != null ? "labelled by the NASA archive" : "on a site the NASA archive labelled"}</small>`] as [string, string]] : []),
    ])}
    ${riskButton(d.latitude, d.longitude)}
  `);
}

export function showPoint(lat: number, lon: number): void {
  panel(`
    <h2>Location</h2>
    ${kv([["Coordinates", `${lat.toFixed(4)}, ${lon.toFixed(4)}`]])}
    ${riskButton(lat, lon)}
  `);
}

function renderRisk(r: RiskForecast): string {
  const rows = r.days
    .map(
      (d) => `<tr>
        <td>${esc(new Date(d.date + "T00:00:00Z").toLocaleDateString(undefined, { weekday: "short", day: "2-digit", timeZone: "UTC" }))}</td>
        <td><span class="pill risk-${d.level}">${LEVEL_LABEL[d.level]}</span></td>
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
      <thead><tr><th>Day</th><th>Danger</th><th>Score</th><th>Tmax</th><th>RHmin</th><th>Wind m/s</th><th>Dry days</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${r.surface === "water" || r.surface === "open_ocean"
      ? `<p class="hint"><b>${r.surface === "open_ocean" ? "Open ocean" : "Water"}</b> (Sentinel-2: ${fmtNum((r.water_fraction ?? 1) * 100, 0)}% water): nothing to burn, so no fire danger whatever the weather.</p>`
      : ""}
    <p class="hint">Fire activity within 50 km: ${r.nearby_detections_7d} detections last 7 d
      (prev. 7 d: ${r.nearby_detections_prev_7d}) · trend <b>${esc(r.activity_trend)}</b>
      ${r.ndvi != null ? ` · Sentinel-2 NDVI ${r.ndvi.toFixed(2)}` : ""}</p>
    <p class="hint">${esc(r.model)} — ${esc(r.disclaimer)} Weather: Open-Meteo.</p>`;
}

/** Delegated handlers for buttons rendered inside the details panel. */
export function wireDetails(openEvent: (id: string) => void): void {
  $("#details-close").addEventListener("click", closeDetails);
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
    const [lat, lon] = btn.dataset.risk!.split(",").map(Number);
    const slot = btn.nextElementSibling as HTMLElement;
    btn.disabled = true;
    slot.innerHTML = '<p class="hint">Loading forecast…</p>';
    try {
      slot.innerHTML = renderRisk(await api.risk(lat, lon));
    } catch (err) {
      slot.innerHTML = "";
      toast(`Forecast failed: ${(err as Error).message}`, "error");
    } finally {
      btn.disabled = false;
    }
  });
}
