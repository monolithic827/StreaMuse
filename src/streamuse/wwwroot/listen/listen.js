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
  duration: document.getElementById('duration'),
  requests: document.getElementById('requests'),
  ask: document.getElementById('ask'),
  q: document.getElementById('q'),
  search: document.getElementById('go'),
  note: document.getElementById('note'),
  result: document.getElementById('result'),
  thumb: document.getElementById('thumb'),
  foundTitle: document.getElementById('found-title'),
  foundArtist: document.getElementById('found-artist'),
  foundAlbum: document.getElementById('found-album'),
  queue: document.getElementById('queue'),
  discard: document.getElementById('discard')
};

const QUEUE_LABELS = { queue: 'Play next', play: 'Play now' };

let track = null;
let receivedAt = 0;
let lastPoll = 0;
let polling = false;
let artVersion = null;
let reachable = false;
let found = null;
let busy = false;

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

  renderRequests(live);

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

/* The host says what the button will do - "queue" lines the track up behind the current one, "play"
   starts it - and the wording for each lives here, so nothing on the wire is display text. */
function renderRequests(live) {
  const action = live ? track.requests : '';

  el.requests.hidden = !action;
  if (action) el.queue.textContent = QUEUE_LABELS[action] || 'Request';
}

function say(text) {
  el.note.textContent = text;
  el.note.hidden = !text;
}

function showResult(result) {
  found = result;
  el.result.hidden = !result;
  if (!result) return;

  el.foundTitle.textContent = result.title;
  el.foundArtist.textContent = result.artist;
  el.foundAlbum.textContent = result.album;

  el.thumb.hidden = !result.artUrl;
  if (result.artUrl) el.thumb.src = result.artUrl;
}

/* Deliberately outside the poll cycle: a result the listener is still deciding on has to survive
   the re-render that happens under it every two seconds. */
async function ask(path, options) {
  busy = true;
  el.search.disabled = el.queue.disabled = el.discard.disabled = true;

  try {
    const response = await fetch(path, options);
    return { ok: response.ok, body: await response.json().catch(() => ({})) };
  } catch {
    return { ok: false, body: {} };
  } finally {
    busy = false;
    el.search.disabled = el.queue.disabled = el.discard.disabled = false;
  }
}

// The buttons are disabled while a call is in flight, but Enter in the field is not, so the form
// needs the guard of its own.
el.ask.addEventListener('submit', async event => {
  event.preventDefault();

  const query = el.q.value.trim();
  if (!query || busy) return;

  showResult(null);
  say('Searching…');

  const { ok, body } = await ask(`search?q=${encodeURIComponent(query)}`);

  if (!ok) return say(body.error || 'Requests are closed right now.');
  if (!body.found) return say('Nothing matched that.');

  say('');
  showResult(body);
});

el.queue.addEventListener('click', async () => {
  if (!found) return;

  const done = track.requests === 'play' ? `Playing ${found.title}` : `Queued ${found.title}`;

  const { ok, body } = await ask('request', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: found.id, title: found.title })
  });

  if (!ok) return say(body.error || 'That did not go through.');

  showResult(null);
  el.q.value = '';
  say(done);
});

el.discard.addEventListener('click', () => {
  showResult(null);
  say('');
  el.q.focus();
  el.q.select();
});

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
