// Vehicles. One 3D model per SUMO vehicle id, keyed by that id (so there can never be two for one id).
//
// This module does NOT simulate anything. It is given two consecutive SUMO snapshots (`from` and `to`, one simulated
// second apart) and draws the vehicles at a fraction `u` between them, so the 1 Hz simulation looks smooth.
// At u = 1 every vehicle is exactly where SUMO reported it. The models are cosmetic: footprint = SUMO length x width.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { shortestAngleDelta } from './coords.js';

const PALETTE = [0xd94b4b, 0x2f6fd0, 0xf1f1f3, 0x2b3a67, 0xf0b429, 0x3aa17e, 0xc9ced6, 0x8a5bd8, 0xe8802f, 0x2aa6c9, 0xb0262e, 0x59636f];
const BODY_STYLES = ['sedan', 'hatch', 'suv', 'van', 'pickup'];

function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  return h >>> 0;
}

// Each car is two meshes: one merged, vertex-coloured body (shell, glass, wheels, headlights baked in) and one small
// brake-light mesh whose material is swapped. Geometry is cached per (style, colour), so 50 cars cost ~100 draw calls.
const tailOff = new THREE.MeshStandardMaterial({ color: 0x5a0b0b, emissive: 0x330000, emissiveIntensity: 0.4 });
const tailOn = new THREE.MeshStandardMaterial({ color: 0xff2a2a, emissive: 0xff1010, emissiveIntensity: 2.4 });
const carMat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.5, metalness: 0.05 });
const GLASS = 0x1b2733, TYRE = 0x141414, LAMP = 0xfff3c4, BED = 0x23262b;
const geoCache = new Map();
const boxCache = new Map();

function rbox(w, h, d, r = 0.12) {
  const key = `${w}|${h}|${d}|${r}`;
  if (!boxCache.has(key)) boxCache.set(key, new RoundedBoxGeometry(w, h, d, 2, r));
  return boxCache.get(key);
}

function part(geo, color, x, y, z = 0) {
  const g = geo.index ? geo.toNonIndexed() : geo.clone();
  for (const name of Object.keys(g.attributes)) if (name !== 'position' && name !== 'normal') g.deleteAttribute(name);
  g.translate(x, y, z);
  const c = new THREE.Color(color), n = g.attributes.position.count;
  const arr = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) { arr[i * 3] = c.r; arr[i * 3 + 1] = c.g; arr[i * 3 + 2] = c.b; }
  g.setAttribute('color', new THREE.BufferAttribute(arr, 3));
  return g;
}

const wheelGeo = new THREE.CylinderGeometry(0.34, 0.34, 0.24, 14).rotateX(Math.PI / 2);

// Model forward = +X, left = -Z, right = +Z. Footprint ~5.0 x 1.8 (scaled to the SUMO values by the caller).
function carGeometries(style, color) {
  const key = `${style}|${color}`;
  if (geoCache.has(key)) return geoCache.get(key);
  const W = 1.8, P = [];
  const add = (geo, c, x, y, z = 0) => P.push(part(geo, c, x, y, z));
  switch (style) {
    case 'hatch':
      add(rbox(4.9, 0.75, W), color, 0, 0.62);
      add(rbox(2.9, 0.62, W - 0.18), GLASS, -0.45, 1.25);
      add(rbox(2.7, 0.08, W - 0.3, 0.03), color, -0.45, 1.58);
      break;
    case 'suv':
      add(rbox(4.9, 0.95, W), color, 0, 0.78);
      add(rbox(3.3, 0.75, W - 0.16), GLASS, -0.35, 1.5);
      add(rbox(3.2, 0.08, W - 0.26, 0.03), color, -0.35, 1.92);
      break;
    case 'van':
      add(rbox(4.9, 1.0, W), color, 0, 0.8);
      add(rbox(3.9, 1.05, W - 0.06), color, -0.5, 1.8);
      add(rbox(1.0, 0.7, W - 0.12), GLASS, 1.45, 1.75);
      break;
    case 'pickup':
      add(rbox(4.9, 0.85, W), color, 0, 0.7);
      add(rbox(1.9, 0.7, W - 0.14), GLASS, 0.55, 1.3);
      add(rbox(1.8, 0.08, W - 0.24, 0.03), color, 0.55, 1.65);
      add(rbox(1.9, 0.12, W - 0.2), BED, -1.45, 1.15);
      break;
    default: // sedan
      add(rbox(4.9, 0.72, W), color, 0, 0.6);
      add(rbox(2.5, 0.58, W - 0.2), GLASS, -0.3, 1.2);
      add(rbox(2.2, 0.08, W - 0.34, 0.03), color, -0.3, 1.52);
  }
  for (const x of [-1.55, 1.55]) for (const z of [-0.85, 0.85]) add(wheelGeo, TYRE, x, 0.34, z);
  for (const z of [-0.62, 0.62]) add(rbox(0.1, 0.18, 0.36, 0.03), LAMP, 2.46, 0.68, z);
  const body = mergeGeometries(P, false);
  const tails = mergeGeometries([-0.62, 0.62].map((z) => part(rbox(0.1, 0.18, 0.4, 0.03), 0xffffff, -2.46, 0.7, z)), false);
  tails.deleteAttribute('color');
  const out = { body, tails };
  geoCache.set(key, out);
  return out;
}

