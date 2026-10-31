// One 3D view = one SUMO instance: scene, camera, orbit controls, labels, and the frame playout.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { buildWorld, SignalRig, SIGNAL_COLOR } from './world.js';
import { VehicleLayer } from './vehicles.js';

const PRESETS = {
  overview: { pos: [46, 44, 62], target: [0, 0, 0] },
  top: { pos: [0, 118, 0.6], target: [0, 0, 0] }, // camera just south of the target, so north is up on screen
  driver: { pos: [2.6, 2.4, 52], target: [1.6, 1.8, 0] }, // behind the southern queue, looking north through the junction
};

const COLOR_CSS = { red: '#ff453a', yellow: '#ffc400', green: '#30d158' };

export class PaneView {
  constructor({ el, net, coords, shadows }) {
    this.el = el;
    this.net = net;
    this.coords = coords;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xb7d4ee);
    this.scene.fog = new THREE.Fog(0xb7d4ee, 320, 900);

    this.scene.add(new THREE.HemisphereLight(0xdff0ff, 0x4b6a4b, 1.15));
    const sun = new THREE.DirectionalLight(0xfff1d6, 2.1);
    sun.position.set(-70, 110, 55);
    if (shadows) {
      sun.castShadow = true;
      sun.shadow.mapSize.set(2048, 2048);
      const c = sun.shadow.camera;
      c.left = -85; c.right = 85; c.top = 85; c.bottom = -85; c.near = 10; c.far = 320;
      sun.shadow.bias = -0.0004;
    }
    this.scene.add(sun);

    this.scene.add(buildWorld(net, coords));
    this.signals = new SignalRig(net, coords);
    this.scene.add(this.signals.group);
    this.vehicles = new VehicleLayer(coords);
    this.scene.add(this.vehicles.group);

    this.camera = new THREE.PerspectiveCamera(42, 1, 1, 2500);
    this.controls = new OrbitControls(this.camera, el);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.495; // never below the ground
    this.controls.minDistance = 8;
    this.controls.maxDistance = 330;
    this.controls.screenSpacePanning = false;
    this.preset('overview', true);

    this.labelsEl = el.querySelector('.labels');
    this._makeLabels();
    this.reset();
  }

  reset() {
    this.prev = null;
    this.cur = null;
    this.arrival = 0;
    this.interval = 200;
    this.vehicles.clear();
    this.signals.set('rrrrrrrrrrrrrrrr');
    this.lastTls = null;
    this.used = null;
  }

  // ---- camera
  preset(name, instant = false) {
    const p = PRESETS[name];
    if (!p) return;
    // Narrow panes (side-by-side) need the camera further back to keep the whole junction in frame.
    const r = this.el.getBoundingClientRect();
    const aspect = r.width > 2 && r.height > 2 ? r.width / r.height : 16 / 9;
    const fit = name === 'overview' ? Math.max(1, Math.sqrt(1.78 / aspect)) : 1;
    const target = new THREE.Vector3(...p.target);
    const to = { pos: target.clone().add(new THREE.Vector3(...p.pos).sub(target).multiplyScalar(fit)), target };
    if (instant) {
      this.camera.position.copy(to.pos);
      this.controls.target.copy(to.target);
      this.controls.update();
      this.tween = null;
    } else {
      this.tween = { t0: performance.now(), dur: 900, fromPos: this.camera.position.clone(), fromTarget: this.controls.target.clone(), to };
    }
  }

  // ---- frames from the server (complete SUMO snapshots)
  pushFrame(f, nowMs, expectedMs) {
    if (this.cur) {
      const gap = nowMs - this.arrival;
      if (gap < 3 * expectedMs) this.interval = this.interval * 0.8 + gap * 0.2; // follow the real arrival rate
      this.prev = this.cur;
    } else {
      this.interval = expectedMs;
    }
    this.cur = f;
    this.arrival = nowMs;
    // SUMO applies a signal switch at the start of the step this frame reports, so the lights change as the
    // interpolation toward this frame begins.
    this.signals.set(f.tls.state);
    this.lastTls = f.tls;
  }

  update(nowMs) {
    if (this.tween) {
      const k = Math.min(1, (nowMs - this.tween.t0) / this.tween.dur);
      const e = k * k * (3 - 2 * k);
      this.camera.position.lerpVectors(this.tween.fromPos, this.tween.to.pos, e);
      this.controls.target.lerpVectors(this.tween.fromTarget, this.tween.to.target, e);
      if (k >= 1) this.tween = null;
    }
    this.controls.update();
    if (this.cur) {
      const u = Math.min(1, Math.max(0, (nowMs - this.arrival) / this.interval));
      this.vehicles.update(this.prev ? this.prev.vehicles : null, this.cur.vehicles, u);
      this.used = { prev: this.prev, cur: this.cur, u }; // exactly what the models on screen were computed from (tests)
    }
  }

  // ---- labels (DOM, so text stays crisp): compass letters and a queue badge per approach
  _makeLabels() {
    this.labels = [];
    const add = (cls, text, worldPos, extra = {}) => {
      const d = document.createElement('div');
      d.className = cls;
      d.textContent = text;
      this.labelsEl.appendChild(d);
      const l = { el: d, pos: new THREE.Vector3(...worldPos), ...extra };
      this.labels.push(l);
      return l;
    };
    const names = { top0A0: 'N', right0A0: 'E', bottom0A0: 'S', left0A0: 'W' };
    this.queueBadges = {};
    for (const a of this.net.approaches) {
      const l = Math.hypot(...a.dir_vec);
      const d = [a.dir_vec[0] / l, a.dir_vec[1] / l];
      const [qx, qy] = a.stop_point;
      const [tx, tz] = this.coords.xz(qx - d[0] * 46, qy - d[1] * 46);
      add('compass', names[a.edge] || a.edge, [tx, 0.2, tz]);
      const [bx, bz] = this.coords.xz(qx - d[0] * 16, qy - d[1] * 16);
      const b = add('qbadge', '0', [bx, 3.2, bz], { lane: a.lane });
      this.queueBadges[a.lane] = b;
    }
  }

  updateLabels(rect) {
    const v = new THREE.Vector3();
    for (const l of this.labels) {
      v.copy(l.pos).project(this.camera);
      const visible = v.z < 1 && Math.abs(v.x) < 1.1 && Math.abs(v.y) < 1.1;
      l.el.style.display = visible ? '' : 'none';
      if (visible) l.el.style.transform = `translate(${((v.x + 1) / 2) * rect.width}px, ${((1 - v.y) / 2) * rect.height}px) translate(-50%, -50%)`;
    }
    if (this.cur) {
      for (const a of this.net.approaches) {
        const b = this.queueBadges[a.lane];
        const lane = this.cur.lanes[a.lane];
        const n = lane ? lane.stopped : 0; // stopped vehicles on this approach (not `halt`, which also counts a car placed this second)
        const ch = this.cur.tls.state[a.straight_link];
        const col = ch === 'G' || ch === 'g' ? 'green' : ch === 'y' || ch === 'Y' ? 'yellow' : 'red';
        const text = n === 0 ? '' : String(n);
        if (b.el.textContent !== text) b.el.textContent = text;
        b.el.style.display = n === 0 ? 'none' : (b.el.style.display === 'none' ? '' : b.el.style.display);
        b.el.style.borderColor = COLOR_CSS[col];
        b.el.classList.toggle('hide', n === 0);
      }
    }
  }

  signalColors() { return this.signals.colors(); }
}

export { COLOR_CSS, SIGNAL_COLOR };
