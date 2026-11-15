// Progressive fire rendering: the globe draws fixed tiles chosen by zoom level.
//
//  zoomed out  -> grouped markers from the server's pre-summed pyramid (one bubble per cell)
//  closer      -> finer cells (each zoom step halves the cell size)
//  ~300 km     -> the individual detections and fire events of ~150 km tiles
//
// The rule that keeps it cheap: fire data is requested only when the camera shows a tile that has
// never been loaded for the current query. Panning back, zooming back, toggling MODIS/VIIRS or a
// layer reuses what is in memory. When new data arrives on the server (data_version) visible
// tiles are revalidated with their ETag, and only tiles whose content changed are rebuilt.

import {
  Cartesian2,
  Cartesian3,
  CircleGeometry,
  CircleOutlineGeometry,
  Color,
  DistanceDisplayCondition,
  GeometryInstance,
  HorizontalOrigin,
  LabelCollection,
  LabelStyle,
  NearFarScalar,
  PointPrimitiveCollection,
  PrimitiveCollection,
  VerticalOrigin,
} from "cesium";

import { api, type CellTile, type FireEvent, type FireTile, type Params, type PointTile } from "./api";
import { batch, instance, MARKER_HEIGHT_M, NO_DEPTH_TEST_WITHIN_M, RAMP, rampIndex } from "./render";
import { ColorGeometryInstanceAttribute, DistanceDisplayConditionGeometryInstanceAttribute, PerInstanceColorAppearance, Primitive, Rectangle, RectangleGeometry, RectangleOutlineGeometry } from "cesium";

export type TileKind = FireTile["kind"];
export interface Lod {
  kind: TileKind;
  z: number;
  cellDeg: number | null; // grouped-marker cell size, null when showing individual detections
}
export interface Sensors {
  viirs: boolean;
  modis: boolean;
}
type BBox = [number, number, number, number];

export const TILE_SHIFT = 4; // 16 x 16 cells per tile (matches the API)
export const MIN_LEVEL = 4; // coarsest cells: 11.25 deg
export const MAX_CELL_LEVEL = 10; // finest grouped cells: 0.18 deg (~20 km)
export const POINT_ZOOM = 7; // ~150 km tiles with individual detections
const MAX_TILES = 48; // an oblique view to the horizon falls back to coarser tiles
const MAX_POINT_TILES = 16;
const CONCURRENCY = 6;
const CACHE_TILES = 400; // tiles kept in memory (GPU + data), least recently used evicted

const CLUSTER_HEIGHT_M = 5000;
const CLUSTER_OUTLINE = Color.WHITE.withAlpha(0.8);
const VIIRS_OUTLINE = Color.WHITE.withAlpha(0.7);
const MODIS_OUTLINE = Color.BLACK.withAlpha(0.8);
const POINT_SCALE = new NearFarScalar(5e4, 1.8, 8e6, 0.7);
const LABEL_RANGE = new DistanceDisplayCondition(0, 2_500_000);
const ACTIVE_COLOR = Color.fromCssColorString("#ff5a1f");
const INACTIVE_COLOR = Color.fromCssColorString("#9a7b5c");

export const tileDeg = (z: number) => 180 / 2 ** z;

/** Tile columns/rows covering a view (which may cross the antimeridian). */
export function tilesFor(b: BBox, z: number): { x: number; y: number }[] {
  const size = tileDeg(z);
  const nx = 2 ** (z + 1);
  const ny = 2 ** z;
  const clampY = (v: number) => Math.min(ny - 1, Math.max(0, Math.floor((v + 90) / size)));
  const col = (v: number) => Math.min(nx - 1, Math.max(0, Math.floor((v + 180) / size)));
  const xs: number[] = [];
  const pushRange = (a: number, c: number) => {
    for (let x = col(a); x <= col(c); x++) xs.push(x);
  };
  if (b[0] <= b[2]) pushRange(b[0], b[2]);
  else {
    pushRange(b[0], 180);
    pushRange(-180, b[2]);
  }
  const out: { x: number; y: number }[] = [];
  for (let y = clampY(b[1]); y <= clampY(b[3]); y++) for (const x of xs) out.push({ x, y });
  return out;
}

