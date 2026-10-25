// Monitor page: sign-in and access shared by both parts, plus the server-side
// (Phase 4, legacy) monitor. The desktop part is in monitor-desktop.js. Talks
// only to the Flask /api/monitor/* endpoints with the user's JWT; the monitor
// service and its secret are never reachable from here. All API text goes into
// the page through textContent or element properties, never innerHTML.
//
// The legacy panel is collapsed by default: one status request on load tells
// whether the service is configured, and it is polled (status and frames) only
// while the panel is open.

// ~9 frames per second at most; a new frame request starts only after the
// previous one has finished.
const FRAME_INTERVAL_MS = 110;
const FRAME_RETRY_MS = 500; // 404 no_frame: the pipeline has not produced one yet
const FRAME_ERROR_MS = 2000; // service unreachable or failing
const STATUS_PENDING_MS = 500;
const STATUS_RUNNING_MS = 1000;
const STATUS_IDLE_MS = 3000;
const STATUS_HIDDEN_MS = 10000;
const FRAME_STATES = ["Detecting", "Recording"];
const DEFAULT_VEHICLE_CLASSES = ["car", "motorcycle", "bus", "truck"];
const STATE_LABELS = {
  Idle: "Idle",
  Initializing: "Initializing",
  Detecting: "Detecting",
  Recording: "Recording",
  Stopping: "Stopping",
  Error: "Error",
};

const els = {
  loginPanel: document.getElementById("login-panel"),
  loginForm: document.getElementById("login-form"),
  username: document.getElementById("username"),
  password: document.getElementById("password"),
  loginButton: document.getElementById("login-button"),
  loginMessage: document.getElementById("login-message"),
  session: document.getElementById("session"),
  sessionUser: document.getElementById("session-user"),
  roleBadge: document.getElementById("role-badge"),
  logoutButton: document.getElementById("logout-button"),
  deniedPanel: document.getElementById("denied-panel"),
  deniedRetry: document.getElementById("denied-retry"),
  monitor: document.getElementById("monitor"),
  stateBadge: document.getElementById("state-badge"),
  feedImage: document.getElementById("feed-image"),
  feedOverlay: document.getElementById("feed-overlay"),
  feedSpinner: document.getElementById("feed-spinner"),
  feedMessage: document.getElementById("feed-message"),
  feedMeta: document.getElementById("feed-meta"),
  recordingBadge: document.getElementById("recording-badge"),
  sourceSelect: document.getElementById("source-select"),
  loopField: document.getElementById("loop-field"),
  loopVideo: document.getElementById("loop-video"),
  startButton: document.getElementById("start-button"),
  stopButton: document.getElementById("stop-button"),
  recStartButton: document.getElementById("rec-start-button"),
  recStopButton: document.getElementById("rec-stop-button"),
  viewerNote: document.getElementById("viewer-note"),
  monitoringStatus: document.getElementById("monitoring-status"),
  recordingStatus: document.getElementById("recording-status"),
  currentSource: document.getElementById("current-source"),
  sessionId: document.getElementById("session-id"),
  controlMessage: document.getElementById("control-message"),
  statsScope: document.getElementById("stats-scope"),
  classBody: document.getElementById("class-body"),
  linesBlock: document.getElementById("lines-block"),
  linesBody: document.getElementById("lines-body"),
  sessionsRefresh: document.getElementById("sessions-refresh"),
  sessionsMessage: document.getElementById("sessions-message"),
  sessionsBody: document.getElementById("sessions-body"),
  legacyPanel: document.getElementById("legacy-panel"),
  legacyLayout: document.getElementById("legacy-layout"),
  legacyControls: document.getElementById("legacy-controls"),
  legacyNote: document.getElementById("legacy-note"),
  legacySummaryState: document.getElementById("legacy-summary-state"),
  legacyConflict: document.getElementById("legacy-conflict"),
};

const TILES = {
  visible_vehicles: document.getElementById("stat-visible-vehicles"),
  unique_vehicles: document.getElementById("stat-unique-vehicles"),
  crossing_vehicles: document.getElementById("stat-crossing-vehicles"),
  visible_pedestrians: document.getElementById("stat-visible-pedestrians"),
  unique_pedestrians: document.getElementById("stat-unique-pedestrians"),
  crossing_pedestrians: document.getElementById("stat-crossing-pedestrians"),
};

const BUTTON_LABELS = new Map([
  [els.startButton, ["Start monitoring", "Starting..."]],
  [els.stopButton, ["Stop monitoring", "Stopping..."]],
  [els.recStartButton, ["Start recording", "Starting recording..."]],
  [els.recStopButton, ["Stop recording", "Saving recording..."]],
]);

