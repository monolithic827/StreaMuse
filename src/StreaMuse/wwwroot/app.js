'use strict';

const METER_BARS = 34;
const SOURCE_LABELS = { apple: 'Apple Music', spotify: 'Spotify', external: 'External' };
const THEMES = ['Auto', 'Dark', 'Light'];

const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

let state = null;
let settings = null;
let copiedUntil = 0;
let socket = null;
let reportedDetailsHeight = -1;
let appliedTheme = '';

// ── transport ───────────────────────────────────────────────────────────────

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).detail || ''; } catch { /* no body */ }
    throw new Error(detail || (path + ' → ' + response.status));
  }
  return response;
}

function connect() {
  socket = new WebSocket('ws://' + location.host + '/ws');

  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === 'state') {
      state = message;
      settings = message.settings;
      render();
    } else if (message.type === 'meter') {
      renderMeter(message.bars, message.peakDb, message.signal);
    } else if (message.type === 'log') {
      pushLog(message.line);
    }
  };

  // Local backend: a dropped socket means a restart, so retry quietly.
  socket.onclose = () => setTimeout(connect, 1000);
}

// ── view model ──────────────────────────────────────────────────────────────

function buildView() {
  const source = state.source;
  const now = state.nowPlaying;
  const encoder = state.encoder;
  const tunnel = state.tunnel;

  const running = encoder.status === 'running' || encoder.status === 'starting';
  const detected = source.detected;

  const publicUrl = tunnel.publicUrl || state.localUrl || '-';
  const progress = now.durationSeconds > 0
    ? Math.min(100, (now.positionSeconds / now.durationSeconds) * 100)
    : 0;

  const statusStyles = {
    idle: 'border:1px solid var(--color-divider);color:color-mix(in srgb, var(--color-text) 50%, transparent)',
    starting: 'background:var(--color-accent-200);color:var(--color-accent-800)',
    running: 'background:var(--color-accent);color:var(--color-bg)',
    error: 'background:var(--color-accent-900);color:var(--color-bg)'
  };

  const statusLabels = { idle: 'Idle', starting: 'Starting', running: 'Streaming', error: 'Error' };

  return {
    sourceName: SOURCE_LABELS[source.source] || source.source,
    externalMode: source.source === 'external',
    codecLine: running ? 'AAC 48 kHz · captured' : (detected ? 'ready' : 'no stream'),

    track: now.title,
    artist: now.artist,
    album: now.album,
    elapsed: clock(now.positionSeconds),
    duration: now.durationSeconds > 0 ? clock(now.durationSeconds) : '--:--',
    progressFill: { style: { width: progress + '%' } },

    connLabel: source.statusText,
    connDot: {
      style: {
        background: detected ? 'var(--color-accent)' : 'var(--color-neutral-400)',
        animation: detected ? 'su-pulse 1.8s ease-in-out infinite' : 'none'
      }
    },

    statusTag: { text: statusLabels[encoder.status] || encoder.status, style: statusStyles[encoder.status] },
    mainBtnLabel: running ? 'Stop stream' : 'Start stream',

    url: { text: publicUrl, title: publicUrl },

    tunnelLabel: tunnelText(tunnel),
    tunnelDot: {
      style: {
        background: tunnel.status === 'up' ? 'var(--color-accent)'
          : tunnel.status === 'error' ? 'var(--color-accent-900)'
            : 'var(--color-neutral-400)'
      }
    },

    device: detected ? source.processName + ' · pid ' + source.processId : 'no capture target',
    encoderShort: running ? 'x264 · ' + settings.width + 'x' + settings.height : 'encoder idle',
    bitrateLabel: encoder.bitrateKbps > 0 ? encoder.bitrateKbps + ' kbps' : '- kbps',
    uptimeLabel: 'up ' + clock(encoder.uptimeSeconds),

    running: running,
    detected: detected
  };
}

function tunnelText(tunnel) {
  if (tunnel.status === 'up') return 'Tunnel up - public';
  if (tunnel.status === 'starting') return 'Tunnel connecting…';
  if (tunnel.status === 'error') return 'Tunnel error - ' + (tunnel.error || 'see log');
  return 'Tunnel off - local only';
}

// ── rendering ───────────────────────────────────────────────────────────────

