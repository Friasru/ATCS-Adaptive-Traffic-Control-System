// SUMO <-> three.js coordinates.
//
// SUMO:    x east, y north, z up. Heading is degrees clockwise from north.
// three.js: x east, y up,   z south  (so north is -z).
// The world is centred on the junction so the camera orbits (0, 0, 0).
//
// Vehicle models point along +X. A rotation of theta about +Y turns +X toward (cos t, 0, -sin t); the heading
// vector is (sin a, 0, -cos a), hence  theta = 90deg - a.  (tests/test_web.py checks this with real SUMO frames)

export class Coords {
  constructor(net) {
    [this.cx, this.cy] = net.center;
  }
  x(sumoX) { return sumoX - this.cx; }
  z(sumoY) { return -(sumoY - this.cy); }
  // SUMO (x, y) -> world [X, Z]
  xz(sumoX, sumoY) { return [sumoX - this.cx, -(sumoY - this.cy)]; }
  // heading (SUMO degrees) -> model yaw (radians about +Y)
  yaw(angleDeg) { return Math.PI / 2 - (angleDeg * Math.PI) / 180; }
  // unit travel direction in world XZ
  dir(angleDeg) {
    const a = (angleDeg * Math.PI) / 180;
    return [Math.sin(a), -Math.cos(a)];
  }
}

export function shortestAngleDelta(a, b) {
  // signed smallest difference b - a in degrees, for headings
  return ((((b - a) % 360) + 540) % 360) - 180;
}
