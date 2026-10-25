// Desktop Monitor part of the Monitor page. It reads only the read-only
// /api/monitor/desktop/* views with the user's JWT: the desktop's device token
// is never in the browser, and nothing here can start, stop or configure a
// desktop. Loaded before monitor.js, whose helpers (api, guard, poll, polling,
// showDenied, setMessage, fmt*, cell) are used at call time.
// All API text goes into the page through textContent or element properties.

const DESK_LAUNCH_URI = "atcs-monitor://open";
const DESK_POLL_WAITING_MS = 1500; // after Open Desktop Monitor, until a desktop reports
const DESK_POLL_LIVE_MS = 2000;
const DESK_POLL_IDLE_MS = 5000;
const DESK_POLL_HIDDEN_MS = 15000;
const DESK_POLL_ERROR_MS = 4000;
const DESK_REQUEST_TIMEOUT_MS = 8000;
// Launching, Windows' prompt and the app's startup normally take a few seconds;
// an idle app sends a heartbeat every 10 s.
const DESK_LAUNCH_HELP_MS = 30000;
const DESK_SESSIONS_ACTIVE_MS = 10000;
const DESK_SESSIONS_IDLE_MS = 60000;
const DESK_DEFAULT_TIMEOUT_S = 15; // DESKTOP_HEARTBEAT_TIMEOUT_S, until the server says otherwise
const DESK_RUNNING_STATES = ["Initializing", "Detecting", "Recording", "Stopping"];
const DESK_VEHICLES = ["car", "motorcycle", "bus", "truck"];
const DESK_CLASS_ROWS = [
  ["car", "Cars"],
  ["bus", "Buses"],
  ["truck", "Trucks"],
  ["motorcycle", "Motorcycles"],
  ["bicycle", "Bicycles"],
  ["person", "Pedestrians"],
];
const DESK_STATE_LABELS = {
  Idle: "Idle",
  Initializing: "Starting",
  Detecting: "Monitoring",
  Recording: "Monitoring and recording",
  Stopping: "Stopping",
  Error: "Error",
};
const DESK_CAMERA_LABELS = { ok: "OK", reconnecting: "Reconnecting", error: "Error", none: "No camera in use" };
const DESK_SOURCE_KINDS = { camera: "Camera", video_file: "Video" };
const DESK_SESSION_ID_RE = /^[0-9a-f]{32}$/;

const deskEls = {
  open: document.getElementById("desk-open"),
  launchMessage: document.getElementById("desk-launch-message"),
  legacyCamera: document.getElementById("desk-legacy-camera"),
  help: document.getElementById("desk-help"),
  connectionBadge: document.getElementById("desk-connection-badge"),
  monitoringBadge: document.getElementById("desk-monitoring-badge"),
  recordingBadge: document.getElementById("desk-recording-badge"),
  deviceField: document.getElementById("desk-device-field"),
  device: document.getElementById("desk-device"),
  statusMessage: document.getElementById("desk-status-message"),
  connection: document.getElementById("desk-connection"),
  monitoring: document.getElementById("desk-monitoring"),
  recording: document.getElementById("desk-recording"),
  lastUpdate: document.getElementById("desk-last-update"),
  deviceName: document.getElementById("desk-device-name"),
  app: document.getElementById("desk-app"),
  source: document.getElementById("desk-source"),
  camera: document.getElementById("desk-camera"),
  inference: document.getElementById("desk-inference"),
  session: document.getElementById("desk-session"),
  timeout: document.getElementById("desk-timeout"),
  statsScope: document.getElementById("desk-stats-scope"),
  classBody: document.getElementById("desk-class-body"),
  filter: document.getElementById("desk-filter"),
  sessionsRefresh: document.getElementById("desk-sessions-refresh"),
  sessionsMessage: document.getElementById("desk-sessions-message"),
  sessionsBody: document.getElementById("desk-sessions-body"),
  tz: document.getElementById("desk-tz"),
  detail: document.getElementById("desk-detail"),
  detailTitle: document.getElementById("desk-detail-title"),
  detailClose: document.getElementById("desk-detail-close"),
  detailMessage: document.getElementById("desk-detail-message"),
  detailBody: document.getElementById("desk-detail-body"),
};

const DESK_TILES = {
  visible_vehicles: document.getElementById("desk-visible-vehicles"),
  unique_vehicles: document.getElementById("desk-unique-vehicles"),
  crossing_vehicles: document.getElementById("desk-crossing-vehicles"),
  visible_pedestrians: document.getElementById("desk-visible-pedestrians"),
  unique_pedestrians: document.getElementById("desk-unique-pedestrians"),
  crossing_pedestrians: document.getElementById("desk-crossing-pedestrians"),
};

