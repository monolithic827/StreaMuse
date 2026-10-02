"""YouTube and SoundCloud as a source: unlike AirPlay and Spotify nothing connects to us, so this
sits idle until a URL or search query is submitted through the panel, then resolves it with yt-dlp
and decodes it with ffmpeg into the same PCM sink every other receiver feeds.

A track submitted while one is already playing queues behind it rather than replacing it - load()
is the only entry point for both, and the track only starts immediately when nothing was playing. A
public song request (search()/enqueue()) feeds the same queue, so a request lines up behind
whatever the panel started instead of needing a queue of its own.

Every track is downloaded and fully decoded to PCM in memory (cache.py) before it plays, rather than
decoded live off the network - see cache.py for why. The item at the head of the queue is prefetched
while the current track is still playing, so by the time it is needed the transition is Decoder
pacing bytes already sitting in RAM rather than a fresh resolve-and-decode; only ever one item ahead
is prefetched, since nothing here plays more than one track ahead anyway. A track's cached bytes are
just a `_Cached` reference dropped the moment it stops being current, whether that is a natural end
or a skip - there is nothing to explicitly clean up the way a file would need.
"""

import asyncio
import re
from dataclasses import dataclass

import aiohttp

from .. import Receiver, Rejected, RequestTrack, TrackState
from . import cache
from .decoder import Decoder
from .extractor import DEFAULT_SEARCH, TrackInfo, extract

THUMBNAIL_TIMEOUT = 10

#: AudioPacer's own default (600ms) is sized for AirPlay's and Spotify's live, real-time-only feeds,
#: where there is nothing to buffer ahead of. yt-dlp's track is already fully decoded and sitting in
#: memory before this ever runs, and each track resets its own pacing reference, so any drift is
#: bounded by that one track's length rather than compounding across a session - a much wider
#: allowance just lets it sit as harmless lead instead of shedding audio. A few seconds of PCM costs
#: nothing worth counting in memory next to the whole track already held.
PACER_MAX_LATENCY_MS = 30_000

#: What the first chunk after a deliberate stop is pushed with instead. That banked lead is harmless
#: while a track plays, but on a skip it is the *skipped* track's audio, and the pacer has no partial
#: flush - so "next" would not be heard until it drained, with the new track's metadata already burned
#: into the video. Pushing once at a tight cap makes push() shed it as the overrun it now is.
PACER_SKIP_LATENCY_MS = 200

#: A public request must not become an arbitrary outbound fetch: yt-dlp's generic extractor will
#: attempt any scheme urllib understands, not just http(s) - verified against a real yt-dlp install,
#: ftp:// is actually attempted (it only fails here for want of a reachable FTP server), so
#: "not http(s)" is not the same thing as "safe". Anything with a scheme prefix at all is URL-shaped
#: and confined to the two sites this source actually serves; only genuinely bare text is safe, since
#: that only ever becomes a YouTube search. The scheme is optional in this pattern because a bare
#: "//host/path" is not bare text either - GenericIE._real_extract promotes it to http(s) before it
#: ever looks at default_search, so it is fetched rather than searched for.
_URL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//")

#: A single track on either site. The queue takes one item per request, and a playlist or a
#: channel is a lookup per entry.
_TRACK_URL = re.compile(
    r"^https?://(?:"
    r"(?:www\.|m\.|music\.)?youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/)[\w-]{11}(?![\w-])"
    r"|youtu\.be/[\w-]{11}(?![\w-])"
    r"|(?:www\.|m\.)?soundcloud\.com/(?!you/|stations/)[\w-]+/"
    r"(?!(?:tracks|albums|sets|reposts|likes|spotlight|comments)(?:[/?#]|$))[\w-]+"
    r"|on\.soundcloud\.com/\w+"
    r")",
    re.IGNORECASE,
)

#: Each search is a yt-dlp process of its own.
MAX_SEARCHES = 2


def _public_query(text: str) -> str | None:
    """What a listener's text is handed to yt-dlp as, or None when it is not taken. Text that is
    not a URL gets the search prefix itself, so it is searched for exactly as typed."""
    # Stripped before the check: urlsplit drops leading whitespace of its own, so " https://..."
    # reaches yt-dlp as a URL while an unstripped check reads it as text.
    text = text.strip()
    if not text:
        return None
    if _URL.match(text):
        return text if _TRACK_URL.match(text) else None
    return f"{DEFAULT_SEARCH}:{text}"


@dataclass(frozen=True)
class _Cached:
    info: TrackInfo
    data: bytearray


