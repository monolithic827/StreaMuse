"""The named pipe go-librespot writes PCM into.

go-librespot is the client here, so this side is the server and its instance must exist before the
daemon starts. The daemon closes the pipe whenever playback stops or moves to another device and
reopens it on the next play, so this is a loop that survives any number of connect cycles - the
pacer fills the silence in between and never learns that anything happened.
"""

import _winapi
import threading
import time

from .. import SAMPLE_RATE

PIPE_NAME = r"\\.\pipe\streamuse-spotify"

#: s16le stereo, so a partial read must not split a frame.
FRAME_BYTES = 4
READ_SIZE = 1 << 16
# go-librespot's write() only blocks once this fills, and that blocking is its only pacing - on
# Unix that role is played by a real device's small hardware buffer. Sized for ~6 seconds, this let
# go-librespot dump several seconds of pre-buffered audio in almost instantly on connect, which its
# own position tracking (advanced by the same write) then reports as "played" well before this
# reader has actually forwarded it - a fixed lag baked in from the first sample of every track.
# Small enough that a real hardware buffer would look similar, this makes writes block quickly
# enough that go-librespot can never get more than a fraction of a second ahead of what has
# actually reached AudioPacer.
BUFFER_SIZE = 1 << 15

#: PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT are all zero.
PIPE_MODE_BYTE = 0

#: How far ahead of real time the drain may run before it starts throttling.
LEAD_SECONDS = 0.2

#: Beyond this much behind, treat it as a stall rather than debt to pay back. Otherwise a moment
#: where this thread doesn't get scheduled - go-librespot keeps writing into the pipe's kernel
#: buffer regardless - would flush as an unthrottled burst once it resumes, which is exactly what
#: the throttle exists to prevent. AudioPacer already fills a gap like this with silence, the same
#: way it does for a genuinely paused source.
MAX_CATCH_UP_SECONDS = 0.5


class PipeReader:
    """Runs its blocking reads on a thread and hands whole frames back on the loop."""

    def __init__(self, loop, on_pcm, hub) -> None:
        self._loop = loop
        self._on_pcm = on_pcm
        self._hub = hub
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="spotify-pipe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # The thread is parked in ConnectNamedPipe; a client open is what releases it.
        try:
            with open(PIPE_NAME, "wb"):
                pass
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.connected = False

    def _serve(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                handle = _winapi.CreateNamedPipe(
                    PIPE_NAME,
                    _winapi.PIPE_ACCESS_INBOUND
                    | (_winapi.FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
                    PIPE_MODE_BYTE,
                    _winapi.PIPE_UNLIMITED_INSTANCES,
                    0, BUFFER_SIZE,
                    _winapi.NMPWAIT_WAIT_FOREVER,
                    _winapi.NULL,
                )
            except OSError as exc:
                self._loop.call_soon_threadsafe(
                    self._hub.error, f"spotify: could not open the audio pipe ({exc})")
                return

            first = False
            try:
                self._read(handle)
            except OSError:
                pass
            finally:
                _winapi.CloseHandle(handle)
                self.connected = False

    def _read(self, handle) -> None:
        try:
            _winapi.ConnectNamedPipe(handle)
        except OSError as exc:
            # 535 means the client got in between the create and the connect.
            if exc.winerror != _winapi.ERROR_PIPE_CONNECTED:
                raise

        if self._stop.is_set():
            return

        self.connected = True
        tail = b""
        # go-librespot's pipe output has no pacing of its own: on Unix, a FIFO's reader is real audio
        # hardware, and its write() blocking on a full buffer is what paces the whole decode loop to
        # real time. Draining as fast as bytes arrive removes that backpressure entirely, so
        # go-librespot decodes and delivers an entire track in a few CPU-bound seconds, then considers
        # it finished and skips to the next one. Sleeping here to match real time is what makes its
        # writes block on a full pipe the way a real device would.
        deadline = time.monotonic()

        while not self._stop.is_set():
            data, _ = _winapi.ReadFile(handle, READ_SIZE)
            if not data:
                return

            data = tail + data
            keep = len(data) - len(data) % FRAME_BYTES
            tail = data[keep:]
            if keep:
                self._loop.call_soon_threadsafe(self._on_pcm, data[:keep])

            deadline += (keep // FRAME_BYTES) / SAMPLE_RATE
            now = time.monotonic()
            ahead = deadline - now - LEAD_SECONDS
            if ahead > 0:
                time.sleep(ahead)
            elif ahead < -MAX_CATCH_UP_SECONDS:
                deadline = now + LEAD_SECONDS
