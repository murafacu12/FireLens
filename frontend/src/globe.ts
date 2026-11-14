import {
  Cartesian2,
  Cartesian3,
  Cartographic,
  Color,
  Credit,
  CustomDataSource,
  DistanceDisplayCondition,
  Entity,
  ImageryLayer,
  Ion,
  LabelStyle,
  Math as CesiumMath,
  NearFarScalar,
  OpenStreetMapImageryProvider,
  PointPrimitiveCollection,
  Rectangle,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  Terrain,
  UrlTemplateImageryProvider,
  VerticalOrigin,
  Viewer,
} from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";

import type { AlertRule, FireDetection, FireEvent } from "./api";

export type BaseLayerId = "esri" | "osm" | "gibs";
export type PickResult =
  | { kind: "detection"; detection: FireDetection }
  | { kind: "event"; eventId: string }
  | { kind: "point"; lat: number; lon: number };

const GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best";
// Markers sit slightly above the ellipsoid and skip depth testing only when the camera is
// close, so they never z-fight with the surface yet stay hidden behind the far side of Earth.
const MARKER_HEIGHT_M = 1000;
const NO_DEPTH_TEST_WITHIN_M = 1_500_000;
const GIBS_CREDIT = new Credit("Imagery: NASA EOSDIS GIBS");

function yesterday(): string {
  return new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
}

/** FRP (MW) -> colour ramp yellow -> orange -> red -> magenta (log scale). */
export function frpColor(frp: number | null): Color {
  const t = Math.min(1, Math.log10(Math.max(1, frp ?? 1)) / 3); // 1 MW .. 1000 MW
  const stops = [Color.fromCssColorString("#ffe14d"), Color.fromCssColorString("#ff8a1f"),
    Color.fromCssColorString("#e8261b"), Color.fromCssColorString("#b0127a")];
  const x = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  return Color.lerp(stops[i], stops[i + 1], x - i, new Color());
}

