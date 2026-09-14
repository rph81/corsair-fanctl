/* corsair-fanctl web UI.
 *
 * Single page, no build step, no framework. The DOM for the six fan cards is
 * built once; polling only refreshes readouts so that typing in a field or
 * dragging a curve handle is never interrupted by a background update.
 */
'use strict';

const FAN_COUNT = 6;
const POLL_MS = 2000;
const CHART_MS = 5000;
const TMIN = 20, TMAX = 100;          // curve editor temperature domain

// Categorical chart colours. Kept independent of the accent: a series palette
// needs mutual distinguishability, which is a different problem from branding.
// The light set is darkened so the lines stay readable on white.
const PALETTE_DARK = ['#4aa3ff', '#3fb950', '#d29922', '#f85149', '#a371f7',
                      '#39c5cf', '#db6d28', '#db61a2'];
const PALETTE_LIGHT = ['#0969da', '#1a7f37', '#9a6700', '#cf222e', '#8250df',
                       '#1b7c83', '#bc4c00', '#bf3989'];

const ACCENT_PRESETS = [
  ['#4aa3ff', 'Blue'], ['#39c5cf', 'Cyan'], ['#3fb950', 'Green'],
  ['#d29922', 'Amber'], ['#db6d28', 'Orange'], ['#f85149', 'Red'],
  ['#db61a2', 'Pink'], ['#a371f7', 'Purple'],
];
const MAX_FAVORITES = 10;

/* ------------------------------------------------------------------ state */

const state = {
  token: null,
  snapshot: null,
  config: null,        // locally edited copy
  dirty: false,
  cards: new Map(),    // fan index -> refs
  catalogKey: '',
  history: [],
  hidden: new Set(),   // chart series keys the viewer has switched off
};

const HIDDEN_KEY = 'fanctl-hidden-series';

/* Chart visibility is a per-browser convenience, so it lives in localStorage
 * rather than the server-side ui config; storage can be unavailable in
 * private windows, hence the try/catch. */
function loadHidden() {
  try {
    const raw = JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]');
    state.hidden = new Set(Array.isArray(raw) ? raw : []);
  } catch (_) { state.hidden = new Set(); }
}

function saveHidden() {
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...state.hidden])); } catch (_) { /* optional */ }
}

function initToken() {
  const fromUrl = new URLSearchParams(location.search).get('token');
  if (fromUrl) {
    localStorage.setItem('fanctl-token', fromUrl);
    history.replaceState(null, '', location.pathname);
  }
  state.token = localStorage.getItem('fanctl-token');
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers);
  if (state.token) headers['X-Auth-Token'] = state.token;
  if (options.body) headers['Content-Type'] = 'application/json';

  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { /* non-JSON */ }

  if (!response.ok) {
    throw new Error((payload && payload.error) || `HTTP ${response.status}`);
  }
  return payload;
}

/* ----------------------------------------------------------- theme + accent */

const hexOk = (value) => /^#[0-9a-f]{6}$/i.test(value || '');

function toRgb(hex) {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
}

function mix(hex, other, amount) {
  const a = toRgb(hex), b = toRgb(other);
  const out = a.map((v, i) => Math.round(v * (1 - amount) + b[i] * amount));
  return '#' + out.map((v) => v.toString(16).padStart(2, '0')).join('');
}

const rgba = (hex, alpha) => `rgba(${toRgb(hex).join(',')},${alpha})`;

