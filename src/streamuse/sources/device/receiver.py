"""Whatever's playing on a Windows playback device the user picks, captured via WASAPI loopback.

Unlike AirPlay/Spotify this identifies no app at all - it's raw audio off a device, with no title,
artist or transport control, for routing something with no receiver of its own (a virtual cable, or
an app's own output device) into the stream.
"""

import threading

import numpy as np
import pyaudiowpatch as pyaudio

from .. import Receiver, TrackState, SAMPLE_RATE

#: Native WASAPI loopback buffer size. Small enough to keep latency reasonable, large enough that
#: the resampler always has plenty of frames to work with per call.
FRAMES_PER_BUFFER = 4096


def list_devices(pa: "pyaudio.PyAudio | None" = None) -> list[str]:
    """Loopback-capable playback devices, for the settings dropdown and for resolving a configured
    name back to a live device. Takes an existing PyAudio instance when the caller already holds
    one (DeviceReceiver polls this every publish tick), or opens a throwaway one otherwise."""
    owns = pa is None
    pa = pa or pyaudio.PyAudio()
    try:
        return [dev["name"] for dev in pa.get_loopback_device_info_generator()]
    finally:
        if owns:
            pa.terminate()


class DeviceReceiver(Receiver):
    source = "device"

    def __init__(self, settings, hub) -> None:
        self._settings = settings
        self._hub = hub

        self._track = TrackState()
        # Held for the receiver's whole life, not per lookup - available/reason are polled once a
        # second by SourceManager even while this isn't the selected source, and creating a fresh
        # PyAudio instance that often would be pure waste.
        self._pa = pyaudio.PyAudio()

        self._sink = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._capturing = False
        self._device_name = ""

    @property
    def available(self) -> bool:
        return self._resolve_device() is not None

    @property
    def reason(self) -> str:
        if not self._settings.deviceCaptureName:
            return "pick a playback device in Settings first"
        if self._resolve_device() is None:
            return "that device is no longer available"
        return ""

    @property
    def connected(self) -> bool:
        return self._capturing

    @property
    def status_text(self) -> str:
        return f"capturing '{self._device_name}'" if self._capturing else "device receiver stopped"

    def track(self) -> TrackState:
        return self._track

    async def start(self, sink) -> None:
        device = self._resolve_device()
        if device is None:
            # available can go stale between a publish tick and this call (the device dropped, or
            # nothing was ever configured) - not a case that can't happen, just a race.
            raise RuntimeError(self.reason or "no playback device configured")

        self._sink = sink
        self._device_name = device["name"]

        native_rate = int(device["defaultSampleRate"])
        channels = device["maxInputChannels"]

        self._stream = self._pa.open(
            format=pyaudio.paInt16, channels=channels, rate=native_rate, input=True,
            input_device_index=device["index"], frames_per_buffer=FRAMES_PER_BUFFER)

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, args=(self._stream, native_rate, channels),
            name="device-capture", daemon=True)
        self._thread.start()
        self._capturing = True

        self._hub.info(f"device: capturing '{self._device_name}' at {native_rate} Hz")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except Exception:
                pass

        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        self._sink = None
        self._capturing = False
        self._track.clear()

    def _resolve_device(self) -> dict | None:
        name = self._settings.deviceCaptureName
        if not name:
            return None
        return next((d for d in self._pa.get_loopback_device_info_generator()
                    if d["name"] == name), None)

    def _capture_loop(self, stream, native_rate: int, channels: int) -> None:
        """Runs on its own thread for the capture's whole lifetime. sink() is called straight from
        here, the same as AirPlay/Spotify's own delivery callbacks - push_audio is documented safe
        to call from a receiver thread."""
        resampler = _Resampler(native_rate, SAMPLE_RATE)

        while not self._stop_event.is_set():
            try:
                raw = stream.read(FRAMES_PER_BUFFER, exception_on_overflow=False)
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._hub.warn(f"device: capture stopped - {exc}")
                self._capturing = False
                return

            pcm = _convert(raw, channels, resampler)
            if pcm and self._sink is not None:
                self._sink(pcm)


class _Resampler:
    """Linear-interpolation resampler that carries its fractional phase across calls, so consecutive
    WASAPI reads stitch together without a click at every chunk boundary - a fresh interpolation per
    chunk would restart phase at 0 each time.

    This is the one place in the app that resamples in Python. Every other source already delivers
    44.1 kHz - WASAPI loopback taps the audio engine's own mixed stream, which runs at whatever rate
    the device negotiated (commonly 48 kHz), with no equivalent guarantee."""

    def __init__(self, source_rate: int, target_rate: int) -> None:
        self._ratio = source_rate / target_rate
        self._carry: np.ndarray | None = None
        self._phase = 0.0

    def push(self, frames: np.ndarray) -> np.ndarray:
        """frames: (n, 2) float32 in [-1, 1]. Returns resampled (m, 2) float32."""
        if self._carry is not None:
            frames = np.concatenate([self._carry, frames], axis=0)
            start = self._phase
        else:
            start = 0.0

        n = frames.shape[0]
        if n < 2:
            self._carry = frames
            return np.zeros((0, frames.shape[1]), dtype=np.float32)

        count = int((n - 1 - start) / self._ratio) + 1
        if count <= 0:
            self._carry = frames
            return np.zeros((0, frames.shape[1]), dtype=np.float32)

        positions = start + np.arange(count) * self._ratio
        indices = np.arange(n)
        out = np.empty((count, frames.shape[1]), dtype=np.float32)
        for channel in range(frames.shape[1]):
            out[:, channel] = np.interp(positions, indices, frames[:, channel])

        next_position = positions[-1] + self._ratio
        carry_start = int(next_position)
        self._carry = frames[carry_start:]
        self._phase = next_position - carry_start
        return out


def _convert(raw: bytes, channels: int, resampler: _Resampler) -> bytes:
    frames = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
    stereo = _to_stereo(frames).astype(np.float32) / 32768.0

    resampled = resampler.push(stereo)
    if resampled.shape[0] == 0:
        return b""

    clipped = np.clip(resampled, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _to_stereo(frames: np.ndarray) -> np.ndarray:
    if frames.shape[1] == 2:
        return frames
    if frames.shape[1] == 1:
        return np.repeat(frames, 2, axis=1)
    return frames[:, :2]
