// Analysis tab: fire statistics and trends for the area in view and the selected period.
import { api, type AnalysisSummary, type Params } from "./api";
import { stateLabel } from "./details";
import { $, esc, fmtNum, fmtTime } from "./util";

const W = 312; // plot width in px (the sidebar is fixed-width)
const fmtInt = (n: number) => n.toLocaleString();
const fmtDay = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" });

function change(pct: number | null, comparable: boolean): string {
  if (!comparable) return `<small class="delta">previous period not fully loaded</small>`;
  if (pct === null) return `<small class="delta">none in the previous period</small>`;
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "■";
  return `<small class="delta">${arrow} ${pct > 0 ? "+" : ""}${fmtNum(pct, 0)}% vs previous period</small>`;
}

interface Bar {
  label: string; // tooltip heading
  tick?: string; // x-axis label, when shown
  viirs: number;
  modis: number;
  extra?: string;
}

/** Stacked bars, VIIRS under MODIS, one shared y-axis. Hover shows the values. */
function bars(data: Bar[], height: number, mark?: Set<number>): string {
  const padL = 34, padB = 16, padT = 6;
  const plotW = W - padL, plotH = height - padB - padT;
  const max = Math.max(1, ...data.map((d) => d.viirs + d.modis));
  const step = plotW / data.length;
  const bw = Math.max(1.5, Math.min(18, step - 2));
  const y = (v: number) => padT + plotH - (v / max) * plotH;
  const grid = [0.5, 1].map((f) => {
    const v = max * f;
    return `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${padL - 4}" y="${y(v) + 3}" text-anchor="end">${fmtShort(v)}</text>`;
  }).join("");
  const everyTick = Math.max(1, Math.ceil(data.length / 6));
  const marks = data.map((d, i) => {
    const x = padL + i * step + (step - bw) / 2;
    const hv = (d.viirs / max) * plotH, hm = (d.modis / max) * plotH;
    const gap = hv > 2 && hm > 2 ? 1 : 0; // surface gap between the two segments
    const tip = `${d.label}|VIIRS ${fmtInt(d.viirs)}|MODIS ${fmtInt(d.modis)}${d.extra ? `|${d.extra}` : ""}`;
    return `<g class="bar" data-tip="${esc(tip)}">
      <rect class="hit" x="${padL + i * step}" y="${padT}" width="${step}" height="${plotH}"/>
      ${hv > 0 ? `<rect class="viirs" x="${x}" y="${y(d.viirs)}" width="${bw}" height="${hv}" rx="${Math.min(2, bw / 2)}"/>` : ""}
      ${hm > 0 ? `<rect class="modis" x="${x}" y="${y(d.viirs + d.modis)}" width="${bw}" height="${Math.max(0, hm - gap)}" rx="${Math.min(2, bw / 2)}"/>` : ""}
      ${mark?.has(i) ? `<circle class="flag" cx="${x + bw / 2}" cy="${Math.max(4, y(d.viirs + d.modis) - 5)}" r="2.5"/>` : ""}
      ${d.tick && i % everyTick === 0 ? `<text class="axis" x="${x + bw / 2}" y="${height - 3}" text-anchor="middle">${esc(d.tick)}</text>` : ""}
    </g>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${height}" role="img">${grid}<line class="base" x1="${padL}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>${marks}</svg>`;
}

function fmtShort(v: number): string {
  if (v >= 10_000) return `${Math.round(v / 1000)}k`;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return String(Math.round(v));
}

const LEGEND = `<div class="chart-legend"><span><i class="sw viirs"></i>VIIRS (375 m)</span><span><i class="sw modis"></i>MODIS (1 km)</span></div>`;

function share(parts: [string, number][]): string {
  const total = parts.reduce((a, [, n]) => a + n, 0);
  if (!total) return `<p class="hint">None.</p>`;
  return `<dl class="kv">${parts.map(([k, n]) => `<dt>${esc(k)}</dt><dd>${fmtInt(n)} <small>(${fmtNum((100 * n) / total, 0)}%)</small></dd>`).join("")}</dl>`;
}

const EVIDENCE: Record<string, string> = { multi_pass: "Seen on several passes", single_pass: "One satellite pass", single_detection: "One detection" };

function eventList(events: AnalysisSummary["events"]["largest_footprint"], by: "area" | "frp"): string {
  if (!events.length) return `<li class="empty">No fire events started in this period.</li>`;
  return events.map((ev) => `<li><a href="#" data-event="${esc(ev.id)}">
      <b>${by === "area" ? `${fmtNum(ev.area_km2, 2)} km² detected footprint` : `${fmtNum(ev.peak_pass_frp_mw, 0)} MW in one ${esc(ev.peak_pass_sensor ?? "")} pass`}</b>
      <small>${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)} · ${ev.detection_count} det. · ${esc(stateLabel(ev))}</small></a></li>`).join("");
}

function areaLabel(b: number[]): string {
  if (b[0] <= -180 && b[2] >= 180 && b[1] <= -90 && b[3] >= 90) return "Whole world";
  const lon = (x: number) => `${Math.abs(x).toFixed(1)}°${x < 0 ? "W" : "E"}`;
  const lat = (y: number) => `${Math.abs(y).toFixed(1)}°${y < 0 ? "S" : "N"}`;
  return `${lon(b[0])} to ${lon(b[2])}, ${lat(b[1])} to ${lat(b[3])}`;
}

function render(a: AnalysisSummary): string {
  const d = a.detections;
  const unusual = new Set(a.unusual_days.map((u) => u.date));
  const daily: Bar[] = a.daily.map((x) => ({
    label: fmtDay(x.date), tick: fmtDay(x.date), viirs: x.viirs, modis: x.modis,
    extra: `${x.events_started} fire event${x.events_started === 1 ? "" : "s"} started${unusual.has(x.date) ? "|Unusually high" : ""}`,
  }));
  const marks = new Set(a.daily.map((x, i) => (unusual.has(x.date) ? i : -1)).filter((i) => i >= 0));
  const tod: Bar[] = a.time_of_day.hours.map((h) => ({ label: `${String(h.hour).padStart(2, "0")}:00–${String(h.hour).padStart(2, "0")}:59`, tick: String(h.hour), viirs: h.viirs, modis: h.modis }));
  const t = a.trend;
  const trendText = t.direction === "not_comparable"
    ? "No trend shown: some days of this period are not loaded, and they would look like quiet days."
    : t.direction === "insufficient_data"
    ? "No trend: it needs a period of at least 2 days with detections in its first half."
    : `${t.direction === "rising" ? "Rising" : t.direction === "falling" ? "Falling" : "Stable"}: ${fmtInt(t.second_half ?? 0)} detections in the second half of the period against ${fmtInt(t.first_half ?? 0)} in the first (${(t.change_pct ?? 0) > 0 ? "+" : ""}${fmtNum(t.change_pct, 0)}%).`;
  const cov = a.coverage;
  const sensor = (name: string, s: AnalysisSummary["detections"]["by_sensor"]["viirs"]) =>
    `<dt>${name}</dt><dd>${fmtInt(s.detections)} det. · max ${fmtNum(s.max_frp_mw)} MW · mean pixel ${s.mean_pixel_frp_mw == null ? "–" : `${fmtNum(s.mean_pixel_frp_mw)} MW`}</dd>`;
  return `
    <p class="hint"><b>Area:</b> ${esc(areaLabel(a.bbox))}<br>${esc(fmtTime(a.start))} – ${esc(fmtTime(a.end))} · ${a.method === "detections" ? "exact count" : `counted in ${fmtNum(Number(a.method.slice(6)), 2)}° cells (approximate at the edges of the view)`}</p>
    ${cov.complete ? "" : `<p class="banner warn">Only ${cov.loaded_days} of ${cov.days} days are fully loaded for this area, so the totals are incomplete. Load the missing days in History mode.</p>`}
    <div class="kpis">
      <div><b>${fmtInt(d.total)}</b><span>fire detections</span>${change(a.previous.detections_change_pct, a.previous.comparable)}</div>
      <div><b>${fmtInt(a.events.started)}</b><span>fire events started</span>${change(a.previous.events_change_pct, a.previous.comparable)}</div>
    </div>
    <h3>Detections per day</h3>
    ${LEGEND}
    ${bars(daily, 120, marks)}
    <p class="hint">${esc(trendText)}${a.unusual_days.length ? ` <span class="flag-dot"></span> ${a.unusual_days.length} unusually high day${a.unusual_days.length > 1 ? "s" : ""} (typical: ${fmtNum(a.unusual_days[0].typical, 0)}/day).` : ""}</p>
    <h3>Time of day <small>(${a.time_of_day.basis === "local_solar" ? `local solar time, UTC${a.time_of_day.utc_offset_hours >= 0 ? "+" : ""}${a.time_of_day.utc_offset_hours}` : "UTC: the view is too wide for one local time"})</small></h3>
    ${bars(tod, 90)}
    <p class="hint">Shows when satellites passed over and saw fire, not when fires burn: each satellite crosses a place at fixed local times.</p>
    <h3>By sensor</h3>
    <dl class="kv">${sensor("VIIRS", d.by_sensor.viirs)}${sensor("MODIS", d.by_sensor.modis)}</dl>
    <p class="hint">The sensors differ in resolution and sensitivity, so their FRP values are listed separately and not combined.</p>
    <h3>Detection confidence</h3>
    ${share([["High", d.by_confidence.high], ["Nominal", d.by_confidence.nominal], ["Low", d.by_confidence.low]])}
    <h3>Fire events: evidence</h3>
    ${share(Object.entries(EVIDENCE).map(([k, label]) => [label, a.events.by_evidence[k] ?? 0] as [string, number]))}
    <h3>Largest detected footprint</h3>
    <ul class="list">${eventList(a.events.largest_footprint, "area")}</ul>
    <h3>Strongest fire power</h3>
    <ul class="list">${eventList(a.events.strongest, "frp")}</ul>
    <p class="hint">A detection is one satellite pixel that saw fire on one pass: counts measure observed fire activity, not the number of fires or the area burned. FireLens does not measure burned area.</p>`;
}

let seq = 0;

/** Run the analysis for a view and period and draw it. `time` is {hours} or {start, end}. */
export async function runAnalysis(bbox: [number, number, number, number] | null, time: Params): Promise<void> {
  const box = $("#analysis-body");
  const mine = ++seq;
  box.innerHTML = `<p class="hint">Analysing…</p>`;
  try {
    const a = await api.analysis({ ...time, bbox: bbox ? bbox.join(",") : undefined });
    if (mine === seq) box.innerHTML = render(a);
  } catch (err) {
    if (mine === seq) box.innerHTML = `<p class="banner warn">Analysis failed: ${esc((err as Error).message)}</p>`;
  }
}

/** Hover tooltips for the bars (one listener for the whole panel). */
export function wireAnalysis(onEvent: (id: string) => void): void {
  const panel = $("#tab-analysis");
  const tip = $("#chart-tip");
  panel.addEventListener("mousemove", (e) => {
    const bar = (e.target as Element).closest<SVGGElement>("g.bar");
    if (!bar) return tip.classList.add("hidden");
    const [head, ...rows] = (bar.dataset.tip ?? "").split("|");
    tip.innerHTML = `<b>${esc(head)}</b>${rows.map((r) => `<div>${esc(r)}</div>`).join("")}`;
    tip.classList.remove("hidden");
    const x = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = `${x}px`;
    tip.style.top = `${e.clientY + 14}px`;
  });
  panel.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  panel.addEventListener("click", (e) => {
    const id = (e.target as HTMLElement).closest<HTMLElement>("[data-event]")?.dataset.event;
    if (id) {
      e.preventDefault();
      onEvent(id);
    }
  });
}
