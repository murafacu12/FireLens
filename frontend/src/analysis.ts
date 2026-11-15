// Analysis tab: fire statistics and trends for the area in view and the selected period.
// Two readings of the same results: Simple (the essentials as graphics) and Advanced (every number).
// The last results are kept, so changing the language or the view redraws them without new requests.
import { api, type AnalysisSummary, type BaselineGroup, type BaselineVerdict, type LandcoverBreakdown, type Params, type SeasonalBaseline, type SensorComparison } from "./api";
import { stateLabel } from "./details";
import { LAND_COLOUR, landLabel, landMeaning, shareBar } from "./firereport";
import { powerWord } from "./guide";
import { isSimple, locale, onPrefsChange, t, tn } from "./prefs";
import type { Key } from "./strings";
import { $, esc, fmtNum, fmtTime } from "./util";

const W = 352; // plot width in px (the sidebar is fixed-width)
const fmtInt = (n: number) => n.toLocaleString(locale());
const fmtDay = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(locale(), { day: "numeric", month: "short", timeZone: "UTC" });

function change(pct: number | null, comparable: boolean): string {
  if (!comparable) return `<small class="delta">${t("an.prevNotLoaded")}</small>`;
  if (pct === null) return `<small class="delta">${t("an.prevNone")}</small>`;
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "■";
  return `<small class="delta">${arrow} ${pct > 0 ? "+" : ""}${fmtNum(pct, 0)}% ${t("an.vsPrev")}</small>`;
}

interface Bar {
  label: string; // tooltip heading
  tick?: string; // x-axis label, when shown
  viirs: number;
  modis: number;
  extra?: string;
}

/** Stacked bars, VIIRS under MODIS, one shared y-axis. Hover shows the values. With `total`, one
 * orange bar per item (simple view). */
