// Analysis tab: fire statistics and trends for the area in view and the selected period.
import { api, type AnalysisSummary, type BaselineGroup, type BaselineVerdict, type LandcoverBreakdown, type Params, type SeasonalBaseline, type SensorComparison } from "./api";
import { stateLabel } from "./details";
import { LAND_COLOUR, shareBar } from "./firereport";
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
    <div id="baseline-section"><h3>Compared with past seasons</h3><p class="hint">Looking up past seasons…</p></div>
    <div id="landcover-section"><h3>What is burning</h3><p class="hint">Reading land cover…</p></div>
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

// ---------------------------------------------------------------- seasonal baseline

const VERDICT: Record<BaselineVerdict, { text: string; tone: "up" | "down" | "flat" | "none" }> = {
  well_above: { text: "Well above normal", tone: "up" },
  above: { text: "Above normal", tone: "up" },
  within_range: { text: "Within the normal range", tone: "flat" },
  below: { text: "Below normal", tone: "down" },
  well_below: { text: "Well below normal", tone: "down" },
  insufficient_seasons: { text: "Not enough past seasons", tone: "none" },
  too_few_fires: { text: "Too few fires to compare", tone: "none" },
  current_incomplete: { text: "Not comparable yet", tone: "none" },
  no_archive: { text: "No archive for these dates", tone: "none" },
  area_not_covered: { text: "No archive for this area yet", tone: "none" },
};

const BASELINE_WHY: Record<string, string> = {
  no_baseline: "The seasonal archive has not been built on this server yet.",
  period_too_short: "It compares whole days (UTC) that ended more than 6 hours ago, and this period has none: choose “7 days” under Filters → Time, or a History range.",
  area_not_covered: "The archive of past seasons does not cover this area yet (it is being downloaded worldwide; Argentina is ready).",
  no_archive: "The archive of past seasons does not cover these dates.",
};

const dayRange = (first: string, last: string) => (first === last ? fmtDay(first) : `${fmtDay(first)} – ${fmtDay(last)}`);

/** One bar per season (oldest first), this period last and highlighted; dashed line at the median. */
function seasonBars(g: BaselineGroup, n: number): string {
  const years = [...(g.years ?? [])].sort((a, b) => a.year - b.year);
  const rows = [...years.map((y) => ({ label: String(y.year), value: y.detections, current: false, first: y.first })),
    { label: "This year", value: g.current ?? 0, current: true, first: "" }];
  const H = 130, padL = 34, padB = 16, padT = 14;
  const plotW = W - padL, plotH = H - padB - padT;
  const max = Math.max(1, ...rows.map((r) => r.value));
  const step = plotW / rows.length;
  const bw = Math.min(34, step - 6);
  const y = (v: number) => padT + plotH - (v / max) * plotH;
  const med = g.median ?? 0;
  const bars = rows.map((r, i) => {
    const x = padL + i * step + (step - bw) / 2;
    const tip = r.current
      ? `This period|${fmtInt(r.value)} detections${g.ratio != null ? `|${fmtNum(g.ratio, 2)}× the median season` : ""}`
      : `${r.label}|${fmtInt(r.value)} detections|${dayRange(r.first, addDays(r.first, n - 1))}`;
    return `<g class="bar" data-tip="${esc(tip)}">
      <rect class="hit" x="${padL + i * step}" y="${padT}" width="${step}" height="${plotH}"/>
      ${r.value > 0 ? `<rect class="${r.current ? "season-now" : "season"}" x="${x}" y="${y(r.value)}" width="${bw}" height="${y(0) - y(r.value)}" rx="3"/>` : ""}
      ${r.current ? `<text class="axis val-now" x="${x + bw / 2}" y="${y(r.value) - 4}" text-anchor="middle">${fmtShort(r.value)}</text>` : ""}
      <text class="axis${r.current ? " now" : ""}" x="${x + bw / 2}" y="${H - 3}" text-anchor="middle">${esc(r.label)}</text>
    </g>`;
  }).join("");
  const grid = [0.5, 1].map((f) => `<line class="grid" x1="${padL}" x2="${W}" y1="${y(max * f)}" y2="${y(max * f)}"/>
    <text class="axis" x="${padL - 4}" y="${y(max * f) + 3}" text-anchor="end">${fmtShort(max * f)}</text>`).join("");
  const medLine = `<line class="median" x1="${padL}" x2="${W}" y1="${y(med)}" y2="${y(med)}"/>
    <text class="axis median-label" x="${padL + 2}" y="${y(med) - 3}">typical ${fmtShort(med)}</text>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Detections per season">${grid}
    <line class="base" x1="${padL}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>${bars}${medLine}</svg>`;
}