const DESK_PERF = {
  inference_fps: [document.getElementById("desk-inference-fps"), (v) => fmtNumber(v, 1)],
  capture_fps: [document.getElementById("desk-capture-fps"), (v) => fmtNumber(v, 1)],
  active_tracks: [document.getElementById("desk-tracks"), (v) => fmtCount(v)],
  latency_ms: [document.getElementById("desk-latency"), (v) => (deskIsNumber(v) ? fmtNumber(v, 1) + " ms" : "–")],
  frames_dropped: [document.getElementById("desk-dropped"), (v) => fmtCount(v)],
};

function deskFreshState() {
  return {
    timeoutS: DESK_DEFAULT_TIMEOUT_S,
    devices: null, // null until the first answer; then the server's device entries
    fetchedAt: null, // performance.now() of the last good status answer
    serverMs: null, // server_time of that answer
    problem: null, // text while the status cannot be read
    selected: null, // device name shown in Connection / Live statistics
    picked: false, // the user chose the device
    launch: null, // { at, serverMs, state: "waiting" | "slow" | "connected", device }
    statusTimer: null,
    statusBusy: false,
    tickTimer: null,
    sessionsTimer: null,
    sessionsSeq: 0,
    sessions: null, // last list shown; kept while a reload fails
    activityKey: null,
    detailId: null,
    detail: null,
    detailSeq: 0,
  };
}

let desk = deskFreshState();

function deskIsNumber(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function deskName(text) {
  // Names are validated as bare names by the server; never show a directory part.
  return String(text).split(/[\\/]/).pop();
}

function deskSourceText(kind, label) {
  const k = DESK_SOURCE_KINDS[kind] || (kind ? String(kind) : "");
  return (k ? k + ": " : "") + (label === null || label === undefined ? "–" : deskName(label));
}

function deskAgo(seconds) {
  if (!deskIsNumber(seconds)) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 1) return "just now";
  if (s < 90) return s + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 172800) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}

// ---------- connection rules ----------
// "Connected" needs a heartbeat that the server received less than
// heartbeat_timeout_s ago (server clock), and a status answer from the server
// that is itself no older than that. A stale answer is never shown as connected.

function deskStale() {
  return desk.fetchedAt === null || performance.now() - desk.fetchedAt > desk.timeoutS * 1000;
}

function deskAge(d) {
  const s = d && d.desktop ? d.desktop.seconds_since_seen : null;
  if (!deskIsNumber(s) || desk.fetchedAt === null) return null;
  return s + (performance.now() - desk.fetchedAt) / 1000;
}

function deskLive(d) {
  if (!d || d.live !== true || deskStale()) return false;
  const age = deskAge(d);
  return age !== null && age <= desk.timeoutS;
}

function deskHasReport(d) {
  return Boolean(d && typeof d.state === "string");
}

function deskHasSession(d) {
  return deskHasReport(d) && typeof d.session_id === "string" && d.session_id !== "";
}

function deskRunning(d) {
  return deskLive(d) && d.running === true && DESK_RUNNING_STATES.includes(d.state);
}

function deskRecording(d) {
  return deskLive(d) && Boolean(d.recording && d.recording.active === true);
}

function deskSelected() {
  if (!Array.isArray(desk.devices)) return null;
  return desk.devices.find((d) => d.desktop.name === desk.selected) || null;
}

// Name of a desktop that reports a camera session right now, for monitor.js.
function desktopCameraInUse() {
  if (!Array.isArray(desk.devices)) return null;
  const d = desk.devices.find((x) => deskRunning(x) && x.source && x.source.kind === "camera");
  return d ? d.desktop.name : null;
}

function deskPickDevice() {
  const list = desk.devices || [];
  if (desk.picked && list.some((d) => d.desktop.name === desk.selected)) return;
  desk.picked = false;
  const seen = (d) => (d.desktop.last_seen_at ? Date.parse(d.desktop.last_seen_at) || 0 : 0);
  const ranked = [...list].sort((a, b) =>
    Number(deskRunning(b)) - Number(deskRunning(a)) ||
    Number(deskLive(b)) - Number(deskLive(a)) ||
    seen(b) - seen(a));
  desk.selected = ranked.length ? ranked[0].desktop.name : null;
}

// ---------- polling ----------

function desktopBegin() {
  desktopStop();
  deskEls.tz.textContent = Intl.DateTimeFormat().resolvedOptions().timeZone || "browser time";
  deskRender();
  deskStatusTick();
  guard(deskLoadSessions);
  deskScheduleTick();
}