/** "system" resolves against the OS preference; everything else is literal. */
function effectiveTheme(theme) {
  if (theme === 'light' || theme === 'dark') return theme;
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function applyTheme(theme) {
  const resolved = effectiveTheme(theme);
  document.documentElement.dataset.theme = resolved;
  applyAccent(state.config ? state.config.ui.accent : '#4aa3ff');
  drawChart();
}

function applyAccent(accent) {
  if (!hexOk(accent)) accent = '#4aa3ff';
  const light = document.documentElement.dataset.theme === 'light';
  const root = document.documentElement.style;

  root.setProperty('--accent', accent);
  // On white, a bright accent has too little contrast for text and for a
  // filled button, so darken it there; on dark it is already legible.
  root.setProperty('--accent-strong', light ? mix(accent, '#000000', 0.28) : accent);
  root.setProperty('--accent-dim',
    light ? mix(accent, '#ffffff', 0.86) : mix(accent, '#0e1116', 0.68));
  root.setProperty('--accent-soft', rgba(accent, light ? 0.14 : 0.12));
  root.setProperty('--accent-glow', rgba(accent, 0.5));
}

function chartPalette() {
  return document.documentElement.dataset.theme === 'light'
    ? PALETTE_LIGHT : PALETTE_DARK;
}

/** Persist just the ui section. Safe as a partial PUT: the server merges. */
async function saveUi(changes) {
  Object.assign(state.config.ui, changes);
  try {
    await api('/api/config', {
      method: 'PUT', body: JSON.stringify({ ui: state.config.ui }),
    });
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------------------------------------------ utils */

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const deepCopy = (value) => JSON.parse(JSON.stringify(value));
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
};

let toastTimer = null;
function toast(message, isError = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.classList.toggle('error', isError);
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 2600);
}

function markDirty() {
  state.dirty = true;
  document.getElementById('btn-apply').disabled = false;
  document.getElementById('btn-revert').disabled = false;
}

function clearDirty() {
  state.dirty = false;
  document.getElementById('btn-apply').disabled = true;
  document.getElementById('btn-revert').disabled = true;
}

function fanConfig(index) {
  return state.config.fans.find((f) => f.index === index);
}

/* ----------------------------------------------------------- curve editor */

class CurveEditor {
  constructor(fanIndex, onChange) {
    this.fanIndex = fanIndex;
    this.onChange = onChange;
    this.width = 400;
    this.height = 190;
    this.pad = { l: 28, r: 10, t: 10, b: 22 };
    this.drag = null;

    this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    this.svg.setAttribute('viewBox', `0 0 ${this.width} ${this.height}`);
    this.svg.addEventListener('pointerdown', (e) => this.onPointerDown(e));
    this.svg.addEventListener('pointermove', (e) => this.onPointerMove(e));
    this.svg.addEventListener('pointerup', (e) => this.onPointerUp(e));
    this.svg.addEventListener('pointercancel', (e) => this.onPointerUp(e));
    this.svg.addEventListener('contextmenu', (e) => e.preventDefault());

    this.now = null;
  }

  get points() { return fanConfig(this.fanIndex).curve; }

  x(temp) {
    const inner = this.width - this.pad.l - this.pad.r;
    return this.pad.l + ((temp - TMIN) / (TMAX - TMIN)) * inner;
  }

  y(duty) {
    const inner = this.height - this.pad.t - this.pad.b;
    return this.height - this.pad.b - (duty / 100) * inner;
  }

  invert(clientX, clientY) {
    const rect = this.svg.getBoundingClientRect();
    const sx = ((clientX - rect.left) / rect.width) * this.width;
    const sy = ((clientY - rect.top) / rect.height) * this.height;
    const inner = this.width - this.pad.l - this.pad.r;
    const innerY = this.height - this.pad.t - this.pad.b;
    return {
      temp: clamp(TMIN + ((sx - this.pad.l) / inner) * (TMAX - TMIN), TMIN, TMAX),
      duty: clamp(((this.height - this.pad.b - sy) / innerY) * 100, 0, 100),
    };
  }

  /* -- interaction -- */

  onPointerDown(event) {
    const handleIndex = event.target.dataset ? event.target.dataset.point : undefined;
    if (handleIndex !== undefined) {
      if (event.button === 2 || event.ctrlKey) {
        this.removePoint(Number(handleIndex));
        return;
      }
      this.drag = Number(handleIndex);
      // Capture keeps the drag alive if the cursor leaves the small SVG box.
      try { this.svg.setPointerCapture(event.pointerId); } catch (_) { /* optional */ }
      event.target.classList.add('dragging');
      return;
    }
    if (event.button !== 0) return;

    // Clicking empty canvas inserts a point where you clicked.
    const { temp, duty } = this.invert(event.clientX, event.clientY);
    const points = this.points;
    points.push([Math.round(temp), Math.round(duty)]);
    points.sort((a, b) => a[0] - b[0]);
    this.commit();
  }

  onPointerMove(event) {
    if (this.drag === null) return;
    const points = this.points;
    const { temp, duty } = this.invert(event.clientX, event.clientY);

    // Keep points ordered by pinning each one between its neighbours.
    const lower = this.drag > 0 ? points[this.drag - 1][0] + 1 : TMIN;
    const upper = this.drag < points.length - 1 ? points[this.drag + 1][0] - 1 : TMAX;
    points[this.drag] = [
      clamp(Math.round(temp), Math.min(lower, upper), Math.max(lower, upper)),
      Math.round(duty),
    ];
    this.render();
    markDirty();
  }

  onPointerUp(event) {
    if (this.drag === null) return;
    this.drag = null;
    this.svg.querySelectorAll('.dragging').forEach((n) => n.classList.remove('dragging'));
    try { this.svg.releasePointerCapture(event.pointerId); } catch (_) { /* already gone */ }
    this.commit();
  }

  removePoint(index) {
    const points = this.points;
    if (points.length <= 2) {
      toast('A curve needs at least two points', true);
      return;
    }
    points.splice(index, 1);
    this.commit();
  }

  commit() {
    this.render();
    markDirty();
    if (this.onChange) this.onChange();
  }

  setNow(temp, duty) {
    this.now = (temp === null || temp === undefined) ? null : { temp, duty };
    this.render();
  }

  /* -- drawing -- */

  render() {
    const ns = 'http://www.w3.org/2000/svg';
    const make = (tag, attrs) => {
      const node = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      return node;
    };
    while (this.svg.firstChild) this.svg.removeChild(this.svg.firstChild);

    for (let duty = 0; duty <= 100; duty += 25) {
      const y = this.y(duty);
      this.svg.append(make('line', {
        class: 'grid-line', x1: this.pad.l, x2: this.width - this.pad.r, y1: y, y2: y,
      }));
      const label = make('text', { class: 'axis-text', x: this.pad.l - 5, y: y + 3, 'text-anchor': 'end' });
      label.textContent = duty;
      this.svg.append(label);
    }
    for (let temp = TMIN; temp <= TMAX; temp += 20) {
      const x = this.x(temp);
      this.svg.append(make('line', {
        class: 'grid-line', x1: x, x2: x, y1: this.pad.t, y2: this.height - this.pad.b,
      }));
      const label = make('text', {
        class: 'axis-text', x, y: this.height - this.pad.b + 11, 'text-anchor': 'middle',
      });
      label.textContent = `${temp}°`;
      this.svg.append(label);
    }

    const points = this.points;
    // Extend the polyline flat to both edges: that is exactly how the daemon
    // clamps outside the first and last point.
    const path = [[TMIN, points[0][1]], ...points, [TMAX, points[points.length - 1][1]]];
    const coords = path.map((p) => `${this.x(p[0])},${this.y(p[1])}`).join(' ');

    this.svg.append(make('polygon', {
      class: 'curve-fill',
      points: `${this.x(TMIN)},${this.y(0)} ${coords} ${this.x(TMAX)},${this.y(0)}`,
    }));
    this.svg.append(make('polyline', { class: 'curve-line', points: coords }));

    if (this.now) {
      const x = this.x(clamp(this.now.temp, TMIN, TMAX));
      this.svg.append(make('line', {
        class: 'now-line', x1: x, x2: x, y1: this.pad.t, y2: this.height - this.pad.b,
      }));
      this.svg.append(make('circle', {
        class: 'now-dot', cx: x, cy: this.y(clamp(this.now.duty, 0, 100)), r: 3.5,
      }));
    }

    points.forEach((point, index) => {
      const handle = make('circle', {
        class: 'handle', cx: this.x(point[0]), cy: this.y(point[1]), r: 5.5,
      });
      handle.dataset.point = index;
      this.svg.append(handle);
    });
  }
}

/* Why each channel sits where it does: [label, css class]. */
const REASONS = {
  curve: ['following curve', ''],
  fixed: ['fixed duty', ''],
  off: ['off', ''],
  stopped: ['stopped (below threshold)', ''],
  disabled: ['not controlled', ''],
  test: ['test override', 'warn'],
  starting: ['starting…', ''],
  'no-sensors': ['no sensor selected → failsafe', 'warn'],
  'sensors-unavailable': ['sensor lost → failsafe', 'bad'],
  emergency: ['EMERGENCY — over temp', 'bad'],
};

/* -------------------------------------------------------------- fan cards */

function buildFanCard(index) {
  const refs = {};
  const card = el('div', { class: 'card' });

  refs.name = el('input', {
    class: 'name', type: 'text', oninput: () => { fanConfig(index).name = refs.name.value; markDirty(); },
  });
  refs.type = el('span', { class: 'tag', text: '—' });
  refs.reason = el('span', { class: 'tag', text: '—' });
  card.append(el('div', { class: 'card-head' }, refs.name, refs.type, refs.reason));
  refs.note = el('p', { class: 'banner inline', hidden: 'hidden' });
  card.append(refs.note);

  refs.rpm = el('div', { class: 'value', text: '—' });
  refs.dutyValue = el('div', { class: 'value', text: '—' });
  refs.temp = el('div', { class: 'value', text: '—' });
  card.append(el('div', { class: 'readouts' },
    el('div', { class: 'readout' }, refs.rpm, el('div', { class: 'label', text: 'speed' })),
    el('div', { class: 'readout' }, refs.dutyValue, el('div', { class: 'label', text: 'duty' })),
    el('div', { class: 'readout' }, refs.temp, el('div', { class: 'label', text: 'control temp' })),
  ));

  refs.dutyBar = el('div');
  card.append(el('div', { class: 'duty-bar' }, refs.dutyBar));

  refs.mode = el('select', {
    onchange: () => { fanConfig(index).mode = refs.mode.value; markDirty(); syncCard(index); },
  });
  for (const [value, label] of [['curve', 'Curve'], ['fixed', 'Fixed'], ['off', 'Off']]) {
    refs.mode.append(el('option', { value, text: label }));
  }
  refs.enabled = el('input', {
    type: 'checkbox',
    onchange: () => { fanConfig(index).enabled = refs.enabled.checked; markDirty(); },
  });
  refs.identify = el('button', {
    text: 'Identify',
    onclick: async () => {
      try {
        await api(`/api/identify/${index}`, {
          method: 'POST', body: JSON.stringify({ duty: 100, seconds: 5 }),
        });
        toast(`Fan ${index} at 100% for 5 s`);
      } catch (err) { toast(err.message, true); }
    },
  });
  card.append(el('div', { class: 'row' },
    el('div', { class: 'field' }, el('label', { text: 'Mode' }), refs.mode),
    el('div', { class: 'field' }, el('label', { text: 'Controlled' }),
      el('label', {}, refs.enabled, document.createTextNode(' enabled'))),
    el('div', { class: 'field', style: 'flex:0 0 auto' }, refs.identify),
  ));

  refs.fixedRow = el('div', { class: 'row' });
  refs.fixedDuty = el('input', {
    type: 'range', min: '0', max: '100', step: '1',
    oninput: () => {
      fanConfig(index).fixed_duty = Number(refs.fixedDuty.value);
      refs.fixedLabel.textContent = `${refs.fixedDuty.value}%`;
      markDirty();
    },
  });
  refs.fixedLabel = el('span', { class: 'tag', text: '0%' });
  refs.fixedRow.append(
    el('div', { class: 'field' }, el('label', { text: 'Fixed duty' }), refs.fixedDuty),
    refs.fixedLabel,
  );
  card.append(refs.fixedRow);

  refs.curveWrap = el('div', { class: 'curve' });
  refs.editor = new CurveEditor(index, null);
  refs.curveWrap.append(refs.editor.svg);
  refs.curveWrap.append(el('div', {
    class: 'curve-help',
    text: 'Drag a point to move it · click the graph to add one · right-click a point to delete',
  }));
  card.append(refs.curveWrap);

  refs.sensors = el('div', { class: 'sensor-list' });
  refs.mix = el('select', {
    onchange: () => { fanConfig(index).mix = refs.mix.value; markDirty(); },
  });
  for (const [value, label] of [['max', 'Hottest'], ['avg', 'Average'], ['min', 'Coolest']]) {
    refs.mix.append(el('option', { value, text: label }));
  }
  refs.sensorBlock = el('div', {},
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', { text: 'Temperature sources' }), refs.sensors),
    ),
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', { text: 'When several are selected, follow the' }), refs.mix),
    ),
  );
  card.append(refs.sensorBlock);

  const numberField = (key, label, attrs) => {
    const input = el('input', Object.assign({ type: 'number' }, attrs, {
      oninput: () => {
        const value = input.value === '' ? null : Number(input.value);
        fanConfig(index)[key] = value;
        markDirty();
      },
    }));
    refs[key] = input;
    return el('div', { class: 'field' }, el('label', { text: label }), input);
  };

  refs.force_mode = el('select', {
    onchange: () => {
      fanConfig(index).force_mode = refs.force_mode.value || null;
      markDirty();
    },
  });
  for (const [value, label] of [
    ['', 'Auto-detect'], ['dc', 'DC — 3-pin or 2-wire'], ['pwm', 'PWM — 4-pin'],
  ]) {
    refs.force_mode.append(el('option', { value, text: label }));
  }

  const advanced = el('details', { class: 'advanced' }, el('summary', { text: 'Advanced' }));
  advanced.append(
    el('div', { class: 'row' },
      el('div', { class: 'field' }, el('label', { text: 'Fan type' }), refs.force_mode),
      el('div', { class: 'field' }, el('p', {
        class: 'hint-text',
        text: 'Override only if a fan is not detected. 2-wire fans have no tach '
            + 'wire, so they never report RPM and often read as empty.',
      })),
    ),
    el('div', { class: 'row' },
      numberField('min_duty', 'Min duty %', { min: '0', max: '100', step: '1' }),
      numberField('max_duty', 'Max duty %', { min: '0', max: '100', step: '1' }),
      numberField('stop_below', 'Stop below °C', { step: '1', placeholder: 'never' }),
    ),
    el('div', { class: 'row' },
      numberField('hysteresis', 'Hysteresis °C', { min: '0', max: '20', step: '0.5' }),
      numberField('ramp_up', 'Ramp up %/s', { min: '0.5', max: '100', step: '0.5' }),
      numberField('ramp_down', 'Ramp down %/s', { min: '0.5', max: '100', step: '0.5' }),
    ),
    el('div', { class: 'row' },
      numberField('spin_up_duty', 'Spin-up duty %', { min: '0', max: '100', step: '1' }),
      numberField('spin_up_ms', 'Spin-up time ms', { min: '0', max: '5000', step: '50' }),
    ),
  );
  card.append(advanced);

  refs.card = card;
  state.cards.set(index, refs);
  return card;
}

