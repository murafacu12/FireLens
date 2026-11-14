export const $ = <T extends HTMLElement = HTMLElement>(sel: string, root: ParentNode = document): T => {
  const el = root.querySelector<T>(sel);
  if (!el) throw new Error(`missing element ${sel}`);
  return el;
};

export function esc(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export const isoDay = (d: Date) => d.toISOString().slice(0, 10);

export function daysAgo(n: number): Date {
  return new Date(Date.now() - n * 86_400_000);
}

export function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  });
}

export const fmtNum = (n: number | null | undefined, digits = 1) =>
  n == null ? "–" : n.toLocaleString(undefined, { maximumFractionDigits: digits });

let toastTimer: number | undefined;
export function toast(message: string, kind: "info" | "error" = "info"): void {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => el.classList.add("hidden"), 4000);
}
