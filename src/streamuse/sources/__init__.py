"""What a receiver is, and which one is currently listening.

Both receivers deliver interleaved s16le at 44.1 kHz and report the same track fields, so the rest
of the app never learns which one is running. Only the selected receiver runs at a time.
"""

import asyncio
import time
from dataclasses import dataclass

from ..state import NowPlaying, SourceOption, SourceState

#: What every receiver delivers, and therefore what the pacer and ffmpeg's input are set to.
SAMPLE_RATE = 44100

PUBLISH_INTERVAL = 1.0

LABELS = {"apple": "Apple Music", "spotify": "Spotify", "ytdlp": "yt-dlp"}


class TrackState:
    """Track fields plus a position that keeps running between the sender's updates - a sender only
    reports one on play, pause and seek."""

    def __init__(self) -> None:
        self.title = ""
        self.artist = ""
        self.album = ""
        self.playing = False
        self.duration = 0.0
        self._position = 0.0
        self._position_at = time.monotonic()

    def clear(self) -> None:
        self.__init__()

    def set_text(self, title: str, artist: str, album: str) -> None:
        self.title, self.artist, self.album = title, artist, album

    def set_position(self, position: float, duration: float | None = None) -> None:
        self._position = max(0.0, position)
        self._position_at = time.monotonic()
        if duration is not None:
            self.duration = max(0.0, duration)

    def set_playing(self, playing: bool) -> None:
        if playing == self.playing:
            return
        # Freeze the extrapolated value before changing which way it runs.
        self._position = self.position
        self._position_at = time.monotonic()
        self.playing = playing

    @property
    def position(self) -> float:
        if not self.playing:
            return self._position
        elapsed = time.monotonic() - self._position_at
        return min(self._position + elapsed, self.duration) if self.duration else self._position + elapsed

    def snapshot(self, artwork_version: int) -> NowPlaying:
        return NowPlaying(self.title, self.artist, self.album, self.playing,
                          self.position, self.duration, artwork_version)


@dataclass(frozen=True)
class RequestTrack:
    """One resolved search result. The id is what comes back to enqueue it, and each receiver
    validates its own shape before acting on it."""

    id: str
    title: str
    artist: str
    album: str
    artUrl: str


class Receiver:
    """Implemented by the AirPlay and Spotify receivers."""

    source = ""

    #: What enqueue does to the sender: "queue" plays it after the current track, "play" starts it
    #: now. Empty when the receiver cannot take requests at all.
    request_action = ""

    @property
    def available(self) -> bool:
        raise NotImplementedError

    @property
    def reason(self) -> str:
        return ""

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    @property
    def client(self) -> str:
        return ""

    @property
    def status_text(self) -> str:
        raise NotImplementedError

    def track(self) -> TrackState:
        raise NotImplementedError

    async def start(self, sink) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def control(self, command: str) -> bool:
        return False

    async def load(self, query: str) -> bool:
        """Overridden only by a source that accepts an on-demand URL or search query rather than
        waiting for something else to connect."""
        return False

    async def search(self, query: str) -> RequestTrack | None:
        return None

    async def enqueue(self, track_id: str) -> bool:
        return False

    async def open_request(self, track_id: str) -> bool:
        """Only reached for a receiver whose enqueue parks the request for the host instead of
        acting on it."""
        return False


class SourceManager:
    def __init__(self, settings, hub, artwork, sink, receivers: dict[str, Receiver]) -> None:
        self._settings = settings
        self._hub = hub
        self._artwork = artwork
        self._sink = sink
        self._receivers = receivers
        self._active: Receiver | None = None
        self._gate = asyncio.Lock()
        self._publisher: asyncio.Task | None = None

    @property
    def active(self) -> Receiver | None:
        return self._active

    async def select(self, source: str) -> None:
        async with self._gate:
            receiver = self._receivers.get(source)
            if receiver is self._active:
                return

            if self._active is not None:
                await self._active.stop()
                self._active = None
                self._artwork.set(None)

            if receiver is None:
                self._publish()
                return

            if not receiver.available:
                self._hub.warn(f"{LABELS.get(source, source)} is unavailable: {receiver.reason}")
                self._publish()
                return

            try:
                await receiver.start(self._sink)
            except Exception as exc:
                self._hub.error(f"could not start the {LABELS.get(source, source)} receiver: {exc}")
                self._publish()
                return

            self._active = receiver
            self._publish()

    async def control(self, command: str) -> bool:
        receiver = self._active
        return await receiver.control(command) if receiver is not None else False

    async def load(self, query: str) -> bool:
        receiver = self._active
        return await receiver.load(query) if receiver is not None else False

    @property
    def request_action(self) -> str:
        receiver = self._active
        return receiver.request_action if receiver is not None else ""

    async def search(self, query: str) -> RequestTrack | None:
        receiver = self._active
        return await receiver.search(query) if receiver is not None else None

    async def enqueue(self, track_id: str) -> bool:
        receiver = self._active
        return await receiver.enqueue(track_id) if receiver is not None else False

    async def open_request(self, track_id: str) -> bool:
        receiver = self._active
        return await receiver.open_request(track_id) if receiver is not None else False

    def start_publishing(self) -> None:
        self._publisher = asyncio.create_task(self._publish_loop())

    async def stop(self) -> None:
        if self._publisher is not None:
            self._publisher.cancel()
            self._publisher = None
        async with self._gate:
            if self._active is not None:
                await self._active.stop()
                self._active = None

    async def _publish_loop(self) -> None:
        while True:
            try:
                self._publish()
            except Exception as exc:
                self._hub.error(f"source publishing stopped: {exc}")
                return
            await asyncio.sleep(PUBLISH_INTERVAL)

    def _publish(self) -> None:
        receiver = self._active
        selected = self._settings.source

        options = [
            SourceOption(name, other.available, other.reason)
            for name, other in self._receivers.items()
        ]

        if receiver is None:
            missing = self._receivers.get(selected)
            status = missing.reason if missing is not None else "no receiver for this source"
            self._hub.set_source(SourceState(selected, False, "", status, options))
            self._hub.set_now_playing(NowPlaying())
            return

        self._hub.set_source(SourceState(
            selected, receiver.connected, receiver.client, receiver.status_text, options))
        self._hub.set_now_playing(receiver.track().snapshot(self._artwork.version))
