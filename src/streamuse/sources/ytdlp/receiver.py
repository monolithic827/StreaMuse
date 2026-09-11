"""YouTube and SoundCloud as a source: unlike AirPlay and Spotify nothing connects to us, so this
sits idle until a URL or search query is submitted through the panel, then resolves it with yt-dlp
and decodes it with ffmpeg into the same PCM sink every other receiver feeds.

A track submitted while one is already playing queues behind it rather than replacing it - load()
is the only entry point for both, and only starts playing immediately when the queue was empty. A
public song request (search()/enqueue()) feeds the same queue, so a request lines up behind
whatever the panel started instead of needing a queue of its own.

Every track is downloaded into memory (cache.py) before it plays, rather than decoded straight off
the network - see cache.py for why. The item at the head of the queue is prefetched while the current
track is still playing, so by the time it is needed the transition is ffmpeg reading bytes already
sitting in RAM rather than a fresh resolve-and-connect; only ever one item ahead is prefetched, since
nothing here plays more than one track ahead anyway. A track's cached bytes are just a `_Cached`
reference dropped the moment it stops being current, whether that is a natural end or a skip - there
is nothing to explicitly clean up the way a file would need.
"""

import asyncio
import re
from dataclasses import dataclass

import aiohttp

from ...state import QueueItem
from .. import Receiver, RequestTrack, TrackState
from . import cache
from .decoder import Decoder
from .extractor import TrackInfo, extract_async

THUMBNAIL_TIMEOUT = 10

#: A public request must not become an arbitrary outbound fetch: yt-dlp's generic extractor will
#: attempt any scheme urllib understands, not just http(s) - verified against a real yt-dlp install,
#: ftp:// is actually attempted (it only fails here for want of a reachable FTP server), so
#: "not http(s)" is not the same thing as "safe". Anything with a scheme prefix at all is URL-shaped
#: and confined to the two sites this source actually serves; only genuinely bare text is safe, since
#: that only ever becomes a YouTube search.
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_ALLOWED_HOST = re.compile(
    r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be|soundcloud\.com|on\.soundcloud\.com)/",
    re.IGNORECASE,
)


def _is_allowed(query: str) -> bool:
    return not _URL.match(query) or bool(_ALLOWED_HOST.match(query))


@dataclass(frozen=True)
class _Cached:
    info: TrackInfo
    data: bytes


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
        self._queue: list[QueueItem] = []
        #: Serializes stop/advance/play so a track ending naturally at the same moment as a manual
        #: "next" (or two quick loads) can't both decide the decoder is free and start one each.
        self._gate = asyncio.Lock()
        #: Resolves the queue's head to a `_Cached` ahead of needing it. Always started for
        #: `self._queue[0]` and always consumed into the current track on the next advance, so its
        #: identity never needs tracking separately from the queue's own.
        self._next: asyncio.Task | None = None

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
            return "Paste a link, or search, for yt-dlp to play"
        suffix = f" - {len(self._queue)} queued" if self._queue else ""
        return (f"Playing '{self._title}'" if self._track.playing else f"Paused - '{self._title}'") + suffix

    def track(self) -> TrackState:
        return self._track

    async def start(self, sink) -> None:
        self._sink = sink

    async def stop(self) -> None:
        async with self._gate:
            await self._stop_decoder()
            await self._discard_next_locked()
            self._queue.clear()
            self._sink = None

    async def control(self, command: str) -> bool:
        if command == "playpause":
            return await self._toggle()
        if command == "next":
            async with self._gate:
                await self._stop_decoder()
                await self._advance_locked()
            return True
        return False

    async def load(self, query: str, title: str = "", artist: str = "", duration: float = 0.0) -> bool:
        """Queues the query behind whatever is already playing, or plays it immediately if nothing
        is - the only distinction between "play" and "add to queue" is whether the queue was empty
        when this was called."""
        if self._sink is None:
            return False
        if self._deps.ffmpeg is None:
            self._hub.error("yt-dlp: ffmpeg is not available - check the Dependencies panel")
            return False

        async with self._gate:
            self._queue.append(QueueItem(query, title, artist, duration))
            if self._decoder is None:
                await self._advance_locked()
            else:
                self._ensure_next_locked()
        return True

    async def search(self, query: str) -> RequestTrack | None:
        query = query.strip()
        if not query or not _is_allowed(query):
            return None

        try:
            info = await extract_async(query, self._settings.cookiesFile)
        except Exception as exc:
            self._hub.warn(f"yt-dlp: request search for '{query}' failed - {exc}")
            return None

        return RequestTrack(
            id=info.webpage_url, title=info.title, artist=info.artist, album="", artUrl=info.thumbnail_url)

    async def enqueue(self, track_id: str) -> bool:
        if not track_id or not _is_allowed(track_id):
            return False
        return await self.load(track_id)

    async def _advance(self) -> None:
        """The gate-acquiring entry point - only `_on_finished`'s detached task calls this one, since
        every other caller already holds the gate when it wants the next queued item to start."""
        async with self._gate:
            await self._advance_locked()

    async def _advance_locked(self) -> None:
        """Plays the next queued item, if any - called once at start and again every time a track
        ends, whether it finished on its own or was skipped. Assumes the gate is already held."""
        if not self._queue:
            return
        item = self._queue.pop(0)
        task, self._next = self._next, None
        await self._play(item, task)
        self._ensure_next_locked()

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

    async def _resolve_and_cache(self, item: QueueItem) -> _Cached:
        self._hub.info(f"yt-dlp: resolving '{item.query}'")
        info = await extract_async(item.query, self._settings.cookiesFile)
        data = await cache.download(self._deps.ffmpeg, info.stream_url, info.http_headers)
        return _Cached(info, data)

    async def _play(self, item: QueueItem, task: asyncio.Task | None) -> None:
        # Shows what a search result already told us immediately, in case resolving and caching is
        # slow - a pasted link has no title yet and stays blank until it resolves below.
        if item.title:
            self._title = item.title
            self._track.set_text(item.title, item.artist, "")
            self._track.set_position(0, item.duration)
            self._track.set_playing(True)

        if task is None:
            task = asyncio.create_task(self._resolve_and_cache(item))

        try:
            cached = await task
        except Exception as exc:
            self._hub.error(f"yt-dlp: could not resolve '{item.query}' - {exc}")
            await self._advance_locked()
            return

        self._title = cached.info.title
        self._track.set_text(cached.info.title, cached.info.artist, "")
        self._track.set_position(0, cached.info.duration)
        self._track.set_playing(True)

        self._artwork.set(None)
        if cached.info.thumbnail_url:
            self._artwork.set(await _fetch(cached.info.thumbnail_url))

        decoder = Decoder(self._hub, asyncio.get_running_loop())
        decoder.on_finished = self._on_finished
        decoder.start(self._deps.ffmpeg, cached.data, self._deliver)
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
            # Sync, like spotify/pipe.py's own stop() - a bounded join while ffmpeg's kill()
            # unblocks the read thread, not something worth threading through to_thread for a
            # deliberate stop rather than steady playback.
            decoder.stop()
        self._track.clear()
        self._title = ""

    def _on_finished(self) -> None:
        """The decoder calls this itself once ffmpeg's stdout closes - never on a deliberate stop(),
        where the caller already owns clearing the track. Runs on the same loop the decoder's own
        pump task does, so scheduling the next track is safe to do straight from here."""
        self._decoder = None
        self._track.set_playing(False)
        asyncio.create_task(self._advance())

    def _deliver(self, pcm: bytes) -> None:
        if self._sink is not None:
            self._sink(pcm)


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
