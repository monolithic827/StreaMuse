'use strict';

/* Polls the read-only feed beside this page. URLs are relative so the page works unchanged under a
   tunnel hostname and 127.0.0.1 alike. */

const POLL_MS = 2000;
const TICK_MS = 500;

const el = {
  status: document.getElementById('status'),
  statusText: document.getElementById('status-text'),
  eyebrow: document.getElementById('eyebrow'),
  title: document.getElementById('title'),
  artist: document.getElementById('artist'),
  album: document.getElementById('album'),
  cover: document.getElementById('cover'),
  artEmpty: document.getElementById('art-empty'),
  progress: document.getElementById('progress'),
  fill: document.getElementById('fill'),
  elapsed: document.getElementById('elapsed'),
  duration: document.getElementById('duration')
};

let track = null;
let receivedAt = 0;
let lastPoll = 0;
let polling = false;
let artVersion = null;
let reachable = false;

function time(seconds) {
  if (!(seconds > 0)) return '0:00';
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

/* Position is pushed by the source only on play/pause/seek, so the host extrapolates it and the bar
   carries on between polls rather than stepping once every two seconds. */
function position() {
  if (!track || !track.playing) return track ? track.positionSeconds : 0;

  const advanced = track.positionSeconds + (Date.now() - receivedAt) / 1000;
  return track.durationSeconds > 0 ? Math.min(advanced, track.durationSeconds) : advanced;
}

/* The version is a string: it is a 63-bit hash, and JSON.parse rounds one past 2^53 into a number
   the host would not recognise as its own. */
function renderArt(version) {
  if (version === artVersion) return;
  artVersion = version;

  if (version !== '0') {
    el.cover.src = `art?v=${version}`;
    el.cover.hidden = false;
    el.artEmpty.hidden = true;
    return;
  }

  el.cover.removeAttribute('src');
  el.cover.hidden = true;
  el.artEmpty.hidden = false;
}

// Clearing the version is what lets the next poll retry: a dropped request would otherwise leave
// "No artwork" until the cover itself changes.
el.cover.addEventListener('error', () => {
  artVersion = null;
  el.cover.hidden = true;
  el.artEmpty.hidden = false;
});

function renderProgress() {
  const live = reachable && track && track.live;
  const duration = live && track ? track.durationSeconds : 0;

  el.progress.hidden = !(duration > 0);
  if (!(duration > 0)) return;

  const at = position();
  el.fill.style.width = `${Math.min(100, (at / duration) * 100)}%`;
  el.elapsed.textContent = time(at);
  el.duration.textContent = time(duration);
}

function render() {
  const live = reachable && track && track.live;

  document.body.dataset.live = live ? 'true' : 'false';
  el.status.dataset.live = live ? 'true' : 'false';
  el.statusText.textContent = !reachable ? 'Unreachable' : live ? 'Live' : 'Off air';

  if (!live) {
    el.eyebrow.textContent = reachable ? 'Nothing streaming' : 'Lost contact';
    el.title.textContent = reachable ? 'Off air' : 'Trying to reconnect…';
    el.artist.textContent = '';
    el.album.textContent = '';
    renderArt('0');
    renderProgress();
    return;
  }

  const title = track.title;

  el.eyebrow.textContent = title ? 'Now playing' : 'On air';
  el.title.textContent = title || 'No track info';
  el.artist.textContent = title ? track.artist : '';
  el.album.textContent = title ? track.album : '';

  renderArt(track.artworkVersion);
  renderProgress();
}

/* One at a time, and the interval is counted from the response: a request slower than POLL_MS would
   otherwise have a second one started under it, and the two can land out of order - stamping stale
   fields with a fresh receivedAt, which walks the progress bar backwards. */
async function poll() {
  if (polling) return;
  polling = true;

  try {
    const response = await fetch('now', { cache: 'no-store' });
    if (!response.ok) throw new Error(String(response.status));

    track = await response.json();
    receivedAt = Date.now();
    reachable = true;
  } catch {
    reachable = false;
  } finally {
    polling = false;
    lastPoll = Date.now();
  }

  render();
}

function tick() {
  if (document.hidden) return;

  if (Date.now() - lastPoll >= POLL_MS) {
    poll();
    return;
  }

  renderProgress();
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) poll();
});

setInterval(tick, TICK_MS);
poll();
