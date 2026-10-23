// Controller comparison dashboard. All API text goes into the page through
// textContent or element properties, never innerHTML.

const METRICS = [
  { key: "avg_wait_s", canvas: "chart-avg-wait", label: "Average waiting time" },
  { key: "avg_time_loss_s", canvas: "chart-time-loss", label: "Average time loss" },
  { key: "max_wait_s", canvas: "chart-max-wait", label: "Maximum waiting time" },
];
const TABLE_METRICS = ["avg_wait_s", "max_wait_s", "avg_time_loss_s", "avg_trip_duration_s"];
// Fixed colour per controller, in /api/controllers order, so a controller keeps
// its colour whichever runs exist.
const PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"];
const INK_SECONDARY = "#52606d";
const GRID = "#eef0f2";

const els = {
  loginPanel: document.getElementById("login-panel"),
  loginForm: document.getElementById("login-form"),
  username: document.getElementById("username"),
  password: document.getElementById("password"),
  loginButton: document.getElementById("login-button"),
  registerButton: document.getElementById("register-button"),
  loginMessage: document.getElementById("login-message"),
  session: document.getElementById("session"),
  sessionUser: document.getElementById("session-user"),
  logoutButton: document.getElementById("logout-button"),
  dashboard: document.getElementById("dashboard"),
  scenarioSelect: document.getElementById("scenario-select"),
  exportController: document.getElementById("export-controller"),
  refreshButton: document.getElementById("refresh-button"),
  exportButton: document.getElementById("export-button"),
  status: document.getElementById("status"),
  scenarioInfo: document.getElementById("scenario-info"),
  emptyMessage: document.getElementById("empty-message"),
  baselineMessage: document.getElementById("baseline-message"),
  compareBody: document.getElementById("compare-body"),
  legend: document.getElementById("legend"),
  timeseriesNote: document.getElementById("timeseries-note"),
};

// The token lives in memory only: a reload signs the user out.
const session = { token: null, username: null, expiryTimer: null };
const charts = {};
let controllers = [];
let compareSeq = 0;

class SessionExpired extends Error {}

