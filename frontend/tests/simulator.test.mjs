import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const sim = require("../js/sim/engine.js");

function options(controller, ew, ns, extra) {
  return Object.assign({ controller, ew, ns, seed: 1, duration: 3600 }, extra);
}

test("the same seed gives identical results", () => {
  const a = sim.runHeadless(options("queue_based", 500, 200));
  const b = sim.runHeadless(options("queue_based", 500, 200));
  assert.deepEqual(a, b);
});

test("every controller sees the same arrivals", () => {
  const counts = sim.CONTROLLERS.filter((c) => c !== "manual").map(
    (c) => sim.runHeadless(options(c, 500, 200)).arrived
  );
  assert.equal(new Set(counts).size, 1);
});

test("an unknown controller is rejected", () => {
  assert.throws(() => sim.createSimulation(options("magic", 100, 100)));
});

test("traffic in one direction only does not break the simulation", () => {
  const stats = sim.runHeadless(options("fixed", 400, 0, { duration: 600 }));
  assert.ok(stats.completed > 0);
  assert.equal(stats.arrived > 0, true);
});

test("cars never overlap", () => {
  for (const controller of ["fixed", "queue_based", "wait_time"]) {
    const s = sim.createSimulation(options(controller, 900, 700, { duration: 1800 }));
    let smallest = Infinity;
    while (!s.done) {
      s.step();
      const byApproach = {};
      for (const car of s.snapshot().cars) {
        (byApproach[car.approach] ||= []).push(car);
      }
      for (const cars of Object.values(byApproach)) {
        cars.sort((p, q) => q.pos - p.pos);
        for (let i = 1; i < cars.length; i++) {
          smallest = Math.min(smallest, cars[i - 1].pos - cars[i - 1].length - cars[i].pos);
        }
      }
    }
    assert.ok(smallest > 0, controller + " smallest gap " + smallest);
  }
});

test("the light sequence is always safe and in order", () => {
  for (const controller of ["fixed", "queue_based", "wait_time"]) {
    const s = sim.createSimulation(options(controller, 500, 200, { duration: 1800 }));
    const order = [
      "East-west green",
      "East-west yellow",
      "All red",
      "North-south green",
      "North-south yellow",
      "All red",
    ];
    let last = s.snapshot().phase;
    let index = 0;
    assert.equal(last, order[0]);
    while (!s.done) {
      s.step();
      const snap = s.snapshot();
      assert.ok(!(snap.ew !== "red" && snap.ns !== "red"), "both directions open");
      if (snap.phase !== last) {
        index = (index + 1) % order.length;
        assert.equal(snap.phase, order[index]);
        last = snap.phase;
      }
    }
  }
});

test("green times respect the minimum of each controller", () => {
  const expected = { fixed: 42, queue_based: 20, wait_time: 20 };
  for (const [controller, minimum] of Object.entries(expected)) {
    const s = sim.createSimulation(options(controller, 500, 200, { duration: 3600 }));
    let last = s.snapshot().phase;
    let startedAt = 0;
    const lengths = [];
    while (!s.done) {
      s.step();
      const snap = s.snapshot();
      if (snap.phase !== last) {
        if (last.endsWith("green")) lengths.push(s.time - startedAt);
        last = snap.phase;
        startedAt = s.time;
      }
    }
    assert.ok(lengths.length > 5, controller + " switched too rarely");
    assert.ok(Math.min(...lengths) >= minimum - 1e-9, controller + " shortest green " + Math.min(...lengths));
  }
});

test("the fixed timer gives each direction exactly 42 seconds of green", () => {
  const s = sim.createSimulation(options("fixed", 500, 200, { duration: 1200 }));
  let last = s.snapshot().phase;
  let startedAt = 0;
  const lengths = [];
  while (!s.done) {
    s.step();
    const snap = s.snapshot();
    if (snap.phase !== last) {
      if (last.endsWith("green")) lengths.push(s.time - startedAt);
      last = snap.phase;
      startedAt = s.time;
    }
  }
  assert.ok(lengths.every((x) => Math.abs(x - 42) < 1e-9));
});

test("cars do not run the red light", () => {
  for (const controller of ["fixed", "queue_based", "wait_time"]) {
    const s = sim.createSimulation(options(controller, 900, 700, { duration: 1800 }));
    const redSince = { EW: null, NS: null };
    const lastPos = new Map();
    let violations = 0;
    while (!s.done) {
      s.step();
      const snap = s.snapshot();
      for (const [group, state] of [["EW", snap.ew], ["NS", snap.ns]]) {
        if (state === "red") {
          if (redSince[group] === null) redSince[group] = snap.time;
        } else {
          redSince[group] = null;
        }
      }
      for (const car of snap.cars) {
        const group = car.approach === "W" || car.approach === "E" ? "EW" : "NS";
        const before = lastPos.get(car.id);
        if (before !== undefined && before < 0 && car.pos >= 0) {
          if (redSince[group] !== null && snap.time - redSince[group] > 2) violations += 1;
        }
        lastPos.set(car.id, car.pos);
      }
    }
    assert.equal(violations, 0, controller + " ran the red " + violations + " times");
  }
});

test("adaptive controllers beat the fixed timer when traffic is uneven or balanced", () => {
  for (const [ew, ns] of [[500, 200], [300, 300], [200, 600]]) {
    const fixed = sim.runHeadless(options("fixed", ew, ns));
    for (const controller of ["queue_based", "wait_time"]) {
      const adaptive = sim.runHeadless(options(controller, ew, ns));
      assert.ok(
        adaptive.avgWait < 0.7 * fixed.avgWait,
        controller + " " + ew + "/" + ns + ": " + adaptive.avgWait + " vs " + fixed.avgWait
      );
    }
  }
});

test("stats are consistent", () => {
  const stats = sim.runHeadless(options("queue_based", 500, 200));
  assert.ok(stats.arrived >= stats.completed);
  assert.ok(stats.completed > 0.95 * stats.arrived);
  assert.ok(stats.avgWait > 0);
  assert.ok(stats.maxWait >= stats.avgWait);
  assert.ok(stats.avgTimeLoss >= 0);
});

test("manual control: the light stays until the player asks, and respects the minimum", () => {
  const s = sim.createSimulation(options("manual", 500, 200, { duration: 400 }));
  s.run(200);
  assert.equal(s.snapshot().phase, "East-west green");
  assert.equal(s.canSwitch(), true);

  const early = sim.createSimulation(options("manual", 500, 200, { duration: 400 }));
  early.run(2);
  assert.equal(early.canSwitch(), false);
  early.requestSwitch();
  early.run(1);
  assert.equal(early.snapshot().phase, "East-west green");
  early.run(4);
  assert.equal(early.snapshot().phase, "East-west yellow");
});

test("manual control: a request during yellow is ignored", () => {
  const s = sim.createSimulation(options("manual", 500, 200, { duration: 400 }));
  s.run(10);
  s.requestSwitch();
  s.run(1);
  assert.equal(s.snapshot().phase, "East-west yellow");
  s.requestSwitch();
  s.run(10);
  assert.equal(s.snapshot().phase, "North-south green");
  s.run(100);
  assert.equal(s.snapshot().phase, "North-south green");
});