function renderSensorList(index) {
  const refs = state.cards.get(index);
  const fan = fanConfig(index);
  const catalog = (state.snapshot && state.snapshot.sensors) || [];
  refs.sensors.replaceChildren();

  if (!catalog.length) {
    refs.sensors.append(el('div', { class: 'empty', text: 'No temperature sensors detected yet.' }));
    return;
  }

  refs.sensorReadings = new Map();
  // Selected sources first, so a fan's bindings are visible without scrolling
  // the list. Only re-sorted on load/apply, never mid-click.
  const chosen = new Set(fan.sensors);
  const ordered = [...catalog.filter((s) => chosen.has(s.id)),
                   ...catalog.filter((s) => !chosen.has(s.id))];
  for (const sensor of ordered) {
    const checkbox = el('input', {
      type: 'checkbox',
      onchange: () => {
        const selected = new Set(fanConfig(index).sensors);
        if (checkbox.checked) selected.add(sensor.id); else selected.delete(sensor.id);
        fanConfig(index).sensors = catalog.filter((s) => selected.has(s.id)).map((s) => s.id);
        markDirty();
      },
    });
    checkbox.checked = fan.sensors.includes(sensor.id);
    const reading = el('span', { class: 'reading', text: '—' });
    refs.sensorReadings.set(sensor.id, reading);
    refs.sensors.append(el('label', {}, checkbox, document.createTextNode(sensor.label), reading));
  }
}

