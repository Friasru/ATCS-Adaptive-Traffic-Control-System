// Local stand-in for the Flask backend used by the frontend tests. It serves
// the real files in frontend/ and imitates the /api routes the pages call, with
// the same status codes and response shapes as backend/app/api/*.py and the
// monitor service (see monitor/docs/web_service.md). Tests change `mock`
// directly to script states and failures.
//
// Desktop telemetry views (/api/monitor/desktop/*) are served from
// fixtures/desktop-payloads.json, which is real backend output
// (backend/tests/desktop_dashboard_fixture.py; its shape is checked by
// backend/tests/test_desktop_dashboard_contract.py). Like the backend, the
// mock derives live / connection / running / recording.active from the time
// the server "received" each device's last heartbeat.

import http from "node:http";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const CLASSES = ["person", "car", "motorcycle", "bicycle", "bus", "truck"];
const VEHICLES = ["car", "motorcycle", "bus", "truck"];
// 1x1 JPEG.
export const JPEG = Buffer.from(
  "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=",
  "base64",
);

export const DESKTOP = JSON.parse(readFileSync(new URL("./fixtures/desktop-payloads.json", import.meta.url), "utf8"));
const DESK_RUNNING = ["Initializing", "Detecting", "Recording", "Stopping"];
const DEVICE_NAME_RE = /^[A-Za-z0-9_.-]{3,64}$/;
const clone = (x) => structuredClone(x);
// The backend's iso(): UTC with an explicit offset.
export const serverIso = (ms) => new Date(ms).toISOString().replace("Z", "+00:00");

// A registered desktop. template: "recording" | "idle" | "never" (fixture
// device_*); ageS: seconds since its last heartbeat reached the server.
export function deskDevice(template, { name, ageS = 0, ...overrides } = {}) {
  const doc = clone(DESKTOP["device_" + template]);
  if (name) doc.desktop.name = name;
  Object.assign(doc, overrides);
  return { doc, receivedAt: template === "never" ? null : Date.now() - ageS * 1000 };
}

// The desktop sends a heartbeat now.
export function heartbeat(device) {
  device.receivedAt = Date.now();
}

// A stored session from the fixture (session_*), optionally changed.
export function deskSession(template, overrides = {}) {
  return { ...clone(DESKTOP["session_" + template]), ...overrides };
}

export function deskDetail(template, overrides = {}) {
  return { ...clone(DESKTOP["detail_" + template]), ...overrides };
}

function deskStatusView(dev, now) {
  const d = clone(dev.doc);
  const age = dev.receivedAt === null ? null : (now - dev.receivedAt) / 1000;
  const live = age !== null && age <= mock.desktop.timeoutS;
  d.desktop.connection = dev.receivedAt === null ? "never_connected" : live ? "connected" : "disconnected";
  d.desktop.last_seen_at = dev.receivedAt === null ? null : serverIso(dev.receivedAt);
  d.desktop.seconds_since_seen = age === null ? null : Math.round(age * 10) / 10;
  d.live = live;
  d.running = live && DESK_RUNNING.includes(d.state);
  if (d.recording) d.recording.active = live && Boolean(dev.doc.recording && dev.doc.recording.active);
  return d;
}

function desktopApi(res, p, url) {
  if (p === "/monitor/desktop/status") {
    const now = Date.now();
    return send(res, 200, {
      heartbeat_timeout_s: mock.desktop.timeoutS,
      server_time: serverIso(now),
      devices: mock.desktop.devices.map((d) => deskStatusView(d, now)),
    });
  }
  if (p === "/monitor/desktop/sessions") {
    const unknown = [...url.searchParams.keys()].filter((k) => !["limit", "device"].includes(k));
    if (unknown.length) return err(res, 400, "invalid_query", "unknown parameter(s): " + unknown.sort().join(", "));
    const limit = url.searchParams.get("limit") ?? "50";
    if (!/^\d+$/.test(limit) || +limit < 1 || +limit > 200) return err(res, 400, "invalid_query", "limit must be 1 to 200");
    const device = url.searchParams.get("device");
    if (device !== null && !DEVICE_NAME_RE.test(device)) return err(res, 400, "invalid_query", "invalid device name");
    const rows = mock.desktop.sessions.filter((s) => device === null || s.device === device).slice(0, +limit);
    return send(res, 200, { sessions: clone(rows) });
  }
  const m = p.match(/^\/monitor\/desktop\/sessions\/([^/]*)$/);
  if (m) {
    if (!/^[0-9a-f]{32}$/.test(m[1])) return err(res, 400, "invalid_session_id", "session id must be 32 lowercase hex characters");
    const detail = mock.desktop.details[m[1]];
    return detail ? send(res, 200, clone(detail)) : err(res, 404, "not_found", "session not found");
  }
  return send(res, 404, { error: "not found" });
}