function bars(data: Bar[], height: number, mark?: Set<number>, total = false): string {
  const padL = 34, padB = 16, padT = 6;
  const plotW = W - padL, plotH = height - padB - padT;
  const max = Math.max(1, ...data.map((d) => d.viirs + d.modis));
  const step = plotW / Math.max(1, data.length);
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
    const tip = total
      ? `${d.label}|${tn("an.detections", d.viirs + d.modis)}${d.extra ? `|${d.extra}` : ""}`
      : `${d.label}|VIIRS ${fmtInt(d.viirs)}|MODIS ${fmtInt(d.modis)}${d.extra ? `|${d.extra}` : ""}`;
    const body = total
      ? (hv + hm > 0 ? `<rect class="total" x="${x}" y="${y(d.viirs + d.modis)}" width="${bw}" height="${hv + hm}" rx="${Math.min(2, bw / 2)}"/>` : "")
      : `${hv > 0 ? `<rect class="viirs" x="${x}" y="${y(d.viirs)}" width="${bw}" height="${hv}" rx="${Math.min(2, bw / 2)}"/>` : ""}
      ${hm > 0 ? `<rect class="modis" x="${x}" y="${y(d.viirs + d.modis)}" width="${bw}" height="${Math.max(0, hm - gap)}" rx="${Math.min(2, bw / 2)}"/>` : ""}`;
    return `<g class="bar" data-tip="${esc(tip)}">
      <rect class="hit" x="${padL + i * step}" y="${padT}" width="${step}" height="${plotH}"/>
      ${body}
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

const legend = () => `<div class="chart-legend"><span><i class="sw viirs"></i>VIIRS (375 m)</span><span><i class="sw modis"></i>MODIS (1 km)</span></div>`;

function share(parts: [string, number][]): string {
  const total = parts.reduce((a, [, n]) => a + n, 0);
  if (!total) return `<p class="hint">${t("an.none")}</p>`;
  return `<dl class="kv">${parts.map(([k, n]) => `<dt>${esc(k)}</dt><dd>${fmtInt(n)} <small>(${fmtNum((100 * n) / total, 0)}%)</small></dd>`).join("")}</dl>`;
}

function eventList(events: AnalysisSummary["events"]["largest_footprint"], by: "area" | "frp", limit = 5): string {
  if (!events.length) return `<li class="empty">${t("an.noEvents")}</li>`;
  return events.slice(0, limit).map((ev) => `<li><a href="#" data-event="${esc(ev.id)}">
      <b>${by === "area" ? t("an.byArea", { a: fmtNum(ev.area_km2, 2) }) : t("an.byFrp", { mw: fmtNum(ev.peak_pass_frp_mw, 0), sensor: esc(ev.peak_pass_sensor ?? "") })}</b>
      <small>${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)} · ${ev.detection_count} ${t("c.det")} · ${esc(stateLabel(ev))}</small></a></li>`).join("");
}

function areaLabel(b: number[]): string {
  if (b[0] <= -180 && b[2] >= 180 && b[1] <= -90 && b[3] >= 90) return t("an.world");
  const lon = (x: number) => `${Math.abs(x).toFixed(1)}°${x < 0 ? t("an.W") : t("an.E")}`;
  const lat = (y: number) => `${Math.abs(y).toFixed(1)}°${y < 0 ? t("an.S") : t("an.N")}`;
  return t("an.box", { w: lon(b[0]), e: lon(b[2]), s: lat(b[1]), n: lat(b[3]) });
}

function trendText(a: AnalysisSummary): string {
  const tr = a.trend;
  if (tr.direction === "not_comparable") return t("an.trendNotComparable");
  if (tr.direction === "insufficient_data") return t("an.trendInsufficient");
  const word = tr.direction === "rising" ? t("an.rising") : tr.direction === "falling" ? t("an.falling") : t("an.stable");
  return t("an.trend", { word, second: fmtInt(tr.second_half ?? 0), first: fmtInt(tr.first_half ?? 0), pct: `${(tr.change_pct ?? 0) > 0 ? "+" : ""}${fmtNum(tr.change_pct, 0)}` });
}

const EVIDENCE: [string, Key][] = [["multi_pass", "an.evMulti"], ["single_pass", "an.evPass"], ["single_detection", "an.evOne"]];

/** Advanced: every number of the statistics, as before. */
function renderAdvanced(a: AnalysisSummary): string {
  const d = a.detections;
  const unusual = new Set(a.unusual_days.map((u) => u.date));
  const daily: Bar[] = a.daily.map((x) => ({
    label: fmtDay(x.date), tick: fmtDay(x.date), viirs: x.viirs, modis: x.modis,
    extra: `${tn("an.eventsStarted", x.events_started)}${unusual.has(x.date) ? `|${t("an.unusualHigh")}` : ""}`,
  }));
  const marks = new Set(a.daily.map((x, i) => (unusual.has(x.date) ? i : -1)).filter((i) => i >= 0));
  const tod: Bar[] = a.time_of_day.hours.map((h) => ({ label: `${String(h.hour).padStart(2, "0")}:00–${String(h.hour).padStart(2, "0")}:59`, tick: String(h.hour), viirs: h.viirs, modis: h.modis }));
  const cov = a.coverage;
  const sensor = (name: string, s: AnalysisSummary["detections"]["by_sensor"]["viirs"]) =>
    `<dt>${name}</dt><dd>${t("an.sensorLine", { n: fmtInt(s.detections), max: fmtNum(s.max_frp_mw), mean: s.mean_pixel_frp_mw == null ? "–" : `${fmtNum(s.mean_pixel_frp_mw)} MW` })}</dd>`;
  const method = a.method === "detections" ? t("an.exact") : t("an.cells", { deg: fmtNum(Number(a.method.slice(6)), 2) });
  return `
    <p class="hint"><b>${t("an.area")}</b> ${esc(areaLabel(a.bbox))}<br>${esc(fmtTime(a.start))} – ${esc(fmtTime(a.end))} · ${method}</p>
    ${cov.complete ? "" : `<p class="banner warn">${t("an.incomplete", { loaded: cov.loaded_days, days: cov.days })}</p>`}
    <div class="kpis">
      <div><b>${fmtInt(d.total)}</b><span>${t("an.kpiDetections")}</span>${change(a.previous.detections_change_pct, a.previous.comparable)}</div>
      <div><b>${fmtInt(a.events.started)}</b><span>${t("an.kpiEvents")}</span>${change(a.previous.events_change_pct, a.previous.comparable)}</div>
    </div>
    <div id="baseline-section">${baselineSlot()}</div>
    <div id="landcover-section">${landSlot()}</div>
    <h3>${t("an.perDay")}</h3>
    ${legend()}
    ${bars(daily, 120, marks)}
    <p class="hint">${esc(trendText(a))}${a.unusual_days.length ? ` <span class="flag-dot"></span> ${esc(tn("an.unusualDays", a.unusual_days.length, { typical: fmtNum(a.unusual_days[0].typical, 0) }))}` : ""}</p>
    <h3>${t("an.timeOfDay")} <small>(${a.time_of_day.basis === "local_solar" ? t("an.localSolar", { off: `${a.time_of_day.utc_offset_hours >= 0 ? "+" : ""}${a.time_of_day.utc_offset_hours}` }) : t("an.utcWide")})</small></h3>
    ${bars(tod, 90)}
    <p class="hint">${t("an.todHint")}</p>
    <h3>${t("an.bySensor")}</h3>
    <dl class="kv">${sensor("VIIRS", d.by_sensor.viirs)}${sensor("MODIS", d.by_sensor.modis)}</dl>
    <p class="hint">${t("an.sensorHint")}</p>
    <h3>${t("an.confidence")}</h3>
    ${share([[t("al.high"), d.by_confidence.high], [t("al.nominal"), d.by_confidence.nominal], [t("al.low"), d.by_confidence.low]])}
    <h3>${t("an.evidence")}</h3>
    ${share(EVIDENCE.map(([k, label]) => [t(label), a.events.by_evidence[k] ?? 0] as [string, number]))}
    <h3>${t("an.largest")}</h3>
    <ul class="list">${eventList(a.events.largest_footprint, "area")}</ul>
    <h3>${t("an.strongest")}</h3>
    <ul class="list">${eventList(a.events.strongest, "frp")}</ul>
    <p class="hint">${t("an.footnote")}</p>`;
}

// ---------------------------------------------------------------- simple view

function tile(label: string, value: string, sub = "", accent = false): string {
  return `<div class="s-tile${accent ? " accent" : ""}"><div class="k">${label}</div><div class="v">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function changeSub(pct: number | null, comparable: boolean): string {
  if (!comparable || pct === null) return t("an.sNoCompare");
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
  return `<span class="${cls}">${pct > 0 ? "▲" : pct < 0 ? "▼" : "■"} ${pct > 0 ? "+" : ""}${fmtNum(pct, 0)}%</span> ${t("an.sVsPrev")}`;
}

