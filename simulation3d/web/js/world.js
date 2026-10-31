// The static scene: ground, roads, markings, sidewalks, buildings, trees and signal masts.
// Road geometry is generated from the SUMO network sent by the server (lane shapes, junction polygon), so it is the
// same geometry SUMO simulates. Nothing here moves or decides anything.
import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const COLORS = {
  sky: 0xb7d4ee,
  grass: 0x4f8a4f,
  asphalt: 0x30343b,
  junction: 0x30343b,
  sidewalk: 0xb9bcc2,
  curb: 0x8d9097,
  white: 0xf2f2f2,
  yellow: 0xf0c419,
};

// small deterministic PRNG so the surroundings look the same on every launch
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296);
}

class Batch {
  constructor() { this.parts = new Map(); }
  add(key, geometry) {
    const g = geometry.index ? geometry.toNonIndexed() : geometry;
    for (const name of Object.keys(g.attributes)) if (name !== 'position' && name !== 'normal') g.deleteAttribute(name);
    if (!this.parts.has(key)) this.parts.set(key, []);
    this.parts.get(key).push(g);
  }
  build(materials, group) {
    for (const [key, list] of this.parts) {
      const mesh = new THREE.Mesh(mergeGeometries(list, false), materials[key]);
      mesh.receiveShadow = true;
      mesh.castShadow = key.startsWith('bld') || key === 'sidewalk';
      mesh.name = `static:${key}`;
      group.add(mesh);
    }
  }
}

// flat quad strip between two world XZ points, lying at height y
function strip(ax, az, bx, bz, width, y) {
  const dx = bx - ax, dz = bz - az, len = Math.hypot(dx, dz) || 1;
  const nx = (-dz / len) * (width / 2), nz = (dx / len) * (width / 2);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute([
    ax + nx, y, az + nz,  bx + nx, y, bz + nz,  bx - nx, y, bz - nz,
    ax + nx, y, az + nz,  bx - nx, y, bz - nz,  ax - nx, y, az - nz,
  ], 3));
  g.setAttribute('normal', new THREE.Float32BufferAttribute(new Array(6).fill([0, 1, 0]).flat(), 3));
  return g;
}

function polyline(batch, key, pts, width, y) {
  for (let i = 0; i < pts.length - 1; i++) batch.add(key, strip(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], width, y));
}

function dashed(batch, key, a, b, width, y, dash = 3, gap = 4.5) {
  const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
  const ux = (b[0] - a[0]) / len, uz = (b[1] - a[1]) / len;
  for (let d = 0; d < len; d += dash + gap) {
    const e = Math.min(d + dash, len);
    batch.add(key, strip(a[0] + ux * d, a[1] + uz * d, a[0] + ux * e, a[1] + uz * e, width, y));
  }
}

// polygon given in SUMO coords (relative to centre) -> extruded slab lying on the ground
function slab(points, height, y0 = 0) {
  const shape = new THREE.Shape(points.map(([x, y]) => new THREE.Vector2(x, y)));
  const g = new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false });
  g.rotateX(-Math.PI / 2); // (x, y, z) -> (x, z, -y): SUMO y -> world -z
  g.translate(0, y0, 0);
  return g;
}

export const SIGNAL_COLOR = { red: 0xff3b30, yellow: 0xffc400, green: 0x2fd35b };

