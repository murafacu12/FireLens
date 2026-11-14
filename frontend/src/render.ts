// Drawing helpers shared by the globe and the fire tiles.
import { Color, ColorGeometryInstanceAttribute, GeometryInstance, PerInstanceColorAppearance, Primitive } from "cesium";

// Markers sit slightly above the ellipsoid and skip depth testing only when the camera is
// close, so they never z-fight with the surface yet stay hidden behind the far side of Earth.
export const MARKER_HEIGHT_M = 1000;
export const NO_DEPTH_TEST_WITHIN_M = 1_500_000;

// FRP (MW) -> colour ramp yellow -> orange -> red -> magenta on a log scale (1..1000 MW).
// Precomputed once: building 50k points must not allocate 50k Color objects.
const RAMP_STEPS = 48;
const RAMP_STOPS = ["#ffe14d", "#ff8a1f", "#e8261b", "#b0127a"].map((c) => Color.fromCssColorString(c));
export const RAMP: { solid: Color; faint: Color }[] = Array.from({ length: RAMP_STEPS }, (_, k) => {
  const x = (k / (RAMP_STEPS - 1)) * (RAMP_STOPS.length - 1);
  const i = Math.min(RAMP_STOPS.length - 2, Math.floor(x));
  const c = Color.lerp(RAMP_STOPS[i], RAMP_STOPS[i + 1], x - i, new Color());
  return { solid: c.withAlpha(0.95), faint: c.withAlpha(0.5) };
});
export function rampIndex(frp: number | null): number {
  const t = Math.min(1, Math.log10(Math.max(1, frp ?? 1)) / 3);
  return Math.round(t * (RAMP_STEPS - 1));
}

/** Batch many shapes into one draw call; geometry is built synchronously so it is
 * ready on the next requested frame (we render on demand, not every frame). */
export function batch(instances: GeometryInstance[], outline: boolean, pickable = true): Primitive | null {
  if (!instances.length) return null;
  return new Primitive({
    geometryInstances: instances,
    appearance: new PerInstanceColorAppearance({ flat: true, translucent: !outline }),
    asynchronous: false,
    // Decorative shapes (zones, radius ring) must not swallow clicks on the fires inside them.
    allowPicking: pickable,
  });
}

export function instance(geometry: ConstructorParameters<typeof GeometryInstance>[0]["geometry"], color: Color, id?: string): GeometryInstance {
  return new GeometryInstance({ geometry, id, attributes: { color: ColorGeometryInstanceAttribute.fromColor(color) } });
}
