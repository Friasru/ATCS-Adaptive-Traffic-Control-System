// Browser tests for the Monitor page (and the navigation and session handling it
// shares with the Dashboard). Run with: cd frontend/tests && npm install && npm test

import { after, afterEach, before, beforeEach, describe, test } from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { DESKTOP, deskDetail, deskDevice, deskSession, heartbeat, makeToken, mock, resetMock, startServer } from "./mock-server.mjs";

let browser;
let server;
let base;
let context;
let page;

// Counts object URLs and lets a test pretend the tab is hidden.
const INSTRUMENT = () => {
  window.__urls = { created: 0, revoked: 0, live: new Set() };
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (obj) => {
    const url = create(obj);
    window.__urls.created++;
    window.__urls.live.add(url);
    return url;
  };
  URL.revokeObjectURL = (url) => {
    window.__urls.revoked++;
    window.__urls.live.delete(url);
    revoke(url);
  };
  // Clicks on the desktop launch link, with whether they came from the user.
  window.__launchClicks = [];
  document.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest('a[href^="atcs-monitor:"]') : null;
    if (a) window.__launchClicks.push({ href: a.getAttribute("href"), trusted: e.isTrusted });
  }, true);
  window.__hidden = false;
  Object.defineProperty(Document.prototype, "hidden", { configurable: true, get: () => window.__hidden });
  Object.defineProperty(Document.prototype, "visibilityState", {
    configurable: true,
    get: () => (window.__hidden ? "hidden" : "visible"),
  });
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, { timeout = 5000, message = "condition" } = {}) {
  const end = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > end) throw new Error("Timed out waiting for " + message);
    await sleep(50);
  }
}

const monitorRequests = (pathPart, method) =>
  mock.requests.filter((r) => r.path.includes(pathPart) && (!method || r.method === method));

async function newPage() {
  const p = await context.newPage();
  await p.addInitScript(INSTRUMENT);
  // What the browser itself was asked to open. Headless Chromium has no
  // atcs-monitor: handler, so this proves the attempt, never a launched app.
  p.launches = [];
  p.on("request", (r) => {
    if (r.url().toLowerCase().startsWith("atcs-monitor:")) p.launches.push(r.url());
  });
  p.on("pageerror", (e) => {
    throw e;
  });
  return p;
}

async function signIn(p, username = "op") {
  await p.goto(base + "/monitor.html");
  await p.fill("#username", username);
  await p.fill("#password", "pw");
  await p.click("#login-button");
  await p.waitForSelector("#session:not([hidden])");
}

// The server-side (legacy) monitor is a collapsed panel; it polls only while open.
async function openLegacy(p) {
  if (!(await p.$eval("#legacy-panel", (d) => d.open))) await p.click("#legacy-panel > summary");
  await waitFor(() => p.$eval("#legacy-panel", (d) => d.open), { message: "legacy panel to open" });
  // Its sources are requested on opening; wait until that answer (or failure) is shown.
  await waitFor(() => p.$eval("#source-select", (s) => s.options.length > 0 && s.options[0].textContent !== "Loading sources..."),
    { message: "legacy sources" });
}

async function signInLegacy(p, username = "op") {
  await signIn(p, username);
  await openLegacy(p);
}

async function storeToken(p, token, username = "op") {
  await p.goto(base + "/scenarios.html");
  await p.evaluate(([t, u]) => sessionStorage.setItem("atcs.session", JSON.stringify({ token: t, username: u })), [token, username]);
}

async function text(p, selector) {
  return (await p.textContent(selector)).trim();
}

async function waitText(p, selector, expected, timeout = 5000) {
  await waitFor(async () => {
    const t = await text(p, selector);
    return expected instanceof RegExp ? expected.test(t) : t.includes(expected);
  }, { timeout, message: selector + " to show " + expected });
}

async function waitRole(p, role) {
  await waitText(p, "#role-badge", role);
}

before(async () => {
  ({ server, base } = await startServer());
  browser = await chromium.launch();
});

after(async () => {
  await browser.close();
  server.close();
});

beforeEach(async () => {
  resetMock();
  context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  page = await newPage();
});

afterEach(async () => {
  await context.close();
  resetMock();
});

describe("navigation", () => {
  for (const [file, current] of [["scenarios.html", "Scenarios"], ["dashboard.html", "Dashboard"], ["monitor.html", "Monitor"]]) {
    test(file + " links to all three pages and marks itself", async () => {
      await page.goto(base + "/" + file);
      const links = await page.$$eval("nav a", (as) => as.map((a) => [a.textContent.trim(), a.getAttribute("href"), a.getAttribute("aria-current")]));
      assert.deepEqual(links.map((l) => l.slice(0, 2)), [
        ["Scenarios", "scenarios.html"],
        ["Dashboard", "dashboard.html"],
        ["Monitor", "monitor.html"],
      ]);
      assert.deepEqual(links.filter((l) => l[2] === "page").map((l) => l[0]), [current]);
    });
  }

  test("following the links moves between the pages", async () => {
    await page.goto(base + "/scenarios.html");
    await page.click("nav a[href='monitor.html']");
    await page.waitForURL(/monitor\.html$/);
    await page.click("nav a[href='dashboard.html']");
    await page.waitForURL(/dashboard\.html$/);
    await page.click("nav a[href='scenarios.html']");
    await page.waitForURL(/scenarios\.html$/);
  });
});