function compactCount(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${Math.round(n / 1000)}k`;
}

const inBox = (lat: number, lon: number, b: BBox) =>
  lat >= b[1] && lat <= b[3] && (b[0] <= b[2] ? lon >= b[0] && lon <= b[2] : lon >= b[0] || lon <= b[2]);

// ------------------------------------------------------------------ renderers

/** Merge the sensors that are switched on into one bubble per cell. */
function mergedCells(t: CellTile, s: Sensors) {
  const c = t.cells;
  const out: { lat: number; lon: number; n: number; max: number }[] = [];
  for (let i = 0; i < c.c.length; i++) {
    const vn = s.viirs ? c.vn[i] : 0;
    const mn = s.modis ? c.mn[i] : 0;
    const n = vn + mn;
    if (!n) continue;
    out.push({
      n,
      lat: (c.vlat[i] * vn + c.mlat[i] * mn) / n,
      lon: (c.vlon[i] * vn + c.mlon[i] * mn) / n,
      max: Math.max(vn ? c.vmax[i] : 0, mn ? c.mmax[i] : 0),
    });
  }
  return out;
}

function renderCells(t: CellTile, s: Sensors): PrimitiveCollection {
  const group = new PrimitiveCollection();
  const cells = mergedCells(t, s);
  if (!cells.length) return group;
  const bubbles = new PointPrimitiveCollection();
  const labels = new LabelCollection();
  for (const cell of cells) {
    const size = Math.min(34, 8 + 3 * Math.log2(cell.n));
    const position = Cartesian3.fromDegrees(cell.lon, cell.lat, CLUSTER_HEIGHT_M);
    bubbles.add({
      id: { kind: "cell", lat: cell.lat, lon: cell.lon, cellDeg: t.cell_deg, count: cell.n, maxFrp: cell.max },
      position,
      pixelSize: size,
      color: RAMP[rampIndex(cell.max)].solid.withAlpha(0.82),
      outlineColor: CLUSTER_OUTLINE,
      outlineWidth: 1.5,
      disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
    });
    if (size >= 17) {
      labels.add({
        position,
        text: compactCount(cell.n),
        font: "600 11px Inter, system-ui, sans-serif",
        fillColor: Color.WHITE,
        outlineColor: Color.BLACK.withAlpha(0.85),
        outlineWidth: 3,
        style: LabelStyle.FILL_AND_OUTLINE,
        verticalOrigin: VerticalOrigin.CENTER,
        horizontalOrigin: HorizontalOrigin.CENTER,
        disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
      });
    }
  }
  group.add(bubbles);
  group.add(labels);
  return group;
}

// Close in, each detection is also drawn as its pixel footprint on the ground (nominal size, as
// NASA's FIRMS map does), so neighbouring hotspots a few hundred metres apart stay distinct
// instead of merging into one dot. Only within FOOTPRINT_RANGE_M of the camera, and only for tiles
// small enough to build quickly.
const FOOTPRINT_RANGE_M = 120_000;
const FOOTPRINT_MAX_PER_TILE = 6000;
const NOMINAL_KM = { viirs: 0.375, modis: 1.0 };
const FOOTPRINT_SHOW = new DistanceDisplayConditionGeometryInstanceAttribute(0, FOOTPRINT_RANGE_M);

function footprints(d: PointTile["points"], s: Sensors): Primitive[] {
  const fills: GeometryInstance[] = [];
  const lines: GeometryInstance[] = [];
  for (let i = 0; i < d.count; i++) {
    const viirs = d.viirs[i] === 1;
    if (viirs ? !s.viirs : !s.modis) continue;
    const half = (viirs ? NOMINAL_KM.viirs : NOMINAL_KM.modis) / 2;
    const dLat = half / 111.2;
    const dLon = half / (111.2 * Math.max(Math.cos((d.lat[i] * Math.PI) / 180), 0.05));
    const rectangle = Rectangle.fromDegrees(d.lon[i] - dLon, d.lat[i] - dLat, d.lon[i] + dLon, d.lat[i] + dLat);
    const id = { kind: "detection", id: d.id[i], frp: d.frp[i], viirs, t: d.t[i], conf: d.conf[i] };
    const colour = RAMP[rampIndex(d.frp[i])].solid;
    fills.push(new GeometryInstance({ geometry: new RectangleGeometry({ rectangle, height: 0 }), id,
      attributes: { color: ColorGeometryInstanceAttribute.fromColor(colour.withAlpha(0.55)), distanceDisplayCondition: FOOTPRINT_SHOW } }));
    lines.push(new GeometryInstance({ geometry: new RectangleOutlineGeometry({ rectangle, height: 0 }), id,
      attributes: { color: ColorGeometryInstanceAttribute.fromColor(viirs ? Color.WHITE.withAlpha(0.85) : Color.BLACK.withAlpha(0.85)), distanceDisplayCondition: FOOTPRINT_SHOW } }));
  }
  if (!fills.length) return [];
  const make = (instances: GeometryInstance[], translucent: boolean) => new Primitive({
    geometryInstances: instances, asynchronous: false,
    appearance: new PerInstanceColorAppearance({ flat: true, translucent }),
  });
  return [make(fills, true), make(lines, false)];
}

function renderPoints(t: PointTile, s: Sensors): PrimitiveCollection {
  const group = new PrimitiveCollection();
  const d = t.points;
  if (d.count <= FOOTPRINT_MAX_PER_TILE) for (const p of footprints(d, s)) group.add(p);
  const points = new PointPrimitiveCollection();
  for (let i = 0; i < d.count; i++) {
    const viirs = d.viirs[i] === 1;
    if (viirs ? !s.viirs : !s.modis) continue;
    const ramp = RAMP[rampIndex(d.frp[i])];
    points.add({
      id: { kind: "detection", id: d.id[i], frp: d.frp[i], viirs, t: d.t[i], conf: d.conf[i] },
      position: Cartesian3.fromDegrees(d.lon[i], d.lat[i], MARKER_HEIGHT_M),
      color: d.conf[i] === 0 ? ramp.faint : ramp.solid,
      pixelSize: viirs ? 5 : 8,
      // Outline encodes the sensor: white = VIIRS (375 m), dark = MODIS (1 km)
      outlineColor: viirs ? VIIRS_OUTLINE : MODIS_OUTLINE,
      outlineWidth: 1,
      scaleByDistance: POINT_SCALE,
      disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
    });
  }
  group.add(points);
  return group;
}

/** Fire events currently drawn, by id: lets the hover tooltip describe the circle under the pointer. */
export const drawnEvents = new Map<string, FireEvent>();

export function renderEvents(events: FireEvent[], s: Sensors | null): PrimitiveCollection {
  const group = new PrimitiveCollection();
  for (const ev of events) drawnEvents.set(ev.id, ev);
  const fills: GeometryInstance[] = [];
  const lines: GeometryInstance[] = [];
  const labels = new LabelCollection();
  for (const ev of events) {
    if (s && !((s.viirs && ev.viirs_count > 0) || (s.modis && ev.modis_count > 0))) continue;
    const active = ev.status === "active";
    const color = active ? ACTIVE_COLOR : INACTIVE_COLOR;
    const center = Cartesian3.fromDegrees(ev.longitude, ev.latitude);
    const radius = Math.max(400, Math.sqrt(ev.area_km2 / Math.PI) * 1000);
    // light fill: the circle frames the fire, the detections inside it stay visible
    fills.push(instance(new CircleGeometry({ center, radius, height: 0 }), color.withAlpha(active ? 0.1 : 0.07), ev.id));
    lines.push(instance(new CircleOutlineGeometry({ center, radius, height: 0 }), color, ev.id));
    if (ev.area_km2 >= 1) {
      labels.add({
        position: Cartesian3.fromDegrees(ev.longitude, ev.latitude, MARKER_HEIGHT_M),
        text: `${ev.area_km2.toFixed(1)} km²`,
        font: "12px Inter, system-ui, sans-serif",
        fillColor: Color.WHITE,
        outlineColor: Color.BLACK,
        outlineWidth: 3,
        style: LabelStyle.FILL_AND_OUTLINE,
        verticalOrigin: VerticalOrigin.BOTTOM,
        pixelOffset: new Cartesian2(0, -10),
        distanceDisplayCondition: LABEL_RANGE,
        disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
      });
    }
  }
  for (const p of [batch(fills, false), batch(lines, true)]) if (p) group.add(p);
  group.add(labels);
  return group;
}

// ------------------------------------------------------------------ tile manager

interface Entry {
  key: string; // query | kind/z/x/y
  kind: TileKind;
  z: number;
  x: number;
  y: number;
  data: FireTile | null; // null: failed (counts as loaded so a swap isn't held up forever)
  etag: string;
  epoch: string; // data epoch the tile was validated at
  fires: PrimitiveCollection | null;
  events: PrimitiveCollection | null;
  sensorsDrawn: string; // the sensor combination the primitives were built for
  used: number;
}

export interface TileStats {
  detections: number;
  loading: number;
  truncated: boolean;
}

export class FireTiles {
  private readonly cache = new Map<string, Entry>();
  private readonly loading = new Map<string, AbortController>();
  private queue: Entry[] = [];
  private wanted: Entry[] = [];
  private shown = new Set<Entry>();
  private query = "";
  private params: Params = {};
  private epoch = "0"; // entries start at "" so everything is fetched once
  private sensors: Sensors = { viirs: true, modis: true };
  private clock = 0;
  private lastView: { lod: Lod; bbox: BBox } | null = null;
  onChange: () => void = () => undefined;
  onError: (msg: string) => void = () => undefined;

  constructor(
    private readonly fireRoot: PrimitiveCollection,
    private readonly eventRoot: PrimitiveCollection,
    private readonly redraw: () => void,
  ) {}

  /** Filters + time window. Tiles of other queries stay cached (switching back is instant). */
  setQuery(params: Params): void {
    const query = JSON.stringify(params);
    if (query === this.query) return;
    this.query = query;
    this.params = params;
    this.queue = [];
    for (const [key, ctl] of this.loading) if (!key.startsWith(`${query}|`)) ctl.abort();
  }

  /** New data on the server (or the rolling window moved to a new hour): revalidate what is shown,
   * and anything else when it is next shown. */
  setEpoch(epoch: string): void {
    if (epoch === this.epoch) return;
    this.epoch = epoch;
    if (this.lastView && this.wanted.length) this.view(this.lastView.lod, this.lastView.bbox);
  }

  setSensors(s: Sensors): void {
    this.sensors = { ...s };
    for (const e of this.shown) this.draw(e);
    this.redraw();
    this.onChange();
  }

  /** Show the tiles for this zoom level and view; request only those not in memory. */
  view(lod: Lod, bbox: BBox): void {
    this.lastView = { lod, bbox };
    const ids = tilesFor(bbox, lod.z);
    const cx = (bbox[0] + bbox[2]) / 2;
    const cy = (bbox[1] + bbox[3]) / 2;
    const size = tileDeg(lod.z);
    const dist = (t: { x: number; y: number }) =>
      Math.hypot(-180 + (t.x + 0.5) * size - cx, -90 + (t.y + 0.5) * size - cy);
    ids.sort((a, b) => dist(a) - dist(b)); // centre of the screen first
    this.wanted = ids.map(({ x, y }) => this.entry(lod.kind, lod.z, x, y));
    this.queue = this.wanted.filter((e) => this.needsFetch(e));
    this.pump();
    this.apply();
  }

  /** Tiles needed for a view at a zoom level (used to pick a level that isn't too many tiles). */
  static count(bbox: BBox, z: number): number {
    return tilesFor(bbox, z).length;
  }

  static readonly limits = { MAX_TILES, MAX_POINT_TILES };

  /** How many tiles are on screen (0 before anything has loaded). */
  get shownCount(): number {
    return this.shown.size;
  }

  stats(bbox: BBox): TileStats {
    let detections = 0;
    let truncated = false;
    for (const e of this.wanted) {
      const t = e.data;
      if (!t) continue;
      if (t.kind === "cells") {
        const c = t.cells;
        for (let i = 0; i < c.c.length; i++) {
          if (this.sensors.viirs && c.vn[i] && inBox(c.vlat[i], c.vlon[i], bbox)) detections += c.vn[i];
          if (this.sensors.modis && c.mn[i] && inBox(c.mlat[i], c.mlon[i], bbox)) detections += c.mn[i];
        }
      } else {
        truncated ||= t.truncated;
        const p = t.points;
        for (let i = 0; i < p.count; i++) {
          if ((p.viirs[i] === 1 ? this.sensors.viirs : this.sensors.modis) && inBox(p.lat[i], p.lon[i], bbox)) detections++;
        }
      }
    }
    return { detections, loading: this.wanted.filter((e) => this.needsFetch(e)).length, truncated };
  }

  setVisible(fires: boolean, events: boolean): void {
    this.fireRoot.show = fires;
    this.eventRoot.show = events;
    this.redraw();
  }

  /** Hide every tile (focus mode shows one fire on its own). */
  hideAll(): void {
    this.wanted = [];
    this.queue = [];
    for (const e of this.shown) this.setShown(e, false);
    this.shown.clear();
    this.redraw();
  }

  // ---------------------------------------------------------------- internals

  private entry(kind: TileKind, z: number, x: number, y: number): Entry {
    const key = `${this.query}|${kind}/${z}/${x}/${y}`;
    let e = this.cache.get(key);
    if (!e) {
      e = { key, kind, z, x, y, data: null, etag: "", epoch: "", fires: null, events: null, sensorsDrawn: "", used: 0 };
      this.cache.set(key, e);
    }
    e.used = ++this.clock;
    return e;
  }

  private needsFetch(e: Entry): boolean {
    return e.epoch !== this.epoch;
  }

  private pump(): void {
    while (this.loading.size < CONCURRENCY && this.queue.length) {
      const e = this.queue.shift()!;
      if (this.loading.has(e.key) || !this.needsFetch(e)) continue;
      void this.load(e);
    }
  }

  private async load(e: Entry): Promise<void> {
    const ctl = new AbortController();
    this.loading.set(e.key, ctl);
    const epoch = this.epoch;
    try {
      const { data, etag } = await api.tile(e.kind, e.z, e.x, e.y, this.params, ctl.signal);
      if (etag !== e.etag || !e.data) {
        e.data = data;
        e.etag = etag;
        e.sensorsDrawn = ""; // content changed: rebuild primitives when shown
      }
      e.epoch = epoch;
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      e.epoch = epoch; // don't retry in a loop; the next data epoch retries
      this.onError((err as Error).message);
    } finally {
      this.loading.delete(e.key);
      this.pump();
      this.apply();
    }
  }

  /** Swap atomically per zoom level: old tiles stay on screen until every new tile has arrived,
   * so zooming never flashes an empty globe. */
  private apply(): void {
    const wanted = new Set(this.wanted);
    const ready = this.wanted.filter((e) => e.data !== null || (e.epoch !== "" && !this.loading.has(e.key)));
    const complete = ready.length === this.wanted.length;
    const next = new Set(ready);
    if (!complete) for (const e of this.shown) if (!wanted.has(e) && e.data) next.add(e);
    for (const e of this.shown) if (!next.has(e)) this.setShown(e, false);
    for (const e of next) {
      this.draw(e);
      this.setShown(e, true);
    }
    this.shown = next;
    if (complete) this.evict();
    this.redraw();
    this.onChange();
  }

  private draw(e: Entry): void {
    const s = this.sensors;
    const key = `${s.viirs ? 1 : 0}${s.modis ? 1 : 0}`;
    if (!e.data || e.sensorsDrawn === key) return;
    const wasShown = this.shown.has(e);
    this.destroyPrimitives(e);
    e.fires = e.data.kind === "cells" ? renderCells(e.data, s) : renderPoints(e.data, s);
    e.events = e.data.kind === "points" && e.data.events.length ? renderEvents(e.data.events, s) : null;
    this.fireRoot.add(e.fires);
    if (e.events) this.eventRoot.add(e.events);
    e.sensorsDrawn = key;
    this.setShown(e, wasShown);
  }

  private setShown(e: Entry, v: boolean): void {
    if (e.fires) e.fires.show = v;
    if (e.events) e.events.show = v;
  }

  private destroyPrimitives(e: Entry): void {
    if (e.fires) this.fireRoot.remove(e.fires); // remove() also destroys and frees GPU memory
    if (e.events) this.eventRoot.remove(e.events);
    e.fires = e.events = null;
  }

  private evict(): void {
    if (this.cache.size <= CACHE_TILES) return;
    const idle = [...this.cache.values()].filter((e) => !this.shown.has(e) && !this.loading.has(e.key));
    idle.sort((a, b) => a.used - b.used);
    for (const e of idle.slice(0, this.cache.size - CACHE_TILES)) {
      this.destroyPrimitives(e);
      this.cache.delete(e.key);
    }
  }
}