function b64url(obj) {
  return Buffer.from(JSON.stringify(obj)).toString("base64url");
}

export function makeToken(sub, expSeconds) {
  return b64url({ alg: "HS256", typ: "JWT" }) + "." + b64url({ sub, exp: expSeconds }) + ".sig" + sub;
}

function zero() {
  return Object.fromEntries(CLASSES.map((c) => [c, 0]));
}

export function freshMock() {
  return {
    users: { op: { id: "1", password: "pw", role: "operator" }, vw: { id: "2", password: "pw", role: "viewer" }, ng: { id: "3", password: "pw", role: null } },
    revoked: new Set(), // tokens the server now answers with 401
    requests: [], // { method, path, auth, body, time }
    tokenLifetimeS: 3600,
    // Engine imitation.
    engine: {
      state: "Idle",
      running: false,
      message: "",
      source: null,
      session_id: null,
      recording: false,
      last_error: null,
      model_available: true,
      init_ms: 300,
      rec_ms: 200,
      counts: { visible: zero(), unique: zero(), crossings: zero() },
      line_totals: {},
      frameIndex: 0,
    },
    sources: [
      { id: "camera:0", kind: "camera", label: "Camera 0" },
      { id: "video:car-detection.mp4", kind: "video_file", label: "car-detection.mp4" },
    ],
    sessions: [],
    serviceConfigured: true, // false: the legacy endpoints answer 503 service_not_configured
    desktop: {
      timeoutS: DESKTOP.heartbeat_timeout_s,
      devices: [], // deskDevice(...) entries
      sessions: [], // list rows, newest first
      details: {}, // session_id -> detail document
    },
    // Scripted failures: endpoint -> array of {status, body} answered in order before normal behaviour.
    fail: {},
    frameMissing: 0, // answer this many frame requests with 404 no_frame first
    timers: new Set(),
  };
}

export let mock = freshMock();
export function resetMock() {
  for (const t of mock.timers) clearTimeout(t);
  mock = freshMock();
  return mock;
}

function later(ms, fn) {
  const t = setTimeout(() => {
    mock.timers.delete(t);
    fn();
  }, ms);
  mock.timers.add(t);
}

function sum(d, classes) {
  return classes.reduce((s, c) => s + (d[c] || 0), 0);
}

export function statusDoc() {
  const e = mock.engine;
  const { visible, unique, crossings } = e.counts;
  return {
    running: e.running,
    state: e.state,
    message: e.message,
    source: e.source,
    session_id: e.session_id,
    recording: { active: e.state === "Recording", file: e.state === "Recording" ? "rec_" + e.session_id + ".mp4" : null },
    classes: CLASSES,
    vehicle_classes: VEHICLES,
    visible,
    unique,
    crossings,
    line_totals: e.line_totals,
    totals: {
      visible_vehicles: sum(visible, VEHICLES),
      unique_vehicles: sum(unique, VEHICLES),
      crossing_vehicles: sum(crossings, VEHICLES),
      visible_pedestrians: visible.person,
      unique_pedestrians: unique.person,
      crossing_pedestrians: crossings.person,
    },
    performance: {
      capture_fps: e.running ? 15 : 0,
      inference_fps: e.running ? 12.34 : 0,
      recording_fps: 0,
      latency_ms: e.running ? 41.2 : 0,
      frames_processed: e.frameIndex,
      frames_dropped: 0,
      recording_frames_dropped: 0,
      active_tracks: e.running ? 3 : 0,
    },
    device: e.running ? "cpu" : null,
    model: { name: "yolo11n.pt", available: e.model_available },
    last_error: e.last_error,
    last_session: null,
  };
}

