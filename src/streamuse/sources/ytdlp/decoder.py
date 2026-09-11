"""Decodes one already-downloaded track to raw PCM by running ffmpeg once per track.

The input is always a local file that cache.py finished writing before this ever starts - nothing
here talks to the network. ffmpeg still reads a local file far faster than playback, though, so an
unpaced drain of its stdout would hand the pacer a whole track in a few seconds - the same failure
the Spotify pipe reader exists to avoid (see CLAUDE.md). Draining is paced to real time the same way,
but reading is not: a `_reader` task fills a bounded queue as fast as ffmpeg produces data, and
`_drain` paces its way through that queue on its own clock. Disk reads don't stall the way a network
fetch used to before caching existed, so the queue rarely does more than sit comfortably full, but
keeping the same margin costs nothing and leaves this decoder able to read from anything ffmpeg's
`-i` accepts, not just a finished local file.

`_drain` also primes a small cushion before starting its deadline clock (see `PREBUFFER_SECONDS`),
so a slow-to-open file doesn't read as "already behind" the moment the clock starts and trip the
catch-up path against nothing.
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

#: How much read-ahead the queue holds, in seconds of audio - the cushion a stall in reading the
#: input would drain before it reaches the sink as silence. A local file's `_reader` can fill this
#: almost instantly (nothing paces it the way a network fetch used to), so this stays just large
#: enough to clear `PREBUFFER_SECONDS` with headroom rather than the much bigger margin a network
#: stall used to need - a bigger queue does not reach further, it only banks more backlog that a
#: stall anywhere else in the process (observed: antivirus interfering with ffmpeg's own process
#: lifetime, not this decoder) would have ready to dump downstream at once.
QUEUE_SECONDS = 2.5
QUEUE_SIZE = max(1, round(QUEUE_SECONDS / (READ_SIZE / FRAME_BYTES / SAMPLE_RATE)))

#: How much to bank before `_drain` starts its deadline clock. Without this, the clock starts the
#: instant the decoder does, before real data has necessarily started arriving - a slow file open
#: then reads as "already behind", which trips the catch-up path immediately and gets partially shed
#: downstream, settling into a permanent partial lag for the rest of the track rather than a one-off
#: startup delay. Priming first means that startup cost happens before the clock starts rather than
#: against it.
PREBUFFER_SECONDS = 1.5
PREBUFFER_CHUNKS = max(1, round(PREBUFFER_SECONDS / (READ_SIZE / FRAME_BYTES / SAMPLE_RATE)))

#: How far ahead of real time the drain may run before it throttles.
LEAD_SECONDS = 0.2

#: Beyond this much behind, resync instead of paying the debt back as a burst.
MAX_CATCH_UP_SECONDS = 0.5


def _build_arguments(file_path: str) -> list[str]:
    # -reconnect and friends are a network-protocol option; ffmpeg refuses to start at all with
    # "Option not found" if they are given for a plain local file, which is all this ever opens now.
    return [
        "-hide_banner", "-nostdin", "-loglevel", "level+warning",
        "-i", file_path,
        "-vn", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
    ]


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

    async def start(self, ffmpeg_path: str, file_path: str, on_pcm) -> None:
        self._process = await asyncio.create_subprocess_exec(
            ffmpeg_path, *_build_arguments(file_path),
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
        tasks = [t for t in (self._reader_task, self._drain_task, self._log_task) if t is not None]
        self._reader_task = self._drain_task = self._log_task = None
        for task in tasks:
            task.cancel()
        # Cancelling only requests it - awaited here so none is left dangling half-cancelled once
        # this returns. Left unawaited, a task can still be pending when the loop later stops (at
        # app shutdown), and gets abandoned rather than unwound: interpreter exit then tries to
        # close it via GeneratorExit with no running loop left to do it on, printing an ignored
        # "coroutine ignored GeneratorExit" traceback.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