// role: "operator", "viewer" or null (not known yet).
const session = { token: null, username: null, expiryTimer: null, role: null };
// gen changes whenever polling is torn down (sign-out, access denied, leaving the
// page), so responses that arrive afterwards are dropped.
const poll = { gen: 0, abort: null, statusTimer: null, frameTimer: null, statusBusy: false, frameBusy: false };
let status = null; // last status document from GET /api/monitor/status
let serviceProblem = null; // text while the service cannot be used, else null
let serviceProblemCode = ""; // its error code (service_not_configured, ...)
let frameUrl = null; // object URL currently shown in the feed image
let frameTrouble = null; // text while frames fail although monitoring runs
let sources = [];
let pending = null; // { kind, button, deadline } while a command is in progress
let roleCheck = null;
let sessionsSeq = 0;
let lastSessionKey = null;

class SessionExpired extends Error {}

async function api(path, { method = "GET", body, auth = true, as = "json", timeoutMs = 0 } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (auth) {
    if (!session.token) throw new SessionExpired();
    headers.Authorization = "Bearer " + session.token;
  }
  let signal = auth && poll.abort ? poll.abort.signal : undefined;
  // A request that hangs would stop its polling loop; give up and retry instead.
  if (signal && timeoutMs > 0 && typeof AbortSignal.any === "function" && typeof AbortSignal.timeout === "function") {
    signal = AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]);
  }
  let response;
  try {
    response = await fetch("/api" + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal,
    });
  } catch (err) {
    return { ok: false, status: 0, data: null, frameIndex: "" };
  }
  if (auth && response.status === 401) throw new SessionExpired();
  let data = null;
  try {
    if (as === "blob" && response.ok) data = await response.blob();
    else data = await response.json();
  } catch (err) {
    data = null;
  }
  return { ok: response.ok, status: response.status, data, frameIndex: response.headers.get("X-Frame-Index") || "" };
}

function errorCode(r) {
  return r.data && typeof r.data.code === "string" ? r.data.code : "";
}

// Text for a monitor request that failed because of the server or the monitor
// service. None of these mean the user is signed out.
function serviceFailureText(r) {
  switch (errorCode(r)) {
    case "service_not_configured":
      return "The monitor service is not configured on the server.";
    case "service_unavailable":
      return "The monitor service is not running.";
    case "service_timeout":
      return "The monitor service did not respond in time.";
    case "service_auth_failed":
      return "The server could not authenticate with the monitor service.";
    case "busy":
      return "The monitor service is busy. Retrying...";
    case "shutting_down":
      return "The monitor service is shutting down.";
    case "model_unavailable":
      return "The YOLO model weights are not installed on the monitor service.";
  }
  if (r.status === 0) return "Could not reach the ATCS server.";
  if (r.status === 504) return "The monitor service did not respond in time.";
  if (r.status === 503) return "The monitor service is unavailable.";
  if (r.status >= 500) return "The monitor service reported an error.";
  return "Request failed (" + r.status + ").";
}

function isServiceFailure(r) {
  return r.status === 0 || r.status >= 500;
}

function setMessage(el, text, isError) {
  el.textContent = text;
  el.classList.toggle("error", Boolean(isError));
}

function fmtNumber(value, digits) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "–";
}

function fmtCount(value) {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "–";
}

function fmtDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "–";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + s : m + ":" + s;
}

function fmtTime(text) {
  if (typeof text !== "string" || !text) return "–";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? text : date.toLocaleString();
}

function sumClasses(counts, classes) {
  if (!counts || typeof counts !== "object") return null;
  return classes.reduce((sum, c) => sum + (Number(counts[c]) || 0), 0);
}