function syncCard(index) {
  const refs = state.cards.get(index);
  const fan = fanConfig(index);

  refs.name.value = fan.name;
  refs.mode.value = fan.mode;
  refs.enabled.checked = fan.enabled;
  refs.fixedDuty.value = fan.fixed_duty;
  refs.fixedLabel.textContent = `${fan.fixed_duty}%`;
  refs.mix.value = fan.mix;

  for (const key of ['min_duty', 'max_duty', 'hysteresis', 'ramp_up', 'ramp_down',
                     'spin_up_duty', 'spin_up_ms']) {
    refs[key].value = fan[key];
  }
  refs.force_mode.value = fan.force_mode || '';
  refs.stop_below.value = fan.stop_below === null ? '' : fan.stop_below;

  refs.curveWrap.hidden = fan.mode !== 'curve';
  refs.sensorBlock.hidden = fan.mode !== 'curve';
  refs.fixedRow.hidden = fan.mode !== 'fixed';
  refs.editor.render();
}

/* ------------------------------------------------------------ live update */

function updateLive(snapshot) {
  const dot = document.getElementById('conn-dot');
  const text = document.getElementById('conn-text');
  dot.className = `dot ${snapshot.connected ? 'on' : 'off'}`;
  if (snapshot.connected && snapshot.device) {
    const device = snapshot.device;
    text.textContent = `${device.device} · ${device.backend}` +
      (device.firmware ? ` · fw ${device.firmware}` : '') +
      (snapshot.version ? ` · v${snapshot.version}` : '');
  } else {
    text.textContent = 'device not connected';
  }

  const banner = document.getElementById('banner');
  const notice = snapshot.error || snapshot.warning;
  banner.hidden = !notice;
  if (notice) banner.textContent = notice;

  const rails = document.getElementById('rails');
  rails.replaceChildren(...Object.entries(snapshot.volts || {})
    .map(([rail, value]) => el('span', { text: `${rail} ${value.toFixed(2)} V` })));

  const deviceFans = new Map(
    ((snapshot.device && snapshot.device.fans) || []).map((f) => [f.index, f]));

  for (const fanState of snapshot.fans) {
    const refs = state.cards.get(fanState.index);
    if (!refs) continue;
    const info = deviceFans.get(fanState.index);
    const connected = info ? info.connected : false;

    refs.card.classList.toggle('disconnected', !connected);
    refs.type.textContent = connected ? (info.type || 'connected') : 'not connected';
    refs.type.className = info && info.note ? 'tag bad' : 'tag';
    refs.type.title = (info && info.note) || '';
    refs.note.textContent = (info && info.note) || '';
    refs.note.hidden = !(info && info.note);

    const [reasonText, reasonClass] = REASONS[fanState.reason] || [fanState.reason, ''];
    refs.reason.textContent = reasonText;
    refs.reason.className = `tag ${reasonClass}`;

    refs.rpm.innerHTML = fanState.rpm === null || fanState.rpm === undefined
      ? '—' : `${fanState.rpm}<small> rpm</small>`;
    refs.dutyValue.innerHTML = `${fanState.duty.toFixed(0)}<small>%</small>` +
      (fanState.override ? ' <small>(test)</small>' : '');
    refs.temp.innerHTML = fanState.control_temp === null
      ? '—' : `${fanState.control_temp.toFixed(1)}<small>°C</small>`;
    refs.dutyBar.style.width = `${fanState.duty}%`;

    refs.editor.setNow(fanState.control_temp, fanState.duty);

    if (refs.sensorReadings) {
      for (const [id, node] of refs.sensorReadings) {
        const value = snapshot.temps[id];
        node.textContent = value === undefined ? '—' : `${value.toFixed(1)}°`;
      }
    }
  }
}

