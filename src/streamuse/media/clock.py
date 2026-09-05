"""One monotonic clock per streaming session. Both pacers derive their deadlines from it, which is
what keeps audio and video in step."""

import time


class Clock:
    def __init__(self) -> None:
        self._start: float | None = None

    def restart(self) -> None:
        self._start = time.monotonic()

    def stop(self) -> None:
        self._start = None

    @property
    def elapsed_ms(self) -> int:
        if self._start is None:
            return 0
        return int((time.monotonic() - self._start) * 1000)

    @property
    def elapsed_seconds(self) -> float:
        if self._start is None:
            return 0.0
        return time.monotonic() - self._start