function vehicleClasses() {
  return status && Array.isArray(status.vehicle_classes) ? status.vehicle_classes : DEFAULT_VEHICLE_CLASSES;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

// ---------- session ----------

function showLogin(message) {
  els.monitor.hidden = true;
  els.deniedPanel.hidden = true;
  els.session.hidden = true;
  els.loginPanel.hidden = false;
  setMessage(els.loginMessage, message || "", Boolean(message));
  els.username.focus();
}

function signOut(message) {
  clearTimeout(session.expiryTimer);
  stopPolling();
  session.token = null;
  session.username = null;
  session.role = null;
  clearStoredSession();
  resetView();
  showLogin(message);
}

function expired() {
  signOut("Your session has expired. Sign in again.");
}

async function guard(task) {
  try {
    await task();
  } catch (err) {
    if (err instanceof SessionExpired) expired();
    else throw err;
  }
}

function startSession(username, token, lifetimeMs) {
  session.token = token;
  session.username = username;
  session.role = null;
  saveStoredSession(token, username);
  clearTimeout(session.expiryTimer);
  if (lifetimeMs > 0) session.expiryTimer = setTimeout(expired, Math.min(lifetimeMs, MAX_TIMER_MS));
  els.password.value = "";
  setMessage(els.loginMessage, "");
  els.loginPanel.hidden = true;
  els.sessionUser.textContent = username;
  els.session.hidden = false;
  begin();
}

function restoreSession() {
  const stored = readStoredSession();
  const lifetime = stored ? tokenLifetime(stored.token) : null;
  if (lifetime === null) {
    clearStoredSession();
    showLogin();
  } else if (lifetime <= 0) {
    expired();
  } else {
    startSession(stored.username, stored.token, lifetime);
  }
}

async function login(username, password) {
  const { ok, status: code, data } = await api("/auth/login", {
    method: "POST",
    body: { username, password },
    auth: false,
  });
  if (ok && data && data.access_token) {
    const lifetime = loginLifetime(data.access_token, data.expires_in);
    if (lifetime <= 0) expired();
    else startSession(username, data.access_token, lifetime);
    return;
  }
  if (code === 401) setMessage(els.loginMessage, "Incorrect username or password.", true);
  else if (code === 400) setMessage(els.loginMessage, "Enter a username and password.", true);
  else if (code === 0) setMessage(els.loginMessage, "Could not reach the server.", true);
  else setMessage(els.loginMessage, "Sign-in failed (" + code + ").", true);
}

async function onLogin(event) {
  event.preventDefault();
  const username = els.username.value.trim();
  const password = els.password.value;
  if (!username || !password) {
    setMessage(els.loginMessage, "Enter a username and password.", true);
    return;
  }
  els.loginButton.disabled = true;
  try {
    await login(username, password);
  } finally {
    els.loginButton.disabled = false;
  }
}

// ---------- access ----------

// Accounts with a grant always get the page; only operators may use the controls.
// GET /api/monitor/access reads the account's own grant on the server and never
// contacts the monitor service.
function checkRole() {
  if (session.role || roleCheck) return;
  const gen = poll.gen;
  roleCheck = guard(async () => {
    const r = await api("/monitor/access");
    if (gen !== poll.gen) return;
    if (r.ok && r.data && r.data.can_view === true) setRole(r.data.can_control === true ? "operator" : "viewer");
    else if (r.status === 403) showDenied();
    // Anything else (server unreachable): asked again with the next status.
  }).finally(() => {
    roleCheck = null;
  });
}

function setRole(role) {
  session.role = role;
  els.roleBadge.textContent = role === "operator" ? "Operator" : "Viewer";
  els.roleBadge.hidden = false;
  els.viewerNote.hidden = role === "operator";
  renderControls();
}

function showDenied() {
  stopPolling();
  resetView();
  els.monitor.hidden = true;
  els.loginPanel.hidden = true;
  els.deniedPanel.hidden = false;
}

// ---------- polling ----------

function begin() {
  stopPolling();
  poll.abort = new AbortController();
  els.deniedPanel.hidden = true;
  els.monitor.hidden = false;
  renderAll();
  desktopBegin();
  statusTick(); // legacy service: one request to learn whether it is configured
  checkRole();
  if (legacyOpen()) legacyOpened();
}

// ---------- legacy panel ----------

function legacyOpen() {
  return els.legacyPanel.open;
}

function legacyOpened() {
  if (!polling()) return;
  clearTimeout(poll.statusTimer);
  if (!poll.statusBusy) statusTick();
  guard(loadSources);
  guard(loadSessions);
  syncFramePolling();
}

function legacyClosed() {
  clearTimeout(poll.statusTimer);
  clearTimeout(poll.frameTimer);
  poll.statusTimer = null;
  poll.frameTimer = null;
  clearFrame();
  renderFeed();
}

// Label of the camera the legacy service is using, for monitor-desktop.js.
function legacyCameraSession() {
  const s = status;
  if (serviceProblem || !s || !s.running || !s.source || s.source.kind !== "camera") return null;
  return String(s.source.label || s.source.id);
}

function renderLegacyShell() {
  const notConfigured = serviceProblemCode === "service_not_configured";
  els.legacyLayout.hidden = notConfigured;
  els.legacyControls.hidden = Boolean(serviceProblem);
  els.legacyNote.hidden = !serviceProblem;
  setMessage(els.legacyNote, notConfigured
    ? "The server-side monitor service is not configured on this ATCS server, so it has no controls here. " +
      "Use the Desktop Monitor above."
    : serviceProblem ? serviceProblem + " Its controls are shown again once it answers." : "",
  Boolean(serviceProblem && !notConfigured));
  let summary = "";
  if (notConfigured) summary = "Not configured";
  else if (serviceProblem) summary = "Unavailable";
  else if (status) summary = status.running ? "Running: " + (STATE_LABELS[status.state] || String(status.state)) : "Available, idle";
  els.legacySummaryState.textContent = summary ? "(" + summary + ")" : "";
}

function stopPolling() {
  poll.gen++;
  desktopStop();
  if (poll.abort) poll.abort.abort();
  poll.abort = null;
  clearTimeout(poll.statusTimer);
  clearTimeout(poll.frameTimer);
  poll.statusTimer = null;
  poll.frameTimer = null;
  poll.statusBusy = false;
  poll.frameBusy = false;
  clearFrame();
}

function resetView() {
  status = null;
  serviceProblem = null;
  frameTrouble = null;
  pending = null;
  sources = [];
  lastSessionKey = null;
  els.roleBadge.hidden = true;
  els.viewerNote.hidden = true;
  serviceProblemCode = "";
  els.sessionsBody.replaceChildren();
  setMessage(els.controlMessage, "");
  setMessage(els.sessionsMessage, "");
  desktopReset();
  renderLegacyShell();
}

function polling() {
  return Boolean(session.token && poll.abort);
}

function statusDelay() {
  if (document.hidden) return STATUS_HIDDEN_MS;
  if (pending) return STATUS_PENDING_MS;
  if (status && (status.running || status.state === "Stopping" || status.state === "Initializing")) return STATUS_RUNNING_MS;
  return STATUS_IDLE_MS;
}

function scheduleStatus(delay = statusDelay()) {
  clearTimeout(poll.statusTimer);
  poll.statusTimer = polling() && legacyOpen() ? setTimeout(statusTick, delay) : null;
}

function statusTick() {
  poll.statusTimer = null;
  if (!polling() || poll.statusBusy) return;
  poll.statusBusy = true;
  const gen = poll.gen;
  guard(async () => {
    try {
      const r = await api("/monitor/status");
      if (gen !== poll.gen) return;
      if (r.ok && r.data && typeof r.data === "object") {
        serviceProblem = null;
        serviceProblemCode = "";
        applyStatus(r.data);
      } else if (r.status === 403) {
        showDenied();
      } else {
        serviceProblem = serviceFailureText(r);
        serviceProblemCode = errorCode(r);
        renderAll();
      }
    } finally {
      if (gen === poll.gen) {
        poll.statusBusy = false;
        scheduleStatus();
      }
    }
  });
}

function frameWanted() {
  return polling() && legacyOpen() && !document.hidden && !serviceProblem && status && status.running &&
    FRAME_STATES.includes(status.state);
}

function scheduleFrame(delay) {
  clearTimeout(poll.frameTimer);
  poll.frameTimer = frameWanted() ? setTimeout(frameTick, delay) : null;
}

function syncFramePolling() {
  if (!frameWanted()) {
    clearTimeout(poll.frameTimer);
    poll.frameTimer = null;
    frameTrouble = null;
    if (!(status && status.running && FRAME_STATES.includes(status.state)) || serviceProblem) clearFrame();
    return;
  }
  if (!poll.frameTimer && !poll.frameBusy) scheduleFrame(0);
}

function frameTick() {
  poll.frameTimer = null;
  if (!frameWanted() || poll.frameBusy) return;
  poll.frameBusy = true;
  const gen = poll.gen;
  const started = performance.now();
  let delay = FRAME_INTERVAL_MS;
  guard(async () => {
    try {
      const r = await api("/monitor/frame.jpg", { as: "blob" });
      if (gen !== poll.gen) return;
      // Monitoring stopped (or the tab was hidden) while this frame was on its way.
      if (!frameWanted()) return;
      if (r.ok && r.data instanceof Blob && r.data.size > 0) {
        frameTrouble = null;
        showFrame(r.data, r.frameIndex);
      } else if (r.status === 403) {
        showDenied();
        return;
      } else if (r.status === 404) {
        // No frame yet (or not any more): keep the last one and ask again shortly.
        delay = FRAME_RETRY_MS;
        frameTrouble = null;
      } else {
        delay = FRAME_ERROR_MS;
        frameTrouble = serviceFailureText(r) + " Reconnecting to the feed...";
      }
      renderFeed();
    } finally {
      if (gen === poll.gen) {
        poll.frameBusy = false;
        scheduleFrame(Math.max(0, delay - (performance.now() - started)));
      }
    }
  });
}

function showFrame(blob, index) {
  const previous = frameUrl;
  frameUrl = URL.createObjectURL(blob);
  els.feedImage.src = frameUrl;
  els.feedImage.hidden = false;
  if (previous) URL.revokeObjectURL(previous);
  els.feedMeta.textContent = index ? "Frame " + index : "";
}

function clearFrame() {
  els.feedImage.hidden = true;
  els.feedImage.removeAttribute("src");
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = null;
  els.feedMeta.textContent = "";
}

// ---------- status ----------

function applyStatus(data) {
  status = data;
  resolvePending();
  const key = (data.session_id || "") + "|" + (data.running ? "running" : "stopped");
  // A session that starts or ends changes the recent sessions list.
  if (lastSessionKey !== null && key !== lastSessionKey && legacyOpen()) guard(loadSessions);
  lastSessionKey = key;
  checkRole();
  renderAll();
}

const COMMANDS = {
  start: {
    path: "/monitor/start",
    progress: "Starting monitoring. Loading the model and opening the source can take a while...",
    timeoutMs: 120000,
    done: (s) => FRAME_STATES.includes(s.state) || s.state === "Error" || (!s.running && s.state === "Idle"),
    result: (s) => (FRAME_STATES.includes(s.state) ? ["Monitoring started.", false] : [stoppedText(s), true]),
  },
  stop: {
    path: "/monitor/stop",
    progress: "Stopping monitoring...",
    timeoutMs: 30000,
    done: (s) => !s.running && s.state !== "Stopping",
    result: () => ["Monitoring stopped.", false],
  },
  recStart: {
    path: "/monitor/recording/start",
    progress: "Starting recording with the next processed frame...",
    timeoutMs: 15000,
    done: (s) => s.state === "Recording" || !s.running || s.state === "Error",
    result: (s) => (s.state === "Recording" ? ["Recording started.", false] : ["Recording did not start. " + stoppedText(s), true]),
  },
  recStop: {
    path: "/monitor/recording/stop",
    progress: "Stopping recording and saving the file...",
    timeoutMs: 30000,
    done: (s) => s.state !== "Recording",
    result: () => ["Recording stopped. The file is finalized on the server.", false],
  },
};

function stoppedText(s) {
  return s.last_error || s.message || "Monitoring is not running.";
}

function resolvePending() {
  if (!pending || !status) return;
  const spec = COMMANDS[pending.kind];
  if (spec.done(status)) {
    const [text, isError] = spec.result(status);
    pending = null;
    setMessage(els.controlMessage, text, isError);
  } else if (Date.now() > pending.deadline) {
    pending = null;
    setMessage(els.controlMessage, "This is taking longer than expected. The status shown is the current one.", true);
  }
}

// Messages for refusals from the monitor endpoints; anything else uses the
// API's own message (plain text) or a generic one.
function commandErrorText(r) {
  switch (errorCode(r)) {
    case "invalid_source":
    case "unknown_source":
      return "That source is not available any more. The source list has been refreshed.";
    case "already_running":
      return "Monitoring is already running, possibly started from another browser. The status shows the shared session.";
    case "not_running":
      return "Monitoring is not running.";
    case "not_ready":
      return "Monitoring is still starting. Try again once it is detecting.";
    case "already_recording":
      return "Recording is already active.";
    case "not_recording":
      return "Recording is not active.";
    case "model_unavailable":
      return "The YOLO model weights are not installed on the monitor service, so monitoring cannot start.";
  }
  if (isServiceFailure(r)) return serviceFailureText(r);
  if (r.data && typeof r.data.error === "string") return "Request refused: " + r.data.error;
  return "Request failed (" + r.status + ").";
}

async function command(kind, button) {
  if (pending || session.role !== "operator") return;
  const spec = COMMANDS[kind];
  let body;
  if (kind === "start") {
    const source = els.sourceSelect.value;
    if (!source || desktopCameraConflict()) return;
    body = { source, loop: !els.loopVideo.disabled && els.loopVideo.checked };
  }
  pending = { kind, button, deadline: Date.now() + spec.timeoutMs };
  setMessage(els.controlMessage, spec.progress);
  renderControls();
  const gen = poll.gen;
  let r;
  try {
    r = await api(spec.path, { method: "POST", body });
  } catch (err) {
    pending = null;
    throw err;
  }
  if (gen !== poll.gen) return;
  if (r.ok) {
    // 202 only means the service accepted the command; the status polling
    // below decides when the transition has actually happened.
    if (r.data && typeof r.data.state === "string") applyStatus(r.data);
  } else {
    pending = null;
    if (r.status === 403) {
      setRole("viewer");
      setMessage(els.controlMessage, "Access denied: your account cannot operate the monitor.", true);
    } else {
      setMessage(els.controlMessage, commandErrorText(r), true);
      if (["invalid_source", "unknown_source"].includes(errorCode(r))) guard(loadSources);
    }
    renderControls();
  }
  clearTimeout(poll.statusTimer);
  if (!poll.statusBusy) statusTick();
}

// ---------- sources and sessions ----------

async function loadSources() {
  const gen = poll.gen;
  const r = await api("/monitor/sources");
  if (gen !== poll.gen) return;
  if (r.status === 403) {
    showDenied();
    return;
  }
  if (!r.ok || !r.data || !Array.isArray(r.data.sources)) {
    sources = [];
    renderSources(serviceFailureText(r));
    return;
  }
  sources = r.data.sources.filter((s) => s && typeof s.id === "string" && typeof s.label === "string");
  renderSources();
}

function renderSources(failure) {
  const previous = els.sourceSelect.value;
  els.sourceSelect.replaceChildren();
  if (failure || sources.length === 0) {
    els.sourceSelect.appendChild(new Option(failure ? "Sources unavailable" : "No sources available", ""));
    if (!failure) {
      setMessage(els.controlMessage, "No sources are available: the server has no cameras enabled and no video files.", true);
    }
  } else {
    const groups = [["camera", "Cameras"], ["video_file", "Videos"]];
    for (const [kind, label] of groups) {
      const items = sources.filter((s) => s.kind === kind);
      if (!items.length) continue;
      const group = document.createElement("optgroup");
      group.label = label;
      for (const s of items) group.appendChild(new Option(s.label, s.id));
      els.sourceSelect.appendChild(group);
    }
    for (const s of sources.filter((s) => !groups.some(([kind]) => kind === s.kind))) {
      els.sourceSelect.appendChild(new Option(s.label, s.id));
    }
    if (sources.some((s) => s.id === previous)) els.sourceSelect.value = previous;
  }
  renderControls();
}

async function loadSessions() {
  const seq = ++sessionsSeq;
  const gen = poll.gen;
  els.sessionsRefresh.disabled = true;
  try {
    const r = await api("/monitor/sessions");
    if (gen !== poll.gen || seq !== sessionsSeq) return;
    if (r.status === 403) {
      showDenied();
      return;
    }
    if (!r.ok || !r.data || !Array.isArray(r.data.sessions)) {
      setMessage(els.sessionsMessage, "Could not load sessions. " + serviceFailureText(r), true);
      return;
    }
    renderSessions(r.data.sessions);
  } finally {
    if (seq === sessionsSeq) els.sessionsRefresh.disabled = false;
  }
}

const SOURCE_KINDS = { camera: "Camera", video_file: "Video" };

function renderSessions(rows) {
  els.sessionsBody.replaceChildren();
  setMessage(els.sessionsMessage, rows.length ? "" : "No monitoring sessions yet.");
  const vehicles = vehicleClasses();
  for (const s of rows) {
    if (!s || typeof s !== "object") continue;
    const row = document.createElement("tr");
    row.appendChild(cell(fmtTime(s.started_at)));
    const kind = SOURCE_KINDS[s.source_type] || String(s.source_type || "");
    row.appendChild(cell((kind ? kind + ": " : "") + String(s.source_id ?? "–"), "wrap"));
    const statusCell = cell(String(s.status || "–"));
    statusCell.dataset.status = String(s.status || "");
    statusCell.classList.add("session-status");
    if (s.error) {
      const note = document.createElement("span");
      note.className = "delta";
      note.textContent = String(s.error);
      statusCell.appendChild(note);
      statusCell.classList.add("wrap");
    }
    row.appendChild(statusCell);
    row.appendChild(cell(fmtDuration(s.duration_s), "num"));
    row.appendChild(cell(fmtCount(s.frames_processed), "num"));
    row.appendChild(cell(fmtCount(sumClasses(s.unique, vehicles)), "num"));
    row.appendChild(cell(fmtCount(sumClasses(s.unique, ["person"])), "num"));
    row.appendChild(cell(fmtCount(sumClasses(s.crossings, vehicles)), "num"));
    row.appendChild(cell(fmtCount(s.recordings), "num"));
    const id = String(s.session_id || "");
    const idCell = cell(id.length > 12 ? id.slice(0, 12) + "…" : id, "mono");
    idCell.title = id;
    row.appendChild(idCell);
    els.sessionsBody.appendChild(row);
  }
}

// ---------- rendering ----------

function renderAll() {
  renderLegacyShell();
  renderState();
  renderControls();
  renderStats();
  syncFramePolling();
  renderFeed();
}

function renderState() {
  const s = status;
  const state = serviceProblem ? "unavailable" : s ? s.state : "unknown";
  els.stateBadge.dataset.state = state;
  els.stateBadge.textContent = serviceProblem ? "Service unavailable" : s ? STATE_LABELS[s.state] || String(s.state) : "Checking...";
  els.recordingBadge.hidden = !(s && !serviceProblem && s.recording && s.recording.active);

  if (serviceProblem) {
    els.monitoringStatus.textContent = serviceProblem;
  } else if (!s) {
    els.monitoringStatus.textContent = "Checking...";
  } else {
    const detail = s.state === "Error" ? s.last_error || s.message : s.message;
    els.monitoringStatus.textContent = (STATE_LABELS[s.state] || String(s.state)) + (detail ? " – " + detail : "");
  }
  els.monitoringStatus.classList.toggle("error", Boolean(serviceProblem || (s && s.state === "Error")));

  const rec = s && s.recording;
  if (!s || serviceProblem) els.recordingStatus.textContent = "–";
  else if (rec && rec.active) els.recordingStatus.textContent = "Recording" + (rec.file ? " to " + rec.file : "");
  else els.recordingStatus.textContent = "Not recording";

  els.currentSource.textContent = s && s.source && s.running ? String(s.source.label || s.source.id) : "–";
  if (s && s.session_id) {
    els.sessionId.textContent = String(s.session_id) + (s.running ? "" : " (finished)");
  } else {
    els.sessionId.textContent = "–";
  }
}

function setBusyLabel(button, busy) {
  const [idle, working] = BUTTON_LABELS.get(button);
  button.textContent = busy ? working : idle;
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

// A camera source is selected while a desktop reports a camera session.
function desktopCameraConflict() {
  const selected = sources.find((src) => src.id === els.sourceSelect.value);
  return Boolean(selected && selected.kind === "camera" && desktopCameraInUse());
}

function renderControls() {
  const s = status;
  const operator = session.role === "operator";
  const usable = operator && Boolean(s) && !serviceProblem && !pending;
  const state = s ? s.state : "";
  const running = Boolean(s && s.running);
  const transition = state === "Initializing" || state === "Stopping";
  const modelMissing = Boolean(s && s.model && s.model.available === false);
  const selected = sources.find((src) => src.id === els.sourceSelect.value);

  if (running && s.source && sources.some((src) => src.id === s.source.id)) els.sourceSelect.value = s.source.id;
  const conflict = !running && desktopCameraConflict();
  els.sourceSelect.disabled = !usable || running || transition || sources.length === 0;
  els.loopVideo.disabled = els.sourceSelect.disabled || !selected || selected.kind !== "video_file";
  els.loopField.classList.toggle("disabled", els.loopVideo.disabled);
  els.startButton.disabled = !usable || running || transition || !selected || modelMissing || conflict;
  els.legacyConflict.hidden = !conflict;
  els.legacyConflict.textContent = conflict
    ? "The desktop '" + desktopCameraInUse() + "' is monitoring a camera. This page does not start a server camera " +
      "at the same time (a check made by this page only; the server does not prevent it)."
    : "";
  els.stopButton.disabled = !usable || !running || state === "Stopping";
  els.recStartButton.disabled = !usable || state !== "Detecting";
  els.recStopButton.disabled = !usable || state !== "Recording";
  for (const button of BUTTON_LABELS.keys()) setBusyLabel(button, Boolean(pending && pending.button === button));

  if (operator && modelMissing && !running && !pending) {
    setMessage(els.controlMessage, "The YOLO weights '" + String(s.model.name) + "' are not installed on the monitor service, so monitoring cannot start.", true);
  }
}

function renderStats() {
  const s = serviceProblem ? null : status;
  const totals = (s && s.totals) || {};
  for (const [key, el] of Object.entries(TILES)) el.textContent = fmtCount(totals[key] ?? 0);

  els.statsScope.textContent = !s ? "" : s.running ? "Live" : s.session_id ? "Last session (finished)" : "No session yet";

  els.classBody.replaceChildren();
  const classes = s && Array.isArray(s.classes) ? s.classes : [];
  for (const c of classes) {
    const row = document.createElement("tr");
    row.appendChild(cell(String(c)));
    row.appendChild(cell(fmtCount((s.visible || {})[c] ?? 0), "num"));
    row.appendChild(cell(fmtCount((s.unique || {})[c] ?? 0), "num"));
    row.appendChild(cell(fmtCount((s.crossings || {})[c] ?? 0), "num"));
    els.classBody.appendChild(row);
  }

  els.linesBody.replaceChildren();
  const lines = s && s.line_totals && typeof s.line_totals === "object" ? Object.entries(s.line_totals) : [];
  for (const [line, dirs] of lines) {
    const row = document.createElement("tr");
    row.appendChild(cell(String(line)));
    row.appendChild(cell(fmtCount(sumClasses(dirs && dirs.a_to_b, Object.keys((dirs && dirs.a_to_b) || {}))), "num"));
    row.appendChild(cell(fmtCount(sumClasses(dirs && dirs.b_to_a, Object.keys((dirs && dirs.b_to_a) || {}))), "num"));
    els.linesBody.appendChild(row);
  }
  els.linesBlock.hidden = lines.length === 0;

  const perf = (s && s.performance) || {};
  const live = Boolean(s && s.running);
  document.getElementById("perf-inference-fps").textContent = live ? fmtNumber(perf.inference_fps, 1) : "–";
  document.getElementById("perf-capture-fps").textContent = live ? fmtNumber(perf.capture_fps, 1) : "–";
  document.getElementById("perf-latency").textContent = live && typeof perf.latency_ms === "number" ? fmtNumber(perf.latency_ms, 1) + " ms" : "–";
  document.getElementById("perf-frames").textContent = s ? fmtCount(perf.frames_processed) : "–";
  document.getElementById("perf-dropped").textContent = s ? fmtCount(perf.frames_dropped) : "–";
  document.getElementById("perf-tracks").textContent = live ? fmtCount(perf.active_tracks) : "–";
  document.getElementById("perf-device").textContent = s && s.device ? String(s.device) : "–";
  const model = s && s.model;
  document.getElementById("perf-model").textContent = model
    ? String(model.name) + (model.available === false ? " (weights missing)" : "")
    : "–";
}

function renderFeed() {
  const s = status;
  let text = "";
  let spinner = false;
  if (serviceProblem) {
    text = serviceProblem;
  } else if (!s) {
    text = "Connecting to the monitor...";
    spinner = true;
  } else if (s.state === "Initializing") {
    text = "Starting: loading the model and opening the source...";
    spinner = true;
  } else if (s.state === "Stopping") {
    text = "Stopping...";
    spinner = true;
  } else if (s.state === "Error") {
    text = "Monitoring stopped with an error: " + stoppedText(s);
  } else if (s.running && FRAME_STATES.includes(s.state)) {
    if (frameTrouble) text = frameTrouble;
    else if (!frameUrl) {
      text = document.hidden ? "Feed paused while the page is hidden." : "Waiting for the first frame...";
      spinner = !document.hidden;
    }
  } else {
    text = session.role === "operator"
      ? "Monitoring is not running. Choose a source and press Start monitoring."
      : "Monitoring is not running.";
  }
  els.feedMessage.textContent = text;
  els.feedSpinner.hidden = !spinner;
  els.feedOverlay.hidden = !text;
  els.feedOverlay.classList.toggle("over-frame", Boolean(text && frameUrl));
}

// ---------- page lifecycle ----------

function onVisibility() {
  if (!polling()) return;
  desktopVisibility();
  if (document.hidden) {
    clearTimeout(poll.frameTimer);
    poll.frameTimer = null;
    scheduleStatus(STATUS_HIDDEN_MS);
  } else if (legacyOpen()) {
    clearTimeout(poll.statusTimer);
    if (!poll.statusBusy) statusTick();
    syncFramePolling();
  }
  renderFeed();
}

els.loginForm.addEventListener("submit", onLogin);
els.logoutButton.addEventListener("click", () => signOut());
els.deniedRetry.addEventListener("click", () => {
  if (session.token) begin();
});
els.sourceSelect.addEventListener("change", renderControls);
els.startButton.addEventListener("click", () => guard(() => command("start", els.startButton)));
els.stopButton.addEventListener("click", () => guard(() => command("stop", els.stopButton)));
els.recStartButton.addEventListener("click", () => guard(() => command("recStart", els.recStartButton)));
els.recStopButton.addEventListener("click", () => guard(() => command("recStop", els.recStopButton)));
els.sessionsRefresh.addEventListener("click", () => guard(loadSessions));
els.legacyPanel.addEventListener("toggle", () => (legacyOpen() ? legacyOpened() : legacyClosed()));
document.addEventListener("visibilitychange", onVisibility);
// Leaving the page stops every timer and request and releases the frame.
window.addEventListener("pagehide", stopPolling);
window.addEventListener("pageshow", (event) => {
  if (event.persisted && session.token) begin();
});

restoreSession();
