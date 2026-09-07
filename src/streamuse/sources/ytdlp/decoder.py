"""Decodes one resolved stream URL to raw PCM by running ffmpeg once per track.

ffmpeg decodes across the network far faster than playback, so an unpaced drain of its stdout
would hand the pacer a whole track in a few seconds - the same failure the Spotify pipe reader
exists to avoid (see CLAUDE.md). This paces the drain to real time the same way, and since it is
this side that is slow to read rather than ffmpeg that is slow to write, pausing it lets ffmpeg's
own stdout pipe fill and back-pressure the decode - the same role a real device's small hardware
buffer plays for go-librespot.
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
        self._pump_task: asyncio.Task | None = None
        self._log_task: asyncio.Task | None = None
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
        self._pump_task = asyncio.create_task(self._pump(on_pcm))
        self._log_task = asyncio.create_task(self._read_log(self._process.stderr))

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    async def stop(self) -> None:
        process, self._process = self._process, None
        self.on_finished = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        if self._log_task is not None:
            self._log_task.cancel()
            self._log_task = None

        if process is None:
            return
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
        except (OSError, ProcessLookupError, TimeoutError):
            pass

    async def _pump(self, on_pcm) -> None:
        stdout = self._process.stdout
        tail = b""
        deadline = time.monotonic()

        try:
            while True:
                await self._resume.wait()
                data = await stdout.read(READ_SIZE)
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