describe("authentication", () => {
  test("signs in, then keeps the session across reloads and pages", async () => {
    await signIn(page);
    await waitRole(page, "Operator");
    assert.equal(await text(page, "#session-user"), "op");
    const stored = await page.evaluate(() => JSON.parse(sessionStorage.getItem("atcs.session")));
    assert.equal(stored.username, "op");

    await page.reload();
    await waitRole(page, "Operator");
    assert.equal(await page.isVisible("#login-panel"), false);
    assert.equal(await page.isVisible("#desk-open"), true);

    await page.click("nav a[href='dashboard.html']");
    await page.waitForSelector("#dashboard:not([hidden])");
    assert.equal(await text(page, "#session-user"), "op");
    await page.click("nav a[href='monitor.html']");
    await waitRole(page, "Operator");
  });

  test("wrong password shows an error and stores nothing", async () => {
    await page.goto(base + "/monitor.html");
    await page.fill("#username", "op");
    await page.fill("#password", "nope");
    await page.click("#login-button");
    await waitText(page, "#login-message", "Incorrect username or password.");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });

  test("a malformed stored token is discarded without calling the API", async () => {
    await storeToken(page, "not-a-jwt");
    await page.goto(base + "/monitor.html");
    await page.waitForSelector("#login-panel:not([hidden])");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
    assert.equal(monitorRequests("/api/monitor/").length, 0);
  });

  test("an expired stored token signs the user out without calling the API", async () => {
    await storeToken(page, makeToken("1", Math.floor(Date.now() / 1000) - 10));
    await page.goto(base + "/monitor.html");
    await waitText(page, "#login-message", "Your session has expired");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
    assert.equal(monitorRequests("/api/monitor/").length, 0);
  });

  test("the session ends when the token's exp passes", async () => {
    mock.tokenLifetimeS = 2;
    await signIn(page);
    await waitText(page, "#login-message", "Your session has expired", 6000);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });

  test("HTTP 401 clears the session", async () => {
    await signIn(page);
    await waitRole(page, "Operator");
    const token = JSON.parse(await page.evaluate(() => sessionStorage.getItem("atcs.session"))).token;
    mock.revoked.add(token);
    await waitText(page, "#login-message", "Your session has expired", 6000);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
    assert.equal(await page.isVisible("#monitor"), false);
  });

  test("HTTP 403 shows access denied but keeps the user signed in", async () => {
    await signIn(page, "ng");
    await page.waitForSelector("#denied-panel:not([hidden])");
    assert.equal(await page.isVisible("#monitor"), false);
    assert.equal(await page.isVisible("#session"), true);
    assert.notEqual(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
    assert.equal(monitorRequests("/frame.jpg").length, 0);
    // After a grant, "Check again" opens the monitor without signing in again.
    mock.users.ng.role = "viewer";
    await page.click("#denied-retry");
    await waitRole(page, "Viewer");
    assert.equal(await page.isVisible("#monitor"), true);
  });

  for (const [statusCode, code, expected] of [
    [502, "service_auth_failed", "could not authenticate with the monitor service"],
    [503, "service_unavailable", "The monitor service is not running."],
    [504, "service_timeout", "did not respond in time"],
    [503, "service_not_configured", "not configured"],
  ]) {
    test("HTTP " + statusCode + " " + code + " is a service problem, not a sign-out", async () => {
      mock.fail["GET /monitor/status"] = Array(50).fill({ status: statusCode, body: { error: "x", code } });
      await signInLegacy(page);
      await waitText(page, "#state-badge", "Service unavailable");
      await waitText(page, "#monitoring-status", expected);
      assert.equal(await page.isVisible("#monitor"), true);
      assert.notEqual(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
      assert.equal(await page.isDisabled("#start-button"), true);
      // The page recovers by itself once the service answers again.
      mock.fail["GET /monitor/status"] = [];
      await waitText(page, "#state-badge", "Idle", 6000);
    });
  }

  test("tokens only travel in the Authorization header", async () => {
    mock.desktop.devices = [deskDevice("recording")];
    mock.desktop.sessions = [deskSession("completed")];
    mock.desktop.details = { [DESKTOP.session_completed.session_id]: deskDetail("completed") };
    await signInLegacy(page);
    await page.selectOption("#source-select", "video:car-detection.mp4");
    await page.click("#start-button");
    await waitFor(() => monitorRequests("/frame.jpg").length > 3, { message: "frames" });
    await page.click("#desk-sessions-body button");
    await waitFor(() => monitorRequests("/api/monitor/desktop/sessions/").length > 0, { message: "detail request" });
    const token = JSON.parse(await page.evaluate(() => sessionStorage.getItem("atcs.session"))).token;
    assert.ok(monitorRequests("/api/monitor/desktop/status").length > 0);
    // The browser never calls the device-token endpoints.
    assert.equal(mock.requests.filter((r) => r.path.startsWith("/api/desktop/")).length, 0);
    for (const r of monitorRequests("/api/monitor/")) {
      assert.equal(r.auth, "Bearer " + token, r.method + " " + r.path);
      assert.ok(!r.query.includes(token) && !r.path.includes(token));
    }
  });
});

describe("roles", () => {
  for (const [user, role] of [["op", "Operator"], ["vw", "Viewer"]]) {
    test(role.toLowerCase() + " permissions come from GET /api/monitor/access, with no POST on load", async () => {
      await signInLegacy(page, user);
      await waitRole(page, role);
      await sleep(500);
      const access = monitorRequests("/api/monitor/access");
      assert.ok(access.length >= 1 && access.every((r) => r.method === "GET"));
      assert.equal(monitorRequests("/api/monitor/", "POST").length, 0);
      assert.equal(await page.isVisible("#viewer-note"), role === "Viewer");
      // A reload restores the session from sessionStorage and asks again.
      await page.reload();
      await waitRole(page, role);
      assert.ok(monitorRequests("/api/monitor/access").length > access.length);
      assert.equal(monitorRequests("/api/monitor/", "POST").length, 0);
    });
  }

  test("the role is known even while the monitor service is down", async () => {
    mock.fail["GET /monitor/status"] = Array(50).fill({ status: 503, body: { error: "x", code: "service_unavailable" } });
    await signInLegacy(page);
    await waitRole(page, "Operator");
    await waitText(page, "#state-badge", "Service unavailable");
    assert.equal(await page.isDisabled("#start-button"), true);
  });

  test("a 403 from the access check shows access denied without signing out", async () => {
    mock.fail["GET /monitor/access"] = [{ status: 403, body: { error: "not authorized for the monitor" } }];
    await signIn(page);
    await page.waitForSelector("#denied-panel:not([hidden])");
    assert.equal(await page.isVisible("#session"), true);
    assert.notEqual(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });

  test("a 401 from the access check signs out", async () => {
    mock.fail["GET /monitor/access"] = [{ status: 401, body: { error: "authentication required" } }];
    // Not signIn(): the session ends before the signed-in header could be awaited.
    await page.goto(base + "/monitor.html");
    await page.fill("#username", "op");
    await page.fill("#password", "pw");
    await page.click("#login-button");
    await waitText(page, "#login-message", "Your session has expired");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });

  test("a viewer sees status and frames but cannot operate", async () => {
    mock.engine.init_ms = 0;
    // Started by someone else.
    mock.engine.running = true;
    mock.engine.state = "Detecting";
    mock.engine.session_id = "sess-shared";
    mock.engine.source = mock.sources[1];
    await signInLegacy(page, "vw");
    await waitRole(page, "Viewer");
    await waitFor(() => page.evaluate(() => Boolean(document.getElementById("feed-image").src)), { message: "frame" });
    for (const id of ["#source-select", "#start-button", "#stop-button", "#rec-start-button", "#rec-stop-button", "#loop-video"]) {
      assert.equal(await page.isDisabled(id), true, id);
    }
    assert.equal(await page.isVisible("#viewer-note"), true);
    // Even forced clicks send no command.
    await page.$eval("#stop-button", (b) => {
      b.disabled = false;
      b.click();
    });
    await sleep(300);
    assert.equal(monitorRequests("/monitor/stop", "POST").length, 0);
    assert.equal(monitorRequests("/api/monitor/", "POST").length, 0);
  });

  test("an operator whose grant is downgraded sees the controls locked on 403", async () => {
    await signInLegacy(page);
    await waitRole(page, "Operator");
    mock.users.op.role = "viewer";
    await page.selectOption("#source-select", "camera:0");
    await page.click("#start-button");
    await waitText(page, "#control-message", "Access denied");
    await waitRole(page, "Viewer");
    assert.equal(await page.isDisabled("#start-button"), true);
    assert.equal(await page.isVisible("#session"), true);
  });
});

describe("controls", () => {
  test("lists sources by kind; loop only applies to videos", async () => {
    await signInLegacy(page);
    await waitRole(page, "Operator");
    const groups = await page.$$eval("#source-select optgroup", (gs) => gs.map((g) => [g.label, [...g.children].map((o) => o.value)]));
    assert.deepEqual(groups, [["Cameras", ["camera:0"]], ["Videos", ["video:car-detection.mp4"]]]);
    await page.selectOption("#source-select", "camera:0");
    assert.equal(await page.isDisabled("#loop-video"), true);
    await page.selectOption("#source-select", "video:car-detection.mp4");
    assert.equal(await page.isDisabled("#loop-video"), false);
  });

  test("start waits for the engine, then stop returns to idle", async () => {
    mock.engine.init_ms = 800;
    await signInLegacy(page);
    await waitRole(page, "Operator");
    assert.equal(monitorRequests("/api/monitor/", "POST").length, 0, "never starts on load");
    await page.selectOption("#source-select", "video:car-detection.mp4");
    await page.check("#loop-video");
    await page.click("#start-button");
    const start = monitorRequests("/monitor/start", "POST").at(-1);
    assert.deepEqual(JSON.parse(start.body), { source: "video:car-detection.mp4", loop: true });
    // 202 is not "running yet": the page shows the transition until the status changes.
    await waitText(page, "#state-badge", "Initializing");
    assert.equal(await text(page, "#start-button"), "Starting...");
    for (const id of ["#start-button", "#stop-button", "#rec-start-button", "#rec-stop-button", "#source-select"]) {
      assert.equal(await page.isDisabled(id), true, id);
    }
    await waitText(page, "#state-badge", "Detecting");
    await waitText(page, "#control-message", "Monitoring started.");
    assert.equal(await page.isDisabled("#stop-button"), false);
    assert.equal(await page.isDisabled("#rec-start-button"), false);
    assert.equal(await page.isDisabled("#start-button"), true);
    await waitText(page, "#session-id", "sess-1");

    await page.click("#stop-button");
    await waitText(page, "#state-badge", "Idle");
    await waitText(page, "#control-message", "Monitoring stopped.");
    await waitText(page, "#session-id", "(finished)");
    assert.equal(await page.isDisabled("#start-button"), false);
    // The finished session shows up in the list without a manual refresh.
    await waitText(page, "#sessions-body", "completed");
  });

  test("recording start and stop follow the engine state", async () => {
    mock.engine.init_ms = 0;
    mock.engine.rec_ms = 500;
    await signInLegacy(page);
    await page.selectOption("#source-select", "camera:0");
    await page.click("#start-button");
    await waitText(page, "#state-badge", "Detecting");
    await page.click("#rec-start-button");
    assert.equal(await text(page, "#rec-start-button"), "Starting recording...");
    await waitText(page, "#state-badge", "Recording");
    await waitText(page, "#control-message", "Recording started.");
    assert.equal(await page.isVisible("#recording-badge"), true);
    await waitText(page, "#recording-status", "Recording to rec_");
    assert.equal(await page.isDisabled("#stop-button"), false);
    assert.equal(await page.isDisabled("#rec-start-button"), true);
    await page.click("#rec-stop-button");
    assert.equal(await text(page, "#rec-stop-button"), "Saving recording...");
    await waitText(page, "#state-badge", "Detecting");
    await waitText(page, "#recording-status", "Not recording");
    assert.equal(await page.isVisible("#recording-badge"), false);
  });

  test("repeated clicks send one command", async () => {
    mock.engine.init_ms = 1000;
    await signInLegacy(page);
    await waitRole(page, "Operator");
    await page.selectOption("#source-select", "camera:0");
    await page.$eval("#start-button", (b) => {
      for (let i = 0; i < 5; i++) {
        b.disabled = false;
        b.click();
      }
    });
    await waitText(page, "#state-badge", "Detecting");
    const starts = monitorRequests("/monitor/start", "POST");
    assert.equal(starts.length, 1);
  });

  for (const [name, setup, expected] of [
    ["already running", () => (mock.fail["POST /monitor/start"] = [{ status: 409, body: { error: "monitoring is already running; stop it first", code: "already_running" } }]), "already running"],
    ["unknown source", () => (mock.fail["POST /monitor/start"] = [{ status: 400, body: { error: "source is not in the allowed source list", code: "unknown_source" } }]), "not available any more"],
    ["missing YOLO weights", () => (mock.fail["POST /monitor/start"] = [{ status: 503, body: { error: "model weights 'yolo11n.pt' are not installed", code: "model_unavailable" } }]), "YOLO model weights are not installed"],
    ["service down", () => (mock.fail["POST /monitor/start"] = [{ status: 503, body: { error: "monitor service is not running", code: "service_unavailable" } }]), "not running"],
  ]) {
    test("start refused: " + name, async () => {
      await signInLegacy(page);
      await waitRole(page, "Operator");
      setup();
      const sourcesBefore = monitorRequests("/monitor/sources").length;
      await page.selectOption("#source-select", "camera:0");
      await page.click("#start-button");
      await waitText(page, "#control-message", expected);
      assert.equal(await page.isDisabled("#start-button"), false, "can try again");
      assert.equal(await page.isVisible("#session"), true, "still signed in");
      if (name === "unknown source") await waitFor(() => monitorRequests("/monitor/sources").length > sourcesBefore, { message: "sources reload" });
    });
  }

  test("missing weights in the status disable Start", async () => {
    mock.engine.model_available = false;
    await signInLegacy(page);
    await waitRole(page, "Operator");
    await page.selectOption("#source-select", "camera:0");
    assert.equal(await page.isDisabled("#start-button"), true);
    await waitText(page, "#control-message", "YOLO weights 'yolo11n.pt' are not installed");
    await waitText(page, "#perf-model", "weights missing");
  });

  test("a camera that does not exist shows the engine error", async () => {
    mock.engine.state = "Error";
    mock.engine.last_error = "Camera #0 does not exist (no /dev/video0)";
    await signInLegacy(page);
    await waitText(page, "#state-badge", "Error");
    await waitText(page, "#monitoring-status", "Camera #0 does not exist");
    await waitText(page, "#feed-message", "Camera #0 does not exist");
    assert.equal(await page.isDisabled("#start-button"), false, "can start again");
  });

  test("no sources configured", async () => {
    mock.sources = [];
    await signInLegacy(page);
    await waitRole(page, "Operator");
    await waitText(page, "#control-message", "No sources are available");
    assert.equal(await page.isDisabled("#start-button"), true);
  });

  test("recording too early reports not ready", async () => {
    mock.engine.running = true;
    mock.engine.state = "Detecting";
    await signInLegacy(page);
    await waitRole(page, "Operator");
    mock.fail["POST /monitor/recording/start"] = [{ status: 409, body: { error: "still starting", code: "not_ready" } }];
    await page.click("#rec-start-button");
    await waitText(page, "#control-message", "still starting");
  });
});

describe("statistics and sessions", () => {
  test("renders the status counters with their real meaning", async () => {
    Object.assign(mock.engine, { running: true, state: "Detecting", session_id: "sess-live-1", source: mock.sources[1] });
    mock.engine.counts.visible = { person: 2, car: 3, motorcycle: 0, bicycle: 1, bus: 1, truck: 0 };
    mock.engine.counts.unique = { person: 7, car: 20, motorcycle: 1, bicycle: 2, bus: 2, truck: 3 };
    mock.engine.counts.crossings = { person: 1, car: 9, motorcycle: 0, bicycle: 1, bus: 1, truck: 2 };
    mock.engine.line_totals = { main: { a_to_b: { car: 5, bus: 1 }, b_to_a: { car: 4, truck: 2 } } };
    await signInLegacy(page);
    await waitText(page, "#stat-unique-vehicles", "26");
    assert.equal(await text(page, "#stat-visible-vehicles"), "4");
    assert.equal(await text(page, "#stat-crossing-vehicles"), "12");
    assert.equal(await text(page, "#stat-visible-pedestrians"), "2");
    assert.equal(await text(page, "#stat-unique-pedestrians"), "7");
    assert.equal(await text(page, "#stat-crossing-pedestrians"), "1");
    const rows = await page.$$eval("#class-body tr", (trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent)));
    assert.deepEqual(rows.find((r) => r[0] === "bicycle"), ["bicycle", "1", "2", "1"]);
    assert.equal(rows.length, 6);
    const lines = await page.$$eval("#lines-body tr", (trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent)));
    assert.deepEqual(lines, [["main", "6", "6"]]);
    assert.equal(await text(page, "#perf-inference-fps"), "12.3");
    assert.equal(await text(page, "#perf-latency"), "41.2 ms");
    assert.equal(await text(page, "#session-id"), "sess-live-1");
    assert.equal(await text(page, "#stats-scope"), "Live");
    assert.equal(await text(page, "#current-source"), "car-detection.mp4");
  });

  test("lists recent sessions with the fields the API returns", async () => {
    mock.sessions = [
      { session_id: "0123456789abcdef", source_type: "video_file", source_id: "car-detection.mp4", status: "completed", started_at: "2026-10-20T10:00:00", ended_at: "2026-10-20T10:01:05", error: null, model_name: "yolo11n.pt", device: "cpu", duration_s: 65, frames_processed: 900, unique: { person: 2, car: 5, motorcycle: 0, bicycle: 1, bus: 1, truck: 0 }, crossings: { person: 0, car: 3, motorcycle: 0, bicycle: 0, bus: 0, truck: 1 }, recordings: 1 },
      { session_id: "s2", source_type: "camera", source_id: "0", status: "error", started_at: "bad date", ended_at: null, error: "Camera #0 does not exist", model_name: "yolo11n.pt", device: null, duration_s: null, frames_processed: null, unique: null, crossings: null, recordings: 0 },
    ];
    await signInLegacy(page, "vw");
    await waitFor(async () => (await page.$$("#sessions-body tr")).length === 2, { message: "rows" });
    const rows = await page.$$eval("#sessions-body tr", (trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent)));
    assert.deepEqual(rows[0].slice(1), ["Video: car-detection.mp4", "completed", "1:05", "900", "6", "2", "4", "1", "0123456789ab…"]);
    assert.deepEqual(rows[1].slice(0, 9), ["bad date", "Camera: 0", "errorCamera #0 does not exist", "–", "–", "–", "–", "–", "0"]);
    assert.equal(await page.getAttribute("#sessions-body tr:first-child td:last-child", "title"), "0123456789abcdef");
    assert.equal(await page.$$eval("#sessions-panel a, .sessions-panel a, .sessions-panel button", (els) => els.map((e) => e.id).join()), "sessions-refresh", "no download or export controls");
  });

  test("untrusted text is rendered as text", async () => {
    const evil = '<img src=x onerror="window.__xss=1">';
    mock.sources = [{ id: "video:evil.mp4", kind: "video_file", label: evil }];
    Object.assign(mock.engine, { running: true, state: "Detecting", session_id: evil, message: evil, source: mock.sources[0] });
    mock.engine.line_totals = { [evil]: { a_to_b: { car: 1 }, b_to_a: {} } };
    mock.sessions = [{ session_id: evil, source_type: "video_file", source_id: evil, status: evil, started_at: evil, ended_at: null, error: evil, model_name: evil, device: evil, duration_s: 1, frames_processed: 1, unique: null, crossings: null, recordings: 0 }];
    await signInLegacy(page);
    await waitFor(async () => (await page.$$("#sessions-body tr")).length === 1, { message: "rows" });
    await waitText(page, "#monitoring-status", evil);
    assert.equal(await page.evaluate(() => window.__xss), undefined);
    assert.equal(await page.$$eval("main img", (imgs) => imgs.filter((i) => i.id !== "feed-image").length), 0);
    assert.equal(await page.$eval("#source-select option", (o) => o.textContent), evil);
    assert.equal(await text(page, "#lines-body td"), evil);
  });
});

