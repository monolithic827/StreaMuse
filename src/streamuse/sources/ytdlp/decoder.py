"""Decodes one resolved stream URL to raw PCM by running ffmpeg once per track.

ffmpeg decodes across the network far faster than playback, so an unpaced drain of its stdout
would hand the pacer a whole track in a few seconds - the same failure the Spotify pipe reader
exists to avoid (see CLAUDE.md). Draining is paced to real time the same way, but reading is not:
a `_reader` task fills a bounded queue as fast as ffmpeg produces data, and `_drain` paces its way
through that queue on its own clock. Fetching over the internet stalls in a way a local named pipe
or RTP stream never does - a CDN throttling the connection, or ffmpeg's own `-reconnect` waiting out
a dropped one - and pacing straight off stdout has nothing to absorb that with: the stall reaches
the sink as silence immediately. The queue is the absorber; several seconds of it costs nothing
worth counting in memory. Pausing (`playpause`) stops `_drain` rather than `_reader`, so the queue
fills first and only once it is full does `_reader` stop draining stdout - at that point ffmpeg's
own stdout pipe fills too and back-pressures the decode, the same role a real device's small
hardware buffer plays for go-librespot; the queue just means a short pause no longer starts that
chain immediately.

`_drain` also primes a small cushion before starting its deadline clock (see `PREBUFFER_SECONDS`).
Without that, the clock starts the instant the decoder does, before real data has necessarily
started arriving - connection churn during startup (a slow TLS handshake, an early reconnect) then
reads as "already behind" the moment the clock starts, which trips the catch-up path immediately,
gets partially shed downstream, and settles into a permanent partial lag for the rest of the track
rather than a one-off startup delay. Reproduced live: a track that opened with several seconds of
`-reconnect` churn, audibly tried to catch up, got partially shed, and played the rest of the track
still behind - fixed by letting that churn happen before the clock starts rather than against it.
"""

import asyncio
import contextlib
import subprocess
import time

from ... import jobs
from .. import SAMPLE_RATE

CREATE_NO_WINDOW = 0x08000000

FRAME_BYTES = 4  # s16le stereo
READ_SIZE = 1 << 16

#: How much read-ahead the queue holds, in seconds of audio - the cushion a network stall drains
#: before it reaches the sink as silence.
QUEUE_SECONDS = 8.0
QUEUE_SIZE = max(1, round(QUEUE_SECONDS / (READ_SIZE / FRAME_BYTES / SAMPLE_RATE)))

#: How much to bank before `_drain` starts its deadline clock. Without this, the clock starts the
#: instant the decoder does, before real data has necessarily started arriving - a slow TLS
#: handshake or an early reconnect during startup then reads as "already behind", which trips the
#: catch-up path immediately and gets partially shed downstream, settling into a permanent partial
#: lag for the rest of the track rather than a one-off startup delay. Priming first means that
#: churn happens before the clock starts rather than against it.
PREBUFFER_SECONDS = 1.5
PREBUFFER_CHUNKS = max(1, round(PREBUFFER_SECONDS / (READ_SIZE / FRAME_BYTES / SAMPLE_RATE)))

#: How far ahead of real time the drain may run before it throttles.
LEAD_SECONDS = 0.2

#: Beyond this much behind, resync instead of paying the debt back as a burst.
MAX_CATCH_UP_SECONDS = 0.5


def _build_arguments(stream_url: str, http_headers: dict[str, str]) -> list[str]:
    arguments = [
        "-hide_banner", "-nostdin", "-loglevel", "level+warning",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "4",
    ]
    if http_headers:
        arguments += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())]
    arguments += [
        "-i", stream_url,
        "-vn", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
    ]
    return arguments