function render() {
  if (!state) return;
  const view = buildView();

  for (const element of document.querySelectorAll('[data-bind]')) {
    apply(element, view[element.dataset.bind]);
  }

  document.body.classList.toggle('log-closed', !settings.logExpanded);

  renderSourceOptions(view);
  renderTargets();

  for (const element of document.querySelectorAll('[data-bind-show]')) {
    element.hidden = !view[element.dataset.bindShow];
  }

  for (const button of document.querySelectorAll('[data-main-button]')) {
    button.disabled = !view.detected && !view.running;
    button.classList.toggle('btn-primary', !view.running);
    button.classList.toggle('btn-secondary', view.running);
  }

  const tunnelButton = document.querySelector('[data-tunnel-button]');
  tunnelButton.textContent = state.tunnel.status === 'off' || state.tunnel.status === 'error'
    ? 'Start tunnel'
    : 'Stop tunnel';

  for (const button of document.querySelectorAll('[data-copy]')) {
    button.textContent = Date.now() < copiedUntil ? 'Copied' : 'Copy';
  }

  renderCover();
  renderHealth(view);
  renderLog();
  renderDeps();
  reportDetailsHeight();
  applyTheme();
}

// Auto carries no attribute, so the media query in app.css picks the palette; the host is told the
// colour it resolved to either way, since the window is what shows through a resize.
function applyTheme() {
  const mode = settings.theme;
  const dark = mode === 'Dark' || (mode === 'Auto' && darkQuery.matches);
  const signature = mode + (dark ? ' dark' : ' light');

  if (signature === appliedTheme) return;
  appliedTheme = signature;

  if (mode === 'Auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = mode.toLowerCase();

  const button = document.getElementById('btn-theme');
  for (const icon of button.querySelectorAll('[data-theme-icon]')) {
    icon.style.display = icon.dataset.themeIcon === mode ? '' : 'none';
  }
  button.title = 'Theme - ' + (mode === 'Auto' ? 'auto · ' + (dark ? 'dark' : 'light') : mode.toLowerCase());

  toHost('theme', { dark: dark });
}

function reportDetailsHeight() {
  const pane = document.getElementById('log-panel');
  const open = !document.body.classList.contains('log-closed');
  const height = open ? Math.ceil(pane.getBoundingClientRect().height) : 0;

  if (height === reportedDetailsHeight) return;
  reportedDetailsHeight = height;
  toHost('detailsHeight', { height: height });
}

function apply(element, value) {
  if (value === undefined || value === null) return;

  if (typeof value === 'object') {
    if (value.text !== undefined) element.textContent = value.text;
    if (value.title !== undefined) element.title = value.title;

    if (typeof value.style === 'string') element.style.cssText = value.style;
    else if (value.style) Object.assign(element.style, value.style);

    return;
  }

  element.textContent = value;
}

// state.source.source is what the backend resolved, not necessarily the stored preference.
function renderSourceOptions(view) {
  const options = state.source.options || [];

  for (const label of document.querySelectorAll('[data-source-opt]')) {
    const name = label.dataset.sourceOpt;
    const option = options.find(o => o.source === name);
    const available = !option || option.available;
    const radio = label.querySelector('input');

    label.classList.toggle('unavailable', !available);
    label.title = available
      ? (name === 'external' ? 'Capture any process - pick it below' : '')
      : option.reason;

    radio.disabled = !available;
    radio.checked = radio.value.toLowerCase() === state.source.source;
  }
}

function renderCover() {
  const version = state.nowPlaying.artworkVersion;
  const has = version > 0;

  for (const img of document.querySelectorAll('[data-bind="cover"]')) {
    if (img.dataset.version !== String(version)) {
      img.dataset.version = String(version);
      if (has) img.src = '/api/art?v=' + version;
    }
    img.hidden = !has;
  }

  const empty = document.querySelector('[data-bind="cover-empty"]');
  if (empty) empty.hidden = has;
}

function renderMeter(bars, peakDb, signal) {
  for (const container of document.querySelectorAll('[data-meter]')) {
    if (container.childElementCount !== METER_BARS) {
      container.replaceChildren(...Array.from({ length: METER_BARS }, () => document.createElement('i')));
    }
    container.style.opacity = signal ? '1' : '0.35';

    const children = container.children;
    for (let i = 0; i < METER_BARS; i++) {
      children[i].style.height = (bars[i] || 4) + '%';
    }
  }

  const peakText = (peakDb === null || peakDb === undefined || !isFinite(peakDb))
    ? 'peak −∞ dB'
    : 'peak ' + peakDb.toFixed(1) + ' dB';
  setText('signalLabel', signal ? 'Audio detected · 48 kHz / 2 ch' : 'No signal');
  setText('peakLabel', peakText);
}

function renderHealth(view) {
  const rows = [
    ['Encoder', view.running ? 'x264 veryfast' : 'stopped'],
    ['Video', settings.width + '×' + settings.height + ' · ' + settings.fps + ' fps · ' + settings.videoBitrateKbps + ' kbps'],
    ['Audio', 'AAC ' + settings.audioBitrateKbps + ' kbps · 48 kHz'],
    ['Uptime', clock(state.encoder.uptimeSeconds)],
    ['Dropped frames', String(state.encoder.droppedFrames)]
  ];

  document.getElementById('health-rows').replaceChildren(...rows.map(([key, value]) => {
    const tr = document.createElement('tr');
    const th = document.createElement('th');
    const td = document.createElement('td');
    th.textContent = key;
    td.textContent = value;
    tr.append(th, td);
    return tr;
  }));
}

function renderLog() {
  document.getElementById('log-list').replaceChildren(...state.log.map(makeLogLine));
}

function pushLog(line) {
  if (!state) return;
  state.log.unshift(line);
  state.log.length = Math.min(state.log.length, 200);

  const list = document.getElementById('log-list');
  list.prepend(makeLogLine(line));
  while (list.childElementCount > 200) list.lastElementChild.remove();
}

function makeLogLine(line) {
  const row = document.createElement('div');
  row.className = 'log-line ' + line.level;

  const time = document.createElement('span');
  time.className = 't';
  time.textContent = line.time;

  const level = document.createElement('span');
  level.className = 'lvl';
  level.textContent = line.level;

  const message = document.createElement('span');
  message.className = 'msg';
  message.textContent = line.message;

  row.append(time, level, message);
  return row;
}

function renderDeps() {
  const list = document.getElementById('deps-list');
  list.replaceChildren(...state.dependencies.map(dep => {
    const row = document.createElement('div');
    row.className = 'dep' + (dep.present ? ' ok' : '');

    const dot = document.createElement('i');
    const name = document.createElement('span');
    name.textContent = dep.name;

    const path = document.createElement('span');
    path.className = 'path';
    path.textContent = dep.present ? dep.path : 'not found';
    path.title = path.textContent;

    row.append(dot, name, path);
    return row;
  }));
}

function setText(binding, text) {
  for (const element of document.querySelectorAll('[data-bind="' + binding + '"]')) {
    element.textContent = text;
  }
}

function clock(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = n => String(n).padStart(2, '0');
  return h > 0 ? h + ':' + pad(m) + ':' + pad(s) : m + ':' + pad(s);
}

// ── settings dialog ─────────────────────────────────────────────────────────

function openSettings() {
  fillSettings();
  document.getElementById('settings').hidden = false;
}

function fillSettings() {
  document.getElementById('set-key').value = settings.streamKey;
  document.getElementById('set-vbr').value = settings.videoBitrateKbps;
  document.getElementById('set-abr').value = settings.audioBitrateKbps;
  document.getElementById('set-fps').value = settings.fps;
  document.getElementById('set-overlay').checked = settings.textOverlay;
  document.getElementById('set-token').value = settings.namedTunnelToken;
  document.getElementById('set-host').value = settings.namedTunnelHostname;
  document.getElementById('set-autotunnel').checked = settings.autoTunnel;

  const resolution = settings.width + 'x' + settings.height;
  for (const radio of document.querySelectorAll('input[name="res"]')) {
    radio.checked = radio.value === resolution;
  }
  for (const radio of document.querySelectorAll('input[name="tmode"]')) {
    radio.checked = radio.value === settings.tunnelMode;
  }

  document.getElementById('named-fields').hidden = settings.tunnelMode !== 'Named';
  syncSettingLabels();
}

function syncSettingLabels() {
  document.getElementById('lbl-vbr').textContent = document.getElementById('set-vbr').value + ' kbps';
  document.getElementById('lbl-abr').textContent = document.getElementById('set-abr').value + ' kbps';
  document.getElementById('lbl-fps').textContent = document.getElementById('set-fps').value + ' fps';
}

function readSettings() {
  const resolution = (document.querySelector('input[name="res"]:checked') || {}).value || '1280x720';
  const [width, height] = resolution.split('x').map(Number);

  return {
    streamKey: document.getElementById('set-key').value.trim(),
    width: width,
    height: height,
    fps: Number(document.getElementById('set-fps').value),
    videoBitrateKbps: Number(document.getElementById('set-vbr').value),
    audioBitrateKbps: Number(document.getElementById('set-abr').value),
    textOverlay: document.getElementById('set-overlay').checked,
    tunnelMode: (document.querySelector('input[name="tmode"]:checked') || {}).value || 'Quick',
    namedTunnelToken: document.getElementById('set-token').value.trim(),
    namedTunnelHostname: document.getElementById('set-host').value.trim(),
    autoTunnel: document.getElementById('set-autotunnel').checked
  };
}

// The DOM is only rebuilt when the options change, which keeps an open dropdown from closing
// under the user.
function renderTargets() {
  const select = document.getElementById('target-select');
  const targets = state.source.targets || [];
  const chosen = settings.manualProcessId || 0;

  const options = [{ pid: 0, label: 'Auto - loudest' }].concat(
    targets.map(t => ({
      pid: t.pid,
      label: t.name + ' · pid ' + t.pid + (t.active ? ' · playing' : '')
    })));

  // A process that has gone quiet drops off the list; keep the current pick visible rather than
  // leaving the control blank and the selection apparently lost.
  if (chosen && !options.some(o => o.pid === chosen)) {
    options.push({ pid: chosen, label: (state.source.processName || 'pid ' + chosen) + ' · not playing' });
  }

  const signature = options.map(o => o.pid + ':' + o.label).join('|');
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.replaceChildren(...options.map(o => {
      const element = document.createElement('option');
      element.value = String(o.pid);
      element.textContent = o.label;
      return element;
    }));
  }

  if (select.value !== String(chosen)) select.value = String(chosen);
}