describe("live feed", () => {
  async function startMonitoring(p) {
    mock.engine.init_ms = 0;
    await p.selectOption("#source-select", "camera:0");
    await p.click("#start-button");
    await waitText(p, "#state-badge", "Detecting");
  }

  test("polls authenticated JPEG frames at a bounded rate without overlap", async () => {
    await signInLegacy(page);
    await startMonitoring(page);
    await waitFor(() => page.evaluate(() => document.getElementById("feed-image").src.startsWith("blob:")), { message: "blob frame" });
    assert.equal(await page.isVisible("#feed-overlay"), false);
    const t0 = Date.now();
    const n0 = monitorRequests("/frame.jpg").length;
    await sleep(2000);
    const rate = (monitorRequests("/frame.jpg").length - n0) / ((Date.now() - t0) / 1000);
    assert.ok(rate <= 10.5 && rate >= 4, "frame rate " + rate.toFixed(1));
    const statusRate = monitorRequests("/monitor/status").filter((r) => r.time >= t0).length / 2;
    assert.ok(statusRate < rate, "status is polled more slowly than frames");
    await waitText(page, "#feed-meta", /^Frame \d+$/);
  });

  test("never holds more than one object URL", async () => {
    await signInLegacy(page);
    await startMonitoring(page);
    await waitFor(() => page.evaluate(() => window.__urls.created >= 10), { message: "10 frames" });
    const urls = await page.evaluate(() => ({ created: window.__urls.created, live: window.__urls.live.size }));
    assert.ok(urls.live <= 1, JSON.stringify(urls));
    await page.click("#stop-button");
    await waitText(page, "#state-badge", "Idle");
    assert.equal(await page.evaluate(() => window.__urls.live.size), 0);
    assert.equal(await page.isVisible("#feed-image"), false);
  });

  test("missing frames and a dropped service recover", async () => {
    mock.frameMissing = 4;
    await signInLegacy(page);
    await startMonitoring(page);
    await waitText(page, "#feed-message", "Waiting for the first frame");
    await waitFor(() => page.evaluate(() => document.getElementById("feed-image").src.startsWith("blob:")), { message: "first frame" });
    mock.fail["GET /monitor/frame.jpg"] = Array(2).fill({ status: 503, body: { error: "down", code: "service_unavailable" } });
    await waitText(page, "#feed-message", "Reconnecting");
    // The last frame stays visible under the notice while reconnecting.
    assert.equal(await page.isVisible("#feed-image"), true);
    await waitFor(() => page.isHidden("#feed-overlay"), { timeout: 8000, message: "overlay to clear" });
    assert.equal(await page.isVisible("#session"), true);
  });

  test("pauses frames while hidden and resumes when visible", async () => {
    await signInLegacy(page);
    await startMonitoring(page);
    await waitFor(() => monitorRequests("/frame.jpg").length > 3, { message: "frames" });
    await page.evaluate(() => {
      window.__hidden = true;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await sleep(300);
    const frames = monitorRequests("/frame.jpg").length;
    const statuses = monitorRequests("/monitor/status").length;
    await sleep(1500);
    assert.ok(monitorRequests("/frame.jpg").length - frames <= 1, "no frames while hidden");
    assert.ok(monitorRequests("/monitor/status").length - statuses <= 1, "status slows down while hidden");
    await page.evaluate(() => {
      window.__hidden = false;
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => monitorRequests("/frame.jpg").length > frames + 3, { message: "frames to resume" });
  });

  test("leaving the page or signing out stops polling and releases the frame", async () => {
    await signInLegacy(page);
    await startMonitoring(page);
    await waitFor(() => page.evaluate(() => window.__urls.live.size === 1), { message: "a frame" });
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: false })));
    await sleep(200);
    assert.equal(await page.evaluate(() => window.__urls.live.size), 0);
    const count = mock.requests.length;
    await sleep(1500);
    assert.equal(mock.requests.length, count, "no requests after pagehide");

    await page.reload();
    await openLegacy(page);
    await waitFor(() => page.evaluate(() => window.__urls.live.size === 1), { message: "a frame after reload" });
    await page.click("#logout-button");
    await sleep(200);
    const afterLogout = mock.requests.length;
    await sleep(1500);
    assert.equal(mock.requests.length, afterLogout, "no requests after sign-out");
    assert.equal(await page.evaluate(() => window.__urls.live.size), 0);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });

  test("two tabs share one monitoring session", async () => {
    await signInLegacy(page);
    const other = await newPage();
    await signInLegacy(other, "vw");
    await waitRole(other, "Viewer");
    await startMonitoring(page);
    await waitText(other, "#state-badge", "Detecting", 6000);
    await waitFor(() => other.evaluate(() => document.getElementById("feed-image").src.startsWith("blob:")), { message: "frames in tab 2" });
    // The operator tab's stop is seen by the viewer tab.
    await page.click("#stop-button");
    await waitText(other, "#state-badge", "Idle", 6000);
    await waitFor(() => other.isHidden("#feed-image"), { message: "tab 2 feed cleared" });
    // Each tab keeps its own sign-in (sessionStorage is per tab).
    assert.equal(await other.evaluate(() => JSON.parse(sessionStorage.getItem("atcs.session")).username), "vw");
    assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem("atcs.session")).username), "op");
  });

  test("works at phone width without horizontal overflow", async () => {
    await page.setViewportSize({ width: 375, height: 800 });
    await signInLegacy(page);
    await startMonitoring(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, "horizontal overflow " + overflow + "px");
    const box = await page.locator("#start-button").boundingBox();
    assert.ok(box.height >= 40, "touch-sized buttons");
    const feed = await page.locator("#feed").boundingBox();
    assert.ok(Math.abs(feed.width / feed.height - 16 / 9) < 0.05, "video keeps its aspect ratio");
  });
});