async function api(path, { method = "GET", body, auth = false, as = "json" } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (auth) {
    if (!session.token) throw new SessionExpired();
    headers.Authorization = "Bearer " + session.token;
  }
  let response;
  try {
    response = await fetch("/api" + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    return { ok: false, status: 0, data: null };
  }
  if (auth && response.status === 401) throw new SessionExpired();
  let data = null;
  if (as === "text") {
    data = response.ok ? await response.text() : null;
  } else {
    data = await response.json().catch(() => null);
  }
  return { ok: response.ok, status: response.status, data };
}

function failureText(status, what) {
  if (status === 0) return "Could not reach the server.";
  if (status === 404) return what + " not found.";
  if (status >= 500) return "The server could not complete the request.";
  return "Request failed (" + status + ").";
}

function setMessage(el, text, isError) {
  el.textContent = text;
  el.classList.toggle("error", Boolean(isError));
}

function colorFor(name) {
  const i = controllers.indexOf(name);
  return PALETTE[(i === -1 ? controllers.length : i) % PALETTE.length];
}

function fmt(value, digits = 2) {
  return value === null || value === undefined ? "–" : value.toFixed(digits);
}

// ---------- session ----------

function showLogin(message) {
  els.dashboard.hidden = true;
  els.session.hidden = true;
  els.loginPanel.hidden = false;
  setMessage(els.loginMessage, message || "", Boolean(message));
  els.username.focus();
}

function signOut(message) {
  clearTimeout(session.expiryTimer);
  session.token = null;
  session.username = null;
  compareSeq++;
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

function startSession(username, data) {
  session.token = data.access_token;
  session.username = username;
  clearTimeout(session.expiryTimer);
  if (data.expires_in > 0) session.expiryTimer = setTimeout(expired, data.expires_in * 1000);
  els.password.value = "";
  setMessage(els.loginMessage, "");
  els.loginPanel.hidden = true;
  els.sessionUser.textContent = username;
  els.session.hidden = false;
  els.dashboard.hidden = false;
  guard(loadDashboard);
}

async function login(username, password) {
  const { ok, status, data } = await api("/auth/login", {
    method: "POST",
    body: { username, password },
  });
  if (ok && data && data.access_token) {
    startSession(username, data);
    return;
  }
  if (status === 401) setMessage(els.loginMessage, "Incorrect username or password.", true);
  else if (status === 400) setMessage(els.loginMessage, "Enter a username and password.", true);
  else setMessage(els.loginMessage, failureText(status, "Login"), true);
}

function readCredentials() {
  const username = els.username.value.trim();
  const password = els.password.value;
  if (!username || !password) {
    setMessage(els.loginMessage, "Enter a username and password.", true);
    return null;
  }
  return { username, password };
}

async function withButtons(task) {
  els.loginButton.disabled = true;
  els.registerButton.disabled = true;
  try {
    await task();
  } finally {
    els.loginButton.disabled = false;
    els.registerButton.disabled = false;
  }
}

function onLogin(event) {
  event.preventDefault();
  const creds = readCredentials();
  if (creds) withButtons(() => login(creds.username, creds.password));
}

function onRegister() {
  const creds = readCredentials();
  if (!creds) return;
  withButtons(async () => {
    const { ok, status, data } = await api("/auth/register", { method: "POST", body: creds });
    if (ok) {
      await login(creds.username, creds.password);
    } else if (status === 409) {
      setMessage(els.loginMessage, "That username is already taken.", true);
    } else if (status === 400 && data && Array.isArray(data.errors)) {
      // Validation messages written by our own API, shown as plain text.
      setMessage(els.loginMessage, data.errors.join(" "), true);
    } else {
      setMessage(els.loginMessage, failureText(status, "Registration"), true);
    }
  });
}

// ---------- loading ----------

async function loadDashboard() {
  setMessage(els.status, "Loading scenarios...");
  const [ctrl, scen] = await Promise.all([api("/controllers"), api("/scenarios")]);
  if (!ctrl.ok || !scen.ok || !Array.isArray(scen.data)) {
    setMessage(els.status, "Could not load scenarios. " + failureText(scen.ok ? ctrl.status : scen.status, "Scenarios"), true);
    return;
  }
  controllers = ctrl.data.controllers;
  renderLegend();

  const previousController = els.exportController.value;
  els.exportController.replaceChildren(new Option("All controllers", ""));
  for (const name of controllers) els.exportController.appendChild(new Option(name, name));
  if (controllers.includes(previousController)) els.exportController.value = previousController;

  const previousScenario = els.scenarioSelect.value;
  els.scenarioSelect.replaceChildren();
  for (const s of scen.data) {
    const label = s.name + " (" + s.ew_vehs_per_hour + " / " + s.ns_vehs_per_hour + " veh/h)";
    els.scenarioSelect.appendChild(new Option(label, String(s.id)));
  }
  if (scen.data.some((s) => String(s.id) === previousScenario)) els.scenarioSelect.value = previousScenario;
  if (scen.data.length === 0) {
    setMessage(els.status, "No scenarios yet. Create one on the Scenarios page.");
    renderComparison({ scenario: null, controllers: [] });
    return;
  }
  await loadComparison();
}

async function loadComparison() {
  const scenarioId = els.scenarioSelect.value;
  if (!scenarioId) return;
  const seq = ++compareSeq;
  setMessage(els.status, "Loading comparison...");
  const { ok, status, data } = await api("/compare?scenario_id=" + encodeURIComponent(scenarioId), {
    auth: true,
  });
  if (seq !== compareSeq) return; // a newer selection is already loading
  if (!ok || !data) {
    setMessage(els.status, failureText(status, "Scenario"), true);
    renderComparison({ scenario: null, controllers: [] });
    return;
  }
  setMessage(els.status, "");
  renderComparison(data);
  await loadTimeseries(data.controllers, seq);
}

async function loadTimeseries(entries, seq) {
  const results = await Promise.all(
    entries.map((e) => api("/runs/" + e.run_id + "/timeseries").then((r) => ({ entry: e, r })))
  );
  if (seq !== compareSeq) return;
  const series = [];
  const missing = [];
  for (const { entry, r } of results) {
    if (r.ok && r.data && Array.isArray(r.data.time_s)) series.push({ name: entry.controller, data: r.data });
    else missing.push(entry.controller);
  }
  renderTimeseries(series);
  let note = "";
  if (entries.length === 0) note = "No completed runs to plot.";
  else if (missing.length) {
    note = "No timeseries for " + missing.join(", ") + " (the run's SUMO summary file is not on this server).";
  }
  els.timeseriesNote.textContent = note;
}

// ---------- rendering ----------

function renderLegend() {
  els.legend.replaceChildren();
  for (const name of controllers) {
    const item = document.createElement("li");
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = colorFor(name);
    item.append(swatch, document.createTextNode(name));
    els.legend.appendChild(item);
  }
}

function deltaSpan(entry, key) {
  const span = document.createElement("span");
  span.className = "delta";
  const pct = entry.improvement_vs_fixed_pct;
  if (!pct) return span;
  if (entry.controller === "fixed") {
    span.textContent = "baseline";
    return span;
  }
  const value = pct[key];
  if (value === null || value === undefined) {
    span.textContent = "n/a";
  } else if (value > 0) {
    span.textContent = value.toFixed(1) + "% better";
    span.classList.add("better");
  } else if (value < 0) {
    span.textContent = Math.abs(value).toFixed(1) + "% worse";
    span.classList.add("worse");
  } else {
    span.textContent = "no change";
  }
  return span;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderTable(entries) {
  els.compareBody.replaceChildren();
  for (const entry of entries) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = colorFor(entry.controller);
    name.append(swatch, document.createTextNode(entry.controller));
    row.appendChild(name);
    row.appendChild(cell("#" + entry.run_id));
    row.appendChild(cell(entry.vehicles_completed ?? "–", "num"));
    for (const key of TABLE_METRICS) {
      const td = cell(fmt(entry[key]), "num");
      td.appendChild(deltaSpan(entry, key));
      row.appendChild(td);
    }
    els.compareBody.appendChild(row);
  }
}

// Draws each bar's value above it; missing values get no bar and no label.
const valueLabels = {
  id: "valueLabels",
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    ctx.save();
    ctx.fillStyle = INK_SECONDARY;
    ctx.font = "12px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    chart.getDatasetMeta(0).data.forEach((bar, i) => {
      const value = chart.data.datasets[0].data[i];
      if (value !== null && value !== undefined) ctx.fillText(value.toFixed(1), bar.x, bar.y - 4);
    });
    ctx.restore();
  },
};

