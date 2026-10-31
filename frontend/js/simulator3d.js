// 3D Simulator page. The simulation itself runs in the local simulation3d service (SUMO + the ATCS controllers + the
// three.js renderer), which this page shows in a frame. Its WebSocket belongs to the service's own origin, so the page
// only has to get the service started, say whether it is reachable and load it. It never sends anything to the simulation.
//
// The web app starts the service when this page asks for it (POST /api/simulator3d/start) and tells the page which port
// it listens on. With ?port=NNNN the page does not ask: it only looks for a service somebody started by hand.
(function () {
  "use strict";

  var DEFAULT_PORT = 8766; // simulation3d/run.sh; the Monitor service uses 8765
  var POLL_UP_MS = 5000;
  var POLL_DOWN_MS = 2000;
  var POLL_STARTING_MS = 500;
  var PROBE_TIMEOUT_MS = 2500;

  var stage = document.getElementById("stage");
  var unavailable = document.getElementById("unavailable");
  var reason = document.getElementById("unavailable-reason");
  var reasonDetail = document.getElementById("unavailable-detail");
  var retry = document.getElementById("retry");
  var pill = document.getElementById("service-pill");
  var detail = document.getElementById("service-detail");
  var openTab = document.getElementById("open-tab");

  // ?port=NNNN points the page at a service started by hand on another port; the other parameters (for example
  // ?shadows=0&dpr=1 for a slow graphics card) are handed on to the 3D page.
  var params = new URLSearchParams(window.location.search);
  var port = DEFAULT_PORT;
  var requested = parseInt(params.get("port"), 10);
  var managed = !(requested >= 1024 && requested <= 65535);
  if (!managed) {
    port = requested;
  }
  params.delete("port");
  var query = params.toString();

  // The service only listens on this computer and is plain http, so use the host the page itself was opened on.
  var origin, address, url;
  var framed = null; // the address shown in the frame
  var retryRequested = false;
  var timer = null;

  function setPort(p) {
    port = p;
    origin = "http://" + window.location.hostname + ":" + port;
    address = window.location.hostname + ":" + port;
    url = origin + "/" + (query ? "?" + query : "");
  }

  function show(state, text) {
    pill.className = "pill " + state;
    pill.textContent = { ok: "Service running", down: "Service not running", checking: "Checking" }[state];
    detail.textContent = text;
  }

  // Why the service is not available, from the web app: the reason, the last lines the service printed, and whether
  // starting it again can be tried.
  function explain(message, lines, canRetry) {
    reason.textContent = message || "";
    reason.hidden = !message;
    reasonDetail.textContent = lines || "";
    reasonDetail.hidden = !lines;
    retry.hidden = !canRetry;
  }

  function onUp() {
    if (framed !== url) {
      if (!framed) {
        stage.addEventListener("load", function () { stage.focus(); }, { once: true });
      }
      framed = url;
      stage.src = url;
    }
    stage.hidden = false;
    unavailable.hidden = true;
    openTab.href = url;
    openTab.hidden = false;
    explain("", "", false);
    show("ok", "Real SUMO simulation, served from " + address + ".");
  }

  function onDown() {
    if (framed) {
      // The frame stays; the 3D page reconnects on its own once the service is back.
      show("down", "Lost contact with the service at " + address + "." +
        (managed ? " Starting it again." : " Restart simulation3d/run.sh if it was closed."));
    } else {
      stage.hidden = true;
      unavailable.hidden = false;
      show("down", "Nothing is listening at " + address + ".");
    }
  }

  function onStarting() {
    explain("", "", false);
    unavailable.hidden = true;
    show("checking", "Starting the 3D simulation service…");
  }

  function onFailed(status) {
    stage.hidden = true;
    unavailable.hidden = false;
    show("down", "The 3D simulation service could not be started.");
    explain(status.message, status.detail, true);
  }

  // The service answers /api/health but sends no CORS headers, so the page cannot read the answer. A no-cors request
  // still settles when something is listening and fails when nothing is, which is all that is needed here.
  function probe() {
    var controller = new AbortController();
    var probeTimer = setTimeout(function () { controller.abort(); }, PROBE_TIMEOUT_MS);
    return fetch(origin + "/api/health", { mode: "no-cors", cache: "no-store", signal: controller.signal })
      .then(function () { onUp(); return true; }, function () { onDown(); return false; })
      .then(function (up) { clearTimeout(probeTimer); return up; });
  }

  function probeThenWait() {
    return probe().then(function (up) { return up ? POLL_UP_MS : POLL_DOWN_MS; });
  }

  // Ask the web app for the service (it starts it if needed) and return the delay until the next check.
  function check() {
    if (!managed) {
      return probeThenWait();
    }
    var again = retryRequested;
    retryRequested = false;
    return fetch("/api/simulator3d/start", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ retry: again })
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("no 3D service status");
      }
      return response.json();
    }).then(function (status) {
      if (status.state === "running") {
        setPort(status.port);
        return probeThenWait();
      }
      if (status.state === "failed") {
        onFailed(status);
        return POLL_DOWN_MS;
      }
      if (status.state === "disabled") {
        // Automatic start is off (or not possible here): look for a service somebody started by hand.
        return probe().then(function (up) {
          if (!up) {
            explain(status.message, "", false);
          }
          return up ? POLL_UP_MS : POLL_DOWN_MS;
        });
      }
      onStarting();
      return POLL_STARTING_MS;
    }, function () {
      return probeThenWait(); // the web app could not be asked
    });
  }

  function poll() {
    check().then(function (delay) { timer = setTimeout(poll, delay); });
  }

  retry.addEventListener("click", function () {
    retryRequested = true;
    clearTimeout(timer);
    onStarting();
    poll();
  });

  setPort(port);
  show("checking", managed ? "Starting the 3D simulation service…" : "Looking for the 3D simulation service at " + address + ".");
  poll();
})();
