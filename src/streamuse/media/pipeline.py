"""Orchestrates a streaming session: two loopback TCP inputs, ffmpeg, and the two pacers driven by
one shared clock."""

import asyncio
import contextlib

from .. import paths
from ..state import ERROR, IDLE, RUNNING, STARTING, EncoderState
from . import hls
from .clock import Clock
from .ffmpeg import FfmpegEncoder
from .frames import CoverFrameRenderer
from .meter import LevelMeter
from .pacer import AudioPacer, VideoPacer

CONNECT_TIMEOUT = 10
DRAIN_TIMEOUT = 2
METER_INTERVAL = 0.1

#: ffmpeg consumes each input in bursts while it interleaves and fills a segment, so a writer
#: limited to asyncio's default 64 KiB blocks in drain() for seconds at a time. The pacer reads the
#: clock, not its own progress, so a blocked write shows up as a burst followed by filled silence.
#: This is the buffer the named pipes were given before the move to loopback sockets.
WRITE_BUFFER_LIMIT = 1 << 22


class _Session:
    """Everything one run owns, so a teardown can null it in one move and a late callback from the
    previous run cannot touch the next one."""

    def __init__(self) -> None:
        self.encoder: FfmpegEncoder | None = None
        self.servers: list[asyncio.Server] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.tasks: list[asyncio.Task] = []
        self.stopping = False