function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

function baselineSentence(g: BaselineGroup, days: string): string {
  const years = (g.years ?? []).map((y) => y.year);
  const span = years.length ? `${Math.min(...years)}–${Math.max(...years)}` : "";
  const n = g.seasons;
  const place = g.rank === 1 ? `more than in any of the ${n} past seasons` : g.rank === n + 1 ? `fewer than in any of the ${n} past seasons` : `${ordinal(g.rank ?? 0)} of ${n + 1} seasons`;
  return `${fmtInt(g.current ?? 0)} ${esc(g.label)} detections on ${days}: ${place}
    (${span}: ${fmtInt(g.min ?? 0)}–${fmtInt(g.max ?? 0)}, typical ${fmtInt(Math.round(g.median ?? 0))}${g.ratio != null ? `; ${fmtNum(g.ratio, 2)}× typical` : ""}).`;
}

function ordinal(k: number): string {
  const s = k % 100 >= 11 && k % 100 <= 13 ? "th" : (["th", "st", "nd", "rd"][k % 10] ?? "th");
  return `${k}${s}`;
}

function renderBaseline(b: SeasonalBaseline): string {
  if (!b.available || !b.headline) {
    const why = BASELINE_WHY[b.reason ?? ""] ?? Object.values(b.groups).map((g) => VERDICT[g.verdict]?.text).filter(Boolean)[0] ?? "";
    return `<h3>Compared with past seasons</h3><p class="hint">${esc(why)}</p>`;
  }
  const g = b.groups[b.headline];
  const other = b.groups[b.headline === "viirs_noaa20" ? "modis" : "viirs_noaa20"];
  const days = b.days ? dayRange(b.days.first, b.days.last) : "";
  const v = VERDICT[g.verdict];
  const cd = g.current_days;
  const notes = [
    b.days && b.days.count < 7 ? `Only ${b.days.count} day${b.days.count > 1 ? "s" : ""}: short periods swing a lot from year to year.` : "",
    g.area_fraction < 0.98 ? `Counted over ${fmtNum(100 * g.area_fraction, 0)}% of the view (whole ${b.cell_deg}° cells the archive covers in every year).` : `Counted over whole ${b.cell_deg}° cells around the view.`,
    cd && cd.missing ? `${cd.missing} day${cd.missing > 1 ? "s are" : " is"} missing for this period.` : "",
  ].filter(Boolean).join(" ");
  const otherLine = other && other.seasons >= 2 && other.current != null
    ? `<dl class="kv"><dt>${esc(other.label)}</dt><dd>${esc(VERDICT[other.verdict].text)} · ${fmtInt(other.current)} vs typical ${fmtInt(Math.round(other.median ?? 0))}</dd></dl>`
    : "";
  return `
    <h3>Compared with past seasons</h3>
    <p class="verdict ${v.tone}"><span class="verdict-icon" aria-hidden="true">${v.tone === "up" ? "▲" : v.tone === "down" ? "▼" : v.tone === "flat" ? "●" : "○"}</span>${esc(v.text)}</p>
    <p class="hint">${baselineSentence(g, days)}</p>
    ${g.seasons >= 2 ? seasonBars(g, b.days?.count ?? 1) : ""}
    ${otherLine}
    <p class="hint">Same calendar days of each year, same satellites every year (${esc(g.label)}; NOAA-21 is left out because it only flies since 2023, and more satellites mean more detections).
      ${esc(notes)} "Above" or "below" normal means outside the range of every past season.</p>`;
}

// ---------------------------------------------------------------- what is burning