function desktopStop() {
  clearTimeout(desk.statusTimer);
  clearTimeout(desk.tickTimer);
  clearTimeout(desk.sessionsTimer);
  desk.statusTimer = null;
  desk.tickTimer = null;
  desk.sessionsTimer = null;
  desk.statusBusy = false;
  desk.sessionsSeq++;
  desk.detailSeq++;
}

function desktopReset() {
  desktopStop();
  desk = deskFreshState();
  deskEls.sessionsBody.replaceChildren();
  deskEls.filter.replaceChildren(new Option("All desktops", ""));
  deskEls.device.replaceChildren();
  setMessage(deskEls.launchMessage, "");
  setMessage(deskEls.sessionsMessage, "");
  deskEls.help.hidden = true;
  deskCloseDetail(false);
  deskRender();
}

function desktopVisibility() {
  if (!polling()) return;
  if (document.hidden) {
    deskScheduleStatus(DESK_POLL_HIDDEN_MS);
  } else {
    clearTimeout(desk.statusTimer);
    if (!desk.statusBusy) deskStatusTick();
    deskScheduleTick();
    guard(deskLoadSessions);
  }
}

function deskStatusDelay() {
  if (document.hidden) return DESK_POLL_HIDDEN_MS;
  // Faster only until the troubleshooting help shows; a late desktop is still noticed after that.
  if (desk.launch && desk.launch.state === "waiting") return DESK_POLL_WAITING_MS;
  if (desk.problem) return DESK_POLL_ERROR_MS;
  if ((desk.devices || []).some(deskLive)) return DESK_POLL_LIVE_MS;
  return DESK_POLL_IDLE_MS;
}

function deskScheduleStatus(delay = deskStatusDelay()) {
  clearTimeout(desk.statusTimer);
  desk.statusTimer = polling() ? setTimeout(deskStatusTick, delay) : null;
}

function deskStatusTick() {
  desk.statusTimer = null;
  if (!polling() || desk.statusBusy) return;
  desk.statusBusy = true;
  const gen = poll.gen;
  guard(async () => {
    try {
      const r = await api("/monitor/desktop/status", { timeoutMs: DESK_REQUEST_TIMEOUT_MS });
      if (gen !== poll.gen) return;
      if (r.status === 403) {
        showDenied();
      } else if (r.ok && r.data && Array.isArray(r.data.devices)) {
        deskApplyStatus(r.data);
      } else {
        desk.problem = deskFailureText(r, "desktop status");
        deskRender();
      }
    } finally {
      if (gen === poll.gen) {
        desk.statusBusy = false;
        deskScheduleStatus();
      }
    }
  });
}

// Once a second: "N s ago", heartbeat expiry between answers, launch timeout.
function deskScheduleTick() {
  clearTimeout(desk.tickTimer);
  desk.tickTimer = polling() && !document.hidden ? setTimeout(deskTick, 1000) : null;
}

function deskTick() {
  desk.tickTimer = null;
  if (!polling()) return;
  deskRender();
  deskScheduleTick();
}

function deskFailureText(r, what) {
  if (r.status === 0) return "Could not reach the ATCS server to read the " + what + ". Retrying...";
  if (r.status === 429) return "The ATCS server is busy. Retrying...";
  if (r.status >= 500) return "The ATCS server could not read the " + what + " (" + r.status + "). Retrying...";
  if (r.data && typeof r.data.error === "string") return "Request refused: " + r.data.error;
  return "Could not read the " + what + " (" + r.status + ").";
}

function deskApplyStatus(data) {
  desk.problem = null;
  if (deskIsNumber(data.heartbeat_timeout_s) && data.heartbeat_timeout_s > 0) desk.timeoutS = data.heartbeat_timeout_s;
  desk.fetchedAt = performance.now();
  const server = typeof data.server_time === "string" ? Date.parse(data.server_time) : NaN;
  desk.serverMs = Number.isNaN(server) ? null : server;
  desk.devices = data.devices.filter((d) => d && typeof d === "object" && d.desktop && typeof d.desktop.name === "string");
  deskPickDevice();
  deskCheckLaunch();
  deskFillFilter();

  // A session starting, ending or a desktop coming and going changes the history.
  const key = desk.devices.map((d) => d.desktop.name + ":" + (d.session_id || "") + ":" + deskRunning(d) + ":" + deskLive(d)).join("|");
  if (desk.activityKey !== null && key !== desk.activityKey) guard(deskLoadSessions);
  desk.activityKey = key;

  deskRender();
  if (typeof renderControls === "function") renderControls();
}

// ---------- Open Desktop Monitor ----------

