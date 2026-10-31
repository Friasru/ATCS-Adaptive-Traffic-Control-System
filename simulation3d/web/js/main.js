// Wires the pieces together: server messages -> views and HUD; render loop; keyboard shortcuts.
// The browser renders what the server sends. It makes no traffic decisions and simulates nothing.
import * as THREE from 'three';
import { NetClient } from './net.js';
import { Coords } from './coords.js';
import { PaneView } from './view.js';
import { Ui } from './ui.js';

const params = new URLSearchParams(location.search);
const shadows = params.get('shadows') !== '0';
const dprCap = Number(params.get('dpr')) || 2;

const canvas = document.getElementById('gl');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, dprCap));
renderer.shadowMap.enabled = shadows;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const net = new NetClient('/ws');
const ui = new Ui((cmd, extra) => net.send(cmd, extra));

let model = null; // network description from the server
let coords = null;
const panes = [];
let runId = -1;
let state = null;
const stats = { frames: 0, fps: 0, lastFpsT: performance.now(), rx: 0, latencies: [], interArrival: [], lastRx: 0 };

function ensurePane(i) {
  if (!panes[i]) panes[i] = new PaneView({ el: document.getElementById(`pane${i}`), net: model, coords, shadows });
  return panes[i];
}

let lastMode = null;
function layout(mode) {
  const views = document.getElementById('views');
  views.className = mode === 'compare' ? 'compare' : 'single';
  document.getElementById('pane1').hidden = mode !== 'compare';
  ensurePane(0);
  if (mode === 'compare') ensurePane(1);
  if (mode !== lastMode) { // pane sizes changed: both sides start from the same, re-fitted overview camera
    lastMode = mode;
    panes.forEach((p) => p && !p.el.hidden && p.preset('overview', true));
  }
}

function resetPanes() { panes.forEach((p) => p && p.reset()); }

net.onconnection = (up) => document.getElementById('conn').classList.toggle('off', !up);

// The server may deliver state/frames before `hello` on a (re)connect; hold the newest of each until the network
// model is known, then replay them, so nothing is lost and nothing is applied to a scene that does not exist yet.
const early = { state: null, frame: null };

net.on('hello', (m) => {
  model = m.net;
  coords = new Coords(model);
  ui.onHello(m);
  if (early.state) onState(early.state);
  if (early.frame) onFrame(early.frame);
  early.state = early.frame = null;
});
net.on('state', (s) => (model ? onState(s) : (early.state = s)));
net.on('frame', (m) => (model ? onFrame(m) : (early.frame = m)));

function onState(s) {
  state = s;
  if (s.run_id !== null && s.run_id !== runId) { runId = s.run_id; resetPanes(); } // a new run was announced
  if (s.run_id === null) { runId = -1; resetPanes(); } // selection changed: clear the scene
  layout(s.mode);
  ui.onState(s);
}

function onFrame(m) {
  if (m.run_id < runId) return; // stale frame from a previous run: ignore
  if (m.run_id > runId) { runId = m.run_id; resetPanes(); }
  const now = performance.now();
  const expected = 1000 / (state ? state.speed : 1);
  m.panes.forEach((pf, i) => panes[i] && panes[i].pushFrame(pf, now, expected));
  ui.onFrame(m);
  stats.rx += 1;
  if (stats.lastRx) stats.interArrival.push(now - stats.lastRx);
  stats.lastRx = now;
  stats.latencies.push(Date.now() - m.srv_ms);
  if (stats.latencies.length > 600) { stats.latencies.shift(); stats.interArrival.shift(); }
  lastFrameMsg = m;
}
net.on('result', (m) => ui.onResult(m));
net.on('benchmarks', (m) => ui.onBenchmarks(m));
net.on('error', (m) => ui.error(m.message));

let lastFrameMsg = null;