function buildCar(style, color) {
  const { body, tails } = carGeometries(style, color);
  const g = new THREE.Group();
  const b = new THREE.Mesh(body, carMat);
  b.castShadow = true;
  const t = new THREE.Mesh(tails, tailOff);
  g.add(b, t);
  g.userData.tail = t;
  return g;
}

export class VehicleLayer {
  constructor(coords) {
    this.coords = coords;
    this.group = new THREE.Group();
    this.group.name = 'vehicles';
    this.byId = new Map(); // SUMO vehicle id -> { obj, braking }
  }

  count() { return this.byId.size; }
  ids() { return [...this.byId.keys()].sort(); }

  _ensure(v) {
    let e = this.byId.get(v.id);
    if (!e) {
      const h = hash(v.id);
      const obj = buildCar(BODY_STYLES[h % BODY_STYLES.length], PALETTE[(h >>> 8) % PALETTE.length]);
      obj.name = `veh:${v.id}`;
      obj.scale.set((v.len || 5) / 5, 1, (v.wid || 1.8) / 1.8);
      this.group.add(obj);
      e = { obj, braking: null };
      this.byId.set(v.id, e);
    }
    return e;
  }

  // from/to: arrays of SUMO vehicles from two consecutive snapshots (from may be null at the very first frame).
  // u in [0, 1]: how far we are from `from` to `to`.
  update(from, to, u) {
    const fromMap = new Map((from || []).map((v) => [v.id, v]));
    const toMap = new Map(to.map((v) => [v.id, v]));
    const live = new Set();

    for (const [id, b] of toMap) {
      live.add(id);
      const a = fromMap.get(id);
      let x, y, ang;
      if (a) {
        x = a.x + (b.x - a.x) * u;
        y = a.y + (b.y - a.y) * u;
        ang = a.a + shortestAngleDelta(a.a, b.a) * u;
      } else { // inserted during this step: back-extrapolate along its heading, never drawn behind where SUMO put it
        const [dx, dz] = this.coords.dir(b.a);
        const back = b.v * (1 - u);
        x = b.x - dx * back;
        y = b.y + dz * back; // SUMO y = -world z
        ang = b.a;
      }
      this._place(this._ensure(b), b, x, y, ang, b.v < 0.3 || (a && b.v < a.v - 0.8));
    }
    for (const [id, a] of fromMap) { // left the network during this step: carry on along the heading, then drop
      if (toMap.has(id) || u >= 1) continue;
      live.add(id);
      const [dx, dz] = this.coords.dir(a.a);
      const fwd = a.v * u;
      this._place(this._ensure(a), a, a.x + dx * fwd, a.y - dz * fwd, a.a, false);
    }
    for (const [id, e] of this.byId) {
      if (!live.has(id)) {
        this.group.remove(e.obj);
        this.byId.delete(id);
      }
    }
  }

  // x, y: SUMO front-bumper position. The model's centre is half a vehicle length behind it along the heading.
  _place(e, v, x, y, angDeg, braking) {
    const half = (v.len || 5) / 2;
    const [dx, dz] = this.coords.dir(angDeg);
    const [wx, wz] = this.coords.xz(x, y);
    e.obj.position.set(wx - dx * half, 0.02, wz - dz * half);
    e.obj.rotation.y = this.coords.yaw(angDeg);
    if (e.braking !== braking) {
      e.braking = braking;
      e.obj.userData.tail.material = braking ? tailOn : tailOff;
    }
  }

  clear() {
    for (const e of this.byId.values()) this.group.remove(e.obj);
    this.byId.clear();
  }

  // for tests: world-space centre of each model
  debugPositions() {
    const out = {};
    for (const [id, e] of this.byId) out[id] = [e.obj.position.x, e.obj.position.z, e.obj.rotation.y];
    return out;
  }
}
