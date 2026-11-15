// Making the map self-explanatory: plain-language help, an on-map legend, hover tooltips and a
// short first-visit guide. Every text comes from strings.ts (English / Spanish).
import type { FireEvent } from "./api";
import { locale, onPrefsChange, t, tn } from "./prefs";
import type { Key } from "./strings";
import { $, esc, fmtNum, fmtTime } from "./util";

// ---------------------------------------------------------------- glossary ("?" buttons)

export const GLOSSARY_KEYS = ["detection", "frp", "confidence", "sensors", "daynight", "event", "status", "freshness", "location", "ndvi"] as const;
export type GlossaryKey = (typeof GLOSSARY_KEYS)[number];

const gTitle = (k: string) => t(`g.${k}.title` as Key);
const gText = (k: string) => t(`g.${k}.text` as Key);
const known = (k: string | undefined): k is GlossaryKey => !!k && (GLOSSARY_KEYS as readonly string[]).includes(k);

const pop = document.createElement("div");
pop.className = "help-pop hidden";
pop.setAttribute("role", "dialog");
document.body.appendChild(pop);

function showHelp(btn: HTMLElement): void {
  const key = btn.dataset.help;
  if (!known(key)) return;
  pop.innerHTML = `<b>${esc(gTitle(key))}</b><p>${esc(gText(key))}</p>`;
  pop.classList.remove("hidden");
  const r = btn.getBoundingClientRect();
  const x = Math.min(r.left, window.innerWidth - pop.offsetWidth - 12);
  const below = r.bottom + 8 + pop.offsetHeight < window.innerHeight;
  pop.style.left = `${Math.max(12, x)}px`;
  pop.style.top = `${below ? r.bottom + 8 : r.top - pop.offsetHeight - 8}px`;
  pop.dataset.for = key;
}

function labelHelpButtons(): void {
  document.querySelectorAll<HTMLElement>("button[data-help]").forEach((b) => {
    if (known(b.dataset.help)) b.setAttribute("aria-label", t("app.explain", { what: gTitle(b.dataset.help) }));
  });
}

/** "?" buttons anywhere in the page open a short explanation; a click elsewhere or Esc closes it. */
export function wireHelp(): void {
  labelHelpButtons();
  onPrefsChange(() => {
    labelHelpButtons();
    pop.classList.add("hidden");
  });
  document.addEventListener("click", (e) => {
    const btn = (e.target as HTMLElement).closest<HTMLElement>("[data-help]");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      if (!pop.classList.contains("hidden") && pop.dataset.for === btn.dataset.help) pop.classList.add("hidden");
      else showHelp(btn);
    } else if (!(e.target as HTMLElement).closest(".help-pop")) {
      pop.classList.add("hidden");
    }
  });
  document.addEventListener("keydown", (e) => e.key === "Escape" && pop.classList.add("hidden"));
}

export const helpButton = (key: GlossaryKey) =>
  `<button type="button" class="help" data-help="${key}" aria-label="${esc(t("app.explain", { what: gTitle(key) }))}">?</button>`;

// ---------------------------------------------------------------- fire power in words

export type PowerLevel = "small" | "moderate" | "intense" | "extreme";
export function powerLevel(mw: number | null | undefined): PowerLevel {
  const v = mw ?? 0;
  if (v < 10) return "small";
  if (v < 100) return "moderate";
  if (v < 1000) return "intense";
  return "extreme";
}
export const powerWord = (mw: number | null | undefined) => t(`power.${powerLevel(mw)}` as Key);

// ---------------------------------------------------------------- hover tooltip

const tip = document.createElement("div");
tip.className = "map-tip hidden";
document.body.appendChild(tip);

export type HoverTarget =
  | { kind: "cell"; count: number; cellDeg: number; maxFrp?: number }
  | { kind: "detection"; id: string; frp?: number | null; viirs?: boolean; t?: number; conf?: number }
  | { kind: "event"; event: FireEvent | undefined; eventId: string };

const CONF: Key[] = ["tip.confLow", "tip.confNominal", "tip.confHigh"];

