// DOM controls, per-pane HUD and the results panel. All state shown here comes from server messages; the buttons
// only send commands, so the page cannot drift from what SUMO is actually doing.

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DIR_NAME = { top0A0: 'North', bottom0A0: 'South', right0A0: 'East', left0A0: 'West' };
const SIGNAL_WORD = { green: 'GREEN', yellow: 'YELLOW', red: 'RED' };

export function fmtClock(t) {
  const s = Math.floor(t);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}
const f1 = (v) => (v === null || v === undefined ? '–' : Number(v).toFixed(1));
const f2 = (v) => (v === null || v === undefined ? '–' : Number(v).toFixed(2));

export function colorOf(ch) {
  return ch === 'G' || ch === 'g' ? 'green' : ch === 'y' || ch === 'Y' || ch === 'u' ? 'yellow' : 'red';
}

// The per-pane statistics, in three groups so a number is never read as the wrong kind of quantity. Every pane
// (single view, and both panes of a side-by-side run) is built by this one function from the same frame fields:
//   now    instantaneous: can go up and down            (counts.in_network, counts.stopped)
//   total  cumulative since the run started: never goes down   (counts.departed, counts.arrived)
//   wait   running average / maximum over the vehicles that have already finished (live.avg_wait, live.max_wait)
const DIRECTION_TIP = 'stopped: vehicles below 0.1 m/s on this direction\'s approach lane right now. waiting: the combined time those vehicles have been standing (SUMO lane waiting time).';
const WAIT_TIP = 'Running figure over vehicles that have already finished their trip; it can move either way. The final numbers come from SUMO\'s trip records when the run ends.';
export function hudMetrics(p) {
  const c = p.counts, live = p.live || {};
  const w = (v) => (v === null || v === undefined ? '–' : f1(v));
  return [
    {
      scope: 'now', title: 'Right now',
      tiles: [
        { key: 'active', label: 'active vehicles', value: c.in_network, tip: 'Vehicles inside the simulated network right now (spawned − completed). Goes up and down.' },
        { key: 'stopped', label: 'stopped vehicles', value: c.stopped, tip: 'Vehicles below 0.1 m/s right now (SUMO\'s halting definition). A vehicle that was only just placed on the road is not counted: it has not driven yet. Goes up and down.' },
      ],
    },
    {
      scope: 'total', title: 'Total since start',
      tiles: [
        { key: 'spawned', label: 'vehicles spawned', value: c.departed, tip: 'Vehicles that have entered the simulation since the run started. Never goes down.' },
        { key: 'completed', label: 'vehicles completed', value: c.arrived, tip: 'Vehicles that have finished their trip and left the network. Never goes down.' },
      ],
    },
    {
      scope: 'wait', title: 'Wait of completed trips',
      tiles: [
        { key: 'avg_wait', label: 'avg wait (s)', value: w(live.avg_wait), dim: live.avg_wait == null, tip: WAIT_TIP },
        { key: 'max_wait', label: 'max wait (s)', value: w(live.max_wait), dim: live.max_wait == null, tip: WAIT_TIP },
      ],
    },
  ];
}
const tile = (t) => `<div class="metric" data-metric="${t.key}" title="${esc(t.tip)}"><div class="v ${t.dim ? 'dim' : ''}">${esc(t.value)}</div><div class="k">${esc(t.label)}</div></div>`;

export class Ui {
  constructor(send) {
    this.send = send;
    this.status = 'idle';
    this.state = null;
    this.hello = null;
    this.bench = { results: {}, live: [], busy: false };
    this.lastResult = null;
    this.groups = []; // [{ name, link (straight-link index of a lane in that group) }]
    this._wire();
  }

  _wire() {
    const act = (id, fn) => $(id).addEventListener('click', fn);
    act('btn-start', () => this.send('start'));
    act('btn-pause', () => this.send(this.status === 'paused' ? 'resume' : 'pause'));
    act('btn-restart', () => this.send('restart'));
    act('btn-stop', () => this.send('stop'));
    act('btn-mode', () => this.toggleCompare());
    act('btn-results', () => this.toggleResults());
    act('btn-results-close', () => this.toggleResults(false));
    act('btn-bench', () => this.send('benchmark', { scenario: $('sel-scenario').value }));
    $('sel-ctrl').addEventListener('change', () => this.select());
    $('sel-ctrl2').addEventListener('change', () => this.select());
    $('sel-scenario').addEventListener('change', () => this.select());
  }

  // Selecting while a run is active ends that run explicitly and starts a fresh one with the new choice.
  select(controllers) {
    const wasActive = this.status === 'running' || this.status === 'paused';
    const ctrls = controllers || this.currentControllers();
    this.send('select', { controllers: ctrls, scenario: $('sel-scenario').value });
    if (wasActive) this.send('start');
  }

