import {
  CameraEventType,
  Cartesian2,
  Cartesian3,
  Cartographic,
  CircleGeometry,
  CircleOutlineGeometry,
  Color,
  Credit,
  GeometryInstance,
  ImageryLayer,
  Ion,
  KeyboardEventModifier,
  LabelCollection,
  Math as CesiumMath,
  NearFarScalar,
  OpenStreetMapImageryProvider,
  type OrthographicOffCenterFrustum,
  PointPrimitiveCollection,
  Primitive,
  PrimitiveCollection,
  Rectangle,
  RectangleGeometry,
  RectangleOutlineGeometry,
  SceneMode,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  Terrain,
  UrlTemplateImageryProvider,
  Viewer,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";

import type { AlertRule, CompactDetections, FireEvent } from "./api";
import { batch, instance, MARKER_HEIGHT_M, NO_DEPTH_TEST_WITHIN_M, RAMP, rampIndex } from "./render";
import type { HoverTarget } from "./guide";
import { drawnEvents, FireTiles, type Lod, MAX_CELL_LEVEL, MIN_LEVEL, POINT_ZOOM, renderEvents, TILE_SHIFT } from "./tiles";

export type BaseLayerId = "esri" | "osm" | "gibs";
export type PickResult =
  | { kind: "detection"; id: string }
  | { kind: "event"; eventId: string }
  | { kind: "point"; lat: number; lon: number }
  | { kind: "cell"; lat: number; lon: number; cellDeg: number; count: number };

const CELL_PX = 45; // target on-screen spacing between grouped markers
const POINTS_MAX_IN_VIEW = 15_000; // above this, a regional view keeps grouped bubbles
const POINTS_ALWAYS_BELOW = 3_000; // at most this many fires in view: always individual (if <= 16 point tiles)

const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best";
const GIBS_CREDIT = new Credit("Imagery: NASA EOSDIS GIBS");
const ZONE_COLOR = Color.fromCssColorString("#38bdf8");

function yesterday(): string {
  return new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
}

const VIIRS_OUTLINE = Color.WHITE.withAlpha(0.7);
const MODIS_OUTLINE = Color.BLACK.withAlpha(0.8);
const POINT_SCALE = new NearFarScalar(5e4, 1.8, 8e6, 0.7);

/** A named group of primitives that can be replaced and toggled as one layer. */
class Layer {
  readonly group: PrimitiveCollection;
  constructor(parent: PrimitiveCollection) {
    this.group = parent.add(new PrimitiveCollection());
  }
  replace(...items: (Primitive | LabelCollection | PointPrimitiveCollection | PrimitiveCollection | null)[]): void {
    this.group.removeAll(); // destroys previous primitives and frees GPU memory
    for (const item of items) if (item) this.group.add(item);
  }
  set show(v: boolean) {
    this.group.show = v;
  }
}

export class Globe {
  readonly viewer: Viewer;
  private readonly baseLayers: Record<BaseLayerId, ImageryLayer>;
  private readonly ndviLayer: ImageryLayer;
  private readonly detections: Layer; // focus mode: one fire's detections
  private readonly events: Layer; // focus mode
  private readonly zones: Layer;
  private readonly pinned: Layer;
  /** Fires everywhere else: tiles chosen by zoom level (see tiles.ts). */
  readonly tiles: FireTiles;
  private detectionIds: string[] = [];

  constructor(container: HTMLElement, onPick: (r: PickResult) => void) {
    const ionToken = import.meta.env.VITE_CESIUM_ION_TOKEN as string | undefined;
    if (ionToken) Ion.defaultAccessToken = ionToken;

    this.viewer = new Viewer(container, {
      baseLayer: false,
      baseLayerPicker: false,
      geocoder: false,
      timeline: false,
      animation: false,
      navigationHelpButton: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: false,
      sceneModePicker: true,
      // Render only when something changes (camera, tiles, data) instead of 60 fps forever.
      requestRenderMode: true,
      maximumRenderTimeChange: Infinity,
      // 4x MSAA is expensive on integrated GPUs; FXAA below is nearly free.
      msaaSamples: 1,
      ...(ionToken ? { terrain: Terrain.fromWorldTerrain() } : {}),
    });
    const scene = this.viewer.scene;
    scene.postProcessStages.fxaa.enabled = true;
    scene.globe.enableLighting = false;
    scene.globe.baseColor = Color.fromCssColorString("#0b1a2b"); // shown while/if tiles fail
    scene.globe.tileCacheSize = 300; // revisiting an area doesn't refetch tiles

    const layers = this.viewer.imageryLayers;
    this.baseLayers = {
      esri: layers.addImageryProvider(
        new UrlTemplateImageryProvider({
          url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
          credit: new Credit("Imagery: Esri, Maxar, Earthstar Geographics"),
          maximumLevel: 18,
        }),
      ),
      osm: layers.addImageryProvider(new OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" })),
      gibs: layers.addImageryProvider(this.gibsTrueColor(yesterday())),
    };
    this.ndviLayer = layers.addImageryProvider(
      new UrlTemplateImageryProvider({
        url: `${GIBS}/VIIRS_SNPP_NDVI_8Day/default/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png`,
        maximumLevel: 8,
        credit: GIBS_CREDIT,
      }),
    );
    this.ndviLayer.alpha = 0.6;
    this.ndviLayer.show = false;
    this.setBaseLayer("esri"); // hidden layers request no tiles

    // Draw order: zones under events under detections under the pin.
    this.zones = new Layer(scene.primitives);
    const tileEvents = scene.primitives.add(new PrimitiveCollection());
    this.events = new Layer(scene.primitives);
    const tileFires = scene.primitives.add(new PrimitiveCollection());
    this.detections = new Layer(scene.primitives);
    this.pinned = new Layer(scene.primitives);
    this.tiles = new FireTiles(tileFires, tileEvents, () => this.redraw());

    this.viewer.camera.setView({ destination: Cartesian3.fromDegrees(-62, -25, 9_000_000) });
    this.setupZoomControls();

    const handler = new ScreenSpaceEventHandler(scene.canvas);
    handler.setInputAction((e: { position: Cartesian2 }) => {
      const cart = this.viewer.camera.pickEllipsoid(e.position, scene.globe.ellipsoid);
      if (cart) this.zoomToward(cart);
    }, ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
    handler.setInputAction((e: { position: Cartesian2 }) => {
      const picked = scene.pick(e.position);
      const id: unknown = picked?.id;
      if (typeof id === "string" && id.startsWith("evt_")) {
        onPick({ kind: "event", eventId: id });
        return;
      }
      if (typeof id === "number" && this.detectionIds[id] !== undefined) {
        onPick({ kind: "detection", id: this.detectionIds[id] });
        return;
      }
      if (id && typeof id === "object" && "kind" in id) {
        onPick(id as PickResult); // tile markers carry their own description
        return;
      }
      const cart = this.viewer.camera.pickEllipsoid(e.position, scene.globe.ellipsoid);
      if (cart) {
        const c = Cartographic.fromCartesian(cart);
        onPick({ kind: "point", lat: CesiumMath.toDegrees(c.latitude), lon: CesiumMath.toDegrees(c.longitude) });
      }
    }, ScreenSpaceEventType.LEFT_CLICK);

    // Hover: describe what is under the pointer (at most once per animation frame).
    let pending: Cartesian2 | null = null;
    handler.setInputAction((e: { endPosition: Cartesian2 }) => {
      if (!pending) {
        requestAnimationFrame(() => {
          const p = pending;
          pending = null;
          this.hover(p);
        });
      }
      pending = Cartesian2.clone(e.endPosition);
    }, ScreenSpaceEventType.MOUSE_MOVE);
    scene.canvas.addEventListener("mouseleave", () => {
      pending = null;
      this.hoverListener?.(null, 0, 0);
      scene.canvas.style.cursor = "";
    });
  }

  private hoverListener: ((h: HoverTarget | null, x: number, y: number) => void) | null = null;

  /** Called with what the pointer is over (or null), in page coordinates. */
  onHover(fn: (h: HoverTarget | null, x: number, y: number) => void): void {
    this.hoverListener = fn;
  }

  private hover(pos: Cartesian2 | null): void {
    const scene = this.viewer.scene;
    if (!pos || !this.hoverListener) return;
    const id: unknown = scene.pick(pos)?.id;
    let target: HoverTarget | null = null;
    if (typeof id === "string" && id.startsWith("evt_")) target = { kind: "event", eventId: id, event: drawnEvents.get(id) };
    else if (id && typeof id === "object" && "kind" in id) {
      const k = (id as { kind: string }).kind;
      if (k === "cell" || k === "detection") target = id as HoverTarget;
    }
    const rect = scene.canvas.getBoundingClientRect();
    scene.canvas.style.cursor = target ? "pointer" : "";
    this.hoverListener(target, rect.left + pos.x, rect.top + pos.y);
  }

  private setupZoomControls(): void {
    const ctrl = this.viewer.scene.screenSpaceCameraController;
    // Trackpad pinch arrives as wheel + Ctrl (Chrome, Edge, Firefox, Safari). Cesium binds only the
    // plain wheel and swallows the Ctrl variant, so pinch-zoom silently did nothing.
    ctrl.zoomEventTypes = [
      CameraEventType.RIGHT_DRAG,
      CameraEventType.WHEEL,
      CameraEventType.PINCH,
      { eventType: CameraEventType.WHEEL, modifier: KeyboardEventModifier.CTRL },
    ];
    ctrl.minimumZoomDistance = 300; // metres: street level, not inside the ground
    ctrl.maximumZoomDistance = 40_000_000; // keep the whole Earth in view instead of a dot in space
    // Cesium's default double-click tracks an entity; double-click zooms in here (handler above).
    this.viewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

    const box = document.createElement("div");
    box.className = "zoom-controls";
    box.innerHTML = `
      <button type="button" data-zoom="in" title="Zoom in (+)" aria-label="Zoom in">+</button>
      <button type="button" data-zoom="out" title="Zoom out (−)" aria-label="Zoom out">−</button>`;
    box.addEventListener("click", (e) => {
      const dir = (e.target as HTMLElement).closest<HTMLElement>("[data-zoom]")?.dataset.zoom;
      if (dir) this.zoomBy(dir === "in" ? 0.5 : 2);
    });
    this.viewer.container.appendChild(box);
    window.addEventListener("keydown", (e) => {
      if ((e.target as HTMLElement).closest("input, select, textarea")) return;
      if (e.key === "+" || e.key === "=") this.zoomBy(0.5);
      else if (e.key === "-" || e.key === "_") this.zoomBy(2);
    });
  }

  /** Camera height in 3D/Columbus view, visible map width in 2D (metres). */
  private currentDistance(): number {
    const cam = this.viewer.camera;
    if (this.viewer.scene.mode === SceneMode.SCENE2D) {
      const f = cam.frustum as OrthographicOffCenterFrustum;
      return (f.right ?? 0) - (f.left ?? 0);
    }
    return cam.positionCartographic.height;
  }

  /** Zoom by a factor of the current distance (< 1 zooms in), within the controller's limits. */
  zoomBy(factor: number): void {
    const ctrl = this.viewer.scene.screenSpaceCameraController;
    const d = this.currentDistance();
    const target = CesiumMath.clamp(d * factor, ctrl.minimumZoomDistance, ctrl.maximumZoomDistance);
    if (target < d) this.viewer.camera.zoomIn(d - target);
    else this.viewer.camera.zoomOut(target - d);
    this.redraw();
  }

  private zoomToward(target: Cartesian3): void {
    const c = Cartographic.fromCartesian(target);
    const h = Math.max(this.currentDistance() * 0.5, this.viewer.scene.screenSpaceCameraController.minimumZoomDistance);
    this.viewer.camera.flyTo({ destination: Cartesian3.fromRadians(c.longitude, c.latitude, h), duration: 0.6 });
  }

  /** In request-render mode, data and visibility changes must ask for a frame. One extra
   * frame lets freshly built primitives and pick buffers settle. */
  private redraw(): void {
    const scene = this.viewer.scene;
    const once = () => {
      scene.postRender.removeEventListener(once);
      scene.requestRender();
    };
    scene.postRender.addEventListener(once);
    scene.requestRender();
  }

  private gibsTrueColor(date: string) {
    return new UrlTemplateImageryProvider({
      url: `${GIBS}/VIIRS_NOAA20_CorrectedReflectance_TrueColor/default/${date}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg`,
      maximumLevel: 9,
      credit: GIBS_CREDIT,
    });
  }

  setBaseLayer(id: BaseLayerId): void {
    for (const [k, layer] of Object.entries(this.baseLayers)) layer.show = k === id;
    this.redraw();
  }

  /** Swap the NASA GIBS daily true-colour mosaic to another date (YYYY-MM-DD). */
  setGibsDate(date: string): void {
    const layers = this.viewer.imageryLayers;
    const old = this.baseLayers.gibs;
    const fresh = layers.addImageryProvider(this.gibsTrueColor(date), layers.indexOf(old));
    fresh.show = old.show;
    layers.remove(old, true);
    this.baseLayers.gibs = fresh;
    this.redraw();
  }

  setNdviVisible(v: boolean): void {
    this.ndviLayer.show = v;
    this.redraw();
  }

  private firesOn = true;
  private eventsOn = true;

  setDetectionsVisible(v: boolean): void {
    this.firesOn = v;
    this.detections.show = v;
    this.tiles.setVisible(this.firesOn, this.eventsOn);
    this.redraw();
  }

  /** Grouped-marker cell size that puts bubbles ~CELL_PX apart on screen at the current zoom. */
  private idealCellDeg(): number {
    const widthKm = (this.viewer.scene.mode === SceneMode.SCENE2D ? this.currentDistance() : this.currentDistance() * 1.15) / 1000;
    const cellsAcross = Math.max(4, this.viewer.canvas.clientWidth / CELL_PX);
    return widthKm / cellsAcross / 111;
  }

  /** Level of detail for the current camera: which tile zoom to draw, and as what.
   * Cells halve in size at each level (180 / 2^level degrees); a tile is 16 x 16 of them. */
  lod(bbox: [number, number, number, number], detectionsInView: number | null = null): Lod {
    let level = Math.round(Math.log2(180 / this.idealCellDeg()));
    // Individual fires as soon as the view is regional (finest cell level, ~500 km across), unless
    // the area is so dense that thousands of overlapping dots would say less than grouped bubbles.
    // Any view that holds only a few thousand fires shows them individually, as NASA's map does.
    const fewEnough = detectionsInView === null || detectionsInView <= POINTS_MAX_IN_VIEW;
    const sparse = detectionsInView !== null && detectionsInView <= POINTS_ALWAYS_BELOW;
    const pointTiles = FireTiles.count(bbox, POINT_ZOOM);
    if (pointTiles <= FireTiles.limits.MAX_POINT_TILES && (level > MAX_CELL_LEVEL || sparse || (level === MAX_CELL_LEVEL && fewEnough))) {
      return { kind: "points", z: POINT_ZOOM, cellDeg: null };
    }
    level = Math.min(MAX_CELL_LEVEL, Math.max(MIN_LEVEL, level));
    // A tilted view to the horizon covers a lot of ground: use coarser cells rather than 100s of tiles.
    while (level > MIN_LEVEL && FireTiles.count(bbox, level - TILE_SHIFT) > FireTiles.limits.MAX_TILES) level--;
    return { kind: "cells", z: level - TILE_SHIFT, cellDeg: 180 / 2 ** level };
  }

  /** The container changed size (side panel shown/hidden): resize the canvas, redraw and reload
   * the tiles for the new view. */
  resize(): void {
    this.viewer.resize();
    this.redraw();
    this.viewer.camera.moveEnd.raiseEvent();
  }

  /** Call `fn` once the camera stops moving (after drags, zooms and flights). */
  onCameraSettled(fn: () => void, delayMs = 250): void {
    let timer: number | undefined;
    this.viewer.camera.moveEnd.addEventListener(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(fn, delayMs);
    });
  }

  setEventsVisible(v: boolean): void {
    this.eventsOn = v;
    this.events.show = v;
    this.tiles.setVisible(this.firesOn, this.eventsOn);
    this.redraw();
  }

  setZonesVisible(v: boolean): void {
    this.zones.show = v;
    this.redraw();
  }

  showDetections(d: CompactDetections): void {
    const points = new PointPrimitiveCollection();
    for (let i = 0; i < d.count; i++) {
      const viirs = d.viirs[i] === 1;
      const ramp = RAMP[rampIndex(d.frp[i])];
      points.add({
        id: i, // index into the columnar arrays; full record is fetched on click
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
    this.detectionIds = d.id;
    this.detections.replace(points);
    this.redraw();
  }

  /** Focus mode: one fire's footprint (every other fire comes from the tiles). */
  showEvents(events: FireEvent[]): void {
    this.events.replace(events.length ? renderEvents(events, null) : null);
    this.redraw();
  }

  showAlertZones(rules: AlertRule[]): void {
    const fills: GeometryInstance[] = [];
    const lines: GeometryInstance[] = [];
    const faint = ZONE_COLOR.withAlpha(0.06);
    for (const r of rules) {
      if (r.west != null && r.south != null && r.east != null && r.north != null) {
        const rectangle = Rectangle.fromDegrees(r.west, r.south, r.east, r.north);
        fills.push(instance(new RectangleGeometry({ rectangle, height: 0 }), faint));
        lines.push(instance(new RectangleOutlineGeometry({ rectangle, height: 0 }), ZONE_COLOR));
      }
      if (r.center_lat != null && r.center_lon != null && r.radius_km != null) {
        const center = Cartesian3.fromDegrees(r.center_lon, r.center_lat);
        const radius = r.radius_km * 1000;
        fills.push(instance(new CircleGeometry({ center, radius, height: 0 }), faint));
        lines.push(instance(new CircleOutlineGeometry({ center, radius, height: 0 }), ZONE_COLOR));
      }
    }
    this.zones.replace(batch(fills, false, false), batch(lines, true, false));
    this.redraw();
  }

  /** Marker + optional radius ring for the point/radius filter. */
  pin(lat: number | null, lon: number | null, radiusKm?: number): void {
    if (lat == null || lon == null) {
      this.pinned.replace();
      this.redraw();
      return;
    }
    const marker = new PointPrimitiveCollection();
    marker.add({
      position: Cartesian3.fromDegrees(lon, lat, MARKER_HEIGHT_M),
      pixelSize: 9,
      color: ZONE_COLOR,
      outlineColor: Color.WHITE,
      outlineWidth: 2,
      disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
    });
    const center = Cartesian3.fromDegrees(lon, lat);
    const ring = radiusKm
      ? [
          batch([instance(new CircleGeometry({ center, radius: radiusKm * 1000, height: 0 }), ZONE_COLOR.withAlpha(0.08))], false, false),
          batch([instance(new CircleOutlineGeometry({ center, radius: radiusKm * 1000, height: 0 }), ZONE_COLOR)], true, false),
        ]
      : [];
    this.pinned.replace(...ring, marker);
    this.redraw();
  }

  /** The part of the view the user is looking at, for choosing the level of detail and the tiles.
   * Looking straight down this is the whole view. Tilted toward the horizon, the view rectangle
   * reaches hundreds of km away and would force grouped bubbles even when the camera is close to
   * the ground; so it is cut to a disc around the screen centre whose radius is 1.5x the distance
   * from the camera to that point (which still covers the foreground). */
  focusBbox(): [number, number, number, number] | null {
    const view = this.viewBbox();
    if (!view || this.viewer.scene.mode !== SceneMode.SCENE3D) return view;
    const scene = this.viewer.scene;
    const centre = this.viewer.camera.pickEllipsoid(new Cartesian2(scene.canvas.clientWidth / 2, scene.canvas.clientHeight / 2), scene.globe.ellipsoid);
    if (!centre) return view;
    const slantKm = Cartesian3.distance(this.viewer.camera.positionWC, centre) / 1000;
    const c = Cartographic.fromCartesian(centre);
    const lat = CesiumMath.toDegrees(c.latitude), lon = CesiumMath.toDegrees(c.longitude);
    const rKm = 1.5 * slantKm;
    const dLat = rKm / 111.2;
    const dLon = rKm / (111.2 * Math.max(Math.cos(c.latitude), 0.05));
    if (dLat >= 90 || dLon >= 180) return view;
    const focus: [number, number, number, number] = [lon - dLon, Math.max(-90, lat - dLat), lon + dLon, Math.min(90, lat + dLat)];
    // keep the smaller of the two on each side (the view can be narrower than the disc)
    const [vw, vs, ve, vn] = view;
    const crosses = vw > ve || focus[0] < -180 || focus[2] > 180;
    if (crosses) return view; // near the antimeridian: keep the plain view rectangle
    const r4 = (x: number) => Math.round(x * 1e4) / 1e4;
    return [r4(Math.max(vw, focus[0])), r4(Math.max(vs, focus[1])), r4(Math.min(ve, focus[2])), r4(Math.min(vn, focus[3]))];
  }

  /** Current camera footprint as west,south,east,north (degrees), or null when looking at space. */
  viewBbox(): [number, number, number, number] | null {
    const r = this.viewer.camera.computeViewRectangle();
    if (!r) return null;
    const d = (x: number) => Math.round(CesiumMath.toDegrees(x) * 1e4) / 1e4;
    return [d(r.west), d(r.south), d(r.east), d(r.north)];
  }

  /** Like viewBbox, but when the edge of the globe is in view (where viewBbox falls back to the
   * whole world) it returns the box around the visible cap: everything up to the horizon. */
  visibleBbox(): [number, number, number, number] | null {
    const box = this.viewBbox();
    if (!box || this.viewer.scene.mode !== SceneMode.SCENE3D || box[2] - box[0] < 359) return box;
    const c = this.viewer.camera.positionCartographic;
    const R = 6_371_000;
    const theta = Math.acos(R / (R + Math.max(0, c.height))); // angular distance to the horizon
    const lat = c.latitude, lon = CesiumMath.toDegrees(c.longitude);
    const deg = CesiumMath.toDegrees;
    const south = deg(lat - theta), north = deg(lat + theta);
    const ratio = Math.sin(theta) / Math.cos(lat);
    if (south <= -90 || north >= 90 || ratio >= 1) return [-180, Math.max(-90, south), 180, Math.min(90, north)];
    const dlon = deg(Math.asin(ratio));
    const wrap = (x: number) => ((x + 540) % 360) - 180;
    const r4 = (x: number) => Math.round(x * 1e4) / 1e4;
    return [r4(wrap(lon - dlon)), r4(south), r4(wrap(lon + dlon)), r4(north)];
  }

  flyTo(lat: number, lon: number, heightM = 60_000): void {
    this.viewer.camera.flyTo({ destination: Cartesian3.fromDegrees(lon, lat, heightM), duration: 1.6 });
  }

  flyToBox(w: number, s: number, e: number, n: number): void {
    const pad = Math.max(0.05, (n - s) * 0.6, (e - w) * 0.6);
    this.viewer.camera.flyTo({ destination: Rectangle.fromDegrees(w - pad, s - pad, e + pad, n + pad), duration: 1.6 });
  }
}