// Server clock now, estimated from the last answer: comparing server receive
// times with it avoids any difference between the browser and server clocks.
function deskServerNow() {
  if (desk.serverMs === null || desk.fetchedAt === null) return null;
  return desk.serverMs + (performance.now() - desk.fetchedAt);
}

function deskOnOpenClick(event) {
  // Only a real click or key press opens the link; script-generated clicks do nothing.
  if (!event.isTrusted) {
    event.preventDefault();
    return;
  }
  // The browser handles the link itself (and may ask first). Whether the app
  // started is only known when it reports to the server.
  const now = deskServerNow();
  desk.launch = { at: performance.now(), serverMs: now === null ? null : now - 1000, state: "waiting", device: null };
  deskEls.help.hidden = true;
  deskRenderLaunch();
  clearTimeout(desk.statusTimer);
  if (!desk.statusBusy) deskStatusTick();
}

function deskCheckLaunch() {
  const launch = desk.launch;
  if (!launch) return;
  if (launch.state === "connected") {
    // The desktop that answered went away again: the connection panel says so.
    const d = (desk.devices || []).find((x) => x.desktop.name === launch.device);
    if (!deskLive(d)) desk.launch = null;
    return;
  }
  const fresh = (desk.devices || []).find((d) => {
    if (!deskLive(d)) return false;
    if (launch.serverMs === null) return true;
    const seen = Date.parse(d.desktop.last_seen_at);
    return !Number.isNaN(seen) && seen >= launch.serverMs;
  });
  if (fresh) {
    launch.state = "connected";
    launch.device = fresh.desktop.name;
    desk.selected = fresh.desktop.name;
    desk.picked = true;
    deskEls.help.hidden = true;
  }
}

function deskRenderLaunch() {
  const launch = desk.launch;
  if (launch && launch.state !== "connected" && performance.now() - launch.at > DESK_LAUNCH_HELP_MS) {
    launch.state = "slow";
    deskEls.help.hidden = false;
  }
  let text = "";
  if (launch && launch.state === "connected") {
    text = "Connected: the desktop '" + launch.device + "' is reporting to ATCS.";
  } else if (launch && launch.state === "slow") {
    text = "Still waiting for a desktop connection. No desktop has reported since you pressed the button; see below.";
  } else if (launch) {
    text = "Waiting for desktop connection... Your browser was asked to open ATCS Monitor; this page only knows the " +
      "app is running once it reports to the ATCS server.";
  }
  // A live region: rewriting the same text every second would be announced again.
  if (deskEls.launchMessage.textContent !== text) {
    setMessage(deskEls.launchMessage, text, Boolean(launch && launch.state === "slow"));
  }
  deskEls.launchMessage.dataset.state = launch ? launch.state : "";
}

// ---------- rendering: connection and live statistics ----------

function deskRender() {
  deskRenderLaunch();
  deskRenderConnection();
  deskRenderStats();
  const legacy = typeof legacyCameraSession === "function" ? legacyCameraSession() : null;
  deskEls.legacyCamera.hidden = !legacy;
  deskEls.legacyCamera.textContent = legacy
    ? "The server-side legacy monitor is using a camera (" + legacy + "). Stop it before starting a camera in the " +
      "desktop app: two programs may not be able to share one camera."
    : "";
}

function deskRenderDeviceSelect() {
  const list = desk.devices || [];
  deskEls.deviceField.hidden = list.length < 2;
  const names = list.map((d) => d.desktop.name);
  const current = [...deskEls.device.options].map((o) => o.value);
  if (names.join("\n") !== current.join("\n")) {
    deskEls.device.replaceChildren(...names.map((n) => new Option(n, n)));
  }
  if (desk.selected !== null) deskEls.device.value = desk.selected;
}

