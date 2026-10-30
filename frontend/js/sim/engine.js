(function (root) {
  "use strict";

  const DT = 0.5;
  const VMAX = 13.89;
  const ACCEL = 2.6;
  const DECEL = 4.5;
  const EMERGENCY_DECEL = 9;
  const TAU = 1;
  const CAR_LENGTH = 5;
  const MIN_GAP = 2.5;
  const SIGMA = 0.5;
  const SPAWN_DIST = 200;
  const EXIT_DIST = 100;
  const STOP_MARGIN = 0.5;
  const HALT_SPEED = 0.1;
  const YELLOW = 3;
  const ALL_RED = 1;
  const MANUAL_MIN_GREEN = 5;
  const APPROACHES = ["W", "E", "N", "S"];
  const GROUP_OF = { W: "EW", E: "EW", N: "NS", S: "NS" };
  const APPROACHES_OF = { EW: ["W", "E"], NS: ["N", "S"] };
  const PHASES = [
    { name: "East-west green", ew: "green", ns: "red", group: "EW" },
    { name: "East-west yellow", ew: "yellow", ns: "red", group: null },
    { name: "All red", ew: "red", ns: "red", group: null },
    { name: "North-south green", ew: "red", ns: "green", group: "NS" },
    { name: "North-south yellow", ew: "red", ns: "yellow", group: null },
    { name: "All red", ew: "red", ns: "red", group: null },
  ];
  const IDEAL_TRIP = (SPAWN_DIST + EXIT_DIST) / VMAX;

  function makeRng(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function buildSchedule(rate, duration, seed, arrivals) {
    const times = [];
    if (rate <= 0) return times;
    const rng = makeRng(seed);
    if (arrivals === "random") {
      const perSecond = rate / 3600;
      let t = -Math.log(1 - rng()) / perSecond;
      while (t < duration) {
        times.push(t);
        t += -Math.log(1 - rng()) / perSecond;
      }
      return times;
    }
    const interval = 3600 / rate;
    for (let t = rng() * interval; t < duration; t += interval) times.push(t);
    return times;
  }

  function safeSpeed(v, vLeader, gap) {
    return vLeader + (gap - vLeader * TAU) / ((v + vLeader) / (2 * DECEL) + TAU);
  }

  function otherGroup(group) {
    return group === "EW" ? "NS" : "EW";
  }

  const CONTROLLERS = {
    fixed: function () {
      return {
        label: "Fixed timer",
        shouldSwitch: function (ctx) {
          return ctx.elapsed >= 42;
        },
      };
    },
    queue_based: function () {
      const minGreen = 20;
      const maxGreen = 60;
      const margin = 2;
      return {
        label: "Queue based",
        shouldSwitch: function (ctx) {
          if (ctx.elapsed < minGreen) return false;
          const current = ctx.queue(ctx.group);
          const other = ctx.queue(otherGroup(ctx.group));
          return other >= current + margin || (ctx.elapsed >= maxGreen && other > 0);
        },
      };
    },
    wait_time: function () {
      const minGreen = 20;
      const maxGreen = 45;
      const margin = 0;
      return {
        label: "Wait time",
        shouldSwitch: function (ctx) {
          if (ctx.elapsed < minGreen) return false;
          const current = ctx.waiting(ctx.group);
          const other = ctx.waiting(otherGroup(ctx.group));
          return other > current + margin || (ctx.elapsed >= maxGreen && other > 0);
        },
      };
    },
    manual: function () {
      return {
        label: "You",
        shouldSwitch: function (ctx) {
          return ctx.switchRequested && ctx.elapsed >= MANUAL_MIN_GREEN;
        },
      };
    },
  };

  function createSimulation(options) {
    const ew = options.ew;
    const ns = options.ns;
    const seed = options.seed === undefined ? 1 : options.seed;
    const duration = options.duration === undefined ? 3600 : options.duration;
    const arrivals = options.arrivals === "random" ? "random" : "even";
    const makeController = CONTROLLERS[options.controller];
    if (!makeController) throw new Error("unknown controller: " + options.controller);
    const controller = makeController();

    const lanes = { W: [], E: [], N: [], S: [] };
    const pending = { W: [], E: [], N: [], S: [] };
    const schedule = {};
    const cursor = { W: 0, E: 0, N: 0, S: 0 };
    APPROACHES.forEach(function (a, i) {
      const rate = GROUP_OF[a] === "EW" ? ew : ns;
      schedule[a] = buildSchedule(rate, duration, seed * 7919 + i * 104729 + 1, arrivals);
    });

    let time = 0;
    let phase = 0;
    let elapsed = 0;
    let nextId = 1;
    let switchRequested = false;
    let arrived = 0;
    let completed = 0;
    let waitOfCompleted = 0;
    let lossOfCompleted = 0;
    let maxWait = 0;

    function light(group) {
      return group === "EW" ? PHASES[phase].ew : PHASES[phase].ns;
    }

    function queue(group) {
      let n = 0;
      APPROACHES_OF[group].forEach(function (a) {
        lanes[a].forEach(function (car) {
          if (car.pos < 0 && car.v < HALT_SPEED) n += 1;
        });
      });
      return n;
    }

    function waiting(group) {
      let total = 0;
      APPROACHES_OF[group].forEach(function (a) {
        lanes[a].forEach(function (car) {
          if (car.pos < 0) total += car.stopWait;
        });
      });
      return total;
    }

    function mustStop(car, state) {
      if (state === "green" || car.committed) return false;
      if (state === "yellow") {
        const dist = Math.max(-car.pos, 0.1);
        if ((car.v * car.v) / (2 * dist) <= DECEL) return true;
        car.committed = true;
        return false;
      }
      return true;
    }

    function admit(a) {
      const lane = pending[a];
      while (lane.length > 0) {
        const rear = lanes[a][lanes[a].length - 1];
        let speed = VMAX;
        if (rear) {
          const gap = rear.pos - rear.length + SPAWN_DIST - MIN_GAP;
          if (gap < 3) return;
          speed = Math.min(VMAX, Math.sqrt(rear.v * rear.v + 2 * DECEL * gap));
        }
        const car = lane.shift();
        car.pos = -SPAWN_DIST;
        car.v = speed;
        lanes[a].push(car);
      }
    }

    function advanceSignal() {
      elapsed += DT;
      const current = PHASES[phase];
      if (current.group) {
        const ctx = {
          group: current.group,
          elapsed: elapsed,
          queue: queue,
          waiting: waiting,
          switchRequested: switchRequested,
        };
        if (controller.shouldSwitch(ctx)) {
          phase = (phase + 1) % PHASES.length;
          elapsed = 0;
          switchRequested = false;
        }
        return;
      }
      const length = current.ew === "yellow" || current.ns === "yellow" ? YELLOW : ALL_RED;
      if (elapsed >= length) {
        phase = (phase + 1) % PHASES.length;
        elapsed = 0;
      }
    }

    function moveCars(a) {
      const lane = lanes[a];
      const state = light(GROUP_OF[a]);
      const speeds = [];
      for (let i = 0; i < lane.length; i++) {
        const car = lane[i];
        const leader = lane[i - 1];
        let limit = VMAX;
        if (leader) {
          const gap = leader.pos - leader.length - car.pos - MIN_GAP;
          limit = Math.min(limit, safeSpeed(car.v, leader.v, gap));
        }
        if (car.pos < 0 && mustStop(car, state)) {
          limit = Math.min(limit, safeSpeed(car.v, 0, -car.pos - STOP_MARGIN));
        }
        let next = Math.min(limit, car.v + ACCEL * DT, VMAX);
        next = Math.max(next - SIGMA * ACCEL * DT * car.rng(), 0);
        next = Math.max(next, car.v - EMERGENCY_DECEL * DT, 0);
        speeds.push(next);
      }
      const remaining = [];
      for (let i = 0; i < lane.length; i++) {
        const car = lane[i];
        car.v = speeds[i];
        car.pos += car.v * DT;
        if (car.v < HALT_SPEED) {
          car.wait += DT;
          car.stopWait += DT;
        } else {
          car.stopWait = 0;
        }
        if (car.wait > maxWait) maxWait = car.wait;
        if (car.pos > EXIT_DIST) {
          completed += 1;
          waitOfCompleted += car.wait;
          lossOfCompleted += time + DT - car.arrive - IDEAL_TRIP;
        } else {
          remaining.push(car);
        }
      }
      lanes[a] = remaining;
    }

    function step() {
      APPROACHES.forEach(function (a) {
        const times = schedule[a];
        while (cursor[a] < times.length && times[cursor[a]] <= time) {
          pending[a].push({
            id: nextId++,
            approach: a,
            pos: 0,
            v: 0,
            length: CAR_LENGTH,
            arrive: times[cursor[a]],
            wait: 0,
            stopWait: 0,
            committed: false,
            rng: makeRng(seed * 1000003 + nextId),
          });
          arrived += 1;
          cursor[a] += 1;
        }
        pending[a].forEach(function (car) {
          car.wait += DT;
          if (car.wait > maxWait) maxWait = car.wait;
        });
        admit(a);
      });
      advanceSignal();
      APPROACHES.forEach(moveCars);
      time += DT;
    }

    function stats() {
      let waitInSystem = 0;
      APPROACHES.forEach(function (a) {
        lanes[a].forEach(function (car) {
          waitInSystem += car.wait;
        });
        pending[a].forEach(function (car) {
          waitInSystem += car.wait;
        });
      });
      return {
        time: time,
        arrived: arrived,
        completed: completed,
        avgWait: arrived > 0 ? (waitOfCompleted + waitInSystem) / arrived : 0,
        maxWait: maxWait,
        avgTimeLoss: completed > 0 ? lossOfCompleted / completed : 0,
        queueEW: queue("EW"),
        queueNS: queue("NS"),
        backlog: pending.W.length + pending.E.length + pending.N.length + pending.S.length,
      };
    }

    function snapshot() {
      const cars = [];
      APPROACHES.forEach(function (a) {
        lanes[a].forEach(function (car) {
          cars.push({ id: car.id, approach: a, pos: car.pos, v: car.v, length: car.length });
        });
      });
      return {
        time: time,
        phase: PHASES[phase].name,
        elapsed: elapsed,
        ew: PHASES[phase].ew,
        ns: PHASES[phase].ns,
        cars: cars,
      };
    }

    function requestSwitch() {
      if (PHASES[phase].group) switchRequested = true;
    }

    function canSwitch() {
      return PHASES[phase].group !== null && elapsed >= MANUAL_MIN_GREEN;
    }

    function run(seconds) {
      const target = Math.min(duration, time + seconds);
      while (time < target) step();
    }

    return {
      step: step,
      run: run,
      stats: stats,
      snapshot: snapshot,
      requestSwitch: requestSwitch,
      canSwitch: canSwitch,
      get time() {
        return time;
      },
      get done() {
        return time >= duration;
      },
      duration: duration,
      label: controller.label,
    };
  }

  function runHeadless(options) {
    const sim = createSimulation(options);
    while (!sim.done) sim.step();
    return sim.stats();
  }

  const api = {
    DT: DT,
    CONTROLLERS: Object.keys(CONTROLLERS),
    LABELS: { fixed: "Fixed timer", queue_based: "Queue based", wait_time: "Wait time", manual: "You" },
    SPAWN_DIST: SPAWN_DIST,
    EXIT_DIST: EXIT_DIST,
    createSimulation: createSimulation,
    runHeadless: runHeadless,
  };

  root.ATCSSim = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
