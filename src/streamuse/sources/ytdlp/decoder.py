"""Decodes one already-downloaded track to raw PCM by running ffmpeg once per track, on its own
thread rather than the shared asyncio loop - the same pattern spotify/pipe.py already uses for the
same reason: real-time pacing that must never be at the mercy of whatever else the loop is doing.

The input is always the bytes cache.py already finished downloading into memory before this ever
starts - nothing here talks to the network, and nothing here touches disk either, so there is no file
for antivirus real-time scanning to intercept mid-open. ffmpeg reads it over its stdin pipe
(`-i pipe:0`) instead of a path, fed by its own thread rather than the read thread, for the same
reason PipeReader's own writer/reader are never the same call: writing several MB to stdin and only
then reading stdout would deadlock the moment ffmpeg's own stdout pipe fills - it would be blocked
writing PCM nobody is draining yet, while this side is blocked writing input it isn't ready to accept.

A dedicated OS thread doing a blocking `read()` paced by `time.sleep()` needs no separate read-ahead
queue the way an asyncio coroutine sharing the loop did - the OS pipe between ffmpeg and this thread
already backpressures exactly like the named pipe go-librespot writes into, and nothing on the shared
loop (a slow HLS bitrate measurement, video frame compositing, another decoder's own I/O) can stall a
thread that never asks the loop for anything mid-read. `on_pcm` and `on_finished` still have to cross
back onto the loop through `call_soon_threadsafe`, the same as the hub itself is mutated from a
receiver thread (see CLAUDE.md).
"""

import contextlib
import subprocess
import threading
import time

from ... import jobs
from .. import SAMPLE_RATE

CREATE_NO_WINDOW = 0x08000000

FRAME_BYTES = 4  # s16le stereo
READ_SIZE = 1 << 16

#: How far ahead of real time the read loop may run before it throttles.
LEAD_SECONDS = 0.2

#: Beyond this much behind, resync instead of paying the debt back as a burst.
MAX_CATCH_UP_SECONDS = 0.5


def _build_arguments() -> list[str]:
    # -reconnect and friends are a network-protocol option; ffmpeg refuses to start at all with
    # "Option not found" if they are given for stdin, which is all this ever reads now.
    return [
        "-hide_banner", "-nostdin", "-loglevel", "level+warning",
        "-i", "pipe:0",
        "-vn", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
    ]


class Decoder:
    """One instance decodes exactly one track; a new one is made for the next."""

    def __init__(self, hub, loop) -> None:
        self._hub = hub
        self._loop = loop
        self._process: subprocess.Popen | None = None
        self._writer_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._log_thread: threading.Thread | None = None
        #: Set before the process is killed, so the reader thread can tell a deliberate stop() apart
        #: from ffmpeg exiting on its own once it notices EOF - the same distinction the previous
        #: asyncio version drew from whether stop() had already cleared self._process.
        self._stopping = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        #: Called once the track ends or the decode fails - never on a deliberate stop(). Always
        #: fires via call_soon_threadsafe, so it runs on the loop no matter which thread noticed.
        self.on_finished = None

    def start(self, ffmpeg_path: str, data: bytes, on_pcm) -> None:
        self._process = subprocess.Popen(
            [ffmpeg_path, *_build_arguments()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(self._process)
        self._writer_thread = threading.Thread(
            target=self._write_input, args=(data,), name="ytdlp-write", daemon=True)
        self._writer_thread.start()
        self._reader_thread = threading.Thread(
            target=self._read_and_pace, args=(on_pcm,), name="ytdlp-decode", daemon=True)
        self._reader_thread.start()
        self._log_thread = threading.Thread(
            target=self._read_log, name="ytdlp-log", daemon=True)
        self._log_thread.start()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def stop(self) -> None:
        self._stopping.set()
        self._resume.set()  # release a paused read loop so it can see the stop
        self.on_finished = None

        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

        for thread in (self._writer_thread, self._reader_thread, self._log_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._writer_thread = self._reader_thread = self._log_thread = None

        if process is None:
            return
        with contextlib.suppress(Exception):
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            with contextlib.suppress(Exception):
                stream.close()

    def _write_input(self, data: bytes) -> None:
        stdin = self._process.stdin
        try:
            stdin.write(data)
        except OSError:
            # ffmpeg exiting early (a bad track) closes its end first - not this side's problem to
            # report, _read_log already carries ffmpeg's own reason.
            pass
        finally:
            with contextlib.suppress(OSError):
                stdin.close()

    def _read_and_pace(self, on_pcm) -> None:
        stdout = self._process.stdout
        tail = b""
        deadline = time.monotonic()
        fault = None

        try:
            while True:
                self._resume.wait()
                if self._stopping.is_set():
                    return

                data = stdout.read(READ_SIZE)
                if not data:
                    return

                data = tail + data
                keep = len(data) - len(data) % FRAME_BYTES
                tail = data[keep:]
                if keep:
                    self._loop.call_soon_threadsafe(on_pcm, data[:keep])

                deadline += (keep // FRAME_BYTES) / SAMPLE_RATE
                now = time.monotonic()
                ahead = deadline - now - LEAD_SECONDS
                if ahead > 0:
                    time.sleep(ahead)
                elif ahead < -MAX_CATCH_UP_SECONDS:
                    deadline = now + LEAD_SECONDS
        except OSError as exc:
            fault = exc
        finally:
            # A deliberate stop() already owns clearing the track - this only fires for a natural
            # end or a decode failure, the same distinction the gone self._process-is-None check drew.
            if not self._stopping.is_set():
                if fault is not None:
                    self._loop.call_soon_threadsafe(
                        self._hub.warn, f"yt-dlp decode: reading stopped - {fault}")
                if self.on_finished is not None:
                    self._loop.call_soon_threadsafe(self.on_finished)

    def _read_log(self) -> None:
        stderr = self._process.stderr
        try:
            for raw in stderr:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                lowered = line.lower()
                level = "error" if "[error]" in lowered or "[fatal]" in lowered else "warn"
                self._loop.call_soon_threadsafe(self._hub.log, level, f"yt-dlp decode: {line[:200]}")
        except OSError:
            pass