/** Donut of land-cover shares with a short legend. */
function donut(l: LandcoverBreakdown): string {
  const parts = l.classes.filter((c) => c.share >= 0.005);
  const R = 46, r = 30, C = 58;
  let a0 = -Math.PI / 2;
  const arcs = parts.map((c) => {
    const a1 = a0 + c.share * 2 * Math.PI;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (ang: number, rad: number) => `${(C + rad * Math.cos(ang)).toFixed(2)} ${(C + rad * Math.sin(ang)).toFixed(2)}`;
    const d = c.share >= 0.999
      ? `M ${p(0, R)} A ${R} ${R} 0 1 1 ${p(Math.PI, R)} A ${R} ${R} 0 1 1 ${p(0, R)} M ${p(0, r)} A ${r} ${r} 0 1 0 ${p(Math.PI, r)} A ${r} ${r} 0 1 0 ${p(0, r)} Z`
      : `M ${p(a0, R)} A ${R} ${R} 0 ${large} 1 ${p(a1, R)} L ${p(a1, r)} A ${r} ${r} 0 ${large} 0 ${p(a0, r)} Z`;
    a0 = a1;
    return `<path d="${d}" fill="${LAND_COLOUR[c.class] ?? "#777"}" stroke="#101010" stroke-width="1.5"><title>${esc(`${landLabel(c.class)} ${fmtNum(100 * c.share, 0)}%`)}</title></path>`;
  }).join("");
  const top = parts[0];
  return `<div class="donut-wrap">
      <svg class="donut" viewBox="0 0 116 116" role="img" aria-label="${esc(t("an.whatBurning"))}">${arcs}
        ${top ? `<text x="58" y="56" text-anchor="middle" font-size="19">${fmtNum(100 * top.share, 0)}%</text><text x="58" y="72" text-anchor="middle" font-size="9" fill="#9b9b9b" font-weight="600">${esc(landLabel(top.class).split(" ")[0])}</text>` : ""}
      </svg>
      <ul class="donut-legend">${parts.slice(0, 5).map((c) => `<li><i style="background:${LAND_COLOUR[c.class] ?? "#777"}"></i>${esc(landLabel(c.class))}<b>${fmtNum(100 * c.share, 0)}%</b></li>`).join("")}</ul>
    </div>`;
}

function renderSimple(a: AnalysisSummary): string {
  const d = a.detections;
  const daily: Bar[] = a.daily.map((x) => ({ label: fmtDay(x.date), tick: fmtDay(x.date), viirs: x.viirs, modis: x.modis, extra: tn("an.eventsStarted", x.events_started) }));
  const top = a.events.largest_footprint.slice(0, 3);
  return `
    <div class="s-head"><div class="where">${esc(areaLabel(a.bbox))} · ${esc(fmtDay(a.start.slice(0, 10)))} – ${esc(fmtDay(a.end.slice(0, 10)))}</div></div>
    ${a.coverage.complete ? "" : `<p class="banner warn">${t("an.sIncomplete")}</p>`}
    <div class="s-tiles">
      ${tile(t("an.sDetections"), fmtInt(d.total), changeSub(a.previous.detections_change_pct, a.previous.comparable), true)}
      ${tile(t("an.sFires"), fmtInt(a.events.started), changeSub(a.previous.events_change_pct, a.previous.comparable))}
    </div>
    <div id="baseline-section">${baselineSlot()}</div>
    <h3>${t("an.sActivity")}</h3>
    ${bars(daily, 110, undefined, true)}
    <p class="hint">${esc(trendText(a))}</p>
    <div id="landcover-section">${landSlot()}</div>
    <h3>${t("an.sBiggest")}</h3>
    <ul class="list">${top.length ? top.map((ev) => `<li><a href="#" data-event="${esc(ev.id)}"><b>${t("an.sFireLine", { a: fmtNum(ev.area_km2, ev.area_km2 < 10 ? 1 : 0), power: powerWord(ev.peak_pass_frp_mw) })}</b>
      <small>${esc(stateLabel(ev))} · ${ev.latitude.toFixed(2)}, ${ev.longitude.toFixed(2)}</small></a></li>`).join("") : `<li class="empty">${t("an.noEvents")}</li>`}</ul>
    <p class="hint">${t("an.sWhat")}</p>
    <button type="button" class="more-link" data-set-view="advanced">${t("an.sMore")} →</button>`;
}

// ---------------------------------------------------------------- seasonal baseline

const VERDICT: Record<BaselineVerdict, { key: Key; tone: "up" | "down" | "flat" | "none" }> = {
  well_above: { key: "bl.wellAbove", tone: "up" },
  above: { key: "bl.above", tone: "up" },
  within_range: { key: "bl.within", tone: "flat" },
  below: { key: "bl.below", tone: "down" },
  well_below: { key: "bl.wellBelow", tone: "down" },
  insufficient_seasons: { key: "bl.insufficient", tone: "none" },
  too_few_fires: { key: "bl.tooFew", tone: "none" },
  current_incomplete: { key: "bl.incomplete", tone: "none" },
  no_archive: { key: "bl.noArchive", tone: "none" },
  area_not_covered: { key: "bl.noArea", tone: "none" },
};

const BASELINE_WHY: Record<string, Key> = {
  no_baseline: "bl.whyNoBaseline",
  period_too_short: "bl.whyShort",
  area_not_covered: "bl.whyArea",
  no_archive: "bl.whyArchive",
};

const dayRange = (first: string, last: string) => (first === last ? fmtDay(first) : `${fmtDay(first)} – ${fmtDay(last)}`);

/** One bar per season (oldest first), this period last and highlighted; dashed line at the median. */
function seasonBars(g: BaselineGroup, n: number): string {
  const years = [...(g.years ?? [])].sort((a, b) => a.year - b.year);
  const rows = [...years.map((y) => ({ label: String(y.year), value: y.detections, current: false, first: y.first })),
    { label: t("bl.thisYear"), value: g.current ?? 0, current: true, first: "" }];
  const H = 130, padL = 34, padB = 16, padT = 14;
  const plotW = W - padL, plotH = H - padB - padT;
  const max = Math.max(1, ...rows.map((r) => r.value));
  const step = plotW / rows.length;
  const bw = Math.min(34, step - 6);
  const y = (v: number) => padT + plotH - (v / max) * plotH;
  const med = g.median ?? 0;
  const barsSvg = rows.map((r, i) => {
    const x = padL + i * step + (step - bw) / 2;
    const tip = r.current
      ? `${t("bl.thisPeriod")}|${tn("an.detections", r.value)}${g.ratio != null ? `|${t("bl.ratioTip", { r: fmtNum(g.ratio, 2) })}` : ""}`
      : `${r.label}|${tn("an.detections", r.value)}|${dayRange(r.first, addDays(r.first, n - 1))}`;
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
    <text class="axis median-label" x="${padL + 2}" y="${y(med) - 3}">${t("bl.typical")} ${fmtShort(med)}</text>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t("bl.perSeason"))}">${grid}
    <line class="base" x1="${padL}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>${barsSvg}${medLine}</svg>`;
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
  const place = g.rank === 1 ? t("bl.moreThanAll", { n }) : g.rank === n + 1 ? t("bl.fewerThanAll", { n }) : t("bl.rank", { k: g.rank ?? 0, n: n + 1 });
  return t("bl.sentence", {
    n: fmtInt(g.current ?? 0), label: esc(g.label), days, place, span, min: fmtInt(g.min ?? 0), max: fmtInt(g.max ?? 0),
    typical: fmtInt(Math.round(g.median ?? 0)), ratio: g.ratio != null ? t("bl.ratio", { r: fmtNum(g.ratio, 2) }) : "",
  });
}