function send(res, status, body, headers = {}) {
  const isBuf = Buffer.isBuffer(body);
  res.writeHead(status, { "Content-Type": isBuf ? "image/jpeg" : "application/json", ...headers });
  res.end(isBuf ? body : JSON.stringify(body));
}

function err(res, status, code, message) {
  send(res, status, { error: message, code });
}

function identity(req) {
  const h = req.headers.authorization || "";
  if (!h.startsWith("Bearer ")) return null;
  const token = h.slice(7);
  if (mock.revoked.has(token)) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString());
    if (payload.exp * 1000 < Date.now()) return null;
    return Object.values(mock.users).find((u) => u.id === payload.sub) || null;
  } catch {
    return null;
  }
}

function startEngine(source, loop) {
  const e = mock.engine;
  e.running = true;
  e.state = "Initializing";
  e.message = "Opening " + source.id;
  e.source = source;
  e.session_id = null;
  e.last_error = null;
  e.loop = loop;
  later(e.init_ms, () => {
    if (e.state !== "Initializing") return;
    e.state = "Detecting";
    e.session_id = "sess-" + (mock.sessions.length + 1) + "-abcdef123456";
    e.message = "Detecting on " + source.id + " (cpu, 640 px)";
    mock.sessions.unshift({
      session_id: e.session_id, source_type: source.kind, source_id: source.label, status: "running",
      started_at: new Date().toISOString(), ended_at: null, error: null, model_name: "yolo11n.pt", device: "cpu",
      duration_s: null, frames_processed: null, unique: null, crossings: null, recordings: 0,
    });
  });
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks).toString();
}