export function buildWorld(net, coords) {
  const group = new THREE.Group();
  group.name = 'world';
  const mats = {
    ground: new THREE.MeshStandardMaterial({ color: COLORS.grass, roughness: 1 }),
    asphalt: new THREE.MeshStandardMaterial({ color: COLORS.asphalt, roughness: 0.95 }),
    junction: new THREE.MeshStandardMaterial({ color: COLORS.junction, roughness: 0.95 }),
    sidewalk: new THREE.MeshStandardMaterial({ color: COLORS.sidewalk, roughness: 0.9 }),
    white: new THREE.MeshStandardMaterial({ color: COLORS.white, roughness: 0.7, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 }),
    yellow: new THREE.MeshStandardMaterial({ color: COLORS.yellow, roughness: 0.7, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 }),
    bld0: new THREE.MeshStandardMaterial({ color: 0xc9b9a6, roughness: 0.9 }),
    bld1: new THREE.MeshStandardMaterial({ color: 0x9fb2c8, roughness: 0.85 }),
    bld2: new THREE.MeshStandardMaterial({ color: 0xd7c7b0, roughness: 0.9 }),
    bld3: new THREE.MeshStandardMaterial({ color: 0x8e9aa8, roughness: 0.85 }),
  };

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(4000, 4000), mats.ground);
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.05;
  ground.receiveShadow = true;
  ground.name = 'ground';
  group.add(ground);

  const batch = new Batch();
  const laneW = net.edges[0].lanes[0].width;

  // ---- asphalt: one ribbon per lane, from the lane shapes of the SUMO network
  for (const e of net.edges) {
    for (const lane of e.lanes) {
      const pts = lane.shape.map(([x, y]) => coords.xz(x, y));
      polyline(batch, 'asphalt', pts, lane.width + 0.02, 0.01);
    }
  }
  // ---- junction polygon (rounded corners) straight from the net file
  for (const j of net.junctions) {
    if (j.type !== 'traffic_light' || j.shape.length < 3) continue;
    const pts = j.shape.map(([x, y]) => [x - coords.cx, y - coords.cy]);
    batch.add('junction', slab(pts, 0.012, 0.0));
  }

  // ---- markings. Each approach lane ends at the junction; its opposite lane starts there.
  const E = laneW; // road half-width: lane centre is laneW/2 off the road axis, edge laneW from the axis
  const mouth = net.approaches.map((a) => {
    const [qx, qy] = a.stop_point;
    const [vx, vy] = a.dir_vec;
    const l = Math.hypot(vx, vy);
    const d = [vx / l, vy / l]; // travel direction (SUMO)
    const r = [d[1], -d[0]]; // to the driver's right (right-hand traffic)
    return { a, q: [qx, qy], d, r };
  });
  const dist = Math.hypot(mouth[0].q[0] - net.center[0], mouth[0].q[1] - net.center[1]); // centre -> stop line (7.2 m)
  const R = dist - E; // corner radius of the junction polygon
  const armLen = 190;

  for (const m of mouth) {
    const { d, r, q } = m;
    // road axis point at the mouth: lane centre + (laneW/2) to the driver's left
    const ax = q[0] - r[0] * (laneW / 2), ay = q[1] - r[1] * (laneW / 2);
    const A = (t, lat) => coords.xz(ax - d[0] * t + r[0] * lat, ay - d[1] * t + r[1] * lat); // t metres back, lat to the right
    // double yellow centre line, white edge lines
    for (const off of [-0.12, 0.12]) batch.add('yellow', strip(...A(0.3, off), ...A(armLen, off), 0.12, 0.03));
    for (const off of [E - 0.12, -(E - 0.12)]) batch.add('white', strip(...A(0.3, off), ...A(armLen, off), 0.15, 0.03));
    // dashed lane separators are not needed (one lane per direction)

    // stop line across the approach lane (driver's right half of the road)
    const sl = strip(...A(0.35, laneW / 2), ...A(0.95, laneW / 2), laneW - 0.3, 0.03);
    batch.add('white', sl);

    // zebra crossing just beyond the stop line, inside the junction mouth (decoration: SUMO has no pedestrians)
    for (let lat = -E + 0.5; lat <= E - 0.45; lat += 0.85) {
      batch.add('white', strip(...A(-0.9, lat), ...A(-3.9, lat), 0.45, 0.03));
    }
  }

  // ---- sidewalks: corner pads with rounded inner corners, plus strips along the arms
  const W = 16; // pad size
  const SW = 2.6; // arm sidewalk width
  for (const [sx, sy] of [[1, 1], [-1, 1], [-1, -1], [1, -1]]) {
    // quadrant pad in SUMO-relative coords, mirrored by (sx, sy)
    const pts = [];
    const cx = E + R, cy = E + R;
    pts.push([E, E + R]);
    for (let k = 0; k <= 8; k++) { // arc around (E+R, E+R) from 180deg to 270deg
      const ang = Math.PI + (k / 8) * (Math.PI / 2);
      pts.push([cx + R * Math.cos(ang), cy + R * Math.sin(ang)]);
    }
    pts.push([E + W, E], [E + W, E + W], [E, E + W]);
    const mirrored = pts.map(([x, y]) => [x * sx, y * sy]);
    if (sx * sy < 0) mirrored.reverse(); // keep a consistent winding after mirroring
    batch.add('sidewalk', slab(mirrored, 0.16));
  }
  for (const m of mouth) {
    const { d, r, q } = m;
    for (const side of [1, -1]) {
      // strip beyond the pad, along the arm, on both sides of the road
      const o0 = side * (E + SW / 2);
      const base = [q[0] - r[0] * (laneW / 2), q[1] - r[1] * (laneW / 2)]; // road axis at the mouth
      const p0 = [base[0] - d[0] * (W - 1) + r[0] * o0, base[1] - d[1] * (W - 1) + r[1] * o0];
      const p1 = [base[0] - d[0] * armLen + r[0] * o0, base[1] - d[1] * armLen + r[1] * o0];
      const wx = r[0] * SW / 2, wy = r[1] * SW / 2;
      const poly = [
        [p0[0] - wx, p0[1] - wy], [p1[0] - wx, p1[1] - wy], [p1[0] + wx, p1[1] + wy], [p0[0] + wx, p0[1] + wy],
      ].map(([x, y]) => [x - coords.cx, y - coords.cy]);
      const area = poly.reduce((s, p, i) => s + (p[0] * poly[(i + 1) % 4][1] - poly[(i + 1) % 4][0] * p[1]), 0);
      if (area < 0) poly.reverse();
      batch.add('sidewalk', slab(poly, 0.16));
    }
  }

  // ---- buildings on the four blocks. Heights are capped by distance so that, from the default overview camera
  // (elevation ~30 deg or more), no building can hide the junction.
  const rand = rng(7);
  for (const [sx, sy] of [[1, 1], [-1, 1], [-1, -1], [1, -1]]) {
    for (let gx = 0; gx < 3; gx++) {
      for (let gy = 0; gy < 3; gy++) {
        const off = E + W + 10;
        const bw = 12 + rand() * 10, bd = 12 + rand() * 10;
        const nearest = Math.hypot(off + gx * 26, off + gy * 26);
        const bh = Math.min(7 + rand() * 24, 0.4 * nearest);
        const x = (off + gx * 26 + bw / 2) * sx, y = (off + gy * 26 + bd / 2) * sy;
        const g = new THREE.BoxGeometry(bw, bh, bd);
        g.translate(x, bh / 2, -y);
        batch.add(`bld${Math.floor(rand() * 4)}`, g);
      }
    }
  }

  batch.build(mats, group);

  // ---- trees (instanced) on the corner pads
  const trunkG = new THREE.CylinderGeometry(0.18, 0.25, 2.2, 6).translate(0, 1.1, 0);
  const crownG = new THREE.IcosahedronGeometry(1.7, 1).translate(0, 3.6, 0);
  const trees = [];
  for (const [sx, sy] of [[1, 1], [-1, 1], [-1, -1], [1, -1]]) {
    for (const [tx, ty] of [[E + 5, E + 11], [E + 11, E + 5], [E + 12, E + 12], [E + 4.2, E + 4.2 + 8.5]]) {
      trees.push([tx * sx, ty * sy]);
    }
  }
  const trunks = new THREE.InstancedMesh(trunkG, new THREE.MeshStandardMaterial({ color: 0x6b4a2f, roughness: 1 }), trees.length);
  const crowns = new THREE.InstancedMesh(crownG, new THREE.MeshStandardMaterial({ color: 0x3e7d3e, roughness: 0.9, flatShading: true }), trees.length);
  const m4 = new THREE.Matrix4();
  trees.forEach(([x, y], i) => {
    const s = 0.9 + rand() * 0.4;
    m4.makeScale(s, s, s).setPosition(x, 0.16, -y);
    trunks.setMatrixAt(i, m4);
    crowns.setMatrixAt(i, m4);
  });
  for (const t of [trunks, crowns]) { t.castShadow = true; t.name = 'trees'; group.add(t); }

  return group;
}

