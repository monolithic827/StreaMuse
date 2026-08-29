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
let artVersion = -1;
let reachable = false;

// The host writes "-" for a field it has nothing for, and "Nothing playing" for no track at all.
const value = (text) => (text && text !== '-' ? text : '');

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

function renderArt(version) {
  if (version === artVersion) return;
  artVersion = version;

  if (version > 0) {
    el.cover.src = `art?v=${version}`;
    el.cover.hidden = false;
    el.artEmpty.hidden = true;
    return;
  }

  el.cover.removeAttribute('src');
  el.cover.hidden = true;
  el.artEmpty.hidden = false;
}

// The cover can change between the poll that reported a version and the fetch for it.
el.cover.addEventListener('error', () => {
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
    // Metadata may still be arriving while the stream is stopped, but there is nothing to hear -
    // showing the track would tell a listener something is playing for them when it is not.
    el.eyebrow.textContent = reachable ? 'Nothing streaming' : 'Lost contact';
    el.title.textContent = reachable ? 'Off air' : 'Trying to reconnect…';
    el.artist.textContent = '';
    el.album.textContent = '';
    renderArt(0);
    renderProgress();
    return;
  }

  const title = value(track.title) && track.title !== 'Nothing playing' ? track.title : '';

  el.eyebrow.textContent = title ? 'Now playing' : 'On air';
  el.title.textContent = title || 'No track info';
  el.artist.textContent = title ? value(track.artist) : '';
  el.album.textContent = title ? value(track.album) : '';

  renderArt(track.artworkVersion);
  renderProgress();
}

async function poll() {
  lastPoll = Date.now();

  try {
    const response = await fetch('now', { cache: 'no-store' });
    if (!response.ok) throw new Error(String(response.status));

    track = await response.json();
    receivedAt = Date.now();
    reachable = true;
  } catch {
    reachable = false;
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
