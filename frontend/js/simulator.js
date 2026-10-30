(function () {
  "use strict";

  const Sim = window.ATCSSim;
  const Render = window.ATCSRender;
  const SIZE = 800;
  const MAX_STEPS_PER_FRAME = 200;

  const PRESETS = {
    uneven: { ew: 500, ns: 200, note: "" },
    balanced: { ew: 300, ns: 300, note: "" },
    heldout: { ew: 200, ns: 600, note: "" },
    rush: {
      ew: 900,
      ns: 700,
      note: "In the SUMO tests the adaptive controllers only tied the fixed timer in this scenario. This simplified model shows a bigger advantage, so rely on the SUMO results for rush hour.",
    },
    custom: { ew: null, ns: null, note: "" },
  };

  const DESCRIPTIONS = {
    fixed: "Runs on the clock and ignores the traffic. Each road gets 42 seconds of green, then the light changes.",
    queue_based:
      "Counts the stopped cars on each road. The light changes when the waiting road has at least 2 more stopped cars. Green lasts 20 to 60 seconds.",
    wait_time:
      "Adds up how long the cars on each road have been standing. The light changes when the waiting road's total is higher. Green lasts 20 to 45 seconds.",
  };

  const ids = [
    "scenario", "ew", "ns", "ew-field", "ns-field", "controller", "length", "speed",
    "toggle", "restart", "scenario-note", "stage", "controller-name", "controller-text",
    "stat-time", "stat-phase", "stat-wait", "stat-max", "stat-vehicles", "stat-ew",
    "stat-ns", "status",
  ];
  const els = {};
  ids.forEach(function (id) {
    els[id] = document.getElementById(id);
  });
  const ctx = els.stage.getContext("2d");

  let sim = null;
  let prev = null;
  let cur = null;
  let acc = 0;
  let speed = Number(els.speed.value);
  let running = false;
  let lastFrame = 0;
  let lastStats = 0;

  function clampRate(value) {
    const n = Math.round(Number(value));
    if (!Number.isFinite(n)) return 0;
    return Math.min(1500, Math.max(0, n));
  }

  function mmss(seconds) {
    const whole = Math.floor(seconds);
    const m = Math.floor(whole / 60);
    const s = whole % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function setStatus(text, isError) {
    els.status.textContent = text;
    els.status.className = isError ? "error" : "";
  }

  function readSettings() {
    const key = els.scenario.value;
    let ew = PRESETS[key].ew;
    let ns = PRESETS[key].ns;
    if (key === "custom") {
      ew = clampRate(els.ew.value);
      ns = clampRate(els.ns.value);
    }
    return {
      ew: ew,
      ns: ns,
      controller: els.controller.value,
      duration: Number(els.length.value),
    };
  }

  function render(alpha) {
    const before = new Map();
    prev.cars.forEach(function (car) {
      before.set(car.id, car.pos);
    });
    const cars = cur.cars.map(function (car) {
      const old = before.get(car.id);
      return {
        approach: car.approach,
        v: car.v,
        length: car.length,
        pos: old === undefined ? car.pos : old + (car.pos - old) * alpha,
      };
    });
    Render.draw(ctx, SIZE, {
      cars: cars,
      ew: cur.ew,
      ns: cur.ns,
      phase: cur.phase,
      elapsed: cur.elapsed,
    });
  }

  function updateStats() {
    const stats = sim.stats();
    els["stat-time"].textContent = mmss(sim.time) + " / " + mmss(sim.duration);
    els["stat-phase"].textContent = cur.phase;
    els["stat-wait"].textContent = stats.avgWait.toFixed(1) + " s";
    els["stat-max"].textContent = Math.round(stats.maxWait) + " s";
    els["stat-vehicles"].textContent = stats.completed + " of " + stats.arrived;
    els["stat-ew"].textContent = String(stats.queueEW);
    els["stat-ns"].textContent = String(stats.queueNS);
  }

  function stop() {
    running = false;
  }

  function finish() {
    running = false;
    updateStats();
    const stats = sim.stats();
    els.toggle.textContent = "Run again";
    setStatus(
      "Finished. Average wait " + stats.avgWait.toFixed(1) + " s, longest wait " + Math.round(stats.maxWait) + " s."
    );
  }

  function frame(now) {
    if (!running) return;
    const elapsedReal = Math.min((now - lastFrame) / 1000, 0.1);
    lastFrame = now;
    acc += elapsedReal * speed;
    let steps = Math.min(Math.floor(acc / Sim.DT), MAX_STEPS_PER_FRAME);
    acc -= steps * Sim.DT;
    if (steps > MAX_STEPS_PER_FRAME - 1) acc = 0;
    for (let i = 0; i < steps && !sim.done; i++) {
      if (i === steps - 1) prev = sim.snapshot();
      sim.step();
    }
    if (steps > 0) cur = sim.snapshot();
    if (sim.done) {
      prev = cur;
      render(0);
      finish();
      return;
    }
    render(acc / Sim.DT);
    if (now - lastStats > 200) {
      lastStats = now;
      updateStats();
    }
    window.requestAnimationFrame(frame);
  }

  function start() {
    if (!sim) return;
    if (sim.done) reset();
    if (!sim) return;
    running = true;
    els.toggle.textContent = "Pause";
    setStatus("Running.");
    lastFrame = performance.now();
    window.requestAnimationFrame(frame);
  }

  function pause() {
    running = false;
    els.toggle.textContent = "Resume";
    updateStats();
    setStatus("Paused.");
  }

  function showControllerText() {
    const key = els.controller.value;
    els["controller-name"].textContent = Sim.LABELS[key];
    els["controller-text"].textContent = DESCRIPTIONS[key];
  }

  function reset() {
    stop();
    const settings = readSettings();
    const note = PRESETS[els.scenario.value].note;
    els["scenario-note"].textContent = note;
    els["scenario-note"].hidden = note === "";
    els["ew-field"].hidden = els.scenario.value !== "custom";
    els["ns-field"].hidden = els.scenario.value !== "custom";
    showControllerText();
    if (settings.ew + settings.ns === 0) {
      sim = null;
      els.toggle.disabled = true;
      setStatus("Set traffic above zero for at least one direction.", true);
      return;
    }
    sim = Sim.createSimulation({
      controller: settings.controller,
      ew: settings.ew,
      ns: settings.ns,
      seed: 1,
      duration: settings.duration,
    });
    cur = sim.snapshot();
    prev = cur;
    acc = 0;
    els.toggle.disabled = false;
    els.toggle.textContent = "Start";
    setStatus("Press Start.");
    render(0);
    updateStats();
  }

  function onToggle() {
    if (!sim) return;
    if (running) pause();
    else start();
  }

  els.toggle.addEventListener("click", onToggle);
  els.restart.addEventListener("click", reset);
  ["scenario", "ew", "ns", "controller", "length"].forEach(function (id) {
    els[id].addEventListener("change", reset);
  });
  els.speed.addEventListener("change", function () {
    speed = Number(els.speed.value);
  });

  reset();
})();
