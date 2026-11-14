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
  max_confidence_pct: number;
  status: "active" | "inactive";
}

export interface FireEventDetail extends FireEvent {
  detections: FireDetection[];
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
  days: RiskDay[];
}

export interface Health {
  status: string;
  demo_mode: boolean;
  data_source: "demo" | "firms_public" | "firms_api";
  history_enabled: boolean;
  public_region: string;
  seeding: boolean;
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

type Params = Record<string, string | number | boolean | string[] | null | undefined>;

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
  detections: (p: Params) => request<FireDetection[]>(`/api/detections${query(p)}`),
  detectionsCompact: (p: Params) => request<CompactDetections>(`/api/detections/compact${query(p)}`),
  detection: (id: string) => request<FireDetection>(`/api/detections/${encodeURIComponent(id)}`),
  events: (p: Params) => request<FireEvent[]>(`/api/events${query(p)}`),
  event: (id: string) => request<FireEventDetail>(`/api/events/${encodeURIComponent(id)}`),
  calendar: (p: Params) => request<CalendarDay[]>(`/api/calendar${query(p)}`),
  rules: () => request<AlertRule[]>("/api/alerts/rules"),
  createRule: (r: AlertRuleIn) => request<AlertRule>("/api/alerts/rules", { method: "POST", body: JSON.stringify(r) }),
  deleteRule: (id: number) => request<void>(`/api/alerts/rules/${id}`, { method: "DELETE" }),
  alerts: () => request<Alert[]>("/api/alerts"),
  risk: (lat: number, lon: number) => request<RiskForecast>(`/api/risk${query({ lat, lon })}`),
  backfill: (body: { start: string; end: string; bbox?: string; country?: string }) =>
    request<BackfillJob>("/api/history/backfill", { method: "POST", body: JSON.stringify(body) }),
  jobs: () => request<BackfillJob[]>("/api/history/jobs"),
  countries: () => request<{ code: string; name: string; bbox: string }[]>("/api/history/countries"),
  job: (id: string) => request<BackfillJob>(`/api/history/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id: string) => request<BackfillJob>(`/api/history/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  ingest: (body: object = {}) => request<unknown>("/api/ingest", { method: "POST", body: JSON.stringify(body) }),
};