const verdictIcon = (tone: string) => (tone === "up" ? "▲" : tone === "down" ? "▼" : tone === "flat" ? "●" : "○");

function renderBaseline(b: SeasonalBaseline): string {
  if (!b.available || !b.headline) {
    const why = BASELINE_WHY[b.reason ?? ""];
    const first = Object.values(b.groups).map((g) => VERDICT[g.verdict]?.key).filter(Boolean)[0];
    return `<h3>${t("bl.title")}</h3><p class="hint">${esc(why ? t(why) : first ? t(first) : "")}</p>`;
  }
  const g = b.groups[b.headline];
  const other = b.groups[b.headline === "viirs_noaa20" ? "modis" : "viirs_noaa20"];
  const days = b.days ? dayRange(b.days.first, b.days.last) : "";
  const v = VERDICT[g.verdict];
  if (isSimple()) {
    const ratio = g.ratio != null && v.tone !== "none" ? t("bl.sRatio", { r: fmtNum(g.ratio, 1) }) : "";
    return `
      <h3>${t("bl.title")}</h3>
      <div class="s-card">
        <div class="s-big ${v.tone}"><span class="dot-state"></span>${esc(t(v.key))}</div>
        <p>${t("bl.sLine", { n: fmtInt(g.current ?? 0), typical: fmtInt(Math.round(g.median ?? 0)), seasons: g.seasons })}${ratio ? ` ${ratio}` : ""}</p>
        ${g.seasons >= 2 ? seasonBars(g, b.days?.count ?? 1) : ""}
      </div>`;
  }
  const cd = g.current_days;
  const notes = [
    b.days && b.days.count < 7 ? tn("bl.shortPeriod", b.days.count) : "",
    g.area_fraction < 0.98 ? t("bl.areaPart", { pct: fmtNum(100 * g.area_fraction, 0), deg: b.cell_deg }) : t("bl.areaWhole", { deg: b.cell_deg }),
    cd && cd.missing ? tn("bl.missing", cd.missing) : "",
  ].filter(Boolean).join(" ");
  const otherLine = other && other.seasons >= 2 && other.current != null
    ? `<dl class="kv"><dt>${esc(other.label)}</dt><dd>${esc(t(VERDICT[other.verdict].key))} · ${t("bl.vsTypical", { n: fmtInt(other.current), typical: fmtInt(Math.round(other.median ?? 0)) })}</dd></dl>`
    : "";
  return `
    <h3>${t("bl.title")}</h3>
    <p class="verdict ${v.tone}"><span class="verdict-icon" aria-hidden="true">${verdictIcon(v.tone)}</span>${esc(t(v.key))}</p>
    <p class="hint">${baselineSentence(g, days)}</p>
    ${g.seasons >= 2 ? seasonBars(g, b.days?.count ?? 1) : ""}
    ${otherLine}
    <p class="hint">${t("bl.method", { label: esc(g.label) })} ${esc(notes)} ${t("bl.aboveMeans")}</p>`;
}