  currentControllers() {
    const first = $('sel-ctrl').value;
    return this.compare ? [first, $('sel-ctrl2').value] : [first];
  }

  toggleCompare() {
    const on = !this.compare;
    this.compare = on;
    const first = $('sel-ctrl').value;
    let ctrls = [first];
    if (on) { // Fixed on the left (traditional timing), an adaptive ATCS controller on the right
      const right = first !== 'fixed' ? first : ($('sel-ctrl2').value !== 'fixed' ? $('sel-ctrl2').value : 'queue_based');
      ctrls = ['fixed', right];
      $('sel-ctrl').value = 'fixed';
      $('sel-ctrl2').value = right;
    }
    this.select(ctrls);
  }

  toggleResults(force) {
    const el = $('results');
    el.hidden = force === undefined ? !el.hidden : !force;
    this.renderResults();
  }

  // ------------------------------------------------------------------ server messages
  onHello(m) {
    this.hello = m;
    const fill = (sel, items, val, txt) => {
      sel.innerHTML = '';
      for (const it of items) {
        const o = document.createElement('option');
        o.value = val(it); o.textContent = txt(it);
        sel.appendChild(o);
      }
    };
    fill($('sel-ctrl'), m.controllers, (c) => c.key, (c) => c.title);
    fill($('sel-ctrl2'), m.controllers, (c) => c.key, (c) => c.title);
    fill($('sel-scenario'), m.scenarios, (s) => s.key, (s) => `${s.title} (${s.ew}/${s.ns} veh/h)`);
    const sp = $('speeds');
    sp.innerHTML = '';
    for (const v of m.speeds) {
      const b = document.createElement('button');
      b.textContent = `${v}×`; b.dataset.speed = v;
      b.addEventListener('click', () => this.send('speed', { value: v }));
      sp.appendChild(b);
    }
    // which approaches belong to which green phase, from the network + signal programme sent by the server
    this.groups = [];
    for (const [i, ph] of m.net.tls.phases.entries()) {
      if (!/[Gg]/.test(ph.state)) continue;
      const apps = m.net.approaches.filter((a) => /[Gg]/.test(ph.state[a.straight_link]));
      const names = apps.map((a) => DIR_NAME[a.edge] || a.edge).sort();
      const axis = names.some((n) => n === 'North' || n === 'South') ? 'North–South' : 'East–West';
      this.groups.push({ phase: i, name: axis, link: apps[0].straight_link });
    }
  }

  onState(s) {
    this.state = s;
    this.status = s.status;
    this.compare = s.mode === 'compare';
    const [c0, c1] = s.controllers;
    $('sel-ctrl').value = c0;
    if (c1) $('sel-ctrl2').value = c1;
    $('lbl-ctrl2').hidden = !this.compare;
    $('btn-mode').setAttribute('aria-pressed', String(this.compare));
    $('sel-scenario').value = s.scenario.key;
    $('scenario-info').innerHTML = `<b>${esc(s.scenario.title)}</b> · ${esc(s.scenario.description)} · east–west ${Number(s.scenario.ew)} veh/h, north–south ${Number(s.scenario.ns)} veh/h per direction`;

    const pill = $('status-pill');
    pill.className = `pill ${s.status}`;
    pill.textContent = { idle: 'Ready', starting: 'Starting SUMO…', running: 'Running', paused: 'Paused', ended: 'Finished', stopped: 'Stopped' }[s.status] || s.status;

    const active = s.status === 'running' || s.status === 'paused' || s.status === 'starting';
    $('btn-start').disabled = active;
    $('btn-pause').disabled = !(s.status === 'running' || s.status === 'paused');
    $('btn-pause').textContent = s.status === 'paused' ? '▶ Resume' : '⏸ Pause';
    $('btn-restart').disabled = s.status === 'idle' || s.status === 'starting';
    $('btn-stop').disabled = !active;
    $('btn-bench').disabled = active || s.busy_benchmark;
    for (const b of $('speeds').children) b.classList.toggle('on', Number(b.dataset.speed) === s.speed);
    if (s.status === 'idle') {
      $('clock').textContent = '0:00';
      $('clock-sub').textContent = '';
    }
    this.renderResults();
  }

  onFrame(m) {
    $('clock').textContent = fmtClock(m.t);
    $('clock-sub').textContent = `t = ${m.t.toFixed(0)} s of simulated time`;
    m.panes.forEach((p, i) => this.hud(i, p));
  }