/* ---------------------------------------------------------------- storage */

function tempClass(drive) {
  const limit = drive.temperature_threshold;
  if (!limit || drive.temperature === null) return '';
  // Warn at 80% of the controller's own threshold, alarm at 90%.
  if (drive.temperature >= limit * 0.9) return 'hot';
  if (drive.temperature >= limit * 0.8) return 'warm';
  return '';
}

function updateStorage(storage) {
  const card = document.getElementById('storage-card');
  if (!storage || !storage.enabled) { card.hidden = true; return; }
  card.hidden = false;

  const errorNode = document.getElementById('storage-error');
  errorNode.hidden = !storage.error;
  if (storage.error) errorNode.textContent = storage.error;

  const stateTag = document.getElementById('storage-state');
  const drives = storage.drives || [];
  if (storage.error && !drives.length) {
    stateTag.textContent = 'unavailable';
    stateTag.className = 'tag bad';
  } else if (storage.stale) {
    stateTag.textContent = 'stale → sensors dropped';
    stateTag.className = 'tag bad';
  } else {
    stateTag.textContent = `${drives.length} drive${drives.length === 1 ? '' : 's'}`;
    stateTag.className = 'tag';
  }

  const age = storage.age === null || storage.age === undefined
    ? 'never polled'
    : `updated ${Math.round(storage.age)}s ago`;
  document.getElementById('storage-age').textContent =
    storage.command ? `${storage.command} · ${age}` : age;

  const body = document.querySelector('#disks tbody');
  body.replaceChildren(...drives.map((drive) => {
    const cell = (value, cls) => el('td', cls ? { class: cls } : {},
      document.createTextNode(value === null || value === undefined ? '—' : String(value)));

    const life = drive.usage_remaining;
    const lifeCell = el('td', { class: 'num' });
    if (life === null || life === undefined) {
      lifeCell.textContent = '—';
    } else {
      lifeCell.append(document.createTextNode(`${life.toFixed(0)}%`));
      const bar = el('div', { class: 'bar' });
      bar.append(el('i', { class: life < 20 ? 'low' : '', style: `width:${clamp(life, 0, 100)}%` }));
      lifeCell.append(bar);
    }

    const health = drive.smart_warning
      ? el('span', { class: 'tag bad', text: 'S.M.A.R.T.' })
      : el('span', { class: 'dim', text: drive.last_failure || 'ok' });

    return el('tr', {},
      cell(drive.slot === null ? `${drive.channel}:${drive.device}` : drive.slot),
      cell(drive.path, 'dim'),
      cell(drive.model, 'dim'),
      cell(drive.serial, 'dim'),
      el('td', { class: `num temp ${tempClass(drive)}` },
         document.createTextNode(drive.temperature === null ? '—' : `${drive.temperature.toFixed(0)}°`)),
      cell(drive.temperature_max === null ? null : `${drive.temperature_max.toFixed(0)}°`, 'num dim'),
      cell(drive.temperature_threshold === null ? null : `${drive.temperature_threshold.toFixed(0)}°`, 'num dim'),
      lifeCell,
      cell(drive.power_on_hours === null ? null : drive.power_on_hours.toFixed(0), 'num dim'),
      el('td', {}, health),
    );
  }));
}