// ---------------------------------------------------------------- what is burning

function renderLandcover(l: LandcoverBreakdown): string {
  if (!l.detections) return `<h3>${t("an.whatBurning")}</h3><p class="hint">${t("lc.noDetections")}</p>`;
  const top = l.classes[0];
  const loading = l.coverage < 1 ? ` ${t("lc.loading", { pct: fmtNum(100 * (1 - l.coverage), 0) })}` : "";
  if (isSimple()) {
    return `<h3>${t("an.whatBurning")}</h3>${donut(l)}
      ${top && landMeaning(top.class) ? `<p class="hint">${t("lc.sMostly", { what: esc(landLabel(top.class).toLowerCase()), meaning: esc(landMeaning(top.class)) })}</p>` : ""}
      ${loading ? `<p class="hint">${loading}</p>` : ""}`;
  }
  const rows = l.classes.filter((c) => c.share >= 0.01).slice(0, 6);
  return `
    <h3>${t("an.whatBurning")}</h3>
    ${top ? `<p class="lead-line">${t("lc.lead", { pct: fmtNum(100 * top.share, 0), what: esc(landLabel(top.class).toLowerCase()) })}${landMeaning(top.class) ? ` <small>(${esc(landMeaning(top.class))})</small>` : ""}</p>` : ""}
    ${shareBar(l.classes.map((c) => ({ key: c.class, label: landLabel(c.class), share: c.share, colour: LAND_COLOUR[c.class] ?? "#777" })), 5)}
    <dl class="kv">${rows.map((c) => `<dt>${esc(landLabel(c.class))}</dt><dd>${fmtInt(c.detections)} <small>(${fmtNum(100 * c.share, 0)}%${landMeaning(c.class) ? ` · ${esc(landMeaning(c.class))}` : ""})</small></dd>`).join("")}</dl>
    <p class="hint">${t("lc.method")}${loading}</p>`;
}