// ---------------------------------------------------------------- desktop

const LIVE = DESKTOP.device_recording;

function rowsOf(p, selector) {
  return p.$$eval(selector + " tr", (trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent.trim())));
}

async function desktopReady(p) {
  await p.waitForSelector("#session:not([hidden])");
  await waitFor(async () => !/Checking/.test(await text(p, "#desk-connection-badge")), { message: "first desktop status" });
}

async function signInDesktop(p, username = "op") {
  await signIn(p, username);
  await desktopReady(p);
}

async function rowCount(p, n) {
  await waitFor(async () => (await p.$$("#desk-sessions-body tr")).length === n, { message: n + " session rows" });
}

// Stored sessions as the backend lists them: running (live), completed,
// error (last live counts) and interrupted (statistics never saved).
function useHistory() {
  const kinds = ["running", "completed", "error", "interrupted"];
  mock.desktop.sessions = kinds.map((t) => deskSession(t));
  mock.desktop.details = Object.fromEntries(kinds.map((t) => [DESKTOP["detail_" + t].session_id, deskDetail(t)]));
  return mock.desktop.sessions;
}

describe("desktop monitor: launching", () => {
  test("the button is a fixed atcs-monitor://open link and nothing opens on page load", async () => {
    mock.desktop.devices = [deskDevice("idle", { ageS: 120 })];
    await signInDesktop(page);
    await sleep(1500);
    assert.equal(await page.getAttribute("#desk-open", "href"), "atcs-monitor://open");
    assert.equal(await text(page, "#desk-open"), "Open Desktop Monitor");
    assert.deepEqual(page.launches, [], "no launch without a click");
    assert.deepEqual(await page.evaluate(() => window.__launchClicks), []);
    assert.equal(await text(page, "#desk-launch-message"), "");
    // No frame, object or refresh that could point at the protocol.
    assert.equal(await page.$$eval("iframe, object, embed, meta[http-equiv]", (els) => els.length), 0);
    await page.reload();
    await desktopReady(page);
    await sleep(800);
    assert.deepEqual(page.launches, [], "no launch after a reload");
    assert.equal(mock.requests.filter((r) => r.method === "POST" && r.path.startsWith("/api/monitor")).length, 0);
  });

  test("a real click asks the browser to open the app and waits for a fresh heartbeat", async () => {
    const dev = deskDevice("idle", { ageS: 120 });
    mock.desktop.devices = [dev];
    await signInDesktop(page);
    await waitText(page, "#desk-connection-badge", "Disconnected");
    await page.click("#desk-open");
    await waitFor(() => page.launches.length === 1, { message: "browser asked to open the URI" });
    assert.equal(page.launches[0], "atcs-monitor://open");
    assert.deepEqual(await page.evaluate(() => window.__launchClicks), [{ href: "atcs-monitor://open", trusted: true }]);
    assert.equal(page.url(), base + "/monitor.html", "the page stays");
    await waitText(page, "#desk-launch-message", "Waiting for desktop connection");
    // Invoking the protocol proves nothing: no claim that the app runs yet.
    await sleep(1600);
    assert.match(await text(page, "#desk-launch-message"), /^Waiting for desktop connection/);
    assert.equal(await text(page, "#desk-connection-badge"), "Disconnected");
    heartbeat(dev);
    await waitText(page, "#desk-launch-message", "Connected: the desktop 'lab-pc' is reporting to ATCS.");
    await waitText(page, "#desk-connection-badge", "Connected");
    assert.equal(await page.isVisible("#desk-help"), false);
  });

  test("an already connected desktop is confirmed only by a heartbeat after the click", async () => {
    const dev = deskDevice("idle", { ageS: 4 });
    mock.desktop.devices = [dev];
    await signInDesktop(page);
    await waitText(page, "#desk-connection-badge", "Connected");
    await page.click("#desk-open");
    await waitText(page, "#desk-launch-message", "Waiting for desktop connection");
    await sleep(1600);
    assert.match(await text(page, "#desk-launch-message"), /^Waiting/, "an old heartbeat does not confirm the click");
    heartbeat(dev);
    await waitText(page, "#desk-launch-message", "Connected: the desktop 'lab-pc'");
  });

  test("script-generated clicks do not open anything", async () => {
    await signInDesktop(page);
    await page.$eval("#desk-open", (a) => a.click());
    await page.$eval("#desk-open", (a) => a.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })));
    await sleep(500);
    assert.deepEqual(page.launches, []);
    assert.deepEqual((await page.evaluate(() => window.__launchClicks)).map((c) => c.trusted), [false, false]);
    assert.equal(await text(page, "#desk-launch-message"), "");
  });

  test("keyboard: Enter on the focused link opens it", async () => {
    await signInDesktop(page);
    await page.focus("#desk-open");
    assert.equal(await page.evaluate(() => document.activeElement.id), "desk-open");
    await page.keyboard.press("Enter");
    await waitFor(() => page.launches.length === 1, { message: "keyboard launch" });
    assert.deepEqual((await page.evaluate(() => window.__launchClicks)).map((c) => c.trusted), [true]);
    await waitText(page, "#desk-launch-message", "Waiting for desktop connection");
  });

  test("without a connection, troubleshooting guidance appears and the page keeps waiting", async () => {
    await page.clock.install();
    const dev = deskDevice("never");
    mock.desktop.devices = [dev];
    await signInDesktop(page);
    assert.equal(await page.isVisible("#desk-help"), false, "no guidance before the button is used");
    await page.click("#desk-open");
    await waitText(page, "#desk-launch-message", "Waiting for desktop connection");
    await page.clock.fastForward(31000);
    await waitText(page, "#desk-launch-message", "Still waiting for a desktop connection");
    assert.equal(await page.isVisible("#desk-help"), true);
    const help = (await text(page, "#desk-help")).replace(/\s+/g, " ");
    assert.match(help, /install_uri_handler\.ps1/);
    assert.match(help, /computer showing this page/);
    assert.match(help, /python -m app\.desktop_link check/);
    // The desktop shows up late: the guidance goes away.
    dev.doc = structuredClone(DESKTOP.device_idle);
    dev.doc.desktop.name = "spare-laptop";
    heartbeat(dev);
    await waitText(page, "#desk-launch-message", "Connected: the desktop 'spare-laptop'", 8000);
    assert.equal(await page.isVisible("#desk-help"), false);
  });

  test("the waiting message is not rewritten while it stays the same (screen readers)", async () => {
    mock.desktop.devices = [deskDevice("idle", { ageS: 120 })];
    await signInDesktop(page);
    await page.click("#desk-open");
    await waitText(page, "#desk-launch-message", "Waiting for desktop connection");
    await page.evaluate(() => {
      window.__mutations = 0;
      new MutationObserver((list) => (window.__mutations += list.length))
        .observe(document.getElementById("desk-launch-message"), { childList: true, characterData: true, subtree: true });
    });
    await sleep(3500);
    assert.equal(await page.evaluate(() => window.__mutations), 0);
  });

  test("explains that the app opens on the browser's computer, not the server", async () => {
    await signInDesktop(page);
    const note = await text(page, ".launch-note");
    assert.match(note, /this computer/);
    assert.match(note, /not on the ATCS server/);
    assert.match(note, /browser may ask/);
  });
});

