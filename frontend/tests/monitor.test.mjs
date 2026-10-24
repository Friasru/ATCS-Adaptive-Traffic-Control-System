// Browser tests for the Monitor page (and the navigation and session handling it
// shares with the Dashboard). Run with: cd frontend/tests && npm install && npm test

import { after, afterEach, before, beforeEach, describe, test } from "node:test";
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { makeToken, mock, resetMock, startServer } from "./mock-server.mjs";

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
      await signIn(page);
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
    await signIn(page);
    await page.selectOption("#source-select", "video:car-detection.mp4");
    await page.click("#start-button");
    await waitFor(() => monitorRequests("/frame.jpg").length > 3, { message: "frames" });
    const token = JSON.parse(await page.evaluate(() => sessionStorage.getItem("atcs.session"))).token;
    for (const r of monitorRequests("/api/monitor/")) {
      assert.equal(r.auth, "Bearer " + token, r.method + " " + r.path);
      assert.ok(!r.query.includes(token) && !r.path.includes(token));
    }
  });
});

describe("roles", () => {
  for (const [user, role] of [["op", "Operator"], ["vw", "Viewer"]]) {
    test(role.toLowerCase() + " permissions come from GET /api/monitor/access, with no POST on load", async () => {
      await signIn(page, user);
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
    await signIn(page);
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
    await signIn(page, "vw");
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
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
      await signIn(page);
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
    await signIn(page);
    await waitRole(page, "Operator");
    await page.selectOption("#source-select", "camera:0");
    assert.equal(await page.isDisabled("#start-button"), true);
    await waitText(page, "#control-message", "YOLO weights 'yolo11n.pt' are not installed");
    await waitText(page, "#perf-model", "weights missing");
  });

  test("a camera that does not exist shows the engine error", async () => {
    mock.engine.state = "Error";
    mock.engine.last_error = "Camera #0 does not exist (no /dev/video0)";
    await signIn(page);
    await waitText(page, "#state-badge", "Error");
    await waitText(page, "#monitoring-status", "Camera #0 does not exist");
    await waitText(page, "#feed-message", "Camera #0 does not exist");
    assert.equal(await page.isDisabled("#start-button"), false, "can start again");
  });

  test("no sources configured", async () => {
    mock.sources = [];
    await signIn(page);
    await waitRole(page, "Operator");
    await waitText(page, "#control-message", "No sources are available");
    assert.equal(await page.isDisabled("#start-button"), true);
  });

  test("recording too early reports not ready", async () => {
    mock.engine.running = true;
    mock.engine.state = "Detecting";
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page, "vw");
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
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
    await signIn(page);
    await startMonitoring(page);
    await waitFor(() => page.evaluate(() => window.__urls.live.size === 1), { message: "a frame" });
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: false })));
    await sleep(200);
    assert.equal(await page.evaluate(() => window.__urls.live.size), 0);
    const count = mock.requests.length;
    await sleep(1500);
    assert.equal(mock.requests.length, count, "no requests after pagehide");

    await page.reload();
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
    await signIn(page);
    const other = await newPage();
    await signIn(other, "vw");
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
    await signIn(page);
    await startMonitoring(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, "horizontal overflow " + overflow + "px");
    const box = await page.locator("#start-button").boundingBox();
    assert.ok(box.height >= 40, "touch-sized buttons");
    const feed = await page.locator("#feed").boundingBox();
    assert.ok(Math.abs(feed.width / feed.height - 16 / 9) < 0.05, "video keeps its aspect ratio");
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