function deskRenderConnection() {
  deskRenderDeviceSelect();
  deskEls.timeout.textContent = String(desk.timeoutS);
  const d = deskSelected();
  const live = deskLive(d);
  const report = deskHasReport(d);
  let badge = "unknown";
  let connection;
  let note = desk.problem || "";
  let noteIsError = Boolean(desk.problem);

  if (desk.devices === null) {
    connection = desk.problem ? "Unknown" : "Checking...";
  } else if (deskStale()) {
    connection = "Unknown: the ATCS server has not answered for " + desk.timeoutS + " s";
    noteIsError = true;
  } else if (desk.devices.length === 0) {
    connection = "No desktop registered";
    badge = "never";
    if (!note) {
      note = "No desktop is registered with this ATCS server yet. An administrator registers one with " +
        "'monitor_access.py device add <name>' and stores its token in the desktop app " +
        "(monitor/docs/desktop_link.md).";
    }
  } else if (!d || d.desktop.connection === "never_connected") {
    connection = "Never connected";
    badge = "never";
  } else if (live) {
    connection = "Connected";
    badge = "connected";
  } else {
    connection = "Disconnected";
    badge = "disconnected";
  }

  const badgeText = { connected: "Connected", disconnected: "Disconnected", never: "Not connected", unknown: desk.devices === null && !desk.problem ? "Checking..." : "Status unknown" };
  deskEls.connectionBadge.dataset.state = badge;
  deskEls.connectionBadge.textContent = badgeText[badge];
  deskEls.connection.textContent = connection;
  deskEls.connection.classList.toggle("error", badge === "disconnected" || (badge === "unknown" && noteIsError));

  const running = deskRunning(d);
  const recording = deskRecording(d);
  deskEls.monitoringBadge.hidden = !running;
  deskEls.monitoringBadge.textContent = running ? DESK_STATE_LABELS[d.state] || "Monitoring" : "";
  deskEls.monitoringBadge.dataset.state = running ? (d.state === "Initializing" || d.state === "Stopping" ? d.state : "Detecting") : "";
  deskEls.recordingBadge.hidden = !recording;

  // Monitoring
  if (!report) {
    deskEls.monitoring.textContent = "–";
  } else if (!live) {
    deskEls.monitoring.textContent = "Not reporting (last reported: " + (DESK_STATE_LABELS[d.state] || String(d.state)) + ")";
  } else {
    const detail = d.state === "Error" ? d.last_error || d.message : d.message;
    deskEls.monitoring.textContent = (DESK_STATE_LABELS[d.state] || String(d.state)) + (detail ? " – " + detail : "");
  }
  deskEls.monitoring.classList.toggle("error", Boolean(live && d.state === "Error"));
  if (live && d.state === "Error" && d.last_error && !desk.problem) {
    note = "The desktop reported an error: " + d.last_error;
    noteIsError = true;
  }

  // Recording
  if (!report) deskEls.recording.textContent = "–";
  else if (!live) deskEls.recording.textContent = "Unknown (desktop not connected)";
  else if (recording) deskEls.recording.textContent = "Recording" + (d.recording.file ? " to " + deskName(d.recording.file) : "");
  else deskEls.recording.textContent = "Not recording";

  // Last update (server receive time, shown in local time)
  if (d && d.desktop.last_seen_at) {
    const age = deskAge(d);
    deskEls.lastUpdate.textContent = fmtTime(d.desktop.last_seen_at) + (age !== null ? " (" + deskAgo(age) + ")" : "");
  } else {
    deskEls.lastUpdate.textContent = d ? "Never" : "–";
  }

  deskEls.deviceName.textContent = d ? d.desktop.name : "–";
  const app = d && d.desktop.app;
  deskEls.app.textContent = app && app.name ? String(app.name) + (app.version ? " " + String(app.version) : "") : "–";
  const suffix = live ? "" : " (last reported)";
  deskEls.source.textContent = report && d.source ? deskSourceText(d.source.kind, d.source.label) + suffix : "–";
  deskEls.camera.textContent = live && d.camera_status ? DESK_CAMERA_LABELS[d.camera_status] || String(d.camera_status) : "–";
  deskEls.inference.textContent = report && d.device ? String(d.device) + suffix : "–";
  deskEls.session.textContent = deskHasSession(d) ? String(d.session_id) + suffix : "–";
  setMessage(deskEls.statusMessage, note, noteIsError);
}

function deskRenderStats() {
  const d = deskSelected();
  const live = deskLive(d);
  const session = deskHasSession(d);
  // Cumulative values of the reported session stay meaningful after the desktop
  // goes away; instantaneous ones (in view, FPS, tracks) only while it reports.
  const cumulative = session;
  const instant = session && deskRunning(d);
  const totals = (d && d.totals) || {};

  let scope;
  if (!deskHasReport(d)) scope = "No data reported yet";
  else if (!session) scope = live ? "Desktop idle: no monitoring session" : "Desktop not connected";
  else if (instant) scope = "Live";
  else if (live) scope = "Session ended: last values";
  else scope = "Last reported values: desktop not connected";
  deskEls.statsScope.textContent = scope;

  for (const [key, el] of Object.entries(DESK_TILES)) {
    const known = key.startsWith("visible") ? instant : cumulative;
    el.textContent = known ? fmtCount(totals[key]) : "–";
  }

  deskEls.classBody.replaceChildren();
  for (const [c, label] of DESK_CLASS_ROWS) {
    const row = document.createElement("tr");
    row.dataset.class = c;
    row.appendChild(cell(label));
    row.appendChild(cell(instant ? fmtCount((d.visible || {})[c]) : "–", "num"));
    row.appendChild(cell(cumulative ? fmtCount((d.unique || {})[c]) : "–", "num"));
    row.appendChild(cell(cumulative ? fmtCount((d.crossings || {})[c]) : "–", "num"));
    deskEls.classBody.appendChild(row);
  }

  const perf = (d && d.performance) || {};
  for (const [key, [el, fmt]] of Object.entries(DESK_PERF)) el.textContent = instant ? fmt(perf[key]) : "–";
  document.getElementById("desk-frames").textContent = cumulative ? fmtCount(perf.frames_processed) : "–";
}

