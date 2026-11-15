// Collapsible sidebar. Desktop: hide it to give the globe the full width (remembered). Phones: the
// globe fills the screen and the sidebar is a bottom sheet that shows just the title and totals
// until it is opened (tap or swipe its handle).
import { $ } from "./util";

const MOBILE = window.matchMedia("(max-width: 900px)");
const KEY = "firelens.sidebar";

function stored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}
function store(v: string): void {
  try {
    localStorage.setItem(KEY, v);
  } catch {
    /* private mode: just not remembered */
  }
}

export const isMobile = () => MOBILE.matches;

let onResize: () => void = () => undefined;

function setDesktopCollapsed(collapsed: boolean, remember = true): void {
  const app = $("#app");
  app.classList.toggle("sidebar-collapsed", collapsed);
  $("#sidebar-open").classList.toggle("hidden", !collapsed);
  $("#sidebar-collapse").setAttribute("aria-expanded", String(!collapsed));
  if (remember) store(collapsed ? "collapsed" : "open");
  onResize();
}

function setSheetOpen(open: boolean): void {
  const sidebar = $("#sidebar");
  sidebar.classList.toggle("sheet-open", open);
  const handle = $("#sheet-handle");
  handle.setAttribute("aria-expanded", String(open));
  $(".sheet-label", handle).textContent = open ? "Show the map" : "Filters, layers and analysis";
}

/** On phones, get the sheet out of the way so the user can see (or tap) the map. */
export function revealMap(): void {
  if (isMobile()) setSheetOpen(false);
}

export function wireLayout(resizeGlobe: () => void): void {
  onResize = resizeGlobe;
  const apply = () => {
    if (isMobile()) {
      $("#app").classList.remove("sidebar-collapsed");
      $("#sidebar-open").classList.add("hidden");
      setSheetOpen(false);
    } else {
      $("#sidebar").classList.remove("sheet-open");
      setDesktopCollapsed(stored() === "collapsed", false);
    }
    resizeGlobe();
  };
  $("#sidebar-collapse").addEventListener("click", () => setDesktopCollapsed(true));
  $("#sidebar-open").addEventListener("click", () => setDesktopCollapsed(false));

  const handle = $("#sheet-handle");
  handle.addEventListener("click", () => setSheetOpen(!$("#sidebar").classList.contains("sheet-open")));
  // swipe the handle up to open, down to close
  let startY: number | null = null;
  handle.addEventListener("pointerdown", (e) => (startY = e.clientY));
  handle.addEventListener("pointerup", (e) => {
    if (startY === null) return;
    const dy = e.clientY - startY;
    startY = null;
    if (Math.abs(dy) > 30) {
      e.preventDefault();
      setSheetOpen(dy < 0);
      handle.dataset.swiped = "1"; // the click that follows the swipe must not toggle back
      setTimeout(() => delete handle.dataset.swiped, 400);
    }
  });
  handle.addEventListener("click", (e) => {
    if (handle.dataset.swiped) {
      e.stopImmediatePropagation();
    }
  }, true);
  // choosing a tab in the collapsed sheet opens it
  document.querySelectorAll<HTMLButtonElement>(".tab").forEach((t) => t.addEventListener("click", () => isMobile() && setSheetOpen(true)));
  MOBILE.addEventListener("change", apply);
  apply();
}