function renderLandcover(l: LandcoverBreakdown): string {
  if (!l.detections) return `<h3>What is burning</h3><p class="hint">No detections in this area and period.</p>`;
  const top = l.classes[0];
  const rows = l.classes.filter((c) => c.share >= 0.01).slice(0, 6);
  return `
    <h3>What is burning</h3>
    ${top ? `<p class="lead-line">${fmtNum(100 * top.share, 0)}% of detections on <b>${esc(top.label.toLowerCase())}</b>${top.meaning ? ` <small>(${esc(top.meaning)})</small>` : ""}</p>` : ""}
    ${shareBar(l.classes.map((c) => ({ key: c.class, label: c.label, share: c.share, colour: LAND_COLOUR[c.class] ?? "#777" })), 5)}
    <dl class="kv">${rows.map((c) => `<dt>${esc(c.label)}</dt><dd>${fmtInt(c.detections)} <small>(${fmtNum(100 * c.share, 0)}%${c.meaning ? ` · ${esc(c.meaning)}` : ""})</small></dd>`).join("")}</dl>
    <p class="hint">Land cover under each detection's centre (ESA WorldCover 2021, ~150 m).${l.coverage < 1 ? ` Still downloading the map for ${fmtNum(100 * (1 - l.coverage), 0)}% of the detections: run again in a minute.` : ""}</p>`;
}

// ---------------------------------------------------------------- MODIS vs VIIRS

const pct = (x: number | null) => (x == null ? "–" : `${fmtNum(x, 0)}%`);

/** One horizontal bar per row, a shared 0-100 % scale. */
function pctBars(rows: { label: string; value: number | null; note: string; muted?: boolean }[]): string {
  const labelW = 132, barW = W - labelW - 40, rowH = 22;
  const h = rows.length * rowH;
  const hatch = `<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
    <rect width="5" height="5" fill="#4f5864"/><line x1="0" y1="0" x2="0" y2="5" stroke="#8b98a9" stroke-width="1.5"/></pattern></defs>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${h}" role="img">${hatch}${rows.map((r, i) => {
    const y = i * rowH;
    const v = r.value ?? 0;
    return `<g class="bar" data-tip="${esc(`${r.label}|${pct(r.value)} ${r.note}`)}">
      <rect class="hit" x="0" y="${y}" width="${W}" height="${rowH}"/>
      <text class="axis lbl" x="0" y="${y + 14}">${esc(r.label)}</text>
      <rect class="track" x="${labelW}" y="${y + 6}" width="${barW}" height="10" rx="2"/>
      ${r.value != null ? `<rect class="${r.muted ? "baseline" : "agree"}" x="${labelW}" y="${y + 6}" width="${(v / 100) * barW}" height="10" rx="2"/>` : ""}
      <text class="axis val" x="${W}" y="${y + 14}" text-anchor="end">${pct(r.value)}</text>
    </g>`;
  }).join("")}</svg>`;
}