// ---------- sessions ----------

function deskFillFilter() {
  const names = (desk.devices || []).map((d) => d.desktop.name).sort();
  const value = deskEls.filter.value;
  if (value && !names.includes(value)) names.push(value);
  const current = [...deskEls.filter.options].slice(1).map((o) => o.value);
  if (names.join("\n") === current.join("\n")) return;
  deskEls.filter.replaceChildren(new Option("All desktops", ""), ...names.map((n) => new Option(n, n)));
  deskEls.filter.value = value;
}

function deskScheduleSessions() {
  clearTimeout(desk.sessionsTimer);
  if (!polling()) return;
  const active = (desk.sessions || []).some((s) => !s.finalized) || (desk.devices || []).some(deskLive);
  const delay = document.hidden ? DESK_SESSIONS_IDLE_MS : active ? DESK_SESSIONS_ACTIVE_MS : DESK_SESSIONS_IDLE_MS;
  desk.sessionsTimer = setTimeout(() => guard(deskLoadSessions), delay);
}

async function deskLoadSessions() {
  const seq = ++desk.sessionsSeq;
  const gen = poll.gen;
  clearTimeout(desk.sessionsTimer);
  deskEls.sessionsRefresh.disabled = true;
  deskEls.sessionsRefresh.setAttribute("aria-busy", "true");
  const device = deskEls.filter.value;
  try {
    const query = device ? "?device=" + encodeURIComponent(device) : "";
    const r = await api("/monitor/desktop/sessions" + query, { timeoutMs: DESK_REQUEST_TIMEOUT_MS });
    if (gen !== poll.gen || seq !== desk.sessionsSeq) return;
    if (r.status === 403) {
      showDenied();
      return;
    }
    if (!r.ok || !r.data || !Array.isArray(r.data.sessions)) {
      // Keep what is shown: history does not disappear because one request failed.
      const kept = desk.sessions && desk.sessions.length ? " The list below is from the last successful update." : "";
      setMessage(deskEls.sessionsMessage, deskFailureText(r, "desktop sessions") + kept, true);
      return;
    }
    const seen = new Set();
    desk.sessions = r.data.sessions.filter((s) => {
      if (!s || typeof s !== "object" || typeof s.session_id !== "string" || seen.has(s.session_id)) return false;
      seen.add(s.session_id);
      return true;
    });
    deskRenderSessions(device);
    if (desk.detailId && !(desk.detail && desk.detail.finalized)) guard(() => deskLoadDetail(desk.detailId, true));
  } finally {
    if (gen === poll.gen && seq === desk.sessionsSeq) {
      deskEls.sessionsRefresh.disabled = false;
      deskEls.sessionsRefresh.setAttribute("aria-busy", "false");
      deskScheduleSessions();
    }
  }
}

// What is known about a session's counts, from where the backend took them.
function deskCompleteness(s) {
  if (s.status === "running") {
    return s.live === true
      ? { label: "Live, in progress", kind: "live", unique: true, duration: false }
      : { label: "Pending: the desktop stopped reporting; the summary arrives when it reconnects", kind: "pending", unique: true, duration: false };
  }
  switch (s.counts_origin) {
    case "summary":
      return { label: "Complete", kind: "complete", unique: true, duration: true };
    case "last_live_update":
      return { label: "Incomplete: unique counts from the last live update", kind: "partial", unique: true, duration: false };
    case "incomplete":
      return { label: "Incomplete: unique counts were not saved", kind: "incomplete", unique: false, duration: false };
  }
  return { label: "Unknown", kind: "incomplete", unique: false, duration: false };
}

function deskStatusLabel(s) {
  if (s.status === "running") return s.live === true ? "Live" : "Not reporting";
  return { completed: "Completed", interrupted: "Interrupted", error: "Error" }[s.status] || String(s.status || "–");
}