function barChart(canvasId, label) {
  return new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: { labels: [], datasets: [{ label, data: [], backgroundColor: [], borderRadius: 4, borderSkipped: "start", maxBarThickness: 56 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { top: 18 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: (ctx) => label + ": " + (ctx.raw === null ? "no data" : ctx.raw.toFixed(2) + " s") },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: INK_SECONDARY }, title: { display: true, text: "Controller", color: INK_SECONDARY } },
        y: {
          beginAtZero: true,
          grid: { color: GRID },
          ticks: { color: INK_SECONDARY },
          title: { display: true, text: "Seconds", color: INK_SECONDARY },
        },
      },
    },
    plugins: [valueLabels],
  });
}

function chartsAvailable() {
  return typeof Chart !== "undefined";
}

function renderBarCharts(entries) {
  for (const metric of METRICS) {
    const note = document.querySelector('[data-note="' + metric.key + '"]');
    if (!chartsAvailable()) {
      note.textContent = "Charts could not be loaded.";
      continue;
    }
    if (!charts[metric.key]) charts[metric.key] = barChart(metric.canvas, metric.label);
    const chart = charts[metric.key];
    chart.data.labels = entries.map((e) => e.controller);
    chart.data.datasets[0].data = entries.map((e) => (e[metric.key] ?? null));
    chart.data.datasets[0].backgroundColor = entries.map((e) => colorFor(e.controller));
    chart.update();
    const missing = entries.filter((e) => e[metric.key] === null || e[metric.key] === undefined);
    note.textContent = missing.length ? "No value recorded for " + missing.map((e) => e.controller).join(", ") + "." : "";
  }
}

