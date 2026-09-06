"""The Spotify Connect device the desktop app hands playback to.

go-librespot holds the Connect session and pulls the audio itself; this owns its lifetime, reads
its PCM off a named pipe and turns its events into the same track fields the AirPlay side reports.
"""

import asyncio
import socket

from .. import Receiver, TrackState
from .api import LibrespotApi
from .librespot import LibrespotProcess
from .pipe import PipeReader

#: Events that say playback is on or off; anything else only carries metadata.
PLAYING_EVENTS = {"playing": True, "paused": False, "stopped": False,
                  "not_playing": False, "will_play": False}


class SpotifyReceiver(Receiver):
    source = "spotify"

    def __init__(self, settings, hub, artwork, deps) -> None:
        self._settings = settings
        self._hub = hub
        self._artwork = artwork
        self._deps = deps

        self._track = TrackState()
        self._process = LibrespotProcess(hub)
        self._api: LibrespotApi | None = None
        self._pipe: PipeReader | None = None
        self._sink = None
        self._active = False
        self._username = ""
        self._cover_url = ""
        self._name = ""

    @property
    def available(self) -> bool:
        return self._deps.go_librespot is not None

    @property
    def reason(self) -> str:
        return "" if self.available else "go-librespot is not installed - see the README"

    @property
    def connected(self) -> bool:
        return self._active

    @property
    def client(self) -> str:
        return self._username

    @property
    def status_text(self) -> str:
        if not self._process.running:
            return "Spotify Connect isn't running - go-librespot exited"
        if not self._active:
            return f"Spotify device '{self._name}' - pick it in Spotify"
        return f"Spotify connected as {self._username}" if self._username else "Spotify connected"

    def track(self) -> TrackState:
        return self._track

    async def start(self, sink) -> None:
        if self._deps.go_librespot is None:
            raise RuntimeError("go-librespot is not installed")

        self._sink = sink
        self._name = self._settings.spotifyConnectDeviceName

        # The pipe instance must exist before the daemon tries to open it.
        self._pipe = PipeReader(asyncio.get_running_loop(), self._deliver, self._hub)
        self._pipe.start()

        port = _free_port()
        self._api = LibrespotApi(port, self._hub, self._on_event)
        await self._process.start(self._deps.go_librespot, self._name, port)
        await self._api.start()

        self._hub.info(f"spotify: offering '{self._name}' as a Connect device - pick it in Spotify")

    async def stop(self) -> None:
        if self._api is not None:
            await self._api.stop()
            self._api = None

        await self._process.stop()

        if self._pipe is not None:
            self._pipe.stop()
            self._pipe = None

        self._active = False
        self._username = ""
        self._cover_url = ""
        self._sink = None
        self._track.clear()

    async def control(self, command: str) -> bool:
        return await self._api.command(command) if self._api is not None else False

    async def _on_event(self, kind: str, data: dict) -> None:
        if kind == "status":
            self._active = not data.get("stopped", True)
            self._username = data.get("username") or ""
            track = data.get("track")
            if track:
                await self._apply_track(track)
                self._track.set_playing(not data.get("paused", True))
            return

        if kind == "active":
            self._active = True
        elif kind == "inactive":
            self._active = False
            self._track.set_playing(False)
        elif kind == "metadata":
            await self._apply_track(data)
        elif kind == "seek":
            self._track.set_position(_seconds(data.get("position")),
                                     _seconds(data.get("duration")))
        elif kind in PLAYING_EVENTS:
            self._track.set_playing(PLAYING_EVENTS[kind])

    async def _apply_track(self, track: dict) -> None:
        self._track.set_text(
            track.get("name") or "",
            ", ".join(track.get("artist_names") or []),
            track.get("album_name") or "")
        self._track.set_position(_seconds(track.get("position")),
                                 _seconds(track.get("duration")))

        url = track.get("album_cover_url") or ""
        if url == self._cover_url:
            return

        self._cover_url = url
        if not url:
            self._artwork.set(None)
            return

        # Dropped rather than kept: the new title with the previous album beside it would be worse
        # than a moment with no cover at all.
        self._artwork.set(None)
        if self._api is not None:
            self._artwork.set(await self._api.fetch_cover(url))

    def _deliver(self, pcm: bytes) -> None:
        if self._sink is not None:
            self._sink(pcm)


def _seconds(milliseconds) -> float:
    return (milliseconds or 0) / 1000


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
