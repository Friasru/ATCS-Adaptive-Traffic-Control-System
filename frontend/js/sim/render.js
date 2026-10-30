(function (root) {
  "use strict";

  const HALF = 3.5;
  const LANE = 1.75;
  const CAR_WIDTH = 2;
  const VMAX = 13.89;
  const COLORS = { green: "#2ecc71", yellow: "#f1c40f", red: "#e74c3c" };

  function carRect(car, c, s) {
    const length = car.length * s;
    const width = CAR_WIDTH * s;
    const offset = (car.pos - HALF) * s;
    if (car.approach === "W") {
      return { x: c + offset - length, y: c + LANE * s - width / 2, w: length, h: width };
    }
    if (car.approach === "E") {
      return { x: c - offset, y: c - LANE * s - width / 2, w: length, h: width };
    }
    if (car.approach === "N") {
      return { x: c - LANE * s - width / 2, y: c + offset - length, w: width, h: length };
    }
    return { x: c + LANE * s - width / 2, y: c - offset, w: width, h: length };
  }

  function drawRoads(ctx, size, c, s) {
    ctx.fillStyle = "#dfe6ec";
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "#3b4652";
    ctx.fillRect(0, c - HALF * s, size, 2 * HALF * s);
    ctx.fillRect(c - HALF * s, 0, 2 * HALF * s, size);
    ctx.strokeStyle = "#f5f7fa";
    ctx.lineWidth = Math.max(1, s * 0.25);
    ctx.setLineDash([s * 3, s * 3]);
    ctx.beginPath();
    ctx.moveTo(0, c);
    ctx.lineTo(c - HALF * s, c);
    ctx.moveTo(c + HALF * s, c);
    ctx.lineTo(size, c);
    ctx.moveTo(c, 0);
    ctx.lineTo(c, c - HALF * s);
    ctx.moveTo(c, c + HALF * s);
    ctx.lineTo(c, size);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function drawSignals(ctx, c, s, scene) {
    const bar = Math.max(3, s * 0.8);
    const heads = [
      { state: scene.ew, x: c - HALF * s - bar, y: c, w: bar, h: HALF * s, hx: c - HALF * s - 14, hy: c + HALF * s + 14 },
      { state: scene.ew, x: c + HALF * s, y: c - HALF * s, w: bar, h: HALF * s, hx: c + HALF * s + 14, hy: c - HALF * s - 14 },
      { state: scene.ns, x: c - HALF * s, y: c - HALF * s - bar, w: HALF * s, h: bar, hx: c - HALF * s - 14, hy: c - HALF * s - 14 },
      { state: scene.ns, x: c, y: c + HALF * s, w: HALF * s, h: bar, hx: c + HALF * s + 14, hy: c + HALF * s + 14 },
    ];
    heads.forEach(function (head) {
      ctx.fillStyle = COLORS[head.state];
      ctx.fillRect(head.x, head.y, head.w, head.h);
      ctx.beginPath();
      ctx.arc(head.hx, head.hy, 8, 0, Math.PI * 2);
      ctx.fillStyle = COLORS[head.state];
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#1f2933";
      ctx.stroke();
    });
  }

  function drawCars(ctx, c, s, cars) {
    cars.forEach(function (car) {
      const rect = carRect(car, c, s);
      const hue = Math.round(120 * Math.min(1, car.v / VMAX));
      ctx.fillStyle = "hsl(" + hue + ", 75%, 48%)";
      ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
      ctx.lineWidth = 1;
      ctx.strokeStyle = "rgba(0, 0, 0, 0.55)";
      ctx.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.w - 1, rect.h - 1);
    });
  }

  function drawBadge(ctx, text, x, y, align) {
    ctx.font = "bold 13px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    const width = ctx.measureText(text).width + 14;
    const left = align === "right" ? x - width : x;
    ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
    ctx.fillRect(left, y - 11, width, 22);
    ctx.fillStyle = "#1f2933";
    ctx.textAlign = "left";
    ctx.fillText(text, left + 7, y);
  }

  function drawCounts(ctx, size, c, s, cars) {
    const stopped = { W: 0, E: 0, N: 0, S: 0 };
    cars.forEach(function (car) {
      if (car.pos < 0 && car.v < 0.1) stopped[car.approach] += 1;
    });
    const edge = HALF * s + 14;
    drawBadge(ctx, stopped.W + " stopped", 10, c + edge + 10, "left");
    drawBadge(ctx, stopped.E + " stopped", size - 10, c - edge - 10, "right");
    drawBadge(ctx, stopped.N + " stopped", c - edge, 50, "right");
    drawBadge(ctx, stopped.S + " stopped", c + edge, size - 24, "left");
  }

  function drawLabel(ctx, scene) {
    const text = scene.phase + " - " + Math.floor(scene.elapsed) + " s";
    ctx.font = "bold 15px system-ui, sans-serif";
    ctx.textBaseline = "top";
    const width = ctx.measureText(text).width;
    ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
    ctx.fillRect(8, 8, width + 16, 26);
    ctx.fillStyle = "#1f2933";
    ctx.fillText(text, 16, 13);
  }

  function draw(ctx, size, scene) {
    const c = size / 2;
    const s = (size * 5) / 800;
    drawRoads(ctx, size, c, s);
    drawSignals(ctx, c, s, scene);
    drawCars(ctx, c, s, scene.cars);
    drawCounts(ctx, size, c, s, scene.cars);
    drawLabel(ctx, scene);
  }

  const api = { draw: draw };
  root.ATCSRender = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