/** Log-log scatter of coincident fire clusters: MODIS FRP (x) vs VIIRS FRP (y), 1:1 line. */
function frpScatter(points: SensorComparison["modis_vs_viirs"]["clusters"]["points"]): string {
  const pts = points.filter((p) => p.frp_a > 0 && p.frp_b > 0);
  if (!pts.length) return `<p class="hint">No coincident fire clusters with FRP from both sensors.</p>`;
  const H = 220, pad = 30;
  const lo = Math.floor(Math.log10(Math.min(...pts.map((p) => Math.min(p.frp_a, p.frp_b)))));
  const hi = Math.ceil(Math.log10(Math.max(...pts.map((p) => Math.max(p.frp_a, p.frp_b)))));
  const span = Math.max(1, hi - lo);
  const sx = (v: number) => pad + ((Math.log10(v) - lo) / span) * (W - pad - 6);
  const sy = (v: number) => H - pad - ((Math.log10(v) - lo) / span) * (H - pad - 6);
  const ticks = Array.from({ length: span + 1 }, (_, k) => 10 ** (lo + k));
  const fmt = (v: number) => (v >= 1000 ? `${v / 1000}k` : String(v));
  const axes = ticks.map((t) => `<line class="grid" x1="${sx(t)}" x2="${sx(t)}" y1="6" y2="${H - pad}"/><line class="grid" x1="${pad}" x2="${W - 6}" y1="${sy(t)}" y2="${sy(t)}"/>
    <text class="axis" x="${sx(t)}" y="${H - pad + 12}" text-anchor="middle">${fmt(t)}</text><text class="axis" x="${pad - 4}" y="${sy(t) + 3}" text-anchor="end">${fmt(t)}</text>`).join("");
  const one = `<line class="identity" x1="${sx(10 ** lo)}" y1="${sy(10 ** lo)}" x2="${sx(10 ** hi)}" y2="${sy(10 ** hi)}"/>`;
  const dots = pts.map((p) => `<g class="bar" data-tip="${esc(`Fire cluster at ${p.lat.toFixed(2)}, ${p.lon.toFixed(2)}|MODIS (${p.sat_a}) ${fmtNum(p.frp_a)} MW · ${p.pixels_a} px|VIIRS (${p.sat_b}) ${fmtNum(p.frp_b)} MW · ${p.pixels_b} px|${p.minutes_apart} min apart`)}">
      <circle class="hit" cx="${sx(p.frp_a)}" cy="${sy(p.frp_b)}" r="7"/><circle class="dot" cx="${sx(p.frp_a)}" cy="${sy(p.frp_b)}" r="3"/></g>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">${axes}${one}${dots}
    <text class="axis" x="${W - 6}" y="${H - 4}" text-anchor="end">MODIS fire power (MW) →</text>
    <text class="axis" x="${pad + 4}" y="14">↑ VIIRS fire power (MW)</text>
    <text class="axis id-label" x="${sx(10 ** hi) - 4}" y="${sy(10 ** hi) + 14}" text-anchor="end">same power</text></svg>`;
}

function renderSensors(r: SensorComparison): string {
  const m = r.modis_vs_viirs, b = r.baseline_viirs_vs_viirs, ev = r.events, cl = m.clusters;
  const rows = [
    { label: "MODIS seen by VIIRS", value: m.a.matched_pct, note: `of ${fmtInt(m.a.in_scenes)} MODIS detections in coincident scenes` },
    { label: "VIIRS seen by MODIS", value: m.b.matched_pct, note: `of ${fmtInt(m.b.in_scenes)} VIIRS detections in coincident scenes` },
    ...(b ? [
      { label: "Baseline: NOAA-20 → 21", value: b.a.matched_pct, note: `identical VIIRS sensors, ${b.clusters.median_minutes_apart ?? "–"} min apart`, muted: true },
      { label: "Baseline: NOAA-21 → 20", value: b.b.matched_pct, note: "identical VIIRS sensors", muted: true },
    ] : []),
  ];
  const conf = (["high", "nominal", "low"] as const).map((c) =>
    `<dt>${c[0].toUpperCase() + c.slice(1)}</dt><dd>MODIS ${pct(m.a.by_confidence[c].matched_pct)} <small>(${fmtInt(m.a.by_confidence[c].in_scenes)})</small> · VIIRS ${pct(m.b.by_confidence[c].matched_pct)} <small>(${fmtInt(m.b.by_confidence[c].in_scenes)})</small></dd>`).join("");
  const firstV = ev.first_seen_by.viirs ?? 0, firstM = ev.first_seen_by.modis ?? 0;
  const ratio = cl.median_frp_ratio_b_over_a;
  return `
    <p class="hint"><b>Area:</b> ${esc(areaLabel(r.bbox))}<br>${esc(fmtTime(r.start))} – ${esc(fmtTime(r.end))}</p>
    <p class="hint">Only places both sensors imaged within ${r.window_minutes} min are compared (each has a detection within ${r.scene_km} km).
      Aqua now passes ~100 min after the VIIRS satellites; Terra never coincides (${fmtInt(r.terra_detections_excluded)} Terra detections left out).</p>
    <h3>Agreement in coincident scenes</h3>
    ${pctBars(rows)}
    <p class="hint">The grey baseline is two identical VIIRS instruments: what time, cloud and viewing angle alone leave unmatched.
      The gap between the coloured bars and the baseline is what the sensors themselves account for.</p>
    <h3>Agreement by detection confidence</h3>
    <dl class="kv">${conf}</dl>
    <p class="hint">Share of each sensor's detections the other one also saw, by confidence class (count in brackets).</p>
    <h3>Fire power of the same fire</h3>
    ${frpScatter(cl.points)}
    <p class="hint">${fmtInt(cl.count)} coincident fire clusters (one pass of each sensor, ${cl.median_minutes_apart ?? "–"} min apart in the median).
      ${ratio == null ? "" : `VIIRS measured ${fmtNum(ratio, 2)}× the MODIS fire power in the median cluster (middle half: ${fmtNum(cl.ratio_p25, 2)}–${fmtNum(cl.ratio_p75, 2)}×).`}
      The fire also changed between the two passes, so the spread is not all sensor difference.</p>
    <h3>What each sensor misses</h3>
    <dl class="kv">
      <dt>VIIRS, matched</dt><dd>median ${fmtNum(m.b.median_frp_matched_mw)} MW per pixel</dd>
      <dt>VIIRS, unmatched</dt><dd>median ${fmtNum(m.b.median_frp_unmatched_mw)} MW per pixel</dd>
      <dt>MODIS, matched</dt><dd>median ${fmtNum(m.a.median_frp_matched_mw)} MW per pixel</dd>
      <dt>MODIS, unmatched</dt><dd>median ${fmtNum(m.a.median_frp_unmatched_mw)} MW per pixel</dd>
    </dl>
    <h3>Fire events started in this period</h3>
    ${share([["Seen by both sensors", ev.both], ["VIIRS only", ev.viirs_only], ["MODIS only", ev.modis_only]])}
    <p class="hint">Of the fires both sensors saw, VIIRS detected ${fmtInt(firstV)} first and MODIS ${fmtInt(firstM)}${ev.median_modis_minus_viirs_hours == null ? "" : ` (median: MODIS ${fmtNum(Math.abs(ev.median_modis_minus_viirs_hours), 1)} h ${ev.median_modis_minus_viirs_hours >= 0 ? "later" : "earlier"})`}.
      This depends on overpass times as much as on sensitivity: Terra flies in the morning, before VIIRS.</p>
    <h3>Coincident passes</h3>
    <dl class="kv">${m.satellite_pairs.map((p) => `<dt>${esc(p.a)} – ${esc(p.b)}</dt><dd>${fmtInt(p.coincident_pass_pairs)} pass pairs · ${p.median_minutes_apart} min apart</dd>`).join("") || "<dd>None in this period.</dd>"}</dl>`;
}

let seq = 0;

export async function runSensors(bbox: [number, number, number, number] | null, time: Params): Promise<void> {
  const box = $("#analysis-body");
  const mine = ++seq;
  box.innerHTML = `<p class="hint">Comparing MODIS and VIIRS… a continent-sized view takes a few seconds, a region less than one.</p>`;
  try {
    const r = await api.sensors({ ...time, bbox: bbox ? bbox.join(",") : undefined });
    if (mine === seq) box.innerHTML = renderSensors(r);
  } catch (err) {
    if (mine === seq) box.innerHTML = `<p class="banner warn">Comparison failed: ${esc((err as Error).message)}</p>`;
  }
}

// ---------------------------------------------------------------- run

/** Run the analysis for a view and period and draw it. `time` is {hours} or {start, end}. */
export async function runAnalysis(bbox: [number, number, number, number] | null, time: Params): Promise<void> {
  const box = $("#analysis-body");
  const mine = ++seq;
  box.innerHTML = `<p class="hint">Analysing…</p>`;
  const params = { ...time, bbox: bbox ? bbox.join(",") : undefined };
  // both requests at once; the seasonal comparison fills its section when it arrives
  const seasonal = api.baseline(params).then(renderBaseline, (err) =>
    `<h3>Compared with past seasons</h3><p class="hint">Unavailable: ${esc((err as Error).message)}</p>`);
  const land = api.landcover(params).then(renderLandcover, (err) =>
    `<h3>What is burning</h3><p class="hint">Unavailable: ${esc((err as Error).message)}</p>`);
  try {
    const a = await api.analysis(params);
    if (mine !== seq) return;
    box.innerHTML = render(a);
    const fill = async (id: string, html: Promise<string>) => {
      const h = await html;
      const section = document.getElementById(id);
      if (mine === seq && section) section.innerHTML = h;
    };
    await Promise.all([fill("baseline-section", seasonal), fill("landcover-section", land)]);
  } catch (err) {
    if (mine === seq) box.innerHTML = `<p class="banner warn">Analysis failed: ${esc((err as Error).message)}</p>`;
  }
}

/** Hover tooltips for the bars (one listener for the whole panel). */
export function wireAnalysis(onEvent: (id: string) => void): void {
  const panel = $("#tab-analysis");
  const tip = $("#chart-tip");
  panel.addEventListener("mousemove", (e) => {
    const bar = (e.target as Element).closest<SVGGElement>("g.bar[data-tip]");
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