// ------------------------------------------------------------------------------------------------ signal heads
function glowTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.25, 'rgba(255,255,255,0.45)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

export class SignalRig {
  // One mast per approach with a three-lamp head over the lane. The head shows the state SUMO reports for the
  // straight-ahead link of that lane (all demand in the ATCS scenarios goes straight).
  constructor(net, coords) {
    this.group = new THREE.Group();
    this.group.name = 'signals';
    this.heads = [];
    const poleMat = new THREE.MeshStandardMaterial({ color: 0x4a4f57, roughness: 0.6, metalness: 0.4 });
    const boxMat = new THREE.MeshStandardMaterial({ color: 0x16181c, roughness: 0.6 });
    const glow = glowTexture();
    const laneW = net.edges[0].lanes[0].width;
    for (const a of net.approaches) {
      const [qx, qy] = a.stop_point;
      const l = Math.hypot(...a.dir_vec);
      const d = [a.dir_vec[0] / l, a.dir_vec[1] / l];
      const r = [d[1], -d[0]];
      const g = new THREE.Group();
      g.name = `signal:${a.lane}`;
      const [bx, bz] = coords.xz(qx - d[0] * 1.4 + r[0] * (laneW / 2 + 1.3), qy - d[1] * 1.4 + r[1] * (laneW / 2 + 1.3));
      const yaw = coords.yaw((Math.atan2(d[0], d[1]) * 180) / Math.PI); // travel direction as a SUMO heading
      g.position.set(bx, 0.16, bz);
      g.rotation.y = yaw; // local +X = direction of travel; local +Z = driver's right
      const H = 6.2;
      const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.17, H), poleMat);
      pole.position.y = H / 2; pole.castShadow = true;
      g.add(pole);
      const armLen = laneW / 2 + 1.3;
      const arm = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.14, armLen), poleMat);
      arm.position.set(0, H - 0.2, -armLen / 2); // toward the driver's left = over the lane
      arm.castShadow = true;
      g.add(arm);
      const head = new THREE.Group();
      head.position.set(0, H - 1.25, -armLen + 0.1);
      head.scale.setScalar(1.45);
      const body = new THREE.Mesh(new THREE.BoxGeometry(0.38, 1.1, 0.46), boxMat);
      body.castShadow = true;
      head.add(body);
      const lamps = {};
      [['red', 0.34], ['yellow', 0], ['green', -0.34]].forEach(([name, y]) => {
        const mat = new THREE.MeshStandardMaterial({ color: 0x222222, emissive: SIGNAL_COLOR[name], emissiveIntensity: 0 });
        const lamp = new THREE.Mesh(new THREE.CircleGeometry(0.14, 20), mat);
        lamp.position.set(-0.2, y, 0); // faces back toward the approaching traffic (-X)
        lamp.rotation.y = -Math.PI / 2;
        head.add(lamp);
        const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, color: SIGNAL_COLOR[name], transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0 }));
        sprite.scale.setScalar(1.7);
        sprite.position.set(-0.3, y, 0);
        head.add(sprite);
        lamps[name] = { mat, sprite };
      });
      // visor plates make the head read from a distance
      const plate = new THREE.Mesh(new THREE.BoxGeometry(0.05, 1.3, 0.6), boxMat);
      plate.position.x = 0.2;
      head.add(plate);
      g.add(head);
      this.group.add(g);
      this.heads.push({ approach: a, lamps, current: null });
    }
  }

  // state: the 16-character SUMO signal string for the junction
  set(state) {
    for (const h of this.heads) {
      const ch = state[h.approach.straight_link];
      const color = ch === 'G' || ch === 'g' ? 'green' : ch === 'y' || ch === 'Y' ? 'yellow' : ch === 'u' || ch === 'U' ? 'yellow' : 'red';
      if (h.current === color) continue;
      h.current = color;
      for (const [name, l] of Object.entries(h.lamps)) {
        const on = name === color;
        l.mat.emissiveIntensity = on ? 2.6 : 0;
        l.mat.color.setHex(on ? SIGNAL_COLOR[name] : 0x1d1d1d);
        l.sprite.material.opacity = on ? 0.95 : 0;
      }
    }
  }

  colors() { // for tests and the HUD
    return Object.fromEntries(this.heads.map((h) => [h.approach.lane, h.current]));
  }
}
