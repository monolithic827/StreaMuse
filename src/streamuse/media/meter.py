"""Reduces samples to the 34-bar meter the panel draws, plus a peak reading."""

import array
import math
import operator
import threading

BARS = 34

#: Samples per bar - roughly 25 ms, so a full sweep is about 0.85 s.
SAMPLES_PER_BAR_DIVISOR = 40


class LevelMeter:
    def __init__(self, sample_rate: int, bars: int = BARS) -> None:
        self._samples_per_bar = sample_rate * 2 // SAMPLES_PER_BAR_DIVISOR
        self._bars = [0.0] * bars
        self._lock = threading.Lock()
        self._cursor = 0
        self._window = array.array("h")
        self._decayed_peak = 0.0

    def add(self, chunk: bytes) -> None:
        samples = array.array("h")
        samples.frombytes(chunk[:len(chunk) - len(chunk) % 2])

        with self._lock:
            self._window.extend(samples)

            while len(self._window) >= self._samples_per_bar:
                window = self._window[:self._samples_per_bar]
                del self._window[:self._samples_per_bar]

                peak = max(max(window), -min(window)) / 32768
                rms = math.sqrt(sum(map(operator.mul, window, window)) / len(window)) / 32768

                self._bars[self._cursor] = rms
                self._cursor = (self._cursor + 1) % len(self._bars)

                if peak > self._decayed_peak:
                    self._decayed_peak = peak
                else:
                    self._decayed_peak *= 0.92

    def read(self) -> tuple[list[float], float | None]:
        """Bar heights as percentages, oldest first. Peak is None rather than -inf when silent:
        JSON cannot carry a non-finite number and the throw would be swallowed."""
        with self._lock:
            ordered = [
                round(_to_percent(self._bars[(self._cursor + i) % len(self._bars)]), 1)
                for i in range(len(self._bars))
            ]
            peak_db = None if self._decayed_peak <= 0.00001 else 20 * math.log10(self._decayed_peak)
            return ordered, peak_db

    def reset(self) -> None:
        with self._lock:
            self._bars = [0.0] * len(self._bars)
            self._cursor = 0
            del self._window[:]
            self._decayed_peak = 0.0


def _to_percent(amplitude: float) -> float:
    """Maps linear amplitude onto 0-100 across a 60 dB window."""
    if amplitude <= 0.0001:
        return 4.0
    db = 20 * math.log10(amplitude)
    return min(100.0, max(4.0, (db + 60) / 60 * 100))
