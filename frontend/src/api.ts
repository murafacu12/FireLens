// Typed client for the FireLens API. Types mirror backend/app/schemas.py.

export type Instrument = "MODIS" | "VIIRS";
export type ConfidenceLevel = "low" | "nominal" | "high";

export interface FireDetection {
  id: string;
  source: string;
  instrument: Instrument;
  satellite: string;
  processing_level: string;
  latitude: number;
  longitude: number;
  acquired_at: string;
  daynight: "D" | "N" | null;
  brightness_k: number | null;
  brightness_secondary_k: number | null;
  brightness_saturated: boolean;
  frp_mw: number | null;
  scan_km: number;
  track_km: number;
  pixel_area_km2: number;
  nominal_resolution_m: number;
  confidence_raw: string;
  confidence_pct: number;
  confidence_level: ConfidenceLevel;
  event_id: string | null;
  detection_type: number | null;
  static_kind: "volcano" | "industrial" | "offshore" | null;
}

/** Columnar render-only detections (GET /api/detections/compact). */
export interface CompactDetections {
  count: number;
  id: string[];
  lat: number[];
  lon: number[];
  frp: (number | null)[];
  conf: number[]; // 0 low, 1 nominal, 2 high
  viirs: number[]; // 1 VIIRS, 0 MODIS
  t: number[]; // epoch seconds
}

/** Grouped markers for the zoomed-out map (GET /api/detections/grid). Columnar per cell. */
export interface DetectionGrid {
  cell_deg: number;
  total: number; // exact number of matching detections
  cells: number;
  lat: number[]; // centroid of the cell's detections
  lon: number[];
  count: number[];
  max_frp: (number | null)[];
  sum_frp: number[];
  viirs: number[];
}

/** One map tile of grouped markers (GET /api/tiles/cells/{z}/{x}/{y}). A tile is a 16 x 16 grid of
 * cells; MODIS and VIIRS are reported separately (prefix v / m) so either can be switched off
 * without asking the server again. Per cell: count, FRP sum and max, centroid. */
export interface CellTile {
  kind: "cells";
  z: number;
  x: number;
  y: number;
  level: number;
  cell_deg: number;
  viirs_total: number;
  modis_total: number;
  cells: {
    c: number[]; // cell index inside the tile: row * 16 + column
    vn: number[]; vsum: number[]; vmax: number[]; vlat: number[]; vlon: number[];
    mn: number[]; msum: number[]; mmax: number[]; mlat: number[]; mlon: number[];
  };
}

/** Zoomed-in tile (~150 km): individual detections and the fire events centred in it. */
export interface PointTile {
  kind: "points";
  z: number;
  x: number;
  y: number;
  truncated: boolean;
  points: CompactDetections;
  events: FireEvent[];
}

export type FireTile = CellTile | PointTile;

export interface TileMeta {
  data_version: number;
  hour: number; // current UTC hour since epoch: rolling windows move on when it changes
  tile_shift: number;
  min_level: number;
  max_cell_zoom: number;
  point_zoom: number;
}

export interface FireEvent {
  id: string;
  latitude: number;
  longitude: number;
  west: number;
  south: number;
  east: number;
  north: number;
  start_at: string;
  end_at: string;
  detection_count: number;
  modis_count: number;
  viirs_count: number;
  max_frp_mw: number;
  total_frp_mw: number;
  area_km2: number;
  area_basis: "viirs_375m" | "modis_1km";
  max_confidence_pct: number;
  status: "active" | "inactive"; // two-value view of lifecycle_state (kept for compatibility)
  // event model v3 (docs/FIRE_EVENT_MODEL.md)
  lifecycle_state: "active" | "reactivated" | "inactive" | "archived";
  run_start_at: string | null;
  observed_hours: number;
  pass_count: number;
  evidence: "single_detection" | "single_pass" | "multi_pass";
  satellite_counts: Record<string, number>;
  mean_frp_viirs_mw: number | null;
  mean_frp_modis_mw: number | null;
  peak_pass_frp_mw: number;
  peak_pass_sensor: Instrument | null;
  intensity_trend: "rising" | "falling" | "stable" | "insufficient_data";
  extent_km: number;
  detection_hull_km2: number; // area spanned by detection centres
  detected_footprint_km2: number; // = area_km2: union of nominal pixels
  burned_area_km2: null; // not measured by FireLens
  footprint_growth_km2_per_day: number | null;
  footprint_growth_se: number | null;
  version: number;
  updated_at: string | null;
}

export interface FireEventDetail extends FireEvent {
  detections: FireDetection[];
  resolved_from: string | null; // the id asked for, when it was merged/renamed into this event
}

/** Whole-fire FRP seen by one satellite overpass. */
export interface EventPass {
  acquired_at: string;
  satellite: string;
  instrument: Instrument;
  pixels: number;
  frp_sum: number;
  frp_max: number;
}

export interface EventHistoryEntry {
  event_id: string;
  version: number;
  at: string;
  change: string;
  from_state: string | null;
  to_state: string | null;
  related_id: string | null;
  reason: string | null;
  snapshot: Record<string, unknown>;
}

export interface CalendarDay {
  date: string;
  detections: number;
  events_started: number;
  total_frp_mw: number;
  max_frp_mw: number;
}

export interface AlertRuleIn {
  name: string;
  west?: number;
  south?: number;
  east?: number;
  north?: number;
  center_lat?: number;
  center_lon?: number;
  radius_km?: number;
  min_confidence: ConfidenceLevel;
  min_frp_mw: number;
  min_area_km2: number;
  min_detections: number;
  webhook_url?: string | null;
  enabled: boolean;
}