export class Globe {
  readonly viewer: Viewer;
  private readonly baseLayers: Record<BaseLayerId, ImageryLayer>;
  private readonly ndviLayer: ImageryLayer;
  private readonly points: PointPrimitiveCollection;
  private readonly events = new CustomDataSource("events");
  private readonly zones = new CustomDataSource("alert-zones");
  private readonly pinned = new CustomDataSource("pinned");
  private detectionById = new Map<string, FireDetection>();

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
      ...(ionToken ? { terrain: Terrain.fromWorldTerrain() } : {}),
    });
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
    this.setBaseLayer("esri");

    this.points = this.viewer.scene.primitives.add(new PointPrimitiveCollection());
    void this.viewer.dataSources.add(this.zones);
    void this.viewer.dataSources.add(this.events);
    void this.viewer.dataSources.add(this.pinned);

    this.viewer.scene.globe.enableLighting = false;
    this.viewer.scene.globe.baseColor = Color.fromCssColorString("#0b1a2b"); // shown while/if tiles fail
    this.viewer.camera.setView({ destination: Cartesian3.fromDegrees(-62, -25, 9_000_000) });

    const handler = new ScreenSpaceEventHandler(this.viewer.scene.canvas);
    handler.setInputAction((e: { position: Cartesian2 }) => {
      const picked = this.viewer.scene.pick(e.position);
      if (picked?.id instanceof Entity && picked.id.id.startsWith("evt_")) {
        onPick({ kind: "event", eventId: picked.id.id });
        return;
      }
      if (typeof picked?.id === "string" && this.detectionById.has(picked.id)) {
        onPick({ kind: "detection", detection: this.detectionById.get(picked.id)! });
        return;
      }
      const cart = this.viewer.camera.pickEllipsoid(e.position, this.viewer.scene.globe.ellipsoid);
      if (cart) {
        const c = Cartographic.fromCartesian(cart);
        onPick({ kind: "point", lat: CesiumMath.toDegrees(c.latitude), lon: CesiumMath.toDegrees(c.longitude) });
      }
    }, ScreenSpaceEventType.LEFT_CLICK);
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
  }

  /** Swap the NASA GIBS daily true-colour mosaic to another date (YYYY-MM-DD). */
  setGibsDate(date: string): void {
    const layers = this.viewer.imageryLayers;
    const old = this.baseLayers.gibs;
    const index = layers.indexOf(old);
    const fresh = layers.addImageryProvider(this.gibsTrueColor(date), index);
    fresh.show = old.show;
    layers.remove(old, true);
    this.baseLayers.gibs = fresh;
  }

  setNdviVisible(v: boolean): void {
    this.ndviLayer.show = v;
  }

  setDetectionsVisible(v: boolean): void {
    this.points.show = v;
  }

  setEventsVisible(v: boolean): void {
    this.events.show = v;
  }

  setZonesVisible(v: boolean): void {
    this.zones.show = v;
  }

  showDetections(dets: FireDetection[]): void {
    this.points.removeAll();
    this.detectionById = new Map(dets.map((d) => [d.id, d]));
    for (const d of dets) {
      const viirs = d.instrument === "VIIRS";
      this.points.add({
        id: d.id,
        position: Cartesian3.fromDegrees(d.longitude, d.latitude, MARKER_HEIGHT_M),
        color: frpColor(d.frp_mw).withAlpha(d.confidence_level === "low" ? 0.5 : 0.95),
        pixelSize: viirs ? 5 : 8,
        // Outline encodes the sensor: white = VIIRS (375 m), dark = MODIS (1 km)
        outlineColor: viirs ? Color.WHITE.withAlpha(0.7) : Color.BLACK.withAlpha(0.8),
        outlineWidth: 1,
        scaleByDistance: new NearFarScalar(5e4, 1.8, 8e6, 0.7),
        disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
      });
    }
  }

  showEvents(events: FireEvent[]): void {
    this.events.entities.removeAll();
    for (const ev of events) {
      const active = ev.status === "active";
      const radiusM = Math.max(400, Math.sqrt(ev.area_km2 / Math.PI) * 1000);
      const color = active ? Color.fromCssColorString("#ff5a1f") : Color.fromCssColorString("#9a7b5c");
      this.events.entities.add({
        id: ev.id,
        position: Cartesian3.fromDegrees(ev.longitude, ev.latitude),
        ellipse: {
          semiMajorAxis: radiusM,
          semiMinorAxis: radiusM,
          height: 0,
          material: color.withAlpha(active ? 0.28 : 0.18),
          outline: true,
          outlineColor: color,
        },
        label:
          ev.area_km2 >= 1
            ? {
                text: `${ev.area_km2.toFixed(1)} km²`,
                font: "12px Inter, system-ui, sans-serif",
                fillColor: Color.WHITE,
                outlineColor: Color.BLACK,
                outlineWidth: 3,
                style: LabelStyle.FILL_AND_OUTLINE,
                verticalOrigin: VerticalOrigin.BOTTOM,
                pixelOffset: new Cartesian2(0, -10),
                distanceDisplayCondition: new DistanceDisplayCondition(0, 2_500_000),
                disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M,
              }
            : undefined,
      });
    }
  }

  showAlertZones(rules: AlertRule[]): void {
    this.zones.entities.removeAll();
    const cyan = Color.fromCssColorString("#38bdf8");
    for (const r of rules) {
      if (r.west != null && r.south != null && r.east != null && r.north != null) {
        this.zones.entities.add({
          rectangle: {
            coordinates: Rectangle.fromDegrees(r.west, r.south, r.east, r.north),
            material: cyan.withAlpha(0.06),
            outline: true,
            outlineColor: cyan,
            height: 0,
          },
        });
      }
      if (r.center_lat != null && r.center_lon != null && r.radius_km != null) {
        this.zones.entities.add({
          position: Cartesian3.fromDegrees(r.center_lon, r.center_lat),
          ellipse: {
            semiMajorAxis: r.radius_km * 1000,
            semiMinorAxis: r.radius_km * 1000,
            material: cyan.withAlpha(0.06),
            outline: true,
            outlineColor: cyan,
            height: 0,
          },
        });
      }
    }
  }

  /** Marker + optional radius ring for the point/radius filter. */
  pin(lat: number | null, lon: number | null, radiusKm?: number): void {
    this.pinned.entities.removeAll();
    if (lat == null || lon == null) return;
    this.pinned.entities.add({
      position: Cartesian3.fromDegrees(lon, lat),
      point: { pixelSize: 9, color: Color.fromCssColorString("#38bdf8"), outlineColor: Color.WHITE, outlineWidth: 2,
        disableDepthTestDistance: NO_DEPTH_TEST_WITHIN_M },
      ellipse: radiusKm
        ? { semiMajorAxis: radiusKm * 1000, semiMinorAxis: radiusKm * 1000, height: 0,
            material: Color.fromCssColorString("#38bdf8").withAlpha(0.08), outline: true,
            outlineColor: Color.fromCssColorString("#38bdf8") }
        : undefined,
    });
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
