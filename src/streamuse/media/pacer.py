"""Wall-clock pacing: the reason the stream never stalls.

A receiver delivers nothing while the sender is paused, so both pacers emit on the clock rather
than on source activity - the audio pacer writes exactly one second of frames per second of wall
clock, filling silence on underrun, and the video pacer emits a fixed frame rate from the same
clock. The demuxers derive timestamps from data received, so this is also what keeps A/V in sync.
"""

import asyncio
import threading
from collections import deque

from .clock import Clock

CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2  # s16le stereo

#: How much received audio to hold back to absorb burstiness.
TARGET_LATENCY_MS = 200

#: Above this the buffer is trimmed; the source is running faster than the clock.
MAX_LATENCY_MS = 600

WRITE_INTERVAL_MS = 20


class AudioPacer:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._mixer = None
        self._lock = threading.Lock()
        self._pending: deque[bytes] = deque()
        self._head_offset = 0
        self._pending_bytes = 0
        self._frames_written = 0
        self._silence_frames = 0
        self._dropped_frames = 0
        self.has_signal = False

    def set_mixer(self, mixer) -> None:
        self._mixer = mixer

    def push(self, chunk: bytes) -> None:
        """Called from receiver threads as well as the loop."""
        if not chunk:
            return

        with self._lock:
            self._pending.append(chunk)
            self._pending_bytes += len(chunk)

            # Shed oldest rather than accumulate unbounded latency when the source outruns the clock.
            limit = MAX_LATENCY_MS * self.sample_rate // 1000 * BYTES_PER_FRAME
            while self._pending_bytes > limit and len(self._pending) > 1:
                dropped = self._pending.popleft()
                lost = len(dropped) - self._head_offset
                self._head_offset = 0
                self._pending_bytes -= lost
                self._dropped_frames += lost // BYTES_PER_FRAME

    @property
    def frames_written(self) -> int:
        with self._lock:
            return self._frames_written

    @property
    def silence_frames(self) -> int:
        with self._lock:
            return self._silence_frames

    @property
    def dropped_frames(self) -> int:
        with self._lock:
            return self._dropped_frames

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._head_offset = 0
            self._pending_bytes = 0
            self._frames_written = 0
            self._silence_frames = 0
            self._dropped_frames = 0
        self.has_signal = False

    async def run(self, writer: asyncio.StreamWriter, clock: Clock) -> None:
        interval = WRITE_INTERVAL_MS / 1000
        silence_in_window = 0
        frames_in_window = 0
        window_start = clock.elapsed_ms

        while True:
            due = clock.elapsed_ms * self.sample_rate // 1000 - self._frames_written

            if due <= 0:
                await asyncio.sleep(interval)
                continue

            # Cap catch-up so a stall cannot dump a huge write at once.
            frames = min(due, self.sample_rate)
            wanted = frames * BYTES_PER_FRAME
            chunk = self._drain(wanted)

            if len(chunk) < wanted:
                missing = wanted - len(chunk)
                chunk += bytes(missing)
                silent_frames = missing // BYTES_PER_FRAME
                silence_in_window += silent_frames
                with self._lock:
                    self._silence_frames += silent_frames

            # The DJ mixer blends in on the pacer's own tick, never on receiver delivery - a deck
            # driven by the receiver callback would freeze exactly when the live source goes quiet,
            # which is the one case the mixer exists to paper over. chunk is always full-length real
            # or silence-padded audio by this point, whichever it turns out to be.
            if self._mixer is not None:
                chunk = self._mixer.mix(chunk)

            writer.write(chunk)
            await writer.drain()

            with self._lock:
                self._frames_written += frames
            frames_in_window += frames

            if clock.elapsed_ms - window_start >= 1000:
                self.has_signal = frames_in_window > 0 and silence_in_window < frames_in_window // 2
                silence_in_window = 0
                frames_in_window = 0
                window_start = clock.elapsed_ms

            await asyncio.sleep(interval)

    def _drain(self, wanted: int) -> bytes:
        """Drains the jitter buffer, holding back TARGET_LATENCY_MS as reserve."""
        with self._lock:
            reserve = TARGET_LATENCY_MS * self.sample_rate // 1000 * BYTES_PER_FRAME
            take = min(wanted, max(0, self._pending_bytes - reserve))
            parts: list[bytes] = []
            written = 0

            while written < take and self._pending:
                head = self._pending[0]
                available = len(head) - self._head_offset
                size = min(available, take - written)

                parts.append(head[self._head_offset:self._head_offset + size])
                written += size
                self._head_offset += size
                self._pending_bytes -= size

                if self._head_offset >= len(head):
                    self._pending.popleft()
                    self._head_offset = 0

            return b"".join(parts)


class VideoPacer:
    """Pushes exactly fps JPEG frames per second of wall clock. image2pipe timestamps by frame
    index, so this is what locks video to audio."""

    def __init__(self, renderer) -> None:
        self._renderer = renderer

    async def run(self, writer: asyncio.StreamWriter, clock: Clock, fps: int) -> None:
        interval = max(1, 1000 // max(fps, 1)) / 1000
        frames_written = 0

        while True:
            due = clock.elapsed_ms * fps // 1000 - frames_written

            if due <= 0:
                await asyncio.sleep(interval / 2 + 0.001)
                continue

            # Cap catch-up so a stall cannot dump hundreds of frames at once.
            frames = min(due, fps)
            jpeg = self._renderer.render()

            writer.write(jpeg * frames)
            frames_written += frames
            await writer.drain()

            await asyncio.sleep(interval / 2 + 0.001)