export interface AlertRule extends AlertRuleIn {
  id: number;
  created_at: string;
}

export interface Alert {
  id: number;
  rule_id: number;
  rule_name: string;
  event_id: string;
  severity: "info" | "warning" | "critical";
  message: string;
  latitude: number;
  longitude: number;
  area_km2: number;
  max_frp_mw: number;
  triggered_at: string;
}

export interface RiskDay {
  date: string;
  score: number;
  level: "low" | "moderate" | "high" | "very_high" | "extreme";
  temperature_max_c: number | null;
  humidity_min_pct: number | null;
  wind_max_ms: number | null;
  precipitation_mm: number | null;
  hdw: number | null;
  dry_days: number;
}

export interface RiskForecast {
  latitude: number;
  longitude: number;
  model: string;
  disclaimer: string;
  nearby_detections_7d: number;
  nearby_detections_prev_7d: number;
  activity_trend: string;
  ndvi: number | null;
  surface: "land" | "water" | "open_ocean" | "unknown" | null;
  water_fraction: number | null;
  days: RiskDay[];
}

export interface Health {
  status: string;
  demo_mode: boolean;
  data_source: "demo" | "firms_public" | "firms_api";
  history_enabled: boolean;
  public_region: string;
  seeding: boolean;
  latest_detection_at: string | null;
  refresh_minutes: number;
  event_active_hours: number;
  copernicus_enabled: boolean;
  detections: number;
  events: number;
  last_ingest: string | null;
}

export interface BackfillJob {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  request: { start: string; end: string; bbox: string | null; country: string | null; platforms: string[] };
  total_chunks: number;
  done_chunks: number;
  failed_chunks: number;
  progress: number;
  detections: number;
  events_rebuilt: number;
  current: string | null;
  notes: string[];
  errors: string[];
  created_at: string;
  finished_at: string | null;
}

export type Params = Record<string, string | number | boolean | string[] | null | undefined>;

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

function query(params: Params): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => q.append(k, x));
    else q.append(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

async function fail(resp: Response): Promise<never> {
  let detail = resp.statusText;
  try {
    const body = await resp.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* keep statusText */
  }
  throw new Error(`${resp.status}: ${detail}`);
}

/** A map tile plus its ETag. The server marks tiles `no-cache`, so the browser revalidates a tile
 * it has seen before and an unchanged tile costs a 304, not a download. */
async function tile(kind: FireTile["kind"], z: number, x: number, y: number, p: Params, signal?: AbortSignal): Promise<{ data: FireTile; etag: string }> {
  const resp = await fetch(`${BASE}/api/tiles/${kind}/${z}/${x}/${y}${query(p)}`, { signal });
  if (!resp.ok) return fail(resp);
  return { data: (await resp.json()) as FireTile, etag: resp.headers.get("ETag") ?? "" };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.status === 204 ? (undefined as T) : ((await resp.json()) as T);
}

export const api = {
  health: () => request<Health>("/api/health"),
  tilesMeta: () => request<TileMeta>("/api/tiles/meta"),
  tile,
  detections: (p: Params) => request<FireDetection[]>(`/api/detections${query(p)}`),
  detectionsCompact: (p: Params) => request<CompactDetections>(`/api/detections/compact${query(p)}`),
  grid: (p: Params) => request<DetectionGrid>(`/api/detections/grid${query(p)}`),
  eventsCount: (p: Params) => request<{ count: number }>(`/api/events/count${query(p)}`),
  detection: (id: string) => request<FireDetection>(`/api/detections/${encodeURIComponent(id)}`),
  events: (p: Params) => request<FireEvent[]>(`/api/events${query(p)}`),
  event: (id: string) => request<FireEventDetail>(`/api/events/${encodeURIComponent(id)}`),
  eventTimeline: (id: string) => request<EventPass[]>(`/api/events/${encodeURIComponent(id)}/timeline`),
  eventHistory: (id: string) => request<EventHistoryEntry[]>(`/api/events/${encodeURIComponent(id)}/history`),
  calendar: (p: Params) => request<CalendarDay[]>(`/api/calendar${query(p)}`),
  rules: () => request<AlertRule[]>("/api/alerts/rules"),
  createRule: (r: AlertRuleIn) => request<AlertRule>("/api/alerts/rules", { method: "POST", body: JSON.stringify(r) }),
  deleteRule: (id: number) => request<void>(`/api/alerts/rules/${id}`, { method: "DELETE" }),
  alerts: () => request<Alert[]>("/api/alerts"),
  risk: (lat: number, lon: number) => request<RiskForecast>(`/api/risk${query({ lat, lon })}`),
  backfill: (body: { start: string; end: string; bbox?: string; country?: string }) =>
    request<BackfillJob>("/api/history/backfill", { method: "POST", body: JSON.stringify(body) }),
  jobs: () => request<BackfillJob[]>("/api/history/jobs"),
  coverage: (p: Params) =>
    request<{ days: { date: string; status: "loaded" | "partial" | "missing"; covered: number }[]; missing: string[]; can_load: boolean }>(
      `/api/history/coverage${query(p)}`,
    ),
  countries: () => request<{ code: string; name: string; bbox: string }[]>("/api/history/countries"),
  job: (id: string) => request<BackfillJob>(`/api/history/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id: string) => request<BackfillJob>(`/api/history/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  ingest: (body: object = {}) => request<unknown>("/api/ingest", { method: "POST", body: JSON.stringify(body) }),
};