class StreamPipeline:
    def __init__(self, settings, hub, deps, artwork, tunnel, sample_rate: int) -> None:
        self._settings = settings
        self._hub = hub
        self._deps = deps
        self._artwork = artwork
        self._tunnel = tunnel
        self._dj = None

        self._gate = asyncio.Lock()
        self._clock = Clock()
        self._pacer = AudioPacer(sample_rate)
        self._meter = LevelMeter(sample_rate)
        self._sample_rate = sample_rate
        self._session: _Session | None = None
        self._reported_shed_seconds = 0

    @property
    def running(self) -> bool:
        return self._session is not None

    def set_dj(self, dj) -> None:
        self._dj = dj
        self._pacer.set_mixer(dj)

    def push_audio(self, chunk: bytes) -> None:
        """The sink every receiver feeds. Dropped while no session exists, so a receiver's lifetime
        stays independent of the encoder's."""
        if self._session is None:
            return
        self._meter.add(chunk)
        self._pacer.push(chunk)

    async def start(self) -> bool:
        async with self._gate:
            if self._session is not None:
                return True

            if self._deps.ffmpeg is None:
                self._fail("ffmpeg is not available - check the Dependencies panel")
                return False

            self._hub.set_encoder(EncoderState(STARTING, 0, self._settings.fps, 0, 0, None))
            session = _Session()
            self._session = session

            try:
                return await self._start_core(session)
            except Exception as exc:
                return await self._abort(f"could not start the stream: {exc}")

    async def _start_core(self, session: _Session) -> bool:
        hls.prepare()
        self._meter.reset()
        self._reported_shed_seconds = 0

        loop = asyncio.get_running_loop()
        audio_ready: asyncio.Future = loop.create_future()
        video_ready: asyncio.Future = loop.create_future()

        # Servers must be listening before ffmpeg starts, and the pacers get the writers the
        # handshake produced rather than a field a teardown could null under a running write.
        audio_server = await _listen(audio_ready)
        video_server = await _listen(video_ready)
        session.servers = [audio_server, video_server]

        encoder = FfmpegEncoder(self._hub)
        session.encoder = encoder
        encoder.on_exit = lambda code: self._on_encoder_exit(session, code)

        await encoder.start(
            self._deps.ffmpeg, self._settings,
            _port(audio_server), _port(video_server), self._sample_rate, paths.HLS_DIR)

        try:
            audio_writer, video_writer = await asyncio.wait_for(
                asyncio.gather(audio_ready, video_ready), CONNECT_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            return await self._abort("ffmpeg never connected to the capture inputs")

        session.writers = [audio_writer, video_writer]
        for writer in session.writers:
            writer.transport.set_write_buffer_limits(high=WRITE_BUFFER_LIMIT)

        # Audio buffered before the clock started is stale and would overrun the buffer at once.
        self._pacer.reset()
        self._clock.restart()

        renderer = CoverFrameRenderer(self._settings, self._artwork, self._hub, self._dj)
        video_pacer = VideoPacer(renderer)

        session.tasks = [
            asyncio.create_task(self._run_pacer(
                session, "audio", self._pacer.run(audio_writer, self._clock))),
            asyncio.create_task(self._run_pacer(
                session, "video", video_pacer.run(video_writer, self._clock, self._settings.fps))),
            asyncio.create_task(self._publish_telemetry(session)),
        ]

        self._hub.set_encoder(EncoderState(RUNNING, 0, self._settings.fps, 0, 0, None))
        self._hub.info(f"streaming - {self._hub.local_url}")

        if self._settings.autoTunnel:
            asyncio.create_task(self._tunnel.start())

        return True

    async def stop(self) -> None:
        async with self._gate:
            await self._stop_core()

    async def _abort(self, message: str) -> bool:
        self._fail(message)
        await self._stop_core()
        return False

    async def _stop_core(self) -> None:
        """Every failure path stops the stream, and several can fire for one fault (both pacers see
        the broken socket, then the encoder reports it exited). Only the first has anything to do."""
        session, self._session = self._session, None
        if session is None:
            return

        session.stopping = True

        # Kill the encoder first so a blocked write faults, then wait for the pacers to leave the
        # writers alone before closing the servers under them.
        if session.encoder is not None:
            session.encoder.stop()

        for task in session.tasks:
            task.cancel()
        if session.tasks:
            await asyncio.wait(session.tasks, timeout=DRAIN_TIMEOUT)

        for writer in session.writers:
            with contextlib.suppress(Exception):
                writer.transport.abort()

        for server in session.servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        self._clock.stop()

        if self._hub.encoder.status != ERROR:
            self._hub.set_encoder(EncoderState(IDLE, 0, 0, 0, 0, None))

        await self._tunnel.stop()
        self._hub.info("stream stopped")

    def _on_encoder_exit(self, session: _Session, code: int) -> None:
        if session.stopping or self._session is not session:
            return
        self._fail(f"encoder exited unexpectedly (code {code})")
        asyncio.create_task(self.stop())

    async def _run_pacer(self, session: _Session, track: str, coroutine) -> None:
        """A pacer returns only when it can no longer write, and silence is the designed output for
        a paused source - so a dead pacer looks exactly like a quiet one and nothing else would
        notice. The stop is detached because the teardown draining this task holds the gate."""
        fault = None
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fault = str(exc) or exc.__class__.__name__

        if session.stopping:
            return

        self._fail(f"{track} pacing stopped" + (f": {fault}" if fault else ""))
        asyncio.create_task(self.stop())

    async def _publish_telemetry(self, session: _Session) -> None:
        tick = 0
        try:
            while True:
                await asyncio.sleep(METER_INTERVAL)
                bars, peak_db = self._meter.read()
                self._hub.publish_meter(bars, peak_db, self._pacer.has_signal)

                tick += 1
                if tick % 10 != 0:
                    continue

                shed = self._pacer.dropped_frames // self._sample_rate
                if shed > self._reported_shed_seconds:
                    self._reported_shed_seconds = shed
                    self._hub.warn(f"audio buffer overran - {shed}s shed to stay in sync")

                encoder = session.encoder
                self._hub.set_encoder(EncoderState(
                    RUNNING,
                    hls.measure_bitrate_kbps(),
                    self._settings.fps,
                    encoder.dropped_frames if encoder else 0,
                    self._clock.elapsed_seconds,
                    None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._hub.error(f"telemetry stopped: {exc}")

    def _fail(self, message: str) -> None:
        self._hub.error(message)
        self._hub.set_encoder(EncoderState(ERROR, 0, 0, 0, 0, message))


async def _listen(ready: asyncio.Future) -> asyncio.Server:
    def on_connect(_reader, writer):
        if not ready.done():
            ready.set_result(writer)

    return await asyncio.start_server(on_connect, "127.0.0.1", 0)


def _port(server: asyncio.Server) -> int:
    return server.sockets[0].getsockname()[1]