// ── wiring ──────────────────────────────────────────────────────────────────

function toHost(command, extra) {
  if (window.chrome && window.chrome.webview) {
    window.chrome.webview.postMessage(Object.assign({ command: command }, extra || {}));
  }
}

async function saveSettings(patch) {
  await post('/api/settings', Object.assign({}, settings, patch));
}

document.getElementById('tb-min').onclick = () => toHost('minimize');
document.getElementById('tb-max').onclick = () => toHost('maximize');
document.getElementById('tb-close').onclick = () => toHost('close');

document.getElementById('titlebar').addEventListener('mousedown', event => {
  if (event.button !== 0 || event.target.closest('.titlebar-btn')) return;
  toHost(event.detail === 2 ? 'maximize' : 'drag');
});

document.getElementById('btn-log').onclick = () => saveSettings({ logExpanded: !settings.logExpanded });

// The pane also grows as the health table and log fill in, which no render() need follow.
new ResizeObserver(reportDetailsHeight).observe(document.getElementById('log-panel'));

document.getElementById('btn-theme').onclick = () => {
  saveSettings({ theme: THEMES[(THEMES.indexOf(settings.theme) + 1) % THEMES.length] });
};

darkQuery.addEventListener('change', () => { if (settings) applyTheme(); });

document.getElementById('btn-settings').onclick = openSettings;
document.getElementById('btn-cancel').onclick = () => { document.getElementById('settings').hidden = true; };