describe("desktop monitor: status and live statistics", () => {
  test("a recording desktop shows connected, monitoring, recording and live counts", async () => {
    mock.desktop.devices = [deskDevice("recording")];
    await signInDesktop(page, "vw");
    await waitText(page, "#desk-connection-badge", "Connected");
    assert.equal(await text(page, "#desk-connection"), "Connected");
    assert.equal(await page.isVisible("#desk-monitoring-badge"), true);
    assert.equal(await text(page, "#desk-monitoring-badge"), "Monitoring and recording");
    assert.equal(await page.isVisible("#desk-recording-badge"), true);
    assert.equal(await text(page, "#desk-monitoring"), "Monitoring and recording – " + LIVE.message);
    assert.equal(await text(page, "#desk-recording"), "Recording to " + LIVE.recording.file);
    assert.equal(await text(page, "#desk-device-name"), "alberto-windows");
    assert.equal(await text(page, "#desk-app"), "ATCS Monitor 1.0.0");
    assert.equal(await text(page, "#desk-source"), "Camera: Integrated Camera");
    assert.equal(await text(page, "#desk-camera"), "OK");
    assert.equal(await text(page, "#desk-inference"), "cuda:0");
    assert.equal(await text(page, "#desk-session"), LIVE.session_id);
    assert.match(await text(page, "#desk-last-update"), /\((just now|\d+ s ago)\)$/);
    assert.equal(await text(page, "#desk-stats-scope"), "Live");
    // Fixture (real backend output): visible person 1, car 3, bus 1; unique car 11,
    // motorcycle 1, bus 1, truck 2, bicycle 1, person 4; crossings car 6, truck 1, person 1.
    const tiles = {};
    for (const id of ["visible-vehicles", "unique-vehicles", "crossing-vehicles", "visible-pedestrians", "unique-pedestrians", "crossing-pedestrians"]) {
      tiles[id] = await text(page, "#desk-" + id);
    }
    assert.deepEqual(tiles, { "visible-vehicles": "4", "unique-vehicles": "15", "crossing-vehicles": "7", "visible-pedestrians": "1", "unique-pedestrians": "4", "crossing-pedestrians": "1" });
    assert.equal(tiles["unique-vehicles"], String(LIVE.totals.unique_vehicles));
    assert.deepEqual(await rowsOf(page, "#desk-class-body"), [
      ["Cars", "3", "11", "6"],
      ["Buses", "1", "1", "0"],
      ["Trucks", "0", "2", "1"],
      ["Motorcycles", "0", "1", "0"],
      ["Bicycles", "0", "1", "0"],
      ["Pedestrians", "1", "4", "1"],
    ]);
    assert.equal(await text(page, "#desk-inference-fps"), "10.4");
    assert.equal(await text(page, "#desk-capture-fps"), "29.8");
    assert.equal(await text(page, "#desk-tracks"), "3");
    assert.equal(await text(page, "#desk-latency"), "11.2 ms");
    assert.equal(await text(page, "#desk-frames"), "1500");
    assert.equal(await text(page, "#desk-dropped"), "0");
  });

  test("an idle desktop is connected but its counts are unknown, not zero", async () => {
    mock.desktop.devices = [deskDevice("idle")];
    await signInDesktop(page);
    await waitText(page, "#desk-connection-badge", "Connected");
    assert.equal(await page.isVisible("#desk-monitoring-badge"), false);
    assert.equal(await page.isVisible("#desk-recording-badge"), false);
    assert.equal(await text(page, "#desk-monitoring"), "Idle – Stopped");
    assert.equal(await text(page, "#desk-recording"), "Not recording");
    assert.equal(await text(page, "#desk-camera"), "No camera in use");
    assert.equal(await text(page, "#desk-stats-scope"), "Desktop idle: no monitoring session");
    // The backend sends zeros here; without a session they mean "nothing reported".
    for (const id of ["#desk-visible-vehicles", "#desk-unique-vehicles", "#desk-crossing-pedestrians", "#desk-inference-fps", "#desk-tracks", "#desk-frames"]) {
      assert.equal(await text(page, id), "–", id);
    }
  });

  test("zero counts of a live session are shown as 0; missing fields as –", async () => {
    const zero = { person: 0, car: 0, motorcycle: 0, bicycle: 0, bus: 0, truck: 0 };
    const dev = deskDevice("recording", { visible: { ...zero }, unique: { ...zero }, crossings: { ...zero } });
    dev.doc.totals = { visible_vehicles: 0, unique_vehicles: 0, crossing_vehicles: 0, visible_pedestrians: 0, unique_pedestrians: 0 };
    dev.doc.performance = { inference_fps: 0, capture_fps: 0, frames_processed: 0 };
    mock.desktop.devices = [dev];
    await signInDesktop(page);
    await waitText(page, "#desk-stats-scope", "Live");
    assert.equal(await text(page, "#desk-unique-vehicles"), "0");
    assert.equal(await text(page, "#desk-visible-pedestrians"), "0");
    assert.equal(await text(page, "#desk-crossing-pedestrians"), "–", "field missing from the answer");
    assert.equal(await text(page, "#desk-inference-fps"), "0.0");
    assert.equal(await text(page, "#desk-tracks"), "–", "active_tracks missing");
    assert.equal(await text(page, "#desk-latency"), "–");
    assert.equal(await text(page, "#desk-frames"), "0");
    assert.deepEqual((await rowsOf(page, "#desk-class-body"))[0], ["Cars", "0", "0", "0"]);
  });

  test("a stale heartbeat is disconnected: live values are not shown as current", async () => {
    mock.desktop.devices = [deskDevice("recording", { ageS: 20 })];
    await signInDesktop(page);
    await waitText(page, "#desk-connection-badge", "Disconnected");
    assert.equal(await text(page, "#desk-connection"), "Disconnected");
    assert.equal(await page.isVisible("#desk-monitoring-badge"), false);
    assert.equal(await page.isVisible("#desk-recording-badge"), false);
    assert.equal(await text(page, "#desk-monitoring"), "Not reporting (last reported: Monitoring and recording)");
    assert.equal(await text(page, "#desk-recording"), "Unknown (desktop not connected)");
    assert.match(await text(page, "#desk-last-update"), /\(2\d s ago\)$/);
    assert.equal(await text(page, "#desk-stats-scope"), "Last reported values: desktop not connected");
    assert.equal(await text(page, "#desk-visible-vehicles"), "–");
    assert.equal(await text(page, "#desk-inference-fps"), "–");
    assert.equal(await text(page, "#desk-camera"), "–");
    assert.equal(await text(page, "#desk-unique-vehicles"), "15", "cumulative values of the last report stay");
    assert.equal(await text(page, "#desk-source"), "Camera: Integrated Camera (last reported)");
  });

  test("closing the desktop: the state changes once the heartbeat times out", async () => {
    mock.desktop.devices = [deskDevice("recording", { ageS: 12 })];
    await signInDesktop(page);
    await waitText(page, "#desk-connection-badge", "Connected");
    assert.equal(await text(page, "#desk-stats-scope"), "Live");
    // No further heartbeats: 15 s after the last one the page shows the desktop as gone.
    await waitText(page, "#desk-connection-badge", "Disconnected", 8000);
    assert.equal(await page.isVisible("#desk-recording-badge"), false);
    assert.equal(await text(page, "#desk-visible-vehicles"), "–");
  });

  test("no registered desktop and a never-connected desktop", async () => {
    await signInDesktop(page);
    assert.equal(await text(page, "#desk-connection"), "No desktop registered");
    assert.match(await text(page, "#desk-status-message"), /monitor_access\.py device add/);
    mock.desktop.devices = [deskDevice("never")];
    await page.reload();
    await desktopReady(page);
    await waitText(page, "#desk-connection", "Never connected");
    assert.equal(await text(page, "#desk-last-update"), "Never");
    assert.equal(await text(page, "#desk-stats-scope"), "No data reported yet");
    for (const id of ["#desk-unique-vehicles", "#desk-crossing-vehicles", "#desk-frames", "#desk-monitoring", "#desk-recording"]) {
      assert.equal(await text(page, id), "–", id);
    }
  });

  test("with several desktops the live one is shown and another can be chosen", async () => {
    mock.desktop.devices = [deskDevice("never"), deskDevice("idle"), deskDevice("recording")];
    await signInDesktop(page);
    await waitText(page, "#desk-device-name", "alberto-windows");
    assert.equal(await page.isVisible("#desk-device"), true);
    assert.deepEqual(await page.$$eval("#desk-device option", (os) => os.map((o) => o.value)), ["spare-laptop", "lab-pc", "alberto-windows"]);
    await page.selectOption("#desk-device", "lab-pc");
    await waitText(page, "#desk-device-name", "lab-pc");
    assert.equal(await text(page, "#desk-stats-scope"), "Desktop idle: no monitoring session");
    await sleep(2500);
    assert.equal(await text(page, "#desk-device-name"), "lab-pc", "the choice survives polling");
  });

  test("an error reported by the desktop is shown", async () => {
    mock.desktop.devices = [deskDevice("idle", { state: "Error", last_error: "Camera 'Integrated Camera' disconnected", camera_status: "error" })];
    await signInDesktop(page);
    await waitText(page, "#desk-monitoring", "Error – Camera 'Integrated Camera' disconnected");
    assert.match(await text(page, "#desk-status-message"), /reported an error/);
    assert.equal(await text(page, "#desk-camera"), "Error");
  });
});

