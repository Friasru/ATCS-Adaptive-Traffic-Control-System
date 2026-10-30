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

  const shared = {};
  ["scenario", "ew", "ns", "ew-field", "ns-field", "length", "speed", "toggle", "restart", "scenario-note", "time", "verdict", "status"].forEach(
    function (id) {
      shared[id] = document.getElementById(id);
    }
  );

  function createPanel(side) {
    const get = function (id) {
      return document.getElementById(side + "-" + id);
    };
    const select = get("controller");
    const ctx = get("stage").getContext("2d");
    const text = { light: get("light"), wait: get("wait"), max: get("max"), cars: get("cars") };
    const panel = { select: select, sim: null, prev: null, cur: null };

    panel.reset = function (settings) {
      panel.sim = Sim.createSimulation({
        controller: select.value,
        ew: settings.ew,
        ns: settings.ns,
        seed: 1,
        duration: settings.duration,
      });
      panel.cur = panel.sim.snapshot();
      panel.prev = panel.cur;
      panel.draw(0);
      panel.showStats();
    };

    panel.advance = function (steps) {
      let ran = false;
      for (let i = 0; i < steps && !panel.sim.done; i++) {
        if (i === steps - 1) panel.prev = panel.sim.snapshot();
        panel.sim.step();
        ran = true;
      }
      if (ran) panel.cur = panel.sim.snapshot();
    };

    panel.draw = function (alpha) {
      const before = new Map();
      panel.prev.cars.forEach(function (car) {
        before.set(car.id, car.pos);
      });
      const cars = panel.cur.cars.map(function (car) {
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
        ew: panel.cur.ew,
        ns: panel.cur.ns,
        phase: panel.cur.phase,
        elapsed: panel.cur.elapsed,
      });
    };

    panel.showStats = function () {
      const stats = panel.sim.stats();
      text.light.textContent = panel.cur.phase;
      text.wait.textContent = stats.avgWait.toFixed(1) + " s";
      text.max.textContent = Math.round(stats.maxWait) + " s";
      text.cars.textContent = stats.completed + " of " + stats.arrived;
    };

    return panel;
  }

  const left = createPanel("left");
  const right = createPanel("right");
  const panels = [left, right];

  let acc = 0;
  let speed = Number(shared.speed.value);
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

  function setStatus(message, isError) {
    shared.status.textContent = message;
    shared.status.className = isError ? "error" : "";
  }

  function readSettings() {
    const key = shared.scenario.value;
    let ew = PRESETS[key].ew;
    let ns = PRESETS[key].ns;
    if (key === "custom") {
      ew = clampRate(shared.ew.value);
      ns = clampRate(shared.ns.value);
    }
    return { ew: ew, ns: ns, duration: Number(shared.length.value) };
  }

  function verdictText(final) {
    const a = left.sim.stats();
    const b = right.sim.stats();
    const nameA = Sim.LABELS[left.select.value];
    const nameB = Sim.LABELS[right.select.value];
    if (left.select.value === right.select.value) {
      return "Both sides use the same controller, so they behave identically.";
    }
    if (a.avgWait < 1) return "Traffic is still building up...";
    const change = ((a.avgWait - b.avgWait) / a.avgWait) * 100;
    const word = change >= 0 ? "lower" : "higher";
    const sentence =
      nameB + " has an average wait " + Math.abs(change).toFixed(0) + "% " + word + " than " + nameA +
      " (" + b.avgWait.toFixed(1) + " s against " + a.avgWait.toFixed(1) + " s).";
    return final ? "Finished. " + sentence : "So far: " + sentence;
  }

  function updateAll(final) {
    panels.forEach(function (panel) {
      panel.showStats();
    });
    shared.time.textContent = mmss(left.sim.time) + " / " + mmss(left.sim.duration);
    shared.verdict.textContent = verdictText(final);
  }

  function finish() {
    running = false;
    updateAll(true);
    shared.toggle.textContent = "Run again";
    setStatus("Finished.");
  }

  function frame(now) {
    if (!running) return;
    const elapsedReal = Math.min((now - lastFrame) / 1000, 0.1);
    lastFrame = now;
    acc += elapsedReal * speed;
    const steps = Math.min(Math.floor(acc / Sim.DT), MAX_STEPS_PER_FRAME);
    acc -= steps * Sim.DT;
    if (steps > MAX_STEPS_PER_FRAME - 1) acc = 0;
    panels.forEach(function (panel) {
      panel.advance(steps);
    });
    if (left.sim.done) {
      panels.forEach(function (panel) {
        panel.prev = panel.cur;
        panel.draw(0);
      });
      finish();
      return;
    }
    panels.forEach(function (panel) {
      panel.draw(acc / Sim.DT);
    });
    if (now - lastStats > 200) {
      lastStats = now;
      updateAll(false);
    }
    window.requestAnimationFrame(frame);
  }

  function reset() {
    running = false;
    const settings = readSettings();
    const note = PRESETS[shared.scenario.value].note;
    shared["scenario-note"].textContent = note;
    shared["scenario-note"].hidden = note === "";
    shared["ew-field"].hidden = shared.scenario.value !== "custom";
    shared["ns-field"].hidden = shared.scenario.value !== "custom";
    if (settings.ew + settings.ns === 0) {
      left.sim = null;
      shared.toggle.disabled = true;
      setStatus("Set traffic above zero for at least one direction.", true);
      return;
    }
    panels.forEach(function (panel) {
      panel.reset(settings);
    });
    acc = 0;
    shared.toggle.disabled = false;
    shared.toggle.textContent = "Start";
    shared.time.textContent = "0:00 / " + mmss(settings.duration);
    shared.verdict.textContent = "Press Start to run both controllers.";
    setStatus("");
  }

  function start() {
    if (!left.sim) return;
    if (left.sim.done) reset();
    if (!left.sim) return;
    running = true;
    shared.toggle.textContent = "Pause";
    setStatus("Running.");
    lastFrame = performance.now();
    window.requestAnimationFrame(frame);
  }

  function pause() {
    running = false;
    shared.toggle.textContent = "Resume";
    updateAll(false);
    setStatus("Paused.");
  }

  shared.toggle.addEventListener("click", function () {
    if (!left.sim) return;
    if (running) pause();
    else start();
  });
  shared.restart.addEventListener("click", reset);
  [shared.scenario, shared.ew, shared.ns, shared.length, left.select, right.select].forEach(function (control) {
    control.addEventListener("change", reset);
  });
  shared.speed.addEventListener("change", function () {
    speed = Number(shared.speed.value);
  });

  reset();
})();
