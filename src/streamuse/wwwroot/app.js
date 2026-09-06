'use strict';

const METER_BARS = 34;
const SOURCE_LABELS = { apple: 'Apple Music', spotify: 'Spotify', device: 'Playback Device' };
const THEMES = ['Auto', 'Dark', 'Light'];

const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

let state = null;
let settings = null;
let copiedUntil = 0;
let copiedKind = '';

// Both buttons copy the same stream URL; "Web" copies its directory, where the listener page is.
const copyLabels = { hls: 'Copy', page: 'Web' };
let socket = null;
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
  const connected = source.connected;

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
    codecLine: running ? 'AAC 48 kHz · received' : (connected ? 'ready' : 'no stream'),

    track: now.title || 'Nothing playing',
    artist: now.artist || '-',
    album: now.album || '-',
    elapsed: clock(now.positionSeconds),
    duration: now.durationSeconds > 0 ? clock(now.durationSeconds) : '--:--',
    progressFill: { style: { width: progress + '%' } },

    connLabel: source.statusText,
    connDot: {
      style: {
        background: connected ? 'var(--color-accent)' : 'var(--color-neutral-400)',
        animation: connected ? 'su-pulse 1.8s ease-in-out infinite' : 'none'
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

    device: source.client || 'nothing connected',
    encoderShort: running ? 'x264 · ' + settings.width + 'x' + settings.height : 'encoder idle',
    bitrateLabel: encoder.bitrateKbps > 0 ? encoder.bitrateKbps + ' kbps' : '- kbps',
    uptimeLabel: 'up ' + clock(encoder.uptimeSeconds),

    running: running,
    connected: connected
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

  for (const element of document.querySelectorAll('[data-bind-show]')) {
    element.hidden = !view[element.dataset.bindShow];
  }

  for (const button of document.querySelectorAll('[data-main-button]')) {
    button.classList.toggle('btn-primary', !view.running);
    button.classList.toggle('btn-secondary', view.running);
  }

  const tunnelButton = document.querySelector('[data-tunnel-button]');
  tunnelButton.textContent = state.tunnel.status === 'off' || state.tunnel.status === 'error'
    ? 'Start tunnel'
    : 'Stop tunnel';

  for (const button of document.querySelectorAll('[data-copy]')) {
    button.textContent = Date.now() < copiedUntil && button.dataset.copy === copiedKind
      ? 'Copied'
      : copyLabels[button.dataset.copy];
  }

  renderCover();
  renderHealth(view);
  renderLog();
  renderDeps();
  applyTheme();
}

// Auto carries no attribute, so the media query in app.css picks the palette.
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
    label.title = available ? '' : option.reason;

    radio.disabled = !available;
    radio.checked = radio.value === state.source.source;
  }

  const onDevice = state.source.source === 'device';
  document.getElementById('device-picker').hidden = !onDevice;
  if (onDevice && !deviceOptionsLoaded) populateDeviceOptions();
}

let deviceOptionsLoaded = false;

async function populateDeviceOptions() {
  // Set first, not after: a fetch failure must not retry on every render tick.
  deviceOptionsLoaded = true;
  const select = document.getElementById('set-device');

  try {
    const response = await fetch('/api/devices');
    const names = await response.json();

    select.replaceChildren(...names.map(name => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      return option;
    }));

    select.value = settings.deviceCaptureName;
  } catch { /* left empty - the field itself still shows unavailable via renderSourceOptions */ }
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
  document.getElementById('set-name').value = settings.receiverName;
  document.getElementById('set-spotify-name').value = settings.spotifyConnectDeviceName;
  document.getElementById('set-key').value = settings.streamKey;
  document.getElementById('set-vbr').value = settings.videoBitrateKbps;
  document.getElementById('set-abr').value = settings.audioBitrateKbps;
  document.getElementById('set-fps').value = settings.fps;
  document.getElementById('set-overlay').checked = settings.textOverlay;
  document.getElementById('set-token').value = settings.namedTunnelToken;
  document.getElementById('set-host').value = settings.namedTunnelHostname;
  document.getElementById('set-autotunnel').checked = settings.autoTunnel;
  document.getElementById('set-dj-crossfade').value = settings.djCrossfadeSeconds;
  document.getElementById('set-dj-sfx').checked = settings.djSfxEnabled;
  document.getElementById('set-dj-concurrency').value = settings.djLibraryConcurrency;

  const resolution = settings.width + 'x' + settings.height;
  for (const radio of document.querySelectorAll('input[name="res"]')) {
    radio.checked = radio.value === resolution;
  }
  for (const radio of document.querySelectorAll('input[name="tmode"]')) {
    radio.checked = radio.value === settings.tunnelMode;
  }
  for (const radio of document.querySelectorAll('[data-dj-mode]')) {
    radio.checked = radio.value === settings.djMode;
  }

  document.getElementById('named-fields').hidden = settings.tunnelMode !== 'Named';
  syncSettingLabels();
}

