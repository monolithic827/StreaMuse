'use strict';

// Its own window, so its own tiny client: it takes the same snapshot from the same socket as the
// panel and renders only state.dj. Kept separate from app.js rather than shared, because that file
// binds elements this page does not have.

const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

let socket = null;
let appliedTheme = '';

// The host only pushes a new snapshot when something changes (a status transition, a track landing),
// not on a clock - see DjAddon.StateChanged callers. Position would otherwise sit frozen between those
// events, so playback between snapshots is interpolated locally from wall-clock time; it is exact,
// because a track always plays at the speed it was recorded at (see the README on why).
let positionAnchor = { seconds: 0, atMs: 0, duration: 0, playing: false };

function connect() {
  socket = new WebSocket('ws://' + location.host + '/ws');

  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type !== 'state') return;

    applyTheme(message.settings.theme);
    render(message.dj);
  };

  socket.onclose = () => setTimeout(connect, 1000);
}

function render(dj) {
  const title = document.getElementById('dj-title');
  const artist = document.getElementById('dj-artist');
  const status = document.getElementById('dj-status');
  const tag = document.getElementById('dj-tag');
  const skip = document.getElementById('dj-skip');

  if (!dj) {
    title.textContent = 'DJ plugin not loaded';
    artist.textContent = '';
    status.textContent = 'Install it in Settings → Plugins.';
    tag.textContent = 'off';
    skip.disabled = true;
    positionAnchor = { seconds: 0, atMs: 0, duration: 0, playing: false };
    renderArt(0);
    renderProgress();
    return;
  }

  const playing = dj.nowMixing;
  title.textContent = playing ? playing.title : 'Nothing playing';
  artist.textContent = playing ? [playing.artist, dj.album].filter(Boolean).join(' · ') : '';
  status.textContent = dj.phaseText;
  skip.disabled = !playing && dj.queue.length === 0;

  const beatmatched = dj.confidencePercent !== null && dj.confidencePercent !== undefined;
  tag.textContent = playing ? (beatmatched ? 'beatmatched' : 'mixing') : 'idle';
  tag.style.cssText = playing
    ? 'background:var(--color-accent);color:var(--color-bg)'
    : 'border:1px solid var(--color-divider);color:color-mix(in srgb, var(--color-text) 50%, transparent)';

  positionAnchor = {
    seconds: dj.positionSeconds || 0,
    atMs: Date.now(),
    duration: dj.durationSeconds || 0,
    playing: !!playing
  };

  renderArt(dj.artworkVersion || 0);
  renderProgress();
  renderQueue(dj.queue);
}

function renderArt(version) {
  const img = document.getElementById('dj-art');
  const empty = document.getElementById('dj-art-empty');
  const has = version > 0;

  if (img.dataset.version !== String(version)) {
    img.dataset.version = String(version);
    if (has) img.src = '/api/dj/art?v=' + version;
  }

  img.hidden = !has;
  empty.hidden = has;
}

function renderProgress() {
  const fill = document.getElementById('dj-progress-fill');
  const elapsed = document.getElementById('dj-elapsed');
  const duration = document.getElementById('dj-duration');

  const { seconds, atMs, duration: total, playing } = positionAnchor;
  const current = playing ? Math.min(total || Infinity, seconds + (Date.now() - atMs) / 1000) : 0;

  fill.style.width = (total > 0 ? Math.min(100, (current / total) * 100) : 0) + '%';
  elapsed.textContent = formatClock(current);
  duration.textContent = total > 0 ? formatClock(total) : '--:--';
}

function formatClock(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function renderQueue(entries) {
  const container = document.getElementById('dj-queue');

  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'log-line';

    const message = document.createElement('span');
    message.className = 'msg';
    message.textContent = 'nothing queued';

    empty.append(message);
    container.replaceChildren(empty);
    return;
  }

  container.replaceChildren(...entries.map(entry => {
    const row = document.createElement('div');
    row.className = 'log-line';

    const level = document.createElement('span');
    level.className = 'lvl';
    level.textContent = entry.status;

    const message = document.createElement('span');
    message.className = 'msg';
    message.textContent = entry.artist ? entry.artist + ' - ' + entry.title : entry.title;

    row.append(level, message);
    return row;
  }));
}

// Auto carries no attribute so the media query paints it, exactly as in the panel.
function applyTheme(mode) {
  const dark = mode === 'Dark' || (mode === 'Auto' && darkQuery.matches);
  const signature = mode + (dark ? ' dark' : ' light');

  if (signature === appliedTheme) return;
  appliedTheme = signature;

  if (mode === 'Auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = mode.toLowerCase();
}

function toHost(command) {
  if (window.chrome && window.chrome.webview) window.chrome.webview.postMessage({ command: command });
}

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
}

document.getElementById('tb-min').onclick = () => toHost('minimize');
document.getElementById('tb-close').onclick = () => toHost('close');

document.getElementById('titlebar').addEventListener('mousedown', event => {
  if (event.button !== 0 || event.target.closest('.titlebar-btn')) return;
  toHost('drag');
});

document.getElementById('dj-request').onclick = async () => {
  const input = document.getElementById('dj-query');
  const hint = document.getElementById('dj-hint');
  const query = input.value.trim();
  if (!query) return;

  try {
    await post('/api/dj/request', { query: query });
    input.value = '';
    hint.textContent = 'Fetching “' + query + '”…';
  } catch (error) {
    hint.textContent = error.message;
  }
};

document.getElementById('dj-query').addEventListener('keydown', event => {
  if (event.key === 'Enter') document.getElementById('dj-request').click();
});

document.getElementById('dj-skip').onclick = async () => {
  try {
    await post('/api/dj/skip');
  } catch (error) {
    document.getElementById('dj-hint').textContent = error.message;
  }
};

darkQuery.addEventListener('change', () => { appliedTheme = ''; });

setInterval(renderProgress, 250);

connect();