describe("desktop monitor: sessions", () => {
  test("lists stored sessions with what is known about each", async () => {
    const [running, completed] = useHistory();
    // The same session twice in one answer is shown once.
    mock.desktop.sessions.push(structuredClone(completed));
    // A running session whose desktop went quiet.
    mock.desktop.sessions.splice(1, 0, { ...structuredClone(running), session_id: "f".repeat(32), live: false });
    await signInDesktop(page, "vw");
    await rowCount(page, 5);
    const rows = await rowsOf(page, "#desk-sessions-body");
    assert.deepEqual(rows.map((r) => r.slice(1)), [
      ["alberto-windows", "Camera: Integrated Camera", "Live", "In progress", "15", "4", "7", "1", "–", "Live, in progress", "Details"],
      ["alberto-windows", "Camera: Integrated Camera", "Not reporting", "In progress", "15", "4", "7", "1", "–", "Pending: the desktop stopped reporting; the summary arrives when it reconnects", "Details"],
      ["alberto-windows", "Video: car-detection.mp4", "Completed", "0:43", "26", "7", "13", "2", "1", "Complete", "Details"],
      ["lab-pc", "Camera: Integrated Camera", "Error", "–", "4", "1", "0", "0", "0", "Incomplete: unique counts from the last live update", "Details"],
      ["lab-pc", "Camera: Integrated Camera", "Interrupted", "–", "–", "–", "0", "0", "0", "Incomplete: unique counts were not saved", "Details"],
    ]);
    // Started: the desktop's timestamp in the browser's local time; the original is the tooltip.
    const [shown, original] = await page.$eval(`#desk-sessions-body tr[data-session-id="${completed.session_id}"] td`, (td) => [td.textContent, td.title]);
    assert.equal(original, completed.started_at);
    assert.equal(shown, await page.evaluate((iso) => new Date(iso).toLocaleString(), completed.started_at));
    // No playback or download links: the server stores telemetry, not video.
    assert.equal(await page.$$eval(".desk-sessions-panel a, .desk-sessions-panel video, .desk-sessions-panel [download]", (e) => e.length), 0);
  });

  test("history survives a reload and an offline desktop", async () => {
    useHistory();
    mock.desktop.devices = [deskDevice("recording", { ageS: 300 })];
    await signInDesktop(page);
    await rowCount(page, 4);
    await page.reload();
    await desktopReady(page);
    await rowCount(page, 4);
    assert.equal(await text(page, "#desk-connection-badge"), "Disconnected");
    assert.equal(await text(page, "#desk-sessions-message"), "");
  });

  test("an empty history explains how sessions get here", async () => {
    await signInDesktop(page);
    await waitText(page, "#desk-sessions-message", "No desktop sessions saved yet");
    assert.match(await text(page, "#desk-sessions-message"), /stays after the app is closed/);
  });

  test("filters by desktop with the device parameter", async () => {
    useHistory();
    mock.desktop.devices = [deskDevice("idle"), deskDevice("recording"), deskDevice("never")];
    await signInDesktop(page);
    await rowCount(page, 4);
    assert.deepEqual(await page.$$eval("#desk-filter option", (os) => os.map((o) => o.value)), ["", "alberto-windows", "lab-pc", "spare-laptop"]);
    await page.selectOption("#desk-filter", "lab-pc");
    await rowCount(page, 2);
    assert.ok(monitorRequests("/api/monitor/desktop/sessions").some((r) => r.query === "?device=lab-pc"));
    assert.deepEqual((await rowsOf(page, "#desk-sessions-body")).map((r) => r[1]), ["lab-pc", "lab-pc"]);
    await page.selectOption("#desk-filter", "spare-laptop");
    await waitText(page, "#desk-sessions-message", "No sessions saved for the desktop 'spare-laptop' yet.");
    assert.equal((await page.$$("#desk-sessions-body tr")).length, 0);
  });

  test("session details: complete, incomplete, keyboard and focus", async () => {
    useHistory();
    await signInDesktop(page);
    await rowCount(page, 4);
    const completedId = DESKTOP.detail_completed.session_id;
    await page.focus(`#desk-sessions-body tr[data-session-id="${completedId}"] button`);
    await page.keyboard.press("Enter");
    await waitText(page, "#dd-session", completedId);
    assert.equal(await page.evaluate(() => document.activeElement.id), "desk-detail-title");
    assert.equal(await text(page, "#dd-status"), "Completed");
    assert.equal(await text(page, "#dd-stats"), "Complete");
    assert.equal(await text(page, "#dd-frames"), "1830");
    assert.equal(await text(page, "#dd-duration"), "0:43");
    assert.equal(await text(page, "#dd-fps"), "10.2");
    assert.equal(await text(page, "#dd-model"), "yolo26n.pt on cuda:0");
    // Uploaded after the fact, never reported live: the backend has no app version for it.
    assert.equal(DESKTOP.detail_completed.provenance.app_version, null);
    assert.equal(await text(page, "#dd-app"), "–");
    assert.equal(await text(page, "#dd-events"), "16");
    const classes = await rowsOf(page, "#dd-class-body");
    assert.deepEqual(classes.slice(0, 2), [["Cars", "20", "9"], ["Buses", "2", "1"]]);
    assert.deepEqual(classes.at(-1), ["All vehicles", "26", "13"]);
    assert.deepEqual(await rowsOf(page, "#dd-lines-body"), [["line-1", "5", "2"]]);
    assert.deepEqual(await page.$$eval("#dd-recordings li", (lis) => lis.map((l) => l.textContent)), ["atcs_20261023-120000_3f1c0d6e.mp4 – saved, 0:40, avc1"]);
    assert.equal(await page.$$eval("#desk-detail a, #desk-detail video", (e) => e.length), 0, "names only, no links");
    await page.keyboard.press("Escape");
    assert.equal(await page.isVisible("#desk-detail"), false);
    assert.equal(await page.evaluate(() => document.activeElement.closest("tr").dataset.sessionId), completedId, "focus returns to the row");

    const crashId = DESKTOP.detail_interrupted.session_id;
    await page.click(`#desk-sessions-body tr[data-session-id="${crashId}"] button`);
    await waitText(page, "#dd-session", crashId);
    assert.equal(await text(page, "#dd-stats"), "Incomplete: unique counts were not saved");
    for (const id of ["#dd-duration", "#dd-frames", "#dd-fps"]) assert.equal(await text(page, id), "–", id);
    assert.deepEqual((await rowsOf(page, "#dd-class-body"))[0], ["Cars", "–", "0"]);
    assert.equal(await text(page, "#dd-recordings-empty"), "No recordings in this session.");
    await page.click("#desk-detail-close");
    assert.equal(await page.isVisible("#desk-detail"), false);

    const partialId = DESKTOP.detail_error.session_id;
    await page.click(`#desk-sessions-body tr[data-session-id="${partialId}"] button`);
    await waitText(page, "#dd-session", partialId);
    assert.equal(await text(page, "#dd-frames"), "240 (last live update)");
    assert.equal(await text(page, "#dd-duration"), "–");
    assert.equal(await text(page, "#dd-app"), "1.0.0", "seen live, so the app version is known");
  });

  test("a session that is no longer stored reports 404 in the details", async () => {
    useHistory();
    mock.desktop.details = {};
    await signInDesktop(page);
    await rowCount(page, 4);
    await page.click("#desk-sessions-body tr:nth-child(2) button");
    await waitText(page, "#desk-detail-message", "not stored on the server any more");
    assert.equal(await page.isVisible("#desk-detail-body"), false);
  });

  test("a finished live session moves to the history without a manual refresh", async () => {
    const dev = deskDevice("recording");
    mock.desktop.devices = [dev];
    mock.desktop.sessions = [deskSession("running")];
    await signInDesktop(page);
    await waitText(page, "#desk-sessions-body", "Live, in progress");
    // The desktop stops: idle heartbeat, then its summary is stored.
    dev.doc = structuredClone(DESKTOP.device_idle);
    dev.doc.desktop.name = "alberto-windows";
    heartbeat(dev);
    mock.desktop.sessions = [{ ...deskSession("completed"), session_id: DESKTOP.session_running.session_id }];
    await waitText(page, "#desk-sessions-body", "Complete", 8000);
    assert.equal(await text(page, "#desk-stats-scope"), "Desktop idle: no monitoring session");
  });
});

