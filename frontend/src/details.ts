import { api, type FireDetection, type FireEventDetail, type RiskForecast } from "./api";
import { $, esc, fmtNum, fmtTime, toast } from "./util";

const LEVEL_LABEL: Record<string, string> = {
  low: "Low", moderate: "Moderate", high: "High", very_high: "Very high", extreme: "Extreme",
};

function panel(html: string): void {
  $("#details-body").innerHTML = html;
  $("#details").classList.remove("hidden");
}

export function closeDetails(): void {
  $("#details").classList.add("hidden");
}

function riskButton(lat: number, lon: number): string {
  return `<button type="button" class="secondary" data-risk="${lat},${lon}">Fire-danger forecast here</button><div class="risk-slot"></div>`;
}

function kv(rows: [string, string][]): string {
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

export function showEvent(ev: FireEventDetail, onFocusDetections: (id: string) => void): void {
  const hours = (new Date(ev.end_at).getTime() - new Date(ev.start_at).getTime()) / 3_600_000;
  panel(`
    <h2>Fire event <span class="pill ${ev.status}">${ev.status}</span></h2>
    <p class="mono">${esc(ev.id)}</p>
    ${kv([
      ["Location", `${ev.latitude.toFixed(4)}, ${ev.longitude.toFixed(4)}`],
      ["First seen", esc(fmtTime(ev.start_at))],
      ["Last seen", esc(fmtTime(ev.end_at))],
      ["Duration", `${fmtNum(hours, 0)} h`],
      ["Footprint area", `${fmtNum(ev.area_km2, 2)} km²`],
      ["Detections", `${ev.detection_count} <small>(VIIRS ${ev.viirs_count} · MODIS ${ev.modis_count})</small>`],
      ["Max FRP", `${fmtNum(ev.max_frp_mw)} MW`],
      ["Total FRP", `${fmtNum(ev.total_frp_mw, 0)} MW`],
      ["Max confidence", `${ev.max_confidence_pct}%`],
    ])}
    <p class="hint">Footprint = union of active-fire pixels over the event's life (VIIRS when available). It is not burned area.</p>
    <div class="actions">
      <button type="button" id="focus-dets">Show only this event's detections</button>
    </div>
    ${riskButton(ev.latitude, ev.longitude)}
  `);
  $("#focus-dets").addEventListener("click", () => onFocusDetections(ev.id));
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
