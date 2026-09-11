"""Paces one already-decoded track's raw PCM out to the sink in real time, on its own thread rather
than the shared asyncio loop - the same pattern spotify/pipe.py already uses for the same reason:
real-time pacing that must never be at the mercy of whatever else the loop is doing.

Decoding itself happens in cache.py, during prefetch, off the critical path entirely - by the time
this ever runs, the whole track is already sitting in memory as raw PCM. That leaves this with a
single job: hand it out at 1x real time. `AudioPacer`'s own jitter buffer would otherwise shed
whatever does not fit, so pacing here is what keeps delivery inside its allowance instead of dumping
the whole track in at once - though that allowance is generous for this source specifically (see
receiver.py's PACER_MAX_LATENCY_MS), so small, ordinary timing variance is absorbed as harmless lead
rather than needing to be corrected against. No live process is involved in this anymore, which also
rules out ffmpeg's own process/pipe scheduling as a factor in how that pacing behaves.
"""

import threading
import time

from .. import SAMPLE_RATE

FRAME_BYTES = 4  # s16le stereo
CHUNK_BYTES = (1 << 14) * FRAME_BYTES  # ~0.37s per chunk at 44100Hz

#: How far ahead of real time the pacing may run before it throttles.
LEAD_SECONDS = 0.2

#: Beyond this much behind, resync instead of paying the debt back as a burst.
MAX_CATCH_UP_SECONDS = 0.5


class Decoder:
    """One instance paces exactly one track's already-decoded PCM; a new one is made for the next."""

    def __init__(self, hub, loop) -> None:
        self._hub = hub
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        #: Called once the track ends or the decode fails - never on a deliberate stop(). Always
        #: fires via call_soon_threadsafe, so it runs on the loop no matter which thread noticed.
        self.on_finished = None

    def start(self, pcm: bytes, on_pcm) -> None:
        self._thread = threading.Thread(
            target=self._pace, args=(pcm, on_pcm), name="ytdlp-pace", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def stop(self) -> None:
        self._stopping.set()
        self._resume.set()  # release a paused pacer so it can see the stop
        self.on_finished = None
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None

    def _pace(self, pcm: bytes, on_pcm) -> None:
        deadline = time.monotonic()
        pos = 0

        while pos < len(pcm):
            self._resume.wait()
            if self._stopping.is_set():
                return

            chunk = pcm[pos:pos + CHUNK_BYTES]
            pos += len(chunk)
            self._loop.call_soon_threadsafe(on_pcm, chunk)

            deadline += (len(chunk) // FRAME_BYTES) / SAMPLE_RATE
            now = time.monotonic()
            ahead = deadline - now - LEAD_SECONDS
            if ahead > 0:
                time.sleep(ahead)
            elif ahead < -MAX_CATCH_UP_SECONDS:
                deadline = now + LEAD_SECONDS

        if not self._stopping.is_set() and self.on_finished is not None:
            self._loop.call_soon_threadsafe(self.on_finished)
