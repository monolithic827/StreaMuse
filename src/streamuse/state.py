"""Holds current app state, fans it out to connected panel sockets, and keeps the rolling log.
Every mutation goes through here so the panel and the REST API always agree."""

import asyncio
import json
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime

LOG_CAPACITY = 200

IDLE, STARTING, RUNNING, ERROR = "idle", "starting", "running", "error"
TUNNEL_OFF, TUNNEL_STARTING, TUNNEL_UP, TUNNEL_ERROR = "off", "starting", "up", "error"


@dataclass(frozen=True)
class LogLine:
    time: str
    level: str
    message: str


@dataclass(frozen=True)
class NowPlaying:
    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = False
    positionSeconds: float = 0.0
    durationSeconds: float = 0.0
    artworkVersion: int = 0


@dataclass(frozen=True)
class SourceOption:
    source: str
    available: bool
    reason: str


@dataclass(frozen=True)
class SourceState:
    source: str = "apple"
    connected: bool = False
    client: str = ""
    statusText: str = "Starting up…"
    options: list[SourceOption] = field(default_factory=list)


@dataclass(frozen=True)
class EncoderState:
    status: str = IDLE
    bitrateKbps: int = 0
    fps: int = 0
    droppedFrames: int = 0
    uptimeSeconds: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class TunnelState:
    status: str = TUNNEL_OFF
    publicUrl: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DjQueueEntry:
    id: str
    query: str
    title: str
    artist: str
    status: str


@dataclass(frozen=True)
class DjState:
    queue: list[DjQueueEntry]
    nowMixing: DjQueueEntry | None
    phaseText: str
    confidencePercent: float | None
    album: str
    positionSeconds: float
    durationSeconds: float
    artworkVersion: int


@dataclass(frozen=True)
class DependencyView:
    name: str
    path: str | None

    @property
    def present(self) -> bool:
        return self.path is not None


def _dependency_dict(dep: DependencyView) -> dict:
    return {"name": dep.name, "path": dep.path, "present": dep.present}


def dumps(payload) -> str:
    """allow_nan=False so a non-finite double raises here rather than emitting invalid JSON that a
    browser silently rejects. Detached tasks log their exceptions, so the raise is visible."""
    return json.dumps(payload, allow_nan=False)


class StateHub:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._log: deque[LogLine] = deque(maxlen=LOG_CAPACITY)
        self._clients: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._source = SourceState()
        self._now_playing = NowPlaying()
        self._encoder = EncoderState()
        self._tunnel = TunnelState()
        self._deps: list[DependencyView] = []
        self._local_url: str | None = None
        self._dj = DjState([], None, "Nothing queued", None, "", 0.0, 0.0, 0)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mutations arrive from receiver threads too, so broadcasts are scheduled onto the loop."""
        self._loop = loop

    @property
    def encoder(self) -> EncoderState:
        with self._lock:
            return self._encoder

    @property
    def tunnel(self) -> TunnelState:
        with self._lock:
            return self._tunnel

    @property
    def source(self) -> SourceState:
        with self._lock:
            return self._source

    @property
    def now_playing(self) -> NowPlaying:
        with self._lock:
            return self._now_playing

    @property
    def local_url(self) -> str | None:
        with self._lock:
            return self._local_url

    @property
    def dj(self) -> DjState:
        with self._lock:
            return self._dj

    def set_source(self, value: SourceState) -> None:
        self._mutate("_source", value)

    def set_now_playing(self, value: NowPlaying) -> None:
        self._mutate("_now_playing", value)

    def set_encoder(self, value: EncoderState) -> None:
        self._mutate("_encoder", value)

    def set_tunnel(self, value: TunnelState) -> None:
        self._mutate("_tunnel", value)

    def set_dependencies(self, value: list[DependencyView]) -> None:
        self._mutate("_deps", value)

    def set_local_url(self, value: str | None) -> None:
        self._mutate("_local_url", value)

    def set_dj(self, value: DjState) -> None:
        self._mutate("_dj", value)

    def log(self, level: str, message: str) -> None:
        with self._lock:
            line = LogLine(datetime.now().strftime("%H:%M:%S"), level, message)
            self._log.appendleft(line)

        # A console that cannot encode the message must not take the logger down with it.
        try:
            print(f"[{line.time}] {line.level:<5} {line.message}", flush=True)
        except (UnicodeEncodeError, OSError):
            pass
        self._publish({"type": "log", "line": asdict(line)})

    def info(self, message: str) -> None:
        self.log("info", message)

    def warn(self, message: str) -> None:
        self.log("warn", message)

    def error(self, message: str) -> None:
        self.log("error", message)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "type": "state",
                "source": asdict(self._source),
                "nowPlaying": asdict(self._now_playing),
                "encoder": asdict(self._encoder),
                "tunnel": asdict(self._tunnel),
                "dependencies": [_dependency_dict(d) for d in self._deps],
                "log": [asdict(line) for line in self._log],
                "localUrl": self._local_url,
                "settings": self.settings.to_dict(),
                "dj": asdict(self._dj),
            }

    def publish_meter(self, bars: list[float], peak_db: float | None, signal: bool) -> None:
        """Pushed separately so the meter never forces a full state re-serialize."""
        self._publish({"type": "meter", "bars": bars, "peakDb": peak_db, "signal": signal})

    async def accept_socket(self, socket) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(dumps(self.snapshot()))
        self._clients.add(queue)
        try:
            await self._pump(socket, queue)
        finally:
            self._clients.discard(queue)

    async def _pump(self, socket, queue: asyncio.Queue) -> None:
        """Push-only: reading just holds the socket open until the client leaves. The queue keeps
        sends ordered without ever blocking a mutation."""
        sender = asyncio.create_task(self._send_loop(socket, queue))
        try:
            async for _ in socket:
                pass
        finally:
            sender.cancel()

    @staticmethod
    async def _send_loop(socket, queue: asyncio.Queue) -> None:
        while True:
            payload = await queue.get()
            if socket.closed:
                return
            await socket.send_str(payload)

    def _mutate(self, attribute: str, value) -> None:
        with self._lock:
            setattr(self, attribute, value)
        self._publish(self.snapshot())

    def _publish(self, payload: dict) -> None:
        if not self._clients:
            return
        loop = self._loop
        if loop is None:
            return
        if threading.current_thread() is threading.main_thread() and not loop.is_running():
            return
        loop.call_soon_threadsafe(self._fan_out, payload)

    def _fan_out(self, payload: dict) -> None:
        text = dumps(payload)
        for queue in list(self._clients):
            queue.put_nowait(text)