// ---------------------------------------------------------------- MODIS vs VIIRS (advanced only)

const pct = (x: number | null) => (x == null ? "–" : `${fmtNum(x, 0)}%`);

/** One horizontal bar per row, a shared 0-100 % scale. */
function pctBars(rows: { label: string; value: number | null; note: string; muted?: boolean }[]): string {
  const labelW = 150, barW = W - labelW - 40, rowH = 24;
  const h = rows.length * rowH;
  const hatch = `<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
    <rect width="5" height="5" fill="#3a3a3a"/><line x1="0" y1="0" x2="0" y2="5" stroke="#8a8a8a" stroke-width="1.5"/></pattern></defs>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${h}" role="img">${hatch}${rows.map((r, i) => {
    const y = i * rowH;
    const v = r.value ?? 0;
    return `<g class="bar" data-tip="${esc(`${r.label}|${pct(r.value)} ${r.note}`)}">
      <rect class="hit" x="0" y="${y}" width="${W}" height="${rowH}"/>
      <text class="axis lbl" x="0" y="${y + 15}">${esc(r.label)}</text>
      <rect class="track" x="${labelW}" y="${y + 7}" width="${barW}" height="10" rx="2"/>
      ${r.value != null ? `<rect class="${r.muted ? "baseline" : "agree"}" x="${labelW}" y="${y + 7}" width="${(v / 100) * barW}" height="10" rx="2"/>` : ""}
      <text class="axis val" x="${W}" y="${y + 15}" text-anchor="end">${pct(r.value)}</text>
    </g>`;
  }).join("")}</svg>`;
}