function renderTimeseries(series) {
  if (!chartsAvailable()) {
    els.timeseriesNote.textContent = "Charts could not be loaded.";
    return;
  }
  if (!charts.timeseries) {
    charts.timeseries = new Chart(document.getElementById("chart-timeseries"), {
      type: "line",
      data: { datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 12, boxHeight: 2, color: INK_SECONDARY } },
          tooltip: { callbacks: { title: (items) => "t = " + items[0].parsed.x + " s" } },
        },
        scales: {
          x: { type: "linear", grid: { color: GRID }, ticks: { color: INK_SECONDARY }, title: { display: true, text: "Simulation time (s)", color: INK_SECONDARY } },
          y: { beginAtZero: true, grid: { color: GRID }, ticks: { color: INK_SECONDARY, precision: 0 }, title: { display: true, text: "Halting vehicles (window mean)", color: INK_SECONDARY } },
        },
      },
    });
  }
  charts.timeseries.data.datasets = series.map(({ name, data }) => ({
    label: name,
    data: data.time_s.map((t, i) => ({ x: t, y: data.halting[i] })),
    borderColor: colorFor(name),
    backgroundColor: colorFor(name),
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 4,
  }));
  const ends = series.map(({ data }) => data.time_s[data.time_s.length - 1]).filter((t) => t !== undefined);
  charts.timeseries.options.scales.x.max = ends.length ? Math.max(...ends) : undefined;
  charts.timeseries.update();
}

function renderComparison(data) {
  const entries = data.controllers || [];
  const s = data.scenario;
  els.scenarioInfo.textContent = s ? s.name + ": " + s.ew_vehs_per_hour + " EW / " + s.ns_vehs_per_hour + " NS veh/h" : "";
  els.emptyMessage.hidden = !s || entries.length > 0;
  els.baselineMessage.hidden = entries.length === 0 || entries.some((e) => e.improvement_vs_fixed_pct);
  renderTable(entries);
  renderBarCharts(entries);
  if (entries.length === 0) {
    renderTimeseries([]);
    els.timeseriesNote.textContent = s ? "No completed runs to plot." : "";
  }
}

// ---------- CSV export ----------

function safeFilePart(text) {
  return String(text).replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40) || "all";
}

async function exportCsv() {
  const params = new URLSearchParams();
  const scenarioId = els.scenarioSelect.value;
  const controller = els.exportController.value;
  if (scenarioId) params.set("scenario_id", scenarioId);
  if (controller) params.set("controller", controller);
  els.exportButton.disabled = true;
  try {
    const { ok, status, data } = await api("/runs/export.csv?" + params, { auth: true, as: "text" });
    if (!ok) {
      setMessage(els.status, "Export failed. " + failureText(status, "Export"), true);
      return;
    }
    const rows = data.split("\r\n").filter((line) => line.length > 0).length - 1;
    if (rows <= 0) {
      setMessage(els.status, "No runs match the selected filters, so there is nothing to export.");
      return;
    }
    const option = els.scenarioSelect.selectedOptions[0];
    const scenarioName = option ? option.textContent.split(" (")[0] : "all";
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([data], { type: "text/csv" }));
    link.download = "atcs_runs_" + safeFilePart(scenarioName) + "_" + safeFilePart(controller || "all") + ".csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    setMessage(els.status, "Exported " + rows + (rows === 1 ? " run." : " runs."));
  } finally {
    els.exportButton.disabled = false;
  }
}

els.loginForm.addEventListener("submit", onLogin);
els.registerButton.addEventListener("click", onRegister);
els.logoutButton.addEventListener("click", () => signOut());
els.scenarioSelect.addEventListener("change", () => guard(loadComparison));
els.refreshButton.addEventListener("click", () => guard(loadDashboard));
els.exportButton.addEventListener("click", () => guard(exportCsv));

showLogin();