describe("desktop monitor: failures, access and security", () => {
  test("status failures are reported and the page recovers", async () => {
    mock.desktop.devices = [deskDevice("recording")];
    mock.fail["GET /monitor/desktop/status"] = Array(3).fill({ status: 503, body: { error: "database is locked" } });
    await signInDesktop(page);
    await waitText(page, "#desk-status-message", "could not read the desktop status (503)");
    assert.equal(await page.isVisible("#session"), true, "not a sign-out");
    await waitText(page, "#desk-connection-badge", "Connected", 15000);
    assert.equal(await text(page, "#desk-status-message"), "");
  });

  test("a server that stops answering never leaves a stale Connected", async () => {
    await page.clock.install();
    mock.desktop.devices = [deskDevice("recording")];
    await signInDesktop(page);
    await waitText(page, "#desk-connection-badge", "Connected");
    mock.fail["GET /monitor/desktop/status"] = Array(500).fill({ status: 502, body: { error: "bad gateway" } });
    await page.clock.fastForward(16000);
    await waitText(page, "#desk-connection-badge", "Status unknown");
    assert.match(await text(page, "#desk-connection"), /^Unknown: the ATCS server has not answered/);
    assert.equal(await page.isVisible("#desk-recording-badge"), false);
    assert.equal(await text(page, "#desk-visible-vehicles"), "–");
  });

  test("a failed sessions reload keeps the history that was shown", async () => {
    useHistory();
    await signInDesktop(page);
    await rowCount(page, 4);
    mock.fail["GET /monitor/desktop/sessions"] = [{ status: 500, body: { error: "x" } }];
    await page.click("#desk-sessions-refresh");
    await waitText(page, "#desk-sessions-message", "last successful update");
    assert.equal((await page.$$("#desk-sessions-body tr")).length, 4);
    await page.click("#desk-sessions-refresh");
    await waitText(page, "#desk-sessions-message", /^$/);
    assert.equal((await page.$$("#desk-sessions-body tr")).length, 4);
  });

  test("403 from the desktop endpoints shows access denied; 401 signs out", async () => {
    mock.fail["GET /monitor/desktop/status"] = [{ status: 403, body: { error: "not authorized for the monitor" } }];
    await signIn(page);
    await page.waitForSelector("#denied-panel:not([hidden])");
    assert.equal(await page.isVisible("#monitor"), false);
    assert.notEqual(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
    await page.click("#denied-retry");
    await waitRole(page, "Operator");
    mock.fail["GET /monitor/desktop/sessions"] = [{ status: 401, body: { error: "authentication required" } }];
    await page.click("#desk-sessions-refresh");
    await waitText(page, "#login-message", "Your session has expired");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });

  test("an account without a monitor grant sees no desktop data", async () => {
    mock.desktop.devices = [deskDevice("recording")];
    useHistory();
    await signIn(page, "ng");
    await page.waitForSelector("#denied-panel:not([hidden])");
    await sleep(800);
    assert.equal(await page.isVisible("#desk-open"), false);
    assert.equal(await text(page, "#desk-sessions-body"), "");
    assert.equal(await text(page, "#desk-device-name"), "–");
  });

  for (const [user, role] of [["op", "Operator"], ["vw", "Viewer"]]) {
    test(role.toLowerCase() + ": desktop data is read-only and the launch link needs no server permission", async () => {
      mock.desktop.devices = [deskDevice("recording")];
      useHistory();
      await signInDesktop(page, user);
      await waitRole(page, role);
      await waitText(page, "#desk-connection-badge", "Connected");
      await page.click("#desk-open");
      await page.click("#desk-sessions-body button");
      await waitFor(() => page.isVisible("#desk-detail-body"), { message: "details" });
      await sleep(500);
      // Reads only: nothing but GETs after sign-in, and no call to the device endpoints.
      assert.deepEqual(mock.requests.filter((r) => r.method !== "GET" && r.path !== "/api/auth/login"), []);
      assert.equal(mock.requests.filter((r) => r.path.startsWith("/api/desktop/")).length, 0);
    });
  }

  test("untrusted desktop text and paths are rendered as plain text", async () => {
    const evil = '<img src=x onerror="window.__xss=1">';
    const dev = deskDevice("recording", { message: evil, source: { kind: "camera", label: evil } });
    dev.doc.recording.file = "C:\\Users\\alberto\\Videos\\private\\rec.mp4";
    dev.doc.desktop.app = { name: evil, version: evil };
    mock.desktop.devices = [dev];
    const s = deskSession("completed", { source_id: evil, device: evil });
    mock.desktop.sessions = [s];
    mock.desktop.details = {
      [s.session_id]: deskDetail("completed", {
        source_id: evil,
        model: { name: "/opt/models/yolo.pt", version: evil, device: evil },
        recordings: [{ file: "/home/alberto/rec/a.mp4", ok: true, duration_s: 3, codec: evil }],
      }),
    };
    await signInDesktop(page);
    await waitText(page, "#desk-monitoring", evil);
    assert.equal(await text(page, "#desk-recording"), "Recording to rec.mp4", "no local path");
    assert.equal(await text(page, "#desk-source"), "Camera: " + evil);
    await page.click("#desk-sessions-body button");
    await waitText(page, "#dd-model", "yolo.pt");
    assert.match(await text(page, "#dd-recordings"), /^a\.mp4 – saved/);
    assert.ok(!(await text(page, "#desk-detail")).includes("/home/alberto"));
    assert.equal(await page.evaluate(() => window.__xss), undefined);
    assert.equal(await page.$$eval("main img", (imgs) => imgs.filter((i) => i.id !== "feed-image").length), 0);
  });
});

describe("desktop monitor: legacy panel", () => {
  test("the legacy monitor is collapsed: one status probe, no polling until opened", async () => {
    await signInDesktop(page);
    assert.equal(await page.$eval("#legacy-panel", (d) => d.open), false);
    assert.equal(await text(page, "#legacy-panel > summary .legacy-title"), "Server-side Monitor — Legacy");
    await waitText(page, "#legacy-summary-state", "(Available, idle)");
    await sleep(3500);
    assert.equal(monitorRequests("/api/monitor/status").length, 1);
    for (const path of ["/api/monitor/sources", "/api/monitor/sessions", "/frame.jpg"]) assert.equal(monitorRequests(path).length, 0, path);
    // Keyboard: the summary opens and closes the panel.
    await page.focus("#legacy-panel > summary");
    await page.keyboard.press("Enter");
    await waitFor(() => monitorRequests("/api/monitor/sources").length === 1, { message: "sources on open" });
    await waitFor(() => monitorRequests("/api/monitor/status").length >= 3, { timeout: 8000, message: "status polling while open" });
    await page.keyboard.press("Enter");
    assert.equal(await page.$eval("#legacy-panel", (d) => d.open), false);
    await sleep(300);
    const n = monitorRequests("/api/monitor/status").length;
    await sleep(3500);
    assert.equal(monitorRequests("/api/monitor/status").length, n, "polling stops when closed");
  });

  test("an unconfigured legacy service renders no controls", async () => {
    mock.serviceConfigured = false;
    await signInDesktop(page);
    await waitText(page, "#legacy-summary-state", "(Not configured)");
    await openLegacy(page);
    await waitText(page, "#legacy-note", "not configured on this ATCS server");
    assert.equal(await page.isVisible("#legacy-layout"), false);
    for (const id of ["#start-button", "#source-select", "#rec-start-button"]) assert.equal(await page.isVisible(id), false, id);
    assert.equal(monitorRequests("/api/monitor/", "POST").length, 0);
    assert.equal(await page.isVisible("#desk-open"), true, "the desktop part is unaffected");
  });

  test("an unreachable legacy service hides its controls until it answers", async () => {
    mock.fail["GET /monitor/status"] = Array(4).fill({ status: 503, body: { error: "x", code: "service_unavailable" } });
    await signInDesktop(page);
    await waitText(page, "#legacy-summary-state", "(Unavailable)");
    await openLegacy(page);
    assert.equal(await page.isVisible("#legacy-controls"), false);
    assert.equal(await page.isVisible("#legacy-note"), true);
    await waitFor(() => page.isVisible("#start-button"), { timeout: 10000, message: "controls after recovery" });
    assert.equal(await page.isVisible("#legacy-note"), false);
  });

  test("a desktop camera session blocks starting a server camera from this page", async () => {
    mock.desktop.devices = [deskDevice("recording")];
    await signInDesktop(page);
    await openLegacy(page);
    await waitRole(page, "Operator");
    await page.selectOption("#source-select", "camera:0");
    await waitText(page, "#legacy-conflict", "The desktop 'alberto-windows' is monitoring a camera");
    assert.match(await text(page, "#legacy-conflict"), /the server does not prevent it/);
    assert.equal(await page.isDisabled("#start-button"), true);
    await page.$eval("#start-button", (b) => {
      b.disabled = false;
      b.click();
    });
    await sleep(300);
    assert.equal(monitorRequests("/monitor/start", "POST").length, 0);
    // A video file is not a camera: allowed.
    await page.selectOption("#source-select", "video:car-detection.mp4");
    assert.equal(await page.isDisabled("#start-button"), false);
    assert.equal(await page.isVisible("#legacy-conflict"), false);
  });

  test("a server camera session is pointed out next to the desktop button", async () => {
    Object.assign(mock.engine, { running: true, state: "Detecting", session_id: "sess-x", source: mock.sources[0] });
    await signInDesktop(page);
    await waitText(page, "#desk-legacy-camera", "The server-side legacy monitor is using a camera (Camera 0)");
    assert.equal(await page.isVisible("#desk-legacy-camera"), true);
  });
});

describe("desktop monitor: layout and accessibility", () => {
  for (const [w, h] of [[1280, 900], [375, 800]]) {
    test("no horizontal overflow at " + w + " px with live data and details open", async () => {
      await page.setViewportSize({ width: w, height: h });
      mock.desktop.devices = [deskDevice("recording"), deskDevice("idle")];
      useHistory();
      await signInDesktop(page);
      await rowCount(page, 4);
      await page.click("#desk-sessions-body button");
      await waitFor(() => page.isVisible("#desk-detail-body"), { message: "details" });
      await openLegacy(page);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 0, "horizontal overflow " + overflow + "px");
      const open = await page.locator("#desk-open").boundingBox();
      assert.ok(open.height >= 40, "touch-sized launch button");
      const conn = await page.locator(".desk-connection-panel").boundingBox();
      const stats = await page.locator(".desk-stats-panel").boundingBox();
      if (w < 500) {
        assert.ok(open.width >= 300, "full-width launch button on phones");
        assert.ok(stats.y >= conn.y + conn.height - 1, "panels stack on phones");
      } else {
        assert.ok(Math.abs(stats.y - conn.y) < 2, "side by side on wide screens");
      }
    });
  }

  test("controls have names, live regions announce, and tab order starts at the launch link", async () => {
    mock.desktop.devices = [deskDevice("recording"), deskDevice("idle")];
    useHistory();
    await signInDesktop(page);
    await rowCount(page, 4);
    const unnamed = await page.$$eval("select, button, a[href], summary", (els) => els
      .filter((e) => e.offsetParent !== null)
      .filter((e) => !(e.labels && e.labels.length) && !(e.getAttribute("aria-label") || e.textContent.trim()))
      .map((e) => e.outerHTML.slice(0, 80)));
    assert.deepEqual(unnamed, []);
    assert.equal(await page.getAttribute("#desk-launch-message", "role"), "status");
    assert.equal(await page.getAttribute("#desk-launch-message", "aria-live"), "polite");
    assert.match(await page.getAttribute("#desk-sessions-body button", "aria-label"), /^Details of the session started /);
    const headings = await page.$$eval("main h1", (hs) => hs.filter((h) => h.offsetParent !== null).map((h) => h.textContent));
    assert.deepEqual(headings, ["Desktop Monitor"]);
    // From the sign-out button, the next stop is the launch link.
    await page.focus("#logout-button");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.id), "desk-open");
  });

  test("times are shown in the browser's time zone", async () => {
    await context.close();
    context = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: "Asia/Tokyo", locale: "en-US" });
    page = await newPage();
    const [, completed] = useHistory();
    await signInDesktop(page);
    await waitText(page, "#desk-tz", "Asia/Tokyo");
    const shown = await page.$eval(`#desk-sessions-body tr[data-session-id="${completed.session_id}"] td`, (td) => td.textContent);
    const norm = (t) => t.replace(/\s/g, " ");
    const tokyo = new Date(completed.started_at).toLocaleString("en-US", { timeZone: "Asia/Tokyo" });
    const utc = new Date(completed.started_at).toLocaleString("en-US", { timeZone: "UTC" });
    assert.equal(norm(shown), norm(tokyo));
    assert.notEqual(norm(shown), norm(utc));
  });
});

describe("dashboard regression", () => {
  test("the dashboard still signs in and restores its session through js/auth.js", async () => {
    await page.goto(base + "/dashboard.html");
    await page.fill("#username", "op");
    await page.fill("#password", "pw");
    await page.click("#login-button");
    await page.waitForSelector("#dashboard:not([hidden])");
    await page.reload();
    await page.waitForSelector("#dashboard:not([hidden])");
    assert.equal(await text(page, "#session-user"), "op");
    await page.click("#logout-button");
    await page.waitForSelector("#login-panel:not([hidden])");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("atcs.session")), null);
  });
});