function syncSettingLabels() {
  document.getElementById('lbl-vbr').textContent = document.getElementById('set-vbr').value + ' kbps';
  document.getElementById('lbl-abr').textContent = document.getElementById('set-abr').value + ' kbps';
  document.getElementById('lbl-fps').textContent = document.getElementById('set-fps').value + ' fps';
  document.getElementById('lbl-dj-crossfade').textContent = document.getElementById('set-dj-crossfade').value + 's';
  document.getElementById('lbl-dj-concurrency').textContent = document.getElementById('set-dj-concurrency').value;
}

function readSettings() {
  const resolution = (document.querySelector('input[name="res"]:checked') || {}).value || '1280x720';
  const [width, height] = resolution.split('x').map(Number);

  return {
    receiverName: document.getElementById('set-name').value.trim(),
    spotifyConnectDeviceName: document.getElementById('set-spotify-name').value.trim() || 'StreaMuse',
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
    autoTunnel: document.getElementById('set-autotunnel').checked,
    djCrossfadeSeconds: Number(document.getElementById('set-dj-crossfade').value),
    djSfxEnabled: document.getElementById('set-dj-sfx').checked,
    djMode: (document.querySelector('[data-dj-mode]:checked') || {}).value || 'radio',
    djLibraryConcurrency: Number(document.getElementById('set-dj-concurrency').value)
  };
}

// ── wiring ──────────────────────────────────────────────────────────────────

async function saveSettings(patch) {
  await post('/api/settings', Object.assign({}, settings, patch));
}

document.getElementById('btn-log').onclick = () => saveSettings({ logExpanded: !settings.logExpanded });

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

for (const id of ['set-vbr', 'set-abr', 'set-fps', 'set-dj-crossfade', 'set-dj-concurrency']) {
  document.getElementById(id).addEventListener('input', syncSettingLabels);
}

// window.pywebview.api starts as {} and is only populated with real methods once pywebview
// dispatches "pywebviewready" - calling open_dj() before that fires calls a plain object property,
// throwing "open_dj is not a function" instead of doing anything.
let pywebviewReady = false;
window.addEventListener('pywebviewready', () => { pywebviewReady = true; });

document.getElementById('btn-dj').onclick = () => {
  if (pywebviewReady && window.pywebview && typeof window.pywebview.api.open_dj === 'function') {
    window.pywebview.api.open_dj();
  }
};

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

document.getElementById('set-device').addEventListener('change', event => {
  saveSettings({ deviceCaptureName: event.target.value });
});

for (const button of document.querySelectorAll('[data-player]')) {
  button.addEventListener('click', () => post('/api/player/' + button.dataset.player));
}

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
    const playlist = state.tunnel.publicUrl || state.localUrl || '';
    const url = button.dataset.copy === 'page' ? playlist.replace(/index\.m3u8$/, '') : playlist;
    try { await navigator.clipboard.writeText(url); } catch { /* clipboard blocked */ }
    copiedKind = button.dataset.copy;
    copiedUntil = Date.now() + 1600;
    render();
    setTimeout(render, 1700);
  });
}

connect();
