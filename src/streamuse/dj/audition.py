"""The pre-listen a DJ does in headphones before the crowd hears anything: check the fetched track
is actually playable, find where the music really starts, and refuse it outright if it isn't worth
playing. Without this a silent or near-silent download - a video whose audio track is empty, a
failed fetch that still produced a file - would crossfade the live source out into nothing, and the
first sign of trouble would be dead air on the stream."""

import math
from dataclasses import dataclass

import numpy as np

#: Below this peak the track is treated as unplayable rather than merely quiet.
MIN_PEAK_DB = -45.0
MIN_SECONDS = 5.0

#: Leading audio is skipped until it reaches this fraction of the track's own peak, so the mix
#: starts on the music rather than on an intro of near-silence.
START_THRESHOLD_FRACTION = 0.08


@dataclass
class AuditionResult:
    ok: bool
    reason: str
    start_frame: int
    peak_db: float
    duration_seconds: float


def audition(stereo: np.ndarray, sample_rate: int) -> AuditionResult:
    """stereo is shape (frames, 2) float32."""
    duration = stereo.shape[0] / sample_rate
    if duration < MIN_SECONDS:
        return AuditionResult(False, f"only {duration:.1f}s of audio - too short to mix",
                              0, -math.inf, duration)

    peak = float(np.abs(stereo).max()) if stereo.size else 0.0
    peak_db = 20 * math.log10(peak) if peak > 0 else -math.inf

    if peak_db < MIN_PEAK_DB:
        description = "digital silence" if math.isinf(peak_db) else f"{peak_db:.1f} dB"
        return AuditionResult(False, f"silent download (peak {description})", 0, peak_db, duration)

    return AuditionResult(True, "ok", _find_start(stereo, peak * START_THRESHOLD_FRACTION),
                          peak_db, duration)


def _find_start(stereo: np.ndarray, threshold: float) -> int:
    """First frame whose sample rises above the threshold."""
    if threshold <= 0:
        return 0
    above = np.any(np.abs(stereo) >= threshold, axis=1)
    hit = np.flatnonzero(above)
    return int(hit[0]) if hit.size else 0
