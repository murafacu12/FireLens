import {
  Cartesian2,
  Cartesian3,
  Cartographic,
  CircleGeometry,
  CircleOutlineGeometry,
  Color,
  ColorGeometryInstanceAttribute,
  Credit,
  DistanceDisplayCondition,
  GeometryInstance,
  ImageryLayer,
  Ion,
  LabelCollection,
  LabelStyle,
  Math as CesiumMath,
  NearFarScalar,
  OpenStreetMapImageryProvider,
  PerInstanceColorAppearance,
  PointPrimitiveCollection,
  Primitive,
  PrimitiveCollection,
  Rectangle,
  RectangleGeometry,
  RectangleOutlineGeometry,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  Terrain,
  UrlTemplateImageryProvider,
  VerticalOrigin,
  Viewer,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";

import type { AlertRule, CompactDetections, FireEvent } from "./api";

export type BaseLayerId = "esri" | "osm" | "gibs";
export type PickResult =
  | { kind: "detection"; id: string }
  | { kind: "event"; eventId: string }
  | { kind: "point"; lat: number; lon: number };

const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best";
// Markers sit slightly above the ellipsoid and skip depth testing only when the camera is
// close, so they never z-fight with the surface yet stay hidden behind the far side of Earth.
const MARKER_HEIGHT_M = 1000;
const NO_DEPTH_TEST_WITHIN_M = 1_500_000;
const GIBS_CREDIT = new Credit("Imagery: NASA EOSDIS GIBS");
const ZONE_COLOR = Color.fromCssColorString("#38bdf8");
const ACTIVE_COLOR = Color.fromCssColorString("#ff5a1f");
const INACTIVE_COLOR = Color.fromCssColorString("#9a7b5c");

function yesterday(): string {
  return new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
}

// FRP (MW) -> colour ramp yellow -> orange -> red -> magenta on a log scale (1..1000 MW).
// Precomputed once: building 50k points must not allocate 50k Color objects.
const RAMP_STEPS = 48;
const RAMP_STOPS = ["#ffe14d", "#ff8a1f", "#e8261b", "#b0127a"].map((c) => Color.fromCssColorString(c));
const RAMP: { solid: Color; faint: Color }[] = Array.from({ length: RAMP_STEPS }, (_, k) => {
  const x = (k / (RAMP_STEPS - 1)) * (RAMP_STOPS.length - 1);
  const i = Math.min(RAMP_STOPS.length - 2, Math.floor(x));
  const c = Color.lerp(RAMP_STOPS[i], RAMP_STOPS[i + 1], x - i, new Color());
  return { solid: c.withAlpha(0.95), faint: c.withAlpha(0.5) };
});
function rampIndex(frp: number | null): number {
  const t = Math.min(1, Math.log10(Math.max(1, frp ?? 1)) / 3);
  return Math.round(t * (RAMP_STEPS - 1));
}
const VIIRS_OUTLINE = Color.WHITE.withAlpha(0.7);
const MODIS_OUTLINE = Color.BLACK.withAlpha(0.8);
const POINT_SCALE = new NearFarScalar(5e4, 1.8, 8e6, 0.7);
const LABEL_RANGE = new DistanceDisplayCondition(0, 2_500_000);

/** Batch many shapes into one draw call; geometry is built synchronously so it is
 * ready on the next requested frame (we render on demand, not every frame). */
function batch(instances: GeometryInstance[], outline: boolean, pickable = true): Primitive | null {
  if (!instances.length) return null;
  return new Primitive({
    geometryInstances: instances,
    appearance: new PerInstanceColorAppearance({ flat: true, translucent: !outline }),
    asynchronous: false,
    // Decorative shapes (zones, radius ring) must not swallow clicks on the fires inside them.
    allowPicking: pickable,
  });
}

function instance(geometry: ConstructorParameters<typeof GeometryInstance>[0]["geometry"], color: Color, id?: string): GeometryInstance {
  return new GeometryInstance({ geometry, id, attributes: { color: ColorGeometryInstanceAttribute.fromColor(color) } });
}

/** A named group of primitives that can be replaced and toggled as one layer. */
class Layer {
  readonly group: PrimitiveCollection;
  constructor(parent: PrimitiveCollection) {
    this.group = parent.add(new PrimitiveCollection());
  }
  replace(...items: (Primitive | LabelCollection | PointPrimitiveCollection | null)[]): void {
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
  private readonly detections: Layer;
  private readonly events: Layer;
  private readonly zones: Layer;
  private readonly pinned: Layer;
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
    this.events = new Layer(scene.primitives);
    this.detections = new Layer(scene.primitives);
    this.pinned = new Layer(scene.primitives);

    this.viewer.camera.setView({ destination: Cartesian3.fromDegrees(-62, -25, 9_000_000) });

    const handler = new ScreenSpaceEventHandler(scene.canvas);
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
      const cart = this.viewer.camera.pickEllipsoid(e.position, scene.globe.ellipsoid);
      if (cart) {
        const c = Cartographic.fromCartesian(cart);
        onPick({ kind: "point", lat: CesiumMath.toDegrees(c.latitude), lon: CesiumMath.toDegrees(c.longitude) });
      }
    }, ScreenSpaceEventType.LEFT_CLICK);
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

  setDetectionsVisible(v: boolean): void {
    this.detections.show = v;
    this.redraw();
  }

  setEventsVisible(v: boolean): void {
    this.events.show = v;
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

  showEvents(events: FireEvent[]): void {
    const fills: GeometryInstance[] = [];
    const lines: GeometryInstance[] = [];
    const labels = new LabelCollection();
    for (const ev of events) {
      const active = ev.status === "active";
      const color = active ? ACTIVE_COLOR : INACTIVE_COLOR;
      const center = Cartesian3.fromDegrees(ev.longitude, ev.latitude);
      const radius = Math.max(400, Math.sqrt(ev.area_km2 / Math.PI) * 1000);
      fills.push(instance(new CircleGeometry({ center, radius, height: 0 }), color.withAlpha(active ? 0.28 : 0.18), ev.id));
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
    this.events.replace(batch(fills, false), batch(lines, true), labels);
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

  /** Current camera footprint as west,south,east,north (degrees), or null when looking at space. */
  viewBbox(): [number, number, number, number] | null {
    const r = this.viewer.camera.computeViewRectangle();
    if (!r) return null;
    const d = (x: number) => Math.round(CesiumMath.toDegrees(x) * 1e4) / 1e4;
    return [d(r.west), d(r.south), d(r.east), d(r.north)];
  }

  flyTo(lat: number, lon: number, heightM = 60_000): void {
    this.viewer.camera.flyTo({ destination: Cartesian3.fromDegrees(lon, lat, heightM), duration: 1.6 });
  }

  flyToBox(w: number, s: number, e: number, n: number): void {
    const pad = Math.max(0.05, (n - s) * 0.6, (e - w) * 0.6);
    this.viewer.camera.flyTo({ destination: Rectangle.fromDegrees(w - pad, s - pad, e + pad, n + pad), duration: 1.6 });
  }
}