  hud(i, p) {
    const root = $(`pane${i}`);
    const kind = p.controller === 'fixed' ? 'fixed' : 'adaptive';
    const title = root.querySelector('.pane-title');
    const name = (this.hello.controllers.find((c) => c.key === p.controller) || {}).title || p.controller;
    title.dataset.kind = kind;
    title.querySelector('.title').textContent =
      kind === 'fixed' ? 'Traditional Fixed Timing' : `ATCS Adaptive Control · ${name}`;

    const board = root.querySelector('.signal-board');
    board.innerHTML = '';
    for (const g of this.groups) {
      const col = colorOf(p.tls.state[g.link]);
      const gr = p.groups.find((x) => x.phase === g.phase) || { stopped: 0, wait: 0 };
      const row = document.createElement('div');
      row.className = 'sig-row';
      const inPhase = col === 'green' || col === 'yellow';
      row.innerHTML = `<span class="sig-dot ${col}"></span><span>${esc(g.name)} <b>${SIGNAL_WORD[col]}</b><br><small data-stopped="${gr.stopped}" title="${esc(DIRECTION_TIP)}">${gr.stopped} stopped · ${gr.wait.toFixed(0)} s waiting</small></span><span>${inPhase ? `${p.tls.elapsed.toFixed(0)} s` : ''}</span>`;
      board.appendChild(row);
    }
    root.querySelector('.metrics').innerHTML = hudMetrics(p)
      .map((grp) => `<div class="mgroup" data-scope="${grp.scope}"><div class="mhead">${esc(grp.title)}</div>${grp.tiles.map(tile).join('')}</div>`)
      .join('');
  }

  onResult(m) {
    this.lastResult = m;
    if (m.complete && $('results').hidden) this.toggleResults(true); // run finished: show the measured numbers
    else this.renderResults();
  }
  onBenchmarks(m) { this.bench = m; $('bench-note').textContent = m.busy ? 'running three full-hour simulations…' : ''; this.renderResults(); }

  error(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(this._toast);
    this._toast = setTimeout(() => (t.hidden = true), 6000);
  }

  // ------------------------------------------------------------------ results panel
  renderResults() {
    const el = $('results');
    if (el.hidden || !this.state) return;
    const key = this.state.scenario.key;
    const doc = this.bench.results[key];
    const sel = new Set(this.state.controllers);
    let html = '';
    if (doc) {
      const names = Object.fromEntries(this.hello.controllers.map((c) => [c.key, c.title]));
      html += `<div class="note"><b>${esc(doc.scenario.title)}</b> — ${esc(doc.scenario.description)}. Each row is a full simulated hour (${Number(doc.sim_end_s)} s) run headless in SUMO with the unmodified ATCS controller, identical demand and seed ${Number(doc.seed)}.</div>`;
      html += '<table class="res"><tr><th>Controller</th><th>Avg wait (s)</th><th>Max wait (s)</th><th>Avg time loss (s)</th><th>Vehicles done</th><th>Avg wait vs Fixed</th></tr>';
      for (const [c, m] of Object.entries(doc.controllers)) {
        const imp = (doc.improvement_vs_fixed_pct[c] || {}).avg_wait_s;
        const cls = imp === undefined || imp === null ? '' : imp > 0 ? 'better' : 'worse';
        const impTxt = imp === undefined ? '(baseline)' : imp === null ? '–' : `${imp > 0 ? '−' : '+'}${Math.abs(imp).toFixed(1)}%`;
        html += `<tr class="${sel.has(c) ? 'sel' : ''}"><td>${esc(names[c] || c)}</td><td>${f2(m.avg_wait_s)}</td><td>${m.max_wait_s.toFixed(0)}</td><td>${f2(m.avg_time_loss_s)}</td><td>${m.vehicles_completed}</td><td class="${cls}">${impTxt}</td></tr>`;
      }
      html += '</table>';
      html += `<div class="note">${esc(doc.sumo)} · generated ${esc(doc.generated_at.replace('T', ' ').replace('+00:00', ' UTC'))}. These are SUMO simulation results, not real-world traffic measurements. “vs Fixed”: lower is better (−54.7% = 54.7% less average waiting than Fixed).</div>`;
    } else {
      html += '<div class="note">No benchmark has been run for this scenario yet. Press “Re-run benchmark”.</div>';
    }
    const lives = (this.bench.live || []).filter((l) => l.scenario === key).slice(-6).reverse();
    if (lives.length) {
      html += '<div class="note"><b>Your live runs of this scenario</b> (what you just watched, measured by SUMO):</div>';
      for (const l of lives) {
        const m = l.metrics;
        const how = l.complete ? 'finished' : `stopped early at ${fmtClock(l.sim_time)} (only vehicles that had completed)`;
        html += `<div class="live-line"><b>${esc((this.hello.controllers.find((c) => c.key === l.controller) || {}).title)}</b> — ${how}: ${m.vehicles_completed} vehicles, avg wait ${f2(m.avg_wait_s)} s, max ${m.max_wait_s.toFixed(0)} s, avg time loss ${f2(m.avg_time_loss_s)} s</div>`;
      }
    }
    $('results-body').innerHTML = html;
  }
}