function describe(h: HoverTarget): string {
  if (h.kind === "cell") {
    return `<b>${esc(tn("tip.cell", h.count))}</b>
      <div>${esc(t("tip.cellArea", { km: Math.round(h.cellDeg * 111) }))}</div>
      ${h.maxFrp != null ? `<div>${esc(t("tip.strongest", { mw: fmtNum(h.maxFrp, 0), word: powerWord(h.maxFrp) }))}</div>` : ""}
      <small>${t("tip.zoom")}</small>`;
  }
  if (h.kind === "detection") {
    return `<b>${esc(t("tip.detection", { sensor: h.viirs ? "VIIRS" : "MODIS" }))}</b>
      ${h.t ? `<div>${esc(fmtTime(new Date(h.t * 1000).toISOString()))}</div>` : ""}
      ${h.frp != null ? `<div>${esc(t("tip.power", { mw: fmtNum(h.frp, 1), word: powerWord(h.frp) }))}</div>` : ""}
      ${h.conf != null && CONF[h.conf] ? `<div>${esc(t(CONF[h.conf]))}</div>` : ""}
      <small>${t("tip.details")}</small>`;
  }
  const ev = h.event;
  if (!ev) return `<b>${t("tip.event")}</b><small>${t("tip.details")}</small>`;
  const state = ev.lifecycle_state === "active" ? t("tip.stActive") : ev.lifecycle_state === "reactivated" ? t("tip.stReactivated") : t("tip.stInactive");
  return `<b>${esc(t("tip.eventState", { state }))}</b>
    <div>${esc(t("tip.eventDets", { n: ev.detection_count.toLocaleString(locale()), h: fmtNum(ev.observed_hours ?? 0, 0) }))}</div>
    <div>${esc(t("tip.eventSize", { area: fmtNum(ev.area_km2, ev.area_km2 < 10 ? 2 : 0), mw: fmtNum(ev.peak_pass_frp_mw ?? ev.max_frp_mw, 0) }))}</div>
    <small>${t("tip.details")}</small>`;
}

export function showMapTip(h: HoverTarget | null, x: number, y: number): void {
  if (!h) {
    tip.classList.add("hidden");
    return;
  }
  tip.innerHTML = describe(h);
  tip.classList.remove("hidden");
  const left = x + 16 + tip.offsetWidth > window.innerWidth ? x - tip.offsetWidth - 12 : x + 16;
  const top = y + 16 + tip.offsetHeight > window.innerHeight ? y - tip.offsetHeight - 12 : y + 16;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

// ---------------------------------------------------------------- on-map legend

const storage = {
  get(k: string): string | null {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set(k: string, v: string): void {
    try {
      localStorage.setItem(k, v);
    } catch {
      /* private mode etc.: the page works without it */
    }
  },
};

function legendHtml(open: boolean): string {
  return `
    <button type="button" class="legend-toggle" aria-expanded="${open}">${t("lg.title")} <span aria-hidden="true">▾</span></button>
    <div class="legend-body">
      <div class="legend-title">${t("lg.power")} ${helpButton("frp")}</div>
      <div class="ramp"></div>
      <div class="ramp-labels"><span>${t("power.small")}</span><span>${t("power.moderate")}</span><span>${t("power.intense")}</span><span>${t("power.extreme")}</span></div>
      <div class="ramp-labels mw"><span>1 MW</span><span>10</span><span>100</span><span>1000+</span></div>
      <ul class="legend-keys">
        <li><span class="dot bubble">12</span> ${t("lg.group")}</li>
        <li><span class="dot viirs"></span><span class="dot modis"></span> ${t("lg.one")} ${helpButton("sensors")}</li>
        <li><span class="ring active"></span><span class="ring inactive"></span> ${t("lg.event")} ${helpButton("event")}</li>
      </ul>
    </div>`;
}

export function mountLegend(container: HTMLElement): void {
  const el = document.createElement("section");
  el.className = "map-legend";
  container.appendChild(el);
  const saved = storage.get("firelens.legend");
  // first visit: open on wide screens, collapsed on phones where it would cover the map
  let open = saved ? saved === "open" : window.innerWidth >= 760;
  const draw = () => {
    el.setAttribute("aria-label", t("lg.title"));
    el.innerHTML = legendHtml(open);
    el.classList.toggle("collapsed", !open);
    $<HTMLButtonElement>(".legend-toggle", el).addEventListener("click", () => {
      open = !open;
      storage.set("firelens.legend", open ? "open" : "closed");
      draw();
    });
  };
  draw();
  onPrefsChange(draw);
}

// ---------------------------------------------------------------- first-visit guide

export function mountGuide(container: HTMLElement, reopen: HTMLElement): void {
  const el = document.createElement("section");
  el.className = "map-guide hidden";
  container.appendChild(el);
  const draw = () => {
    el.setAttribute("aria-label", t("mg.title"));
    el.innerHTML = `
      <h2>${t("mg.title")}</h2>
      <ol>
        <li>${t("mg.s1")}</li>
        <li>${t("mg.s2")}</li>
        <li>${t("mg.s3")}</li>
        <li>${t("mg.s4")}</li>
      </ol>
      <div class="actions"><button type="button" class="secondary guide-more">${t("mg.more")}</button><button type="button" class="guide-close">${t("mg.ok")}</button></div>`;
    $(".guide-close", el).addEventListener("click", () => open(false));
    $(".guide-more", el).addEventListener("click", () => {
      open(false);
      document.querySelector<HTMLButtonElement>('.tab[data-tab="guide"]')?.click();
    });
  };
  const open = (v: boolean) => {
    el.classList.toggle("hidden", !v);
    if (!v) storage.set("firelens.guide", "seen");
  };
  draw();
  onPrefsChange(draw);
  reopen.addEventListener("click", () => open(el.classList.contains("hidden")));
  if (storage.get("firelens.guide") !== "seen") open(true);
}