class Decoder:
    """One instance decodes exactly one track; a new one is made for the next."""

    def __init__(self, hub) -> None:
        self._hub = hub
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._drain_task: asyncio.Task | None = None
        self._log_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._resume = asyncio.Event()
        self._resume.set()
        #: Called once the track ends or the decode fails - never on a deliberate stop().
        self.on_finished = None

    async def start(self, ffmpeg_path: str, stream_url: str, http_headers: dict[str, str], on_pcm) -> None:
        self._process = await asyncio.create_subprocess_exec(
            ffmpeg_path, *_build_arguments(stream_url, http_headers),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(self._process)
        self._reader_task = asyncio.create_task(self._reader())
        self._drain_task = asyncio.create_task(self._drain(on_pcm))
        self._log_task = asyncio.create_task(self._read_log(self._process.stderr))

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    async def stop(self) -> None:
        process, self._process = self._process, None
        self.on_finished = None
        for task in (self._reader_task, self._drain_task, self._log_task):
            if task is not None:
                task.cancel()
        self._reader_task = self._drain_task = self._log_task = None

        if process is None:
            return
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
        except (OSError, ProcessLookupError, TimeoutError):
            pass
        finally:
            # A killed process's stdin/stdout/stderr pipe transports are otherwise only closed by
            # their own __del__ once nothing references them - which, on the Windows proactor loop,
            # tries to format an "unclosed transport" warning against a socket that is by then
            # already invalid, and prints an ugly traceback doing it. One track is one Decoder, so
            # this runs on every track change, not just at app shutdown.
            with contextlib.suppress(Exception):
                process._transport.close()

    async def _reader(self) -> None:
        """Fills the queue as fast as ffmpeg produces data - unthrottled beyond the queue's own
        capacity, so a fast stretch banks read-ahead for `_drain` to spend during a slow one."""
        stdout = self._process.stdout
        try:
            while True:
                data = await stdout.read(READ_SIZE)
                await self._queue.put(data)
                if not data:
                    return
        except Exception as exc:
            self._hub.warn(f"yt-dlp decode: reading stopped - {exc}")
            # Awaited rather than put_nowait: _drain must see this EOF marker eventually, even if
            # the queue happens to be full of read-ahead right now.
            with contextlib.suppress(Exception):
                await self._queue.put(b"")

    async def _drain(self, on_pcm) -> None:
        tail = b""

        # Primed without pacing or delivering - this is what lets startup churn resolve before the
        # deadline clock (started only after) has anything to be "behind" against. A track shorter
        # than the prebuffer hits EOF here instead, which the delivery loop below then drains
        # exactly like any other end of stream.
        primed: list[bytes] = []
        eof_after_priming = False
        for _ in range(PREBUFFER_CHUNKS):
            chunk = await self._queue.get()
            if not chunk:
                eof_after_priming = True
                break
            primed.append(chunk)

        deadline = time.monotonic()

        try:
            while True:
                await self._resume.wait()
                if primed:
                    data = primed.pop(0)
                elif eof_after_priming:
                    data = b""
                else:
                    data = await self._queue.get()
                if not data:
                    return

                data = tail + data
                keep = len(data) - len(data) % FRAME_BYTES
                tail = data[keep:]
                if keep:
                    on_pcm(data[:keep])

                deadline += (keep // FRAME_BYTES) / SAMPLE_RATE
                now = time.monotonic()
                ahead = deadline - now - LEAD_SECONDS
                if ahead > 0:
                    await asyncio.sleep(ahead)
                elif ahead < -MAX_CATCH_UP_SECONDS:
                    deadline = now + LEAD_SECONDS
        finally:
            # None here means stop() already owns reaping the process; only a natural end or a
            # decode failure reaches this still holding it.
            if self._process is not None:
                with contextlib.suppress(Exception):
                    await self._process.wait()
            if self.on_finished is not None:
                self.on_finished()

    async def _read_log(self, stream) -> None:
        async for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lowered = line.lower()
            level = "error" if "[error]" in lowered or "[fatal]" in lowered else "warn"
            self._hub.log(level, f"yt-dlp decode: {line[:200]}")
