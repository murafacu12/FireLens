// Making the map self-explanatory: plain-language help, an on-map legend, hover tooltips and a
// short first-visit guide.
import type { FireEvent } from "./api";
import { $, esc, fmtNum, fmtTime } from "./util";

// ---------------------------------------------------------------- glossary ("?" buttons)

export const GLOSSARY: Record<string, { title: string; text: string }> = {
  detection: {
    title: "Fire detection",
    text: "One satellite pixel where a sensor saw unusual heat during one pass over the area. The same fire, seen by several satellites or on several passes, gives several detections.",
  },
  frp: {
    title: "Fire power (FRP)",
    text: "Fire Radiative Power: how much heat the fire was giving off at the moment the satellite passed, in megawatts (MW). Under 10 MW is a small fire; over 1,000 MW is extreme.",
  },
  confidence: {
    title: "Detection confidence",
    text: "How sure NASA's algorithm is that a hot pixel is a real fire. Low-confidence detections are often sun glint, hot bare ground or very small fires. The default, Nominal and up, hides the doubtful ones.",
  },
  sensors: {
    title: "MODIS and VIIRS",
    text: "The two fire sensors FireLens combines. MODIS flies on the Terra and Aqua satellites (1 km pixels, records since 2000). VIIRS flies on Suomi NPP, NOAA-20 and NOAA-21 (375 m pixels): it sees smaller fires.",
  },
  daynight: {
    title: "Day or night pass",
    text: "Whether the satellite passed in daylight or at night. Night passes catch fires that are hidden by smoke or sun glint during the day.",
  },
  event: {
    title: "Fire event",
    text: "Detections close together in space and time, grouped by FireLens into one fire. Its area is the footprint of the satellite pixels that saw fire. It is not the burned area, which FireLens does not measure.",
  },
  status: {
    title: "Active / no recent detection",
    text: "Active means the fire was detected in the last 24 hours. \"No recent detection\" does not mean the fire is out: clouds, smoke or the gap between satellite passes can hide it.",
  },
  freshness: {
    title: "How fresh the data is",
    text: "NASA publishes each satellite pass about 3 hours after the satellite flies over. FireLens checks NASA for new data every hour, and the map updates itself every 5 minutes.",
  },
  location: {
    title: "Area filter",
    text: "The map always shows what the globe is pointing at. Pick a point and a radius to look only at fires around a place, for example a town or a property.",
  },
  ndvi: {
    title: "Vegetation (NDVI)",
    text: "A satellite measure of how green the vegetation is. Green means dense, healthy plants; brown means sparse or dry vegetation, or bare ground.",
  },
};

const pop = document.createElement("div");
pop.className = "help-pop hidden";
pop.setAttribute("role", "dialog");
document.body.appendChild(pop);

function showHelp(btn: HTMLElement): void {
  const entry = GLOSSARY[btn.dataset.help ?? ""];
  if (!entry) return;
  pop.innerHTML = `<b>${esc(entry.title)}</b><p>${esc(entry.text)}</p>`;
  pop.classList.remove("hidden");
  const r = btn.getBoundingClientRect();
  const x = Math.min(r.left, window.innerWidth - pop.offsetWidth - 12);
  const below = r.bottom + 8 + pop.offsetHeight < window.innerHeight;
  pop.style.left = `${Math.max(12, x)}px`;
  pop.style.top = `${below ? r.bottom + 8 : r.top - pop.offsetHeight - 8}px`;
  pop.dataset.for = btn.dataset.help;
}