/* ----------------------------------------------------------------- charts */

function seriesForChart() {
  const sensorIds = new Set();
  for (const fan of state.config.fans) {
    if (fan.mode === 'curve') fan.sensors.forEach((id) => sensorIds.add(id));
  }
  const labels = new Map(
    ((state.snapshot && state.snapshot.sensors) || []).map((s) => [s.id, s.label]));

  const series = [];
  let color = 0;
  for (const id of sensorIds) {
    series.push({
      key: id, label: labels.get(id) || id, kind: 'temp',
      color: chartPalette()[color++ % chartPalette().length],
      values: state.history.map((s) => s.temps[id]),
    });
  }
  for (const fan of state.config.fans) {
    const key = String(fan.index);
    if (!state.history.some((s) => s.duty[key] !== undefined)) continue;
    series.push({
      key: `duty${key}`, label: `${fan.name} duty`, kind: 'duty',
      color: chartPalette()[color++ % chartPalette().length],
      values: state.history.map((s) => s.duty[key]),
    });
  }
  return series;
}

function drawChart() {
  const canvas = document.getElementById('chart');
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = width * dpr;
  canvas.height = height * dpr;

  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const pad = { l: 34, r: 12, t: 10, b: 20 };
  const plotW = width - pad.l - pad.r;
  const plotH = height - pad.t - pad.b;

  const styles = getComputedStyle(document.body);
  const lineColor = styles.getPropertyValue('--line').trim() || '#2a323e';
  const mutedColor = styles.getPropertyValue('--muted').trim() || '#8b98a9';

  ctx.strokeStyle = lineColor;
  ctx.fillStyle = mutedColor;
  ctx.font = '10px ui-monospace, monospace';
  ctx.lineWidth = 1;
  for (let value = 0; value <= 100; value += 25) {
    const y = pad.t + plotH - (value / 100) * plotH;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(width - pad.r, y);
    ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(String(value), pad.l - 6, y + 3);
  }

  if (state.history.length < 2) {
    ctx.textAlign = 'center';
    ctx.fillText('collecting data…', width / 2, height / 2);
    document.getElementById('legend').replaceChildren();
    return;
  }

  const times = state.history.map((s) => s.t);
  const tMin = times[0], tMax = times[times.length - 1];
  const span = Math.max(tMax - tMin, 1);
  const xAt = (t) => pad.l + ((t - tMin) / span) * plotW;
  const yAt = (v) => pad.t + plotH - (clamp(v, 0, 100) / 100) * plotH;

  ctx.textAlign = 'center';
  for (let i = 0; i <= 4; i++) {
    const t = tMin + (span * i) / 4;
    const label = new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    ctx.fillText(label, xAt(t), height - 6);
  }

  const series = seriesForChart();
  ctx.lineWidth = 1.6;
  ctx.lineJoin = 'round';
  for (const item of series) {
    if (state.hidden.has(item.key)) continue;
    ctx.strokeStyle = item.color;
    ctx.setLineDash(item.kind === 'duty' ? [4, 3] : []);
    ctx.beginPath();
    let started = false;
    item.values.forEach((value, i) => {
      if (value === undefined || value === null) { started = false; return; }
      const x = xAt(times[i]), y = yAt(value);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    });
    ctx.stroke();
  }
  ctx.setLineDash([]);

  renderLegend(series);
}

/* Each legend entry is a toggle: click hides or shows that series, shift-click
 * shows it on its own. Hidden entries stay in the legend, dimmed, so they can
 * be switched back on. */
