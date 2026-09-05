"""The named pipe go-librespot writes PCM into.

go-librespot is the client here, so this side is the server and its instance must exist before the
daemon starts. The daemon closes the pipe whenever playback stops or moves to another device and
reopens it on the next play, so this is a loop that survives any number of connect cycles - the
pacer fills the silence in between and never learns that anything happened.
"""

import _winapi
import threading

PIPE_NAME = r"\\.\pipe\streamuse-spotify"

#: s16le stereo, so a partial read must not split a frame.
FRAME_BYTES = 4
READ_SIZE = 1 << 16
BUFFER_SIZE = 1 << 20

#: PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT are all zero.
PIPE_MODE_BYTE = 0


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

        while not self._stop.is_set():
            data, _ = _winapi.ReadFile(handle, READ_SIZE)
            if not data:
                return

            data = tail + data
            keep = len(data) - len(data) % FRAME_BYTES
            tail = data[keep:]
            if keep:
                self._loop.call_soon_threadsafe(self._on_pcm, data[:keep])
