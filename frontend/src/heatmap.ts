// Fire calendar as a contribution-style heatmap: one square per day, weeks as columns (Monday on
// top), colour = detections that day in five levels (quartiles of the active days, as GitHub does).
// Days the caller marks as not loaded are shown as "no data": with satellites watching the whole planet
// daily, a worldwide zero means the day is not loaded, not that nothing burned.
import type { CalendarDay } from "./api";
import { locale, t } from "./prefs";
import { esc, fmtNum, isoDay } from "./util";

export const WEEKS = 26;

/** Monday of the week that contains `d` (UTC). */
export function weekStart(d: Date): Date {
  const x = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  x.setUTCDate(x.getUTCDate() - ((x.getUTCDay() + 6) % 7));
  return x;
}

export function addDays(d: Date, n: number): Date {
  const x = new Date(d.getTime());
  x.setUTCDate(x.getUTCDate() + n);
  return x;
}

/** Thresholds between the four colour levels: quartiles of the days with detections. */
function thresholds(values: number[]): number[] {
  const v = values.filter((x) => x > 0).sort((a, b) => a - b);
  if (!v.length) return [1, 1, 1];
  const q = (p: number) => v[Math.min(v.length - 1, Math.floor(p * v.length))];
  return [q(0.25), q(0.5), q(0.75)];
}

function level(n: number, th: number[]): number {
  if (n <= 0) return 0;
  if (n <= th[0]) return 1;
  if (n <= th[1]) return 2;
  if (n <= th[2]) return 3;
  return 4;
}

const fmtShortDay = (iso: string) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(locale(), { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

export interface HeatmapOptions {
  first: Date; // Monday of the first week
  days: CalendarDay[]; // as returned by /api/calendar for first .. first + 7*WEEKS - 1
  selected: { from: string; to: string } | null; // a day or a period
  noData: Set<string>; // days with no data loaded (shown hatched)
}

export function renderHeatmap(o: HeatmapOptions): string {
  const byDate = new Map(o.days.map((d) => [d.date, d]));
  const th = thresholds(o.days.map((d) => d.detections));
  const today = isoDay(new Date());
  const cell = 11, gap = 2, padL = 26, padT = 16;
  const W = padL + WEEKS * (cell + gap), H = padT + 7 * (cell + gap);
  const dows = t("c.dow").split(",");
  const rows = [0, 2, 4].map((r) => `<text class="hm-label" x="${padL - 5}" y="${padT + r * (cell + gap) + cell - 2}" text-anchor="end">${esc(dows[r])}</text>`).join("");
  let months = "";
  let lastMonth = -1;
  let squares = "";
  for (let w = 0; w < WEEKS; w++) {
    const monday = addDays(o.first, 7 * w);
    if (monday.getUTCMonth() !== lastMonth && monday.getUTCDate() <= 7) {
      lastMonth = monday.getUTCMonth();
      const name = monday.toLocaleDateString(locale(), { month: "short", timeZone: "UTC" }).replace(".", "");
      months += `<text class="hm-label" x="${padL + w * (cell + gap)}" y="${padT - 5}">${esc(name)}</text>`;
    }
    for (let r = 0; r < 7; r++) {
      const iso = isoDay(addDays(monday, r));
      if (iso > today) continue;
      const d = byDate.get(iso);
      const n = d?.detections ?? 0;
      const lv = level(n, th);
      const noData = o.noData.has(iso);
      const tip = n > 0
        ? `${fmtShortDay(iso)}|${t("hm.det", { n: n.toLocaleString(locale()) })}|${t("hm.fires", { n: (d?.events_started ?? 0).toLocaleString(locale()) })}|${t("hm.peak", { mw: fmtNum(d?.max_frp_mw, 0) })}`
        : `${fmtShortDay(iso)}|${noData ? t("hm.noData") : t("hm.none")}`;
      const cls = `hm-day l${lv}${noData ? " nodata" : ""}${iso === today ? " today" : ""}${o.selected && iso >= o.selected.from && iso <= o.selected.to ? " sel" : ""}`;
      squares += `<rect class="${cls}" data-date="${iso}" data-tip="${esc(tip)}" x="${padL + w * (cell + gap)}" y="${padT + r * (cell + gap)}" width="${cell}" height="${cell}" rx="2"/>`;
    }
  }
  return `<svg class="heatmap" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(t("hm.aria"))}">${months}${rows}${squares}</svg>
    <div class="hm-legend"><span>${t("hm.less")}</span>${[0, 1, 2, 3, 4].map((l) => `<i class="hm-day l${l}"></i>`).join("")}<span>${t("hm.more")}</span>
      ${o.noData.size ? `<span class="hm-nodata"><i class="hm-day l0 nodata"></i>${t("hm.noDataShort")}</span>` : ""}</div>`;
}

/** Totals of the shown period, for the cards under the heatmap. */
export function periodSummary(days: CalendarDay[]): { total: number; fires: number; active: number; busiest: CalendarDay | null } {
  let total = 0, fires = 0, active = 0;
  let busiest: CalendarDay | null = null;
  for (const d of days) {
    total += d.detections;
    fires += d.events_started;
    if (d.detections > 0) active++;
    if (!busiest || d.detections > busiest.detections) busiest = d;
  }
  return { total, fires, active, busiest: busiest && busiest.detections > 0 ? busiest : null };
}