async function api(req, res, url, body) {
  const p = url.pathname.slice(4); // strip /api
  const m = req.method;

  if (p === "/controllers" && m === "GET") return send(res, 200, { controllers: ["fixed", "actuated"] });
  if (p === "/scenarios" && m === "GET") return send(res, 200, []);
  if (p === "/auth/login" && m === "POST") {
    const data = JSON.parse(body || "{}");
    const user = mock.users[data.username];
    if (!data.username || !data.password) return send(res, 400, { error: "username and password are required" });
    if (!user || user.password !== data.password) return send(res, 401, { error: "invalid credentials" });
    const exp = Math.floor(Date.now() / 1000) + mock.tokenLifetimeS;
    return send(res, 200, { access_token: makeToken(user.id, exp), token_type: "Bearer", expires_in: mock.tokenLifetimeS });
  }
  if (!p.startsWith("/monitor/")) return send(res, 404, { error: "not found" });

  // Order of checks as in Flask: JWT (401), role (403), body (400), then the service.
  const user = identity(req);
  if (!user) return send(res, 401, { error: "authentication required" });
  const operatorOnly = m === "POST";
  if (user.role !== "operator" && (operatorOnly || user.role !== "viewer")) {
    return send(res, 403, { error: "not authorized for the monitor" });
  }
  let data = {};
  if (body) {
    try {
      data = JSON.parse(body);
    } catch {
      return err(res, 400, "invalid_body", "request body must be a JSON object");
    }
  }
  if (p === "/monitor/start") {
    if (typeof data.source !== "string" || !/^(camera:\d{1,3}|video:[A-Za-z0-9 _.()\-]{1,128})$/.test(data.source)) {
      return err(res, 400, "invalid_source", "source must be the id of an allowed source");
    }
  } else if (m === "POST" && Object.keys(data).length) {
    return err(res, 400, "invalid_body", "this endpoint takes no parameters");
  }

  const key = m + " " + p;
  const queue = mock.fail[key] || mock.fail["* " + p];
  if (queue && queue.length) {
    const f = queue.shift();
    return send(res, f.status, f.body);
  }

  if (p.startsWith("/monitor/desktop/")) {
    return m === "GET" ? desktopApi(res, p, url) : send(res, 405, { error: "method not allowed" });
  }
  if (!mock.serviceConfigured && p !== "/monitor/access") {
    return err(res, 503, "service_not_configured", "monitor service is not configured");
  }

  const e = mock.engine;
  switch (key) {
    case "GET /monitor/access":
      return send(res, 200, { role: user.role, can_view: true, can_control: user.role === "operator" });
    case "GET /monitor/status":
      return send(res, 200, statusDoc());
    case "GET /monitor/sources":
      return send(res, 200, { sources: mock.sources });
    case "GET /monitor/sessions":
      return send(res, 200, { sessions: mock.sessions });
    case "GET /monitor/frame.jpg":
      if (!e.running || !["Detecting", "Recording"].includes(e.state) || mock.frameMissing > 0) {
        if (mock.frameMissing > 0) mock.frameMissing--;
        return err(res, 404, "no_frame", "no frame is available yet");
      }
      e.frameIndex++;
      return send(res, 200, JPEG, { "Cache-Control": "no-store", "X-Frame-Index": String(e.frameIndex) });
    case "POST /monitor/start": {
      const source = mock.sources.find((s) => s.id === data.source);
      if (!source) return err(res, 400, "unknown_source", "source is not in the allowed source list");
      if (e.running) return err(res, 409, "already_running", "monitoring is already running; stop it first");
      if (!e.model_available) return err(res, 503, "model_unavailable", "model weights 'yolo11n.pt' are not installed");
      startEngine(source, Boolean(data.loop));
      return send(res, 202, statusDoc());
    }
    case "POST /monitor/stop":
      if (!e.running) return err(res, 409, "not_running", "monitoring is not running");
      e.state = "Idle";
      e.running = false;
      e.message = "Stopped";
      if (mock.sessions[0] && mock.sessions[0].session_id === e.session_id) {
        Object.assign(mock.sessions[0], { status: "completed", ended_at: new Date().toISOString(), duration_s: 12.4, frames_processed: e.frameIndex, unique: e.counts.unique, crossings: e.counts.crossings });
      }
      return send(res, 200, statusDoc());
    case "POST /monitor/recording/start":
      if (!e.running || ["Idle", "Stopping", "Error"].includes(e.state)) return err(res, 409, "not_running", "monitoring is not running");
      if (e.state === "Initializing") return err(res, 409, "not_ready", "monitoring is still starting; try again shortly");
      if (e.state === "Recording") return err(res, 409, "already_recording", "recording is already active");
      later(e.rec_ms, () => {
        if (e.state === "Detecting") e.state = "Recording";
      });
      return send(res, 202, { ...statusDoc(), recording_requested: true });
    case "POST /monitor/recording/stop":
      if (e.state !== "Recording") return err(res, 409, "not_recording", "recording is not active");
      later(e.rec_ms, () => {
        if (e.state === "Recording") {
          e.state = "Detecting";
          e.message = "Recording saved";
          if (mock.sessions[0]) mock.sessions[0].recordings++;
        }
      });
      return send(res, 202, { ...statusDoc(), recording_stop_requested: true });
  }
  return send(res, 405, { error: "method not allowed" });
}

export function startServer() {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const body = await readBody(req);
    if (url.pathname.startsWith("/api/")) {
      mock.requests.push({ method: req.method, path: url.pathname, query: url.search, auth: req.headers.authorization || null, body, time: Date.now() });
      return api(req, res, url, body);
    }
    const file = path.normalize(path.join(ROOT, url.pathname === "/" ? "index.html" : url.pathname));
    if (!file.startsWith(ROOT + path.sep)) return send(res, 404, { error: "not found" });
    try {
      const content = await readFile(file);
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
      res.end(content);
    } catch {
      send(res, 404, { error: "not found" });
    }
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, base: "http://127.0.0.1:" + server.address().port }));
  });
}