/** "?" buttons anywhere in the page open a short explanation; a click elsewhere or Esc closes it. */
export function wireHelp(): void {
  // static "?" buttons in the page get the same accessible name as generated ones
  document.querySelectorAll<HTMLElement>("button[data-help]").forEach((b) => {
    const t = GLOSSARY[b.dataset.help ?? ""]?.title;
    if (t) b.setAttribute("aria-label", `Explain: ${t}`);
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

export const helpButton = (key: string) =>
  `<button type="button" class="help" data-help="${key}" aria-label="Explain: ${esc(GLOSSARY[key]?.title ?? key)}">?</button>`;

// ---------------------------------------------------------------- fire power in words

export function powerWord(mw: number | null | undefined): string {
  const v = mw ?? 0;
  if (v < 10) return "small";
  if (v < 100) return "moderate";
  if (v < 1000) return "intense";
  return "extreme";
}

// ---------------------------------------------------------------- hover tooltip

const tip = document.createElement("div");
tip.className = "map-tip hidden";
document.body.appendChild(tip);

export type HoverTarget =
  | { kind: "cell"; count: number; cellDeg: number; maxFrp?: number }
  | { kind: "detection"; id: string; frp?: number | null; viirs?: boolean; t?: number; conf?: number }
  | { kind: "event"; event: FireEvent | undefined; eventId: string };

const CONF = ["low", "nominal", "high"];

function describe(h: HoverTarget): string {
  if (h.kind === "cell") {
    const km = Math.round(h.cellDeg * 111);
    return `<b>${h.count.toLocaleString()} fire detection${h.count === 1 ? "" : "s"}</b>
      <div>grouped in an area about ${km} km across</div>
      ${h.maxFrp != null ? `<div>strongest: ${fmtNum(h.maxFrp, 0)} MW (${powerWord(h.maxFrp)})</div>` : ""}
      <small>Click to zoom in</small>`;
  }
  if (h.kind === "detection") {
    return `<b>${h.viirs ? "VIIRS" : "MODIS"} fire detection</b>
      ${h.t ? `<div>${esc(fmtTime(new Date(h.t * 1000).toISOString()))}</div>` : ""}
      ${h.frp != null ? `<div>fire power ${fmtNum(h.frp, 1)} MW (${powerWord(h.frp)})</div>` : ""}
      ${h.conf != null ? `<div>${CONF[h.conf] ?? ""} confidence</div>` : ""}
      <small>Click for details</small>`;
  }
  const ev = h.event;
  if (!ev) return `<b>Fire event</b><small>Click for details</small>`;
  const state = ev.lifecycle_state === "active" ? "active" : ev.lifecycle_state === "reactivated" ? "flared up again" : "no recent detection";
  return `<b>Fire event · ${state}</b>
    <div>${ev.detection_count.toLocaleString()} detections over ${fmtNum(ev.observed_hours ?? 0, 0)} h</div>
    <div>footprint ${fmtNum(ev.area_km2, ev.area_km2 < 10 ? 2 : 0)} km² · peak ${fmtNum(ev.peak_pass_frp_mw ?? ev.max_frp_mw, 0)} MW</div>
    <small>Click for details</small>`;
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

export function mountLegend(container: HTMLElement): void {
  const el = document.createElement("section");
  el.className = "map-legend";
  el.setAttribute("aria-label", "Map legend");
  el.innerHTML = `
    <button type="button" class="legend-toggle" aria-expanded="true">Legend <span aria-hidden="true">▾</span></button>
    <div class="legend-body">
      <div class="legend-title">Fire power ${helpButton("frp")}</div>
      <div class="ramp"></div>
      <div class="ramp-labels"><span>small</span><span>moderate</span><span>intense</span><span>extreme</span></div>
      <div class="ramp-labels mw"><span>1 MW</span><span>10</span><span>100</span><span>1000+</span></div>
      <ul class="legend-keys">
        <li><span class="dot bubble">12</span> Group of detections: the number counts them. Click to zoom in.</li>
        <li><span class="dot viirs"></span><span class="dot modis"></span> One detection: white ring VIIRS, dark ring MODIS ${helpButton("sensors")}</li>
        <li><span class="ring active"></span><span class="ring inactive"></span> Fire event: orange active, brown no recent detection ${helpButton("event")}</li>
      </ul>
    </div>`;
  container.appendChild(el);
  const toggle = $<HTMLButtonElement>(".legend-toggle", el);
  const set = (open: boolean) => {
    el.classList.toggle("collapsed", !open);
    toggle.setAttribute("aria-expanded", String(open));
    storage.set("firelens.legend", open ? "open" : "closed");
  };
  const saved = storage.get("firelens.legend");
  // first visit: open on wide screens, collapsed on phones where it would cover the map
  el.classList.toggle("collapsed", saved ? saved === "closed" : window.innerWidth < 760);
  toggle.setAttribute("aria-expanded", String(!el.classList.contains("collapsed")));
  toggle.addEventListener("click", () => set(el.classList.contains("collapsed")));
}

// ---------------------------------------------------------------- first-visit guide

export function mountGuide(container: HTMLElement, reopen: HTMLElement): void {
  const el = document.createElement("section");
  el.className = "map-guide hidden";
  el.setAttribute("aria-label", "How to read this map");
  el.innerHTML = `
    <h2>How to read this map</h2>
    <ol>
      <li><b>Each mark is fire seen from space</b> by NASA satellites. Zoomed out, nearby detections are grouped into numbered bubbles.</li>
      <li><b>Colour shows fire power:</b> yellow is a small fire, red and purple are intense ones.</li>
      <li><b>Hover over anything</b> for a quick summary, <b>click</b> for details or to zoom in. Scroll or pinch to zoom.</li>
      <li><b>The left panel</b> chooses the period (latest data or history) and filters what you see. Look for the <span class="help static">?</span> buttons for plain-language explanations.</li>
    </ol>
    <div class="actions"><button type="button" class="guide-close">Got it</button></div>`;
  container.appendChild(el);
  const open = (v: boolean) => {
    el.classList.toggle("hidden", !v);
    if (!v) storage.set("firelens.guide", "seen");
  };
  $(".guide-close", el).addEventListener("click", () => open(false));
  reopen.addEventListener("click", () => open(el.classList.contains("hidden")));
  if (storage.get("firelens.guide") !== "seen") open(true);
}