// ---------------------------------------------------------------------------------------------- render loop
function loop(now) {
  requestAnimationFrame(loop);
  const W = window.innerWidth, H = window.innerHeight;
  if (canvas.width !== Math.floor(W * renderer.getPixelRatio()) || canvas.height !== Math.floor(H * renderer.getPixelRatio())) {
    renderer.setSize(W, H, false);
  }
  renderer.setScissorTest(true);
  for (const p of panes) {
    if (!p || p.el.hidden) continue;
    const r = p.el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    renderer.setViewport(r.left, H - r.bottom, r.width, r.height);
    renderer.setScissor(r.left, H - r.bottom, r.width, r.height);
    p.camera.aspect = r.width / r.height;
    p.camera.updateProjectionMatrix();
    p.update(now);
    renderer.render(p.scene, p.camera);
    p.updateLabels(r);
  }
  stats.frames += 1;
  if (now - stats.lastFpsT >= 1000) {
    stats.fps = (stats.frames * 1000) / (now - stats.lastFpsT);
    stats.frames = 0;
    stats.lastFpsT = now;
    document.getElementById('fps').textContent = `${stats.fps.toFixed(0)} fps`;
  }
}
requestAnimationFrame(loop);

// ---------------------------------------------------------------------------------------------- keyboard
// A button that keeps focus after a mouse click would also react to Space; the shortcuts own those keys.
document.addEventListener('click', (e) => { const b = e.target.closest && e.target.closest('button'); if (b) b.blur(); });
document.getElementById('btn-help').addEventListener('click', () => { const h = document.getElementById('help'); h.hidden = !h.hidden; });
const camAll = (name) => panes.forEach((p) => p && !p.el.hidden && p.preset(name));
document.querySelectorAll('#cams button').forEach((b) => b.addEventListener('click', () => camAll(b.dataset.cam)));
window.addEventListener('keydown', (e) => {
  if (e.target instanceof HTMLSelectElement || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  const status = ui.status;
  if (k === ' ') {
    e.preventDefault();
    net.send(status === 'running' ? 'pause' : status === 'paused' ? 'resume' : 'start');
  } else if (k === 'r') net.send('restart');
  else if (k === 'x') net.send('stop');
  else if (k === 'b') ui.toggleCompare();
  else if (k === 'p') ui.toggleResults();
  else if (k === '?' || e.key === '/') document.getElementById('help').hidden = !document.getElementById('help').hidden;
  else if (k === 'o') camAll('overview');
  else if (k === 't') camAll('top');
  else if (k === 'd') camAll('driver');
  else if (k === 'h') document.body.classList.toggle('clean');
  else if (k === 'f') document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  else if (['1', '2', '3'].includes(k) && ui.hello) {
    const key = ui.hello.controllers[Number(k) - 1].key;
    document.getElementById('sel-ctrl').value = key;
    ui.select();
  }
});

// ---------------------------------------------------------------------------------------------- test hooks
// Read-only view of what is on screen, used by tests/test_web.py to compare the rendering with the SUMO frames.
window.__atcs = {
  get fps() { return stats.fps; },
  get status() { return ui.status; },
  get runId() { return runId; },
  get lastFrame() { return lastFrameMsg; },
  rxStats() {
    const mean = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);
    return { frames: stats.rx, meanInterArrivalMs: mean(stats.interArrival), meanServerToClientMs: mean(stats.latencies) };
  },
  net: () => model,
  pane(i) {
    const p = panes[i];
    if (!p) return null;
    return {
      vehicleIds: p.vehicles.ids(),
      vehiclePositions: p.vehicles.debugPositions(),
      signalColors: p.signalColors(),
      u: p.cur ? Math.min(1, Math.max(0, (performance.now() - p.arrival) / p.interval)) : null,
      used: p.used ? { u: p.used.u, prevT: p.used.prev ? p.used.prev.t : null, curT: p.used.cur.t, prev: p.used.prev ? p.used.prev.vehicles : [], cur: p.used.cur.vehicles } : null,
      curT: p.cur ? p.cur.t : null,
      prevT: p.prev ? p.prev.t : null,
      cur: p.cur ? p.cur.vehicles : [],
      prev: p.prev ? p.prev.vehicles : [],
      title: p.el.querySelector('.title').textContent,
      camera: { pos: p.camera.position.toArray(), target: p.controls.target.toArray() },
    };
  },
  coords: () => coords,
  renderer: () => renderer.info.render,
};
