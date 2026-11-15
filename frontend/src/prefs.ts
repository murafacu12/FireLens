// User preferences that change how the panels read, not what the map shows: language (English is
// the main language, Spanish the second) and view (simple = the essentials, more graphic; advanced =
// every number). Both are remembered in the browser; changing either re-renders every open panel.
import { ES, type Key } from "./strings";
import { EN } from "./strings";

export type Lang = "en" | "es";
export type View = "simple" | "advanced";

const store = {
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
      /* private mode: just not remembered */
    }
  },
};

let lang: Lang = store.get("firelens.lang") === "es" ? "es" : "en";
let view: View = store.get("firelens.view") === "advanced" ? "advanced" : "simple";
const listeners: (() => void)[] = [];

export const getLang = () => lang;
export const getView = () => view;
export const isSimple = () => view === "simple";
/** BCP 47 locale for numbers and dates in the chosen language. */
export const locale = () => (lang === "es" ? "es-AR" : "en-US");

/** Translate a key; `{name}` placeholders are filled from `vars`. */
export function t(key: Key, vars?: Record<string, string | number>): string {
  let s: string = (lang === "es" ? ES[key] : EN[key]) ?? EN[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, String(v));
  return s;
}

/** Singular or plural form: keys `<key>_one` / `<key>_other` (n is available as {n}). */
export function tn(key: string, n: number, vars?: Record<string, string | number>): string {
  const k = (n === 1 ? `${key}_one` : `${key}_other`) as Key;
  return t(k, { n: n.toLocaleString(locale()), ...vars });
}

/** Fill every element marked with data-i18n (text), data-i18n-html (markup) or
 * data-i18n-attr="attr:key;attr2:key2". */
export function applyStatic(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n as Key)));
  root.querySelectorAll<HTMLElement>("[data-i18n-html]").forEach((el) => (el.innerHTML = t(el.dataset.i18nHtml as Key)));
  root.querySelectorAll<HTMLElement>("[data-i18n-attr]").forEach((el) => {
    for (const pair of (el.dataset.i18nAttr ?? "").split(";")) {
      const [attr, key] = pair.split(":").map((s) => s.trim());
      if (attr && key) el.setAttribute(attr, t(key as Key));
    }
  });
  document.documentElement.lang = lang;
}

function applyBody(): void {
  document.body.classList.toggle("view-simple", view === "simple");
  document.body.classList.toggle("view-advanced", view === "advanced");
  document.querySelectorAll<HTMLButtonElement>("[data-set-view]").forEach((b) => {
    const on = b.dataset.setView === view;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  document.querySelectorAll<HTMLButtonElement>("[data-set-lang]").forEach((b) => {
    const on = b.dataset.setLang === lang;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
}

/** Something that must re-render when the language or the view changes. */
export function onPrefsChange(cb: () => void): void {
  listeners.push(cb);
}

function changed(): void {
  applyStatic();
  applyBody();
  for (const cb of listeners) {
    try {
      cb();
    } catch (err) {
      console.error("re-render after a preference change failed", err);
    }
  }
}

export function setLang(l: Lang): void {
  if (l === lang) return;
  lang = l;
  store.set("firelens.lang", l);
  changed();
}

export function setView(v: View): void {
  if (v === view) return;
  view = v;
  store.set("firelens.view", v);
  changed();
}

/** Wire the language and view switches and apply the saved preferences to the page. */
export function wirePrefs(): void {
  document.addEventListener("click", (e) => {
    const el = e.target as HTMLElement;
    const v = el.closest<HTMLElement>("[data-set-view]")?.dataset.setView;
    if (v === "simple" || v === "advanced") return setView(v);
    const l = el.closest<HTMLElement>("[data-set-lang]")?.dataset.setLang;
    if (l === "en" || l === "es") setLang(l);
  });
  applyStatic();
  applyBody();
}