document.getElementById('btn-save').onclick = async () => {
  try {
    await saveSettings(readSettings());
    document.getElementById('settings').hidden = true;
  } catch (error) {
    alert('Could not save settings: ' + error.message);
  }
};

document.getElementById('btn-deps').onclick = async () => {
  try { await post('/api/deps/refresh'); } catch { /* logged server-side */ }
};

for (const id of ['set-vbr', 'set-abr', 'set-fps']) {
  document.getElementById(id).addEventListener('input', syncSettingLabels);
}

for (const radio of document.querySelectorAll('input[name="tmode"]')) {
  radio.addEventListener('change', () => {
    document.getElementById('named-fields').hidden = radio.value !== 'Named' || !radio.checked;
  });
}

for (const radio of document.querySelectorAll('[data-source]')) {
  radio.addEventListener('change', () => {
    if (radio.checked) saveSettings({ source: radio.value });
  });
}

document.getElementById('target-select').addEventListener('change', event => {
  saveSettings({ manualProcessId: Number(event.target.value) });
});

for (const button of document.querySelectorAll('[data-main-button]')) {
  button.addEventListener('click', async () => {
    const running = state.encoder.status === 'running' || state.encoder.status === 'starting';
    try {
      await post(running ? '/api/stream/stop' : '/api/stream/start');
    } catch (error) {
      alert(error.message);
    }
  });
}

document.querySelector('[data-tunnel-button]').addEventListener('click', async () => {
  const up = state.tunnel.status === 'up' || state.tunnel.status === 'starting';
  try {
    await post(up ? '/api/tunnel/stop' : '/api/tunnel/start');
  } catch (error) {
    alert(error.message);
  }
});

for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    const url = state.tunnel.publicUrl || state.localUrl || '';
    try { await navigator.clipboard.writeText(url); } catch { /* clipboard blocked */ }
    copiedUntil = Date.now() + 1600;
    render();
    setTimeout(render, 1700);
  });
}

connect();