function renderLegend(series) {
  const keys = series.map((item) => item.key);
  // Forget entries for series that no longer exist (a sensor was unassigned).
  for (const key of [...state.hidden]) if (!keys.includes(key)) state.hidden.delete(key);

  const items = series.map((item) => {
    const visible = !state.hidden.has(item.key);
    const button = el('button', {
      type: 'button', class: 'legend-item',
      title: visible ? 'Click to hide · shift-click to show only this'
                     : 'Click to show · shift-click to show only this',
      onclick: (event) => {
        if (event.shiftKey) {
          soloSeries(item.key, keys);
        } else if (visible && state.hidden.size === keys.length - 1) {
          // Hiding the last visible series would leave an empty chart; treat
          // it as "show everything" instead, which is what people expect.
          state.hidden.clear();
        } else {
          state.hidden[visible ? 'add' : 'delete'](item.key);
        }
        saveHidden();
        drawChart();
      },
    }, el('i', { style: `background:${item.color}` }), document.createTextNode(item.label));
    button.setAttribute('aria-pressed', String(visible));
    return button;
  });

  const anyHidden = state.hidden.size > 0;
  document.getElementById('legend').replaceChildren(...items);
  const showAll = document.getElementById('btn-legend-all');
  showAll.hidden = !anyHidden;
  document.getElementById('legend-help').hidden = series.length < 2;
}

function soloSeries(key, keys) {
  const alreadySolo = !state.hidden.has(key) && state.hidden.size === keys.length - 1;
  state.hidden = alreadySolo ? new Set() : new Set(keys.filter((k) => k !== key));
}

/* ----------------------------------------------------------------- wiring */

function openSettings() {
  const control = state.config.control;
  document.getElementById('set-interval').value = control.interval;
  document.getElementById('set-backend').value = control.backend;
  document.getElementById('set-failsafe').value = control.failsafe_duty;
  document.getElementById('set-emerg-temp').value = control.emergency_temp;
  document.getElementById('set-emerg-duty').value = control.emergency_duty;
  document.getElementById('set-exit-failsafe').checked = control.apply_failsafe_on_exit;
  document.getElementById('set-reassert').value = control.reassert_seconds;
  document.getElementById('set-history').value = state.config.history.seconds;
  document.getElementById('listen-info').textContent =
    `${state.config.http.bind}:${state.config.http.port}`;
  renderAppearance();
  document.getElementById('settings').showModal();
}

function renderAppearance() {
  const ui = state.config.ui;

  document.querySelectorAll('#theme-seg button').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.themeValue === ui.theme));
  });

  const swatch = (colour, title) => {
    const button = el('button', {
      class: 'swatch', type: 'button', title,
      style: `background:${colour}`,
      onclick: () => selectAccent(colour),
    });
    button.setAttribute('aria-pressed', String(colour === ui.accent));
    return button;
  };

  document.getElementById('accent-presets').replaceChildren(
    ...ACCENT_PRESETS.map(([colour, name]) => swatch(colour, name)));

  const favorites = document.getElementById('accent-favorites');
  if (!ui.favorites.length) {
    favorites.replaceChildren(el('span', {
      class: 'favorites-empty',
      text: 'No favourites yet — pick a colour and press “Save to favourites”.',
    }));
  } else {
    favorites.replaceChildren(...ui.favorites.map((colour) => {
      const button = swatch(colour, colour);
      button.append(el('button', {
        class: 'remove', type: 'button', title: `Remove ${colour}`, text: '×',
        onclick: (event) => { event.stopPropagation(); removeFavorite(colour); },
      }));
      return button;
    }));
  }
  document.getElementById('fav-count').textContent =
    `${ui.favorites.length}/${MAX_FAVORITES}`;

  document.getElementById('accent-picker').value = ui.accent;
  document.getElementById('accent-hex').value = ui.accent;
  document.getElementById('btn-fav-add').disabled =
    ui.favorites.includes(ui.accent) || ui.favorites.length >= MAX_FAVORITES;
}

function selectAccent(colour) {
  if (!hexOk(colour)) return;
  state.config.ui.accent = colour.toLowerCase();
  applyAccent(state.config.ui.accent);   // instant, no Apply needed
  renderAppearance();
  saveUi({ accent: state.config.ui.accent });
}

function addFavorite() {
  const ui = state.config.ui;
  if (ui.favorites.includes(ui.accent)) return;
  if (ui.favorites.length >= MAX_FAVORITES) {
    toast(`Favourites are full (${MAX_FAVORITES}) — remove one first`, true);
    return;
  }
  const favorites = [ui.accent, ...ui.favorites].slice(0, MAX_FAVORITES);
  renderAppearanceAfter(saveUi({ favorites }), favorites);
}

function removeFavorite(colour) {
  const favorites = state.config.ui.favorites.filter((c) => c !== colour);
  renderAppearanceAfter(saveUi({ favorites }), favorites);
}

function renderAppearanceAfter(promise, favorites) {
  state.config.ui.favorites = favorites;
  renderAppearance();
  return promise;
}

function bindAppearance() {
  document.getElementById('theme-seg').addEventListener('click', (event) => {
    const value = event.target.dataset ? event.target.dataset.themeValue : null;
    if (!value) return;
    state.config.ui.theme = value;
    applyTheme(value);
    renderAppearance();
    saveUi({ theme: value });
  });

  // Follow the OS while the preference is "system".
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (state.config && state.config.ui.theme === 'system') applyTheme('system');
  });

  const picker = document.getElementById('accent-picker');
  const hex = document.getElementById('accent-hex');

  // Live preview while dragging round the wheel; only persist on commit.
  picker.addEventListener('input', () => {
    state.config.ui.accent = picker.value.toLowerCase();
    hex.value = state.config.ui.accent;
    applyAccent(state.config.ui.accent);
  });
  picker.addEventListener('change', () => selectAccent(picker.value));

  hex.addEventListener('change', () => {
    const value = hex.value.trim().toLowerCase();
    if (hexOk(value)) {
      selectAccent(value);
    } else {
      toast('Enter a colour as #rrggbb', true);
      hex.value = state.config.ui.accent;
    }
  });

  document.getElementById('btn-fav-add').onclick = addFavorite;
}