function deskRenderSessions(device) {
  const rows = desk.sessions || [];
  deskEls.sessionsBody.replaceChildren();
  if (!rows.length) {
    setMessage(deskEls.sessionsMessage, device
      ? "No sessions saved for the desktop '" + device + "' yet."
      : "No desktop sessions saved yet. Open the Desktop Monitor, start a camera or video in the app, then stop it: " +
        "the session appears here and stays after the app is closed.");
  } else {
    setMessage(deskEls.sessionsMessage, "");
  }
  for (const s of rows) {
    const info = deskCompleteness(s);
    const totals = s.totals || {};
    const row = document.createElement("tr");
    row.dataset.sessionId = s.session_id;
    const started = cell(fmtTime(s.started_at));
    if (typeof s.started_at === "string") started.title = s.started_at;
    row.appendChild(started);
    row.appendChild(cell(s.device ? String(s.device) : "–"));
    row.appendChild(cell(deskSourceText(s.source_type, s.source_id), "wrap"));
    const status = cell(deskStatusLabel(s), "session-status");
    status.dataset.status = s.status === "running" ? (s.live ? "live" : "pending") : String(s.status || "");
    row.appendChild(status);
    row.appendChild(cell(info.duration ? fmtDuration(s.duration_s) : s.status === "running" ? "In progress" : "–", "num"));
    row.appendChild(cell(info.unique ? fmtCount(totals.unique_vehicles) : "–", "num"));
    row.appendChild(cell(info.unique ? fmtCount(totals.unique_pedestrians) : "–", "num"));
    row.appendChild(cell(fmtCount(totals.crossing_vehicles), "num"));
    row.appendChild(cell(fmtCount(totals.crossing_pedestrians), "num"));
    row.appendChild(cell(s.finalized ? fmtCount(s.recordings) : "–", "num"));
    const stats = cell(info.label, "wrap stats-cell");
    stats.dataset.kind = info.kind;
    row.appendChild(stats);
    const actions = document.createElement("td");
    if (DESK_SESSION_ID_RE.test(s.session_id)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary small-button";
      button.textContent = "Details";
      button.setAttribute("aria-label", "Details of the session started " + fmtTime(s.started_at));
      button.addEventListener("click", () => guard(() => deskOpenDetail(s.session_id)));
      actions.appendChild(button);
    }
    row.appendChild(actions);
    deskEls.sessionsBody.appendChild(row);
  }
}

// ---------- session details ----------

async function deskOpenDetail(id) {
  if (!DESK_SESSION_ID_RE.test(id)) return;
  desk.detailId = id;
  desk.detail = null;
  deskEls.detail.hidden = false;
  deskEls.detailBody.hidden = true;
  setMessage(deskEls.detailMessage, "Loading session details...");
  deskEls.detailTitle.focus();
  await deskLoadDetail(id, false);
}

async function deskLoadDetail(id, quiet) {
  const seq = ++desk.detailSeq;
  const gen = poll.gen;
  const r = await api("/monitor/desktop/sessions/" + id, { timeoutMs: DESK_REQUEST_TIMEOUT_MS });
  if (gen !== poll.gen || seq !== desk.detailSeq || desk.detailId !== id) return;
  if (r.status === 403) {
    showDenied();
    return;
  }
  if (r.ok && r.data && r.data.session_id === id) {
    desk.detail = r.data;
    setMessage(deskEls.detailMessage, "");
    deskRenderDetail(r.data);
  } else if (!quiet || !desk.detail) {
    const text = r.status === 404 ? "This session is not stored on the server any more." : deskFailureText(r, "session details");
    setMessage(deskEls.detailMessage, text, true);
    deskEls.detailBody.hidden = !desk.detail;
  }
}

function deskCloseDetail(restoreFocus = true) {
  desk.detailSeq++;
  const id = desk.detailId;
  desk.detailId = null;
  desk.detail = null;
  deskEls.detail.hidden = true;
  if (!restoreFocus) return;
  // Rows are redrawn on every refresh: return to the current Details button of that session.
  const button = id && DESK_SESSION_ID_RE.test(id)
    ? deskEls.sessionsBody.querySelector('tr[data-session-id="' + id + '"] button')
    : null;
  (button || deskEls.sessionsRefresh).focus();
}

function deskSetDetail(id, text) {
  document.getElementById(id).textContent = text;
}