/** Log-log scatter of coincident fire clusters: MODIS FRP (x) vs VIIRS FRP (y), 1:1 line. */
function frpScatter(points: SensorComparison["modis_vs_viirs"]["clusters"]["points"]): string {
  const pts = points.filter((p) => p.frp_a > 0 && p.frp_b > 0);
  if (!pts.length) return `<p class="hint">${t("sc.noClusters")}</p>`;
  const H = 230, pad = 30;
  const lo = Math.floor(Math.log10(Math.min(...pts.map((p) => Math.min(p.frp_a, p.frp_b)))));
  const hi = Math.ceil(Math.log10(Math.max(...pts.map((p) => Math.max(p.frp_a, p.frp_b)))));
  const span = Math.max(1, hi - lo);
  const sx = (v: number) => pad + ((Math.log10(v) - lo) / span) * (W - pad - 6);
  const sy = (v: number) => H - pad - ((Math.log10(v) - lo) / span) * (H - pad - 6);
  const ticks = Array.from({ length: span + 1 }, (_, k) => 10 ** (lo + k));
  const fmt = (v: number) => (v >= 1000 ? `${v / 1000}k` : String(v));
  const axes = ticks.map((tk) => `<line class="grid" x1="${sx(tk)}" x2="${sx(tk)}" y1="6" y2="${H - pad}"/><line class="grid" x1="${pad}" x2="${W - 6}" y1="${sy(tk)}" y2="${sy(tk)}"/>
    <text class="axis" x="${sx(tk)}" y="${H - pad + 12}" text-anchor="middle">${fmt(tk)}</text><text class="axis" x="${pad - 4}" y="${sy(tk) + 3}" text-anchor="end">${fmt(tk)}</text>`).join("");
  const one = `<line class="identity" x1="${sx(10 ** lo)}" y1="${sy(10 ** lo)}" x2="${sx(10 ** hi)}" y2="${sy(10 ** hi)}"/>`;
  const dots = pts.map((p) => `<g class="bar" data-tip="${esc(`${t("sc.cluster", { lat: p.lat.toFixed(2), lon: p.lon.toFixed(2) })}|MODIS (${p.sat_a}) ${fmtNum(p.frp_a)} MW · ${p.pixels_a} px|VIIRS (${p.sat_b}) ${fmtNum(p.frp_b)} MW · ${p.pixels_b} px|${t("sc.minApart", { m: p.minutes_apart })}`)}">
      <circle class="hit" cx="${sx(p.frp_a)}" cy="${sy(p.frp_b)}" r="7"/><circle class="dot" cx="${sx(p.frp_a)}" cy="${sy(p.frp_b)}" r="3"/></g>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">${axes}${one}${dots}
    <text class="axis" x="${W - 6}" y="${H - 4}" text-anchor="end">${t("sc.xAxis")} →</text>
    <text class="axis" x="${pad + 4}" y="14">↑ ${t("sc.yAxis")}</text>
    <text class="axis id-label" x="${sx(10 ** hi) - 4}" y="${sy(10 ** hi) + 14}" text-anchor="end">${t("sc.same")}</text></svg>`;
}

function renderSensors(r: SensorComparison): string {
  const m = r.modis_vs_viirs, b = r.baseline_viirs_vs_viirs, ev = r.events, cl = m.clusters;
  const rows = [
    { label: t("sc.modisSeen"), value: m.a.matched_pct, note: t("sc.ofModis", { n: fmtInt(m.a.in_scenes) }) },
    { label: t("sc.viirsSeen"), value: m.b.matched_pct, note: t("sc.ofViirs", { n: fmtInt(m.b.in_scenes) }) },
    ...(b ? [
      { label: t("sc.base2021"), value: b.a.matched_pct, note: t("sc.identical", { m: b.clusters.median_minutes_apart ?? "–" }), muted: true },
      { label: t("sc.base2120"), value: b.b.matched_pct, note: t("sc.identicalShort"), muted: true },
    ] : []),
  ];
  const conf = (["high", "nominal", "low"] as const).map((c) =>
    `<dt>${t(c === "high" ? "al.high" : c === "nominal" ? "al.nominal" : "al.low")}</dt><dd>MODIS ${pct(m.a.by_confidence[c].matched_pct)} <small>(${fmtInt(m.a.by_confidence[c].in_scenes)})</small> · VIIRS ${pct(m.b.by_confidence[c].matched_pct)} <small>(${fmtInt(m.b.by_confidence[c].in_scenes)})</small></dd>`).join("");
  const firstV = ev.first_seen_by.viirs ?? 0, firstM = ev.first_seen_by.modis ?? 0;
  const ratio = cl.median_frp_ratio_b_over_a;
  const lag = ev.median_modis_minus_viirs_hours;
  return `
    <p class="hint"><b>${t("an.area")}</b> ${esc(areaLabel(r.bbox))}<br>${esc(fmtTime(r.start))} – ${esc(fmtTime(r.end))}</p>
    <p class="hint">${t("sc.intro", { w: r.window_minutes, km: r.scene_km, terra: fmtInt(r.terra_detections_excluded) })}</p>
    <h3>${t("sc.agreement")}</h3>
    ${pctBars(rows)}
    <p class="hint">${t("sc.agreementHint")}</p>
    <h3>${t("sc.byConf")}</h3>
    <dl class="kv">${conf}</dl>
    <p class="hint">${t("sc.byConfHint")}</p>
    <h3>${t("sc.power")}</h3>
    ${frpScatter(cl.points)}
    <p class="hint">${t("sc.powerHint", { n: fmtInt(cl.count), m: cl.median_minutes_apart ?? "–" })}
      ${ratio == null ? "" : t("sc.ratio", { r: fmtNum(ratio, 2), lo: fmtNum(cl.ratio_p25, 2), hi: fmtNum(cl.ratio_p75, 2) })}
      ${t("sc.changed")}</p>
    <h3>${t("sc.misses")}</h3>
    <dl class="kv">
      <dt>${t("sc.vMatched")}</dt><dd>${t("sc.median", { mw: fmtNum(m.b.median_frp_matched_mw) })}</dd>
      <dt>${t("sc.vUnmatched")}</dt><dd>${t("sc.median", { mw: fmtNum(m.b.median_frp_unmatched_mw) })}</dd>
      <dt>${t("sc.mMatched")}</dt><dd>${t("sc.median", { mw: fmtNum(m.a.median_frp_matched_mw) })}</dd>
      <dt>${t("sc.mUnmatched")}</dt><dd>${t("sc.median", { mw: fmtNum(m.a.median_frp_unmatched_mw) })}</dd>
    </dl>
    <h3>${t("sc.events")}</h3>
    ${share([[t("sc.both"), ev.both], [t("sc.viirsOnly"), ev.viirs_only], [t("sc.modisOnly"), ev.modis_only]])}
    <p class="hint">${t("sc.first", { v: fmtInt(firstV), m: fmtInt(firstM) })}${lag == null ? "" : t(lag >= 0 ? "sc.lagLater" : "sc.lagEarlier", { h: fmtNum(Math.abs(lag), 1) })}.
      ${t("sc.firstHint")}</p>
    <h3>${t("sc.passes")}</h3>
    <dl class="kv">${m.satellite_pairs.map((p) => `<dt>${esc(p.a)} – ${esc(p.b)}</dt><dd>${t("sc.pairs", { n: fmtInt(p.coincident_pass_pairs), m: p.median_minutes_apart })}</dd>`).join("") || `<dd>${t("sc.noPasses")}</dd>`}</dl>`;
}

// ---------------------------------------------------------------- run, keep, redraw

type Part<T> = { ok: T } | { err: string } | null;
let current:
  | { kind: "stats"; a: AnalysisSummary | null; b: Part<SeasonalBaseline>; l: Part<LandcoverBreakdown> }
  | { kind: "sensors"; r: SensorComparison }
  | null = null;
let seq = 0;

function baselineSlot(): string {
  if (current?.kind !== "stats" || !current.b) return `<h3>${t("bl.title")}</h3><p class="hint">${t("bl.looking")}</p>`;
  return "ok" in current.b ? renderBaseline(current.b.ok) : `<h3>${t("bl.title")}</h3><p class="hint">${t("an.unavailable", { msg: esc(current.b.err) })}</p>`;
}

function landSlot(): string {
  if (current?.kind !== "stats" || !current.l) return `<h3>${t("an.whatBurning")}</h3><p class="hint">${t("lc.reading")}</p>`;
  return "ok" in current.l ? renderLandcover(current.l.ok) : `<h3>${t("an.whatBurning")}</h3><p class="hint">${t("an.unavailable", { msg: esc(current.l.err) })}</p>`;
}

/** Draw what was last computed in the current language and view. */
function redraw(): void {
  const box = document.getElementById("analysis-body");
  if (!box || !current) return;
  if (current.kind === "sensors") {
    box.innerHTML = isSimple() ? `<p class="hint">${t("sc.advancedOnly")}</p>` : renderSensors(current.r);
    return;
  }
  if (!current.a) return;
  box.innerHTML = isSimple() ? renderSimple(current.a) : renderAdvanced(current.a);
}

function redrawSlot(id: "baseline-section" | "landcover-section"): void {
  const el = document.getElementById(id);
  if (el) el.innerHTML = id === "baseline-section" ? baselineSlot() : landSlot();
}

export async function runSensors(bbox: [number, number, number, number] | null, time: Params): Promise<void> {
  const box = $("#analysis-body");
  const mine = ++seq;
  box.innerHTML = `<p class="hint">${t("sc.running")}</p>`;
  try {
    const r = await api.sensors({ ...time, bbox: bbox ? bbox.join(",") : undefined });
    if (mine !== seq) return;
    current = { kind: "sensors", r };
    redraw();
  } catch (err) {
    if (mine === seq) box.innerHTML = `<p class="banner warn">${t("sc.failed", { msg: esc((err as Error).message) })}</p>`;
  }
}

/** Run the analysis for a view and period and draw it. `time` is {hours} or {start, end}. */
export async function runAnalysis(bbox: [number, number, number, number] | null, time: Params): Promise<void> {
  const box = $("#analysis-body");
  const mine = ++seq;
  box.innerHTML = `<p class="hint">${t("an.running")}</p>`;
  const params = { ...time, bbox: bbox ? bbox.join(",") : undefined };
  const state: { kind: "stats"; a: AnalysisSummary | null; b: Part<SeasonalBaseline>; l: Part<LandcoverBreakdown> } = { kind: "stats", a: null, b: null, l: null };
  current = state;
  // all three at once; the seasonal comparison and the land cover fill their sections when they arrive
  const seasonal = api.baseline(params).then((ok) => ({ ok }), (e: Error) => ({ err: e.message }));
  const land = api.landcover(params).then((ok) => ({ ok }), (e: Error) => ({ err: e.message }));
  try {
    const a = await api.analysis(params);
    if (mine !== seq) return;
    state.a = a;
    redraw();
    void seasonal.then((b) => {
      if (mine !== seq) return;
      state.b = b;
      redrawSlot("baseline-section");
    });
    void land.then((l) => {
      if (mine !== seq) return;
      state.l = l;
      redrawSlot("landcover-section");
    });
  } catch (err) {
    if (mine === seq) box.innerHTML = `<p class="banner warn">${t("an.failed", { msg: esc((err as Error).message) })}</p>`;
  }
}

/** Forget the shown results (the analysis kind changed). */
export function clearAnalysis(): void {
  current = null;
  seq++;
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
  onPrefsChange(redraw);
}