class YtDlpReceiver(Receiver):
    source = "ytdlp"

    #: Nothing here interrupts what is already playing, the same as Spotify's own queue.
    request_action = "queue"

    def __init__(self, settings, hub, artwork, deps) -> None:
        self._settings = settings
        self._hub = hub
        self._artwork = artwork
        self._deps = deps

        self._track = TrackState()
        self._sink = None
        self._decoder: Decoder | None = None
        self._title = ""
        self._queue: list[str] = []
        #: Serializes stop/advance/play so a track ending naturally at the same moment as a manual
        #: "next" (or two quick loads) can't both decide the decoder is free and start one each.
        self._gate = asyncio.Lock()
        #: Resolves the queue's head to a `_Cached` ahead of needing it. Always started for
        #: `self._queue[0]` and always consumed into the current track on the next advance, so its
        #: identity never needs tracking separately from the queue's own.
        self._next: asyncio.Task | None = None
        #: The resolve the current track is waiting on, held only so stop() and "next" can cancel it
        #: without first waiting for the gate it runs under. See _cancel_loading.
        self._loading: asyncio.Task | None = None
        #: The one detached advance, kept so it cannot be collected while it runs.
        self._advancing: asyncio.Task | None = None
        self._searches = asyncio.Semaphore(MAX_SEARCHES)
        self._latency_ms = PACER_MAX_LATENCY_MS

    @property
    def available(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        return self._decoder is not None

    @property
    def client(self) -> str:
        return self._title

    @property
    def status_text(self) -> str:
        if self._decoder is None:
            # The gate is held from the resolve to the decoder starting, so with nothing playing it
            # is what says a track is on its way - load() has returned long before.
            if self._gate.locked() or self._queue:
                return "Loading the track"
            return "Paste a link, or search, for yt-dlp to play"
        suffix = f" - {len(self._queue)} queued" if self._queue else ""
        return (f"Playing '{self._title}'" if self._track.playing else f"Paused - '{self._title}'") + suffix

    def track(self) -> TrackState:
        return self._track

    async def start(self, sink) -> None:
        self._sink = sink

    async def stop(self) -> None:
        self._cancel_loading()
        async with self._gate:
            await self._stop_decoder()
            await self._discard_next_locked()
            self._queue.clear()
            self._sink = None

    async def control(self, command: str) -> bool:
        if command == "playpause":
            return await self._toggle()
        if command == "next":
            self._cancel_loading()
            async with self._gate:
                await self._stop_decoder()
                await self._advance_locked()
            return True
        return False

    async def load(self, query: str) -> bool:
        """Queues the query behind whatever is already playing, or plays it immediately if nothing
        is - the only distinction between "play" and "add to queue" is whether anything was playing
        when this was called. Returns once it is queued rather than once it plays: the advance holds
        the gate for as long as the track takes to download, and the panel's POST and a listener's
        request were both left hanging on it."""
        if self._sink is None:
            return False
        if self._deps.ffmpeg is None:
            self._hub.error("yt-dlp: ffmpeg is not available - check the Dependencies panel")
            return False
        if self._deps.yt_dlp is None:
            self._hub.error("yt-dlp.exe is not available - check the Dependencies panel")
            return False

        self._queue.append(query)
        self._spawn_advance()
        return True

    async def search(self, query: str) -> RequestTrack | None:
        query = _public_query(query)
        if query is None:
            return None
        if self._searches.locked():
            raise Rejected("too many searches right now - try again in a moment")

        try:
            async with self._searches:
                info = await extract(self._deps.yt_dlp, query, self._settings.cookiesFile)
        except Rejected:
            # No results, live, too long - a message already safe to show a listener, so it goes to
            # the public search response instead of being swallowed as an extraction failure.
            raise
        except Exception as exc:
            self._hub.warn(f"yt-dlp: request search for '{query}' failed - {exc}")
            return None

        return RequestTrack(
            id=info.webpage_url, title=info.title, artist=info.artist, album="", artUrl=info.thumbnail_url)

    async def enqueue(self, track_id: str) -> bool:
        # Checked again rather than trusted: the id a listener posts need not be one search() gave.
        query = _public_query(track_id)
        return query is not None and await self.load(query)

    def _spawn_advance(self) -> None:
        # Never a second one. An advance still in flight reads the queue again after its last
        # await, so it picks up whatever was appended meanwhile - while a second, queued on the
        # gate, would be handed it ahead of a stop() or "next" that had already cancelled the load
        # in front, and start the following download under the gate they are waiting for.
        if self._advancing is None or self._advancing.done():
            self._advancing = asyncio.create_task(self._advance())

    async def _advance(self) -> None:
        """The gate-acquiring entry point, always run detached - by load() and by a natural finish,
        neither of which holds the gate or can wait for a download under it."""
        try:
            async with self._gate:
                await self._advance_locked()
        except Exception as exc:
            # Detached, so an escaping exception would stop the queue advancing with nothing said.
            self._hub.error(f"yt-dlp: could not start the next track - {exc}")

    async def _advance_locked(self) -> None:
        """Plays the next queued item if nothing is playing, and has the one after it prefetched
        either way - called for every load() and again every time a track ends, whether it finished
        on its own or was skipped. Assumes the gate is already held."""
        # A load() while a track plays lands here wanting only the prefetch, and _on_finished clears
        # the decoder off the gate, so "next" can have started a track before the advance it
        # scheduled runs. Without the decoder check either would start a second one over a live
        # one: both pace into the sink, and the first is no longer referenced by anything that
        # could stop it.
        if self._decoder is None and self._queue:
            query = self._queue.pop(0)
            task, self._next = self._next, None
            await self._play(query, task)
        self._ensure_next_locked()

    def _cancel_loading(self) -> None:
        """A resolve-and-cache is awaited with the gate held, and ffmpeg's -reconnect gives it no
        deadline of its own, so against a stalled CDN stop() and "next" would otherwise wait on the
        gate for as long as it kept retrying - long enough for app._shutdown to time out."""
        if self._loading is not None:
            self._loading.cancel()

    def _ensure_next_locked(self) -> None:
        """Starts caching the new queue head, if there is one and it is not already being cached -
        the only two ways the head can change are `_advance_locked` (which already consumes
        `self._next` into the track that just started) and appending to an empty queue, so nothing
        here ever needs to tell a stale prefetch apart from a current one."""
        if self._next is None and self._queue:
            self._next = asyncio.create_task(self._resolve_and_cache(self._queue[0]))

    async def _discard_next_locked(self) -> None:
        task, self._next = self._next, None
        if task is None:
            return
        task.cancel()
        # gather(..., return_exceptions=True) rather than a bare try/except: CancelledError is not
        # an Exception subclass since 3.8, so awaiting the cancelled task directly would let it
        # escape this coroutine and look like `stop()` itself had been cancelled. Nothing further to
        # do with the result either way - a `_Cached` here is just bytes with no file to clean up.
        await asyncio.gather(task, return_exceptions=True)

    async def _resolve_and_cache(self, query: str) -> _Cached:
        self._hub.info(f"yt-dlp: resolving '{query}'")
        info = await extract(self._deps.yt_dlp, query, self._settings.cookiesFile)
        data = await cache.download(self._deps.ffmpeg, info.stream_url, info.http_headers)
        return _Cached(info, data)

    async def _play(self, query: str, task: asyncio.Task | None) -> None:
        if task is None:
            task = asyncio.create_task(self._resolve_and_cache(query))

        self._loading = task
        try:
            cached = await task
        except asyncio.CancelledError:
            # _cancel_loading, so stop() or "next" is already waiting on the gate this holds -
            # nothing to play and nothing to report.
            return
        except Exception as exc:
            self._hub.error(f"yt-dlp: could not resolve '{query}' - {exc}")
            cached = None
        finally:
            self._loading = None

        if cached is None:
            await self._advance_locked()
            return

        self._title = cached.info.title
        self._track.set_text(cached.info.title, cached.info.artist, "")
        self._track.set_position(0, cached.info.duration)
        self._track.set_playing(True)

        self._artwork.set(None)
        if cached.info.thumbnail_url:
            self._artwork.set(await _fetch(cached.info.thumbnail_url))

        decoder = Decoder(asyncio.get_running_loop())
        decoder.on_finished = self._on_finished
        decoder.start(cached.data, self._deliver)
        self._decoder = decoder

    async def _toggle(self) -> bool:
        if self._decoder is None:
            return False
        playing = not self._track.playing
        self._track.set_playing(playing)
        (self._decoder.resume if playing else self._decoder.pause)()
        return True

    async def _stop_decoder(self) -> None:
        decoder, self._decoder = self._decoder, None
        if decoder is not None:
            decoder.on_finished = None
            # Sync, like spotify/pipe.py's own stop() - a bounded join while the pacing thread
            # notices _stopping, not something worth threading through to_thread for a deliberate
            # stop rather than steady playback.
            decoder.stop()
            self._latency_ms = PACER_SKIP_LATENCY_MS
        self._track.clear()
        self._title = ""

    def _on_finished(self) -> None:
        """The decoder calls this itself once its PCM runs out - never on a deliberate stop(), where
        the caller already owns clearing the track. Runs on the loop, since the pacing thread hands
        it over with call_soon_threadsafe, so scheduling the next track from here is safe."""
        self._decoder = None
        self._track.set_playing(False)
        self._spawn_advance()

    def _deliver(self, pcm: bytes) -> None:
        if self._sink is not None:
            self._sink(pcm, self._latency_ms)
            self._latency_ms = PACER_MAX_LATENCY_MS


async def _fetch(url: str) -> bytes | None:
    """A failed cover fetch must never take the track down with it - _play() already has the track
    playing by the time this runs, and TimeoutError (from the session's own timeout) isn't a
    ClientError, so a slow host would otherwise escape this and crash the request with audio never
    actually started."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=THUMBNAIL_TIMEOUT)) as session:
            async with session.get(url) as reply:
                return await reply.read() if reply.status == 200 else None
    except (aiohttp.ClientError, TimeoutError):
        return None