function deskRenderDetail(s) {
  const info = deskCompleteness(s);
  const totals = s.totals || {};
  deskEls.detailTitle.textContent = "Session details: " + fmtTime(s.started_at);
  deskSetDetail("dd-session", String(s.session_id));
  deskSetDetail("dd-device", s.device ? String(s.device) : "–");
  deskSetDetail("dd-source", deskSourceText(s.source_type, s.source_id));
  deskSetDetail("dd-status", deskStatusLabel(s));
  deskSetDetail("dd-stats", info.label);
  deskSetDetail("dd-started", fmtTime(s.started_at));
  deskSetDetail("dd-ended", s.ended_at ? fmtTime(s.ended_at) : s.status === "running" ? "Not ended" : "–");
  deskSetDetail("dd-duration", info.duration ? fmtDuration(s.duration_s) : "–");
  const frames = info.unique ? fmtCount(s.frames_processed) : "–";
  deskSetDetail("dd-frames", frames + (info.kind === "partial" && frames !== "–" ? " (last live update)" : ""));
  deskSetDetail("dd-fps", deskIsNumber(s.avg_inference_fps) ? fmtNumber(s.avg_inference_fps, 1) : "–");
  const model = s.model && typeof s.model === "object" ? s.model : null;
  const modelEl = document.getElementById("dd-model");
  modelEl.textContent = model && model.name ? deskName(model.name) + (model.device ? " on " + String(model.device) : "") : "–";
  modelEl.title = model && model.version ? String(model.version) : "";
  const prov = s.provenance && typeof s.provenance === "object" ? s.provenance : {};
  deskSetDetail("dd-app", prov.app_version ? String(prov.app_version) : "–");
  deskSetDetail("dd-events", fmtCount(s.crossing_event_count));
  deskSetDetail("dd-received", fmtTime(s.last_received_at));

  const classBody = document.getElementById("dd-class-body");
  classBody.replaceChildren();
  for (const [c, label] of DESK_CLASS_ROWS) {
    const row = document.createElement("tr");
    row.dataset.class = c;
    row.appendChild(cell(label));
    row.appendChild(cell(info.unique ? fmtCount((s.unique || {})[c]) : "–", "num"));
    row.appendChild(cell(fmtCount((s.crossings || {})[c]), "num"));
    classBody.appendChild(row);
  }
  const totalRow = document.createElement("tr");
  totalRow.className = "total-row";
  totalRow.appendChild(cell("All vehicles"));
  totalRow.appendChild(cell(info.unique ? fmtCount(totals.unique_vehicles) : "–", "num"));
  totalRow.appendChild(cell(fmtCount(totals.crossing_vehicles), "num"));
  classBody.appendChild(totalRow);

  const linesBody = document.getElementById("dd-lines-body");
  linesBody.replaceChildren();
  const lines = s.line_totals && typeof s.line_totals === "object" ? Object.entries(s.line_totals) : [];
  for (const [line, dirs] of lines) {
    const row = document.createElement("tr");
    row.appendChild(cell(String(line)));
    for (const dir of ["a_to_b", "b_to_a"]) {
      const counts = dirs && dirs[dir];
      row.appendChild(cell(fmtCount(counts && typeof counts === "object" ? sumClasses(counts, Object.keys(counts)) : null), "num"));
    }
    linesBody.appendChild(row);
  }
  document.getElementById("dd-lines-block").hidden = lines.length === 0;

  const list = document.getElementById("dd-recordings");
  const empty = document.getElementById("dd-recordings-empty");
  list.replaceChildren();
  const recordings = Array.isArray(s.recordings) ? s.recordings.filter((r) => r && typeof r === "object") : [];
  for (const rec of recordings) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "mono";
    name.textContent = rec.file ? deskName(rec.file) : "recording";
    li.appendChild(name);
    const parts = [rec.ok === true ? "saved" : "not saved correctly"];
    if (deskIsNumber(rec.duration_s)) parts.push(fmtDuration(rec.duration_s));
    if (rec.codec) parts.push(String(rec.codec));
    li.appendChild(document.createTextNode(" – " + parts.join(", ")));
    list.appendChild(li);
  }
  empty.hidden = recordings.length > 0;
  empty.textContent = s.finalized ? "No recordings in this session." : "Recordings are listed once the session has ended.";
  deskEls.detailBody.hidden = false;
}

// ---------- events ----------

// The link target is fixed: the launcher accepts this one URI and nothing else.
deskEls.open.setAttribute("href", DESK_LAUNCH_URI);
deskEls.open.addEventListener("click", deskOnOpenClick);
deskEls.device.addEventListener("change", () => {
  desk.selected = deskEls.device.value;
  desk.picked = true;
  deskRender();
});
deskEls.filter.addEventListener("change", () => guard(deskLoadSessions));
deskEls.sessionsRefresh.addEventListener("click", () => guard(deskLoadSessions));
deskEls.detailClose.addEventListener("click", () => deskCloseDetail());
deskEls.detail.addEventListener("keydown", (event) => {
  if (event.key === "Escape") deskCloseDetail();
});
