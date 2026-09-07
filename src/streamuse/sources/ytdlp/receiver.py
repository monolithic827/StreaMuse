"""YouTube and SoundCloud as a source: unlike AirPlay and Spotify nothing connects to us, so this
sits idle until a URL or search query is submitted through the panel, then resolves it with yt-dlp
and decodes it with ffmpeg into the same PCM sink every other receiver feeds.
"""

import asyncio

import aiohttp

from .. import Receiver, TrackState
from .decoder import Decoder
from .extractor import extract_async

THUMBNAIL_TIMEOUT = 10


class YtDlpReceiver(Receiver):
    source = "ytdlp"

    def __init__(self, settings, hub, artwork, deps) -> None:
        self._settings = settings
        self._hub = hub
        self._artwork = artwork
        self._deps = deps

        self._track = TrackState()
        self._sink = None
        self._decoder: Decoder | None = None
        self._title = ""

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
            return "Paste a YouTube or SoundCloud link, or a search, to play"
        return f"Playing '{self._title}'" if self._track.playing else f"Paused - '{self._title}'"

    def track(self) -> TrackState:
        return self._track

    async def start(self, sink) -> None:
        self._sink = sink

    async def stop(self) -> None:
        await self._stop_playback()
        self._sink = None

    async def control(self, command: str) -> bool:
        if command == "playpause":
            return await self._toggle()
        if command == "next":
            await self._stop_playback()
            return True
        return False

    async def load(self, query: str) -> bool:
        if self._sink is None:
            return False
        if self._deps.ffmpeg is None:
            self._hub.error("yt-dlp: ffmpeg is not available - check the Dependencies panel")
            return False

        await self._stop_playback()
        self._hub.info(f"yt-dlp: resolving '{query}'")

        try:
            info = await extract_async(query, self._settings.cookiesFile)
        except Exception as exc:
            self._hub.error(f"yt-dlp: could not resolve '{query}' - {exc}")
            return False

        self._title = info.title
        self._track.set_text(info.title, info.artist, "")
        self._track.set_position(0, info.duration)
        self._track.set_playing(True)

        self._artwork.set(None)
        if info.thumbnail_url:
            self._artwork.set(await _fetch(info.thumbnail_url))

        decoder = Decoder(self._hub)
        decoder.on_finished = self._on_finished
        await decoder.start(self._deps.ffmpeg, info.stream_url, info.http_headers, self._deliver)
        self._decoder = decoder
        return True

    async def _toggle(self) -> bool:
        if self._decoder is None:
            return False
        playing = not self._track.playing
        self._track.set_playing(playing)
        (self._decoder.resume if playing else self._decoder.pause)()
        return True

    async def _stop_playback(self) -> None:
        decoder, self._decoder = self._decoder, None
        if decoder is not None:
            decoder.on_finished = None
            await decoder.stop()
        self._track.clear()
        self._title = ""

    def _on_finished(self) -> None:
        """The decoder calls this itself once ffmpeg's stdout closes - never on a deliberate stop(),
        where the caller already owns clearing the track."""
        self._decoder = None
        self._track.set_playing(False)

    def _deliver(self, pcm: bytes) -> None:
        if self._sink is not None:
            self._sink(pcm)


async def _fetch(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=THUMBNAIL_TIMEOUT)) as session:
            async with session.get(url) as reply:
                return await reply.read() if reply.status == 200 else None
    except aiohttp.ClientError:
        return None