function bindSettings() {
  const bind = (id, apply) => {
    document.getElementById(id).addEventListener('input', (event) => {
      apply(event.target);
      markDirty();
    });
  };
  bind('set-interval', (t) => { state.config.control.interval = Number(t.value); });
  bind('set-backend', (t) => { state.config.control.backend = t.value; });
  bind('set-failsafe', (t) => { state.config.control.failsafe_duty = Number(t.value); });
  bind('set-emerg-temp', (t) => { state.config.control.emergency_temp = Number(t.value); });
  bind('set-emerg-duty', (t) => { state.config.control.emergency_duty = Number(t.value); });
  bind('set-exit-failsafe', (t) => { state.config.control.apply_failsafe_on_exit = t.checked; });
  bind('set-reassert', (t) => { state.config.control.reassert_seconds = Number(t.value); });
  bind('set-history', (t) => { state.config.history.seconds = Number(t.value); });

  document.getElementById('settings-close').onclick = () =>
    document.getElementById('settings').close();
  document.getElementById('btn-settings').onclick = openSettings;
  document.getElementById('btn-reconnect').onclick = async () => {
    try {
      await api('/api/reconnect', { method: 'POST' });
      toast('Reconnecting…');
    } catch (err) { toast(err.message, true); }
  };
}

async function applyChanges() {
  try {
    const saved = await api('/api/config', {
      method: 'PUT', body: JSON.stringify(state.config),
    });
    state.config = saved;
    clearDirty();
    for (let i = 1; i <= FAN_COUNT; i++) { syncCard(i); renderSensorList(i); }
    toast('Saved');
  } catch (err) {
    toast(err.message, true);
  }
}

function revertChanges() {
  if (!state.snapshot) return;
  state.config = deepCopy(state.snapshot.config);
  clearDirty();
  for (let i = 1; i <= FAN_COUNT; i++) { syncCard(i); renderSensorList(i); }
  toast('Reverted');
}

async function poll() {
  try {
    const snapshot = await api('/api/state');
    state.snapshot = snapshot;

    if (!state.config) {
      state.config = deepCopy(snapshot.config);
      applyTheme(state.config.ui.theme);
      const container = document.getElementById('fans');
      for (let i = 1; i <= FAN_COUNT; i++) container.append(buildFanCard(i));
      for (let i = 1; i <= FAN_COUNT; i++) { syncCard(i); }
    } else if (!state.dirty) {
      // Adopt changes made elsewhere (another browser, a config file edit).
      const incoming = JSON.stringify(snapshot.config);
      if (incoming !== JSON.stringify(state.config)) {
        const before = JSON.stringify(state.config.ui);
        state.config = deepCopy(snapshot.config);
        for (let i = 1; i <= FAN_COUNT; i++) syncCard(i);
        if (JSON.stringify(state.config.ui) !== before) {
          applyTheme(state.config.ui.theme);   // changed in another browser
          renderAppearance();
        }
      }
    }

    const catalogKey = snapshot.sensors.map((s) => s.id).join('|');
    if (catalogKey !== state.catalogKey) {
      state.catalogKey = catalogKey;
      for (let i = 1; i <= FAN_COUNT; i++) renderSensorList(i);
    }

    updateLive(snapshot);
    updateStorage(snapshot.storage);
  } catch (err) {
    const banner = document.getElementById('banner');
    banner.hidden = false;
    banner.textContent = err.message.includes('401')
      ? 'Unauthorized — open this page with ?token=YOUR_TOKEN'
      : `Cannot reach the service: ${err.message}`;
  }
}

async function pollHistory() {
  if (!state.config) return;
  try {
    const seconds = Number(document.getElementById('chart-range').value);
    const since = Date.now() / 1000 - seconds;
    const data = await api(`/api/history?since=${since.toFixed(0)}&points=500`);
    state.history = data.samples;
    drawChart();
  } catch (_) { /* the state poll already surfaces connection errors */ }
}

function main() {
  initToken();
  bindSettings();
  bindAppearance();
  document.getElementById('btn-apply').onclick = applyChanges;
  document.getElementById('btn-revert').onclick = revertChanges;
  document.getElementById('chart-range').onchange = pollHistory;
  loadHidden();
  document.getElementById('btn-legend-all').onclick = () => {
    state.hidden.clear();
    saveHidden();
    drawChart();
  };
  document.getElementById('btn-storage-refresh').onclick = async () => {
    try {
      await api('/api/storage/refresh', { method: 'POST' });
      toast('Polling the controller…');
    } catch (err) { toast(err.message, true); }
  };
  window.addEventListener('resize', drawChart);
  window.addEventListener('beforeunload', (event) => {
    if (state.dirty) { event.preventDefault(); event.returnValue = ''; }
  });

  poll().then(pollHistory);
  setInterval(poll, POLL_MS);
  setInterval(pollHistory, CHART_MS);
}

main();
