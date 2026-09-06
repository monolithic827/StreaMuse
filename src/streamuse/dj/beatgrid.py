"""Beat detection with plain numpy, standing in for the SoundTouch-based detector the reference
(.NET) implementation uses - SoundTouch.Net has no Python equivalent, so this reimplements the same
onset/period/confidence *shape* on our own onset envelope instead of SoundTouch's BpmDetect.

Confidence is beat-spacing regularity, not beat strength. A spurious run of onsets picked out of
noise can score as strong as a real kick drum on magnitude alone, but its spacing never settles on
a stable period the way real music's does - that's what actually separates the two, so it's what
gets measured. The reference implementation calibrates its own regularity formula to 0.35 (white
noise 4.6%, real tracks median 96%); this module's onset extraction is different enough that its own
noise ceiling sits much higher (see CONFIDENCE_THRESHOLD below), so the threshold here is
recalibrated against this formula rather than copied.
"""

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 44100

#: Plausible tempo range for the autocorrelation search.
MIN_BPM = 60.0
MAX_BPM = 180.0

#: Below this, a grid is treated as unreadable - fall back to an equal-power fade rather than
#: beatmatch to a guess. Calibrated against this module's own spacing-confidence formula, not a
#: borrowed number: 100 fifteen-second white-noise trials topped out at ~0.47 (mean ~0.26), so
#: 0.55 sits with real margin above the noise ceiling while real click tracks in the same test
#: scored 0.98+.
CONFIDENCE_THRESHOLD = 0.55

#: First beat of a run this long is where the groove has actually locked in - the real DJ cueing
#: decision, not the track's technical start or its very first detected onset.
MIX_IN_WINDOW_BEATS = 8

FFT_SIZE = 2048
HOP_SIZE = 512

_MIN_ONSET_GAP_SECONDS = 0.1
_ONSET_THRESHOLD_STDS = 0.5

#: How far a gap may sit from a whole number of periods and still count as on the grid - matches
#: the reference implementation's GridTolerance, used both for mix-in detection and (there) for the
#: regularity score itself.
_GRID_TOLERANCE = 0.12

MIN_SECONDS = 2.0

#: How much better a harmonic candidate's spacing confidence has to be before an octave switch is
#: trusted, and how good it has to be outright. Measured against synthetic noise: a harmonic
#: candidate can climb to ~0.45-0.47 purely from the minimum-onset-gap spacing artificially
#: regularizing noise, while a genuine octave correction on real material climbs past 0.9 - so the
#: floor sits well clear of the noise ceiling and well under a real correction, not just above the
#: base confidence threshold.
_OCTAVE_SWITCH_MARGIN = 0.15
_OCTAVE_SWITCH_MIN_CONFIDENCE = 0.7


@dataclass
class BeatGrid:
    period_frames: float
    confidence: float
    last_beat_frame: int = 0
    mix_in_frame: int = 0

    @property
    def bpm(self) -> float:
        return 60.0 * SAMPLE_RATE / self.period_frames if self.period_frames > 0 else 0.0


def analyze(mono: np.ndarray, sample_rate: int = SAMPLE_RATE) -> BeatGrid | None:
    """mono is float32 in [-1, 1]. None means the buffer is too short to say anything."""
    if mono.size < max(FFT_SIZE * 4, MIN_SECONDS * sample_rate):
        return None

    envelope = _onset_envelope(mono)
    if envelope.size < 8:
        return None

    period_hops = _estimate_period_hops(envelope, sample_rate)
    if period_hops is None:
        return None

    onset_hops = _pick_onsets(envelope, sample_rate)
    if onset_hops.size < 4:
        return BeatGrid(period_hops * HOP_SIZE, 0.0)

    period_hops = _resolve_octave(onset_hops, period_hops, sample_rate)
    confidence = _cross_validated_confidence(onset_hops, period_hops)
    mix_in_index = _find_mix_in(onset_hops, period_hops)

    onset_frames = onset_hops * HOP_SIZE
    mix_in_frame = int(onset_frames[mix_in_index]) if mix_in_index is not None else int(onset_frames[0])

    return BeatGrid(
        period_frames=period_hops * HOP_SIZE,
        confidence=confidence,
        last_beat_frame=int(onset_frames[-1]),
        mix_in_frame=mix_in_frame,
    )


def _onset_envelope(mono: np.ndarray) -> np.ndarray:
    """Spectral flux: half-wave-rectified frame-to-frame magnitude increase, summed across bins."""
    n_frames = 1 + (mono.size - FFT_SIZE) // HOP_SIZE
    if n_frames < 2:
        return np.zeros(0, dtype=np.float32)

    window = np.hanning(FFT_SIZE).astype(np.float32)
    starts = HOP_SIZE * np.arange(n_frames)[:, None]
    indices = starts + np.arange(FFT_SIZE)[None, :]
    frames = mono[indices] * window
    spectra = np.abs(np.fft.rfft(frames, axis=1))

    flux = np.diff(spectra, axis=0)
    np.clip(flux, 0.0, None, out=flux)
    return flux.sum(axis=1).astype(np.float32)


def _estimate_period_hops(envelope: np.ndarray, sample_rate: int) -> float | None:
    centered = envelope - envelope.mean()
    n = centered.size
    if n < 4:
        return None

    min_lag = max(1, int(sample_rate * 60.0 / MAX_BPM / HOP_SIZE))
    max_lag = min(n - 1, int(sample_rate * 60.0 / MIN_BPM / HOP_SIZE))
    if max_lag <= min_lag:
        return None

    padded = np.zeros(2 * n, dtype=np.float64)
    padded[:n] = centered
    spectrum = np.fft.rfft(padded)
    autocorr = np.fft.irfft(spectrum * np.conj(spectrum))[:n]

    window = autocorr[min_lag:max_lag + 1]
    if window.size == 0 or not np.any(window > 0):
        return None
    best = min_lag + int(np.argmax(window))

    # Parabolic interpolation around the peak for sub-hop precision.
    if 0 < best < autocorr.size - 1:
        y0, y1, y2 = autocorr[best - 1], autocorr[best], autocorr[best + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            best = best + 0.5 * (y0 - y2) / denom

    return float(best)


def _pick_onsets(envelope: np.ndarray, sample_rate: int) -> np.ndarray:
    if envelope.size < 3:
        return np.zeros(0, dtype=np.int64)

    threshold = envelope.mean() + _ONSET_THRESHOLD_STDS * envelope.std()
    is_peak = ((envelope[1:-1] > envelope[:-2]) & (envelope[1:-1] > envelope[2:])
               & (envelope[1:-1] >= threshold))
    peaks = np.flatnonzero(is_peak) + 1
    if peaks.size == 0:
        return np.zeros(0, dtype=np.int64)

    min_gap = max(1, int(_MIN_ONSET_GAP_SECONDS * sample_rate / HOP_SIZE))
    kept: list[int] = []
    for peak in peaks:
        if kept and peak - kept[-1] < min_gap:
            if envelope[peak] > envelope[kept[-1]]:
                kept[-1] = int(peak)
            continue
        kept.append(int(peak))

    return np.array(kept, dtype=np.int64)


def _resolve_octave(onset_hops: np.ndarray, period_hops: float, sample_rate: int) -> float:
    """Autocorrelation alone is prone to octave errors - locking onto half or double the true
    beat, since a real period's second harmonic is also periodic. The actual onsets settle it:
    spacing regularity peaks at the period that matches where they actually fall, not at a
    harmonic of it. The switch requires a decisive margin, not just "higher" - trying three
    candidates against noisy onsets otherwise makes it *more* likely one clears the confidence
    threshold by chance, which is exactly the false-positive this detector exists to avoid."""
    min_period = sample_rate * 60.0 / MAX_BPM / HOP_SIZE
    max_period = sample_rate * 60.0 / MIN_BPM / HOP_SIZE

    best_seed = period_hops
    best_confidence = _cross_validated_confidence(onset_hops, period_hops)

    for factor in (0.5, 2.0):
        candidate = period_hops * factor
        if not (min_period <= candidate <= max_period):
            continue
        confidence = _cross_validated_confidence(onset_hops, candidate)
        if (confidence > best_confidence + _OCTAVE_SWITCH_MARGIN
                and confidence >= _OCTAVE_SWITCH_MIN_CONFIDENCE):
            best_seed, best_confidence = candidate, confidence

    return _refine_period(onset_hops, best_seed)


def _refine_period(onset_hops: np.ndarray, initial_period: float) -> float:
    """Least-squares fit through the onsets rather than the raw autocorrelation peak - the
    library-style rounded estimate is off by enough per beat to be audible drift across a
    multi-beat transition, and the fit brings that down by an order of magnitude."""
    if onset_hops.size < 2 or initial_period <= 0:
        return initial_period

    offsets = (onset_hops - onset_hops[0]).astype(np.float64)
    multiples = np.round(offsets / initial_period)
    valid = multiples > 0
    if not np.any(valid):
        return initial_period

    m, o = multiples[valid], offsets[valid]
    period = float(np.sum(m * o) / np.sum(m * m))
    return period if period > 0 else initial_period


def _cross_validated_confidence(onset_hops: np.ndarray, seed_period: float) -> float:
    """Fitting a free-valued period directly to the onsets it's then scored against lets random
    onsets pick a period that happens to fit them, which is curve-fitting, not detection - a
    handful of white-noise onsets can reach a deceptively high self-fit score this way. Fitting the
    period on one half of the onsets and scoring it against the other half denies noise that
    escape hatch, since a period tuned to fit one arbitrary half has no reason to also fit the
    other unless the spacing is actually regular."""
    if onset_hops.size < 8:
        return _spacing_confidence(onset_hops, _refine_period(onset_hops, seed_period))

    mid = onset_hops.size // 2
    first, second = onset_hops[:mid], onset_hops[mid:]

    period_from_first = _refine_period(first, seed_period)
    score_on_second = _spacing_confidence(second, period_from_first)

    period_from_second = _refine_period(second, seed_period)
    score_on_first = _spacing_confidence(first, period_from_second)

    return (score_on_first + score_on_second) / 2.0


def _spacing_confidence(onset_hops: np.ndarray, period: float) -> float:
    if onset_hops.size < 2 or period <= 0:
        return 0.0

    gaps = np.diff(onset_hops).astype(np.float64)
    nearest_multiple = np.maximum(np.round(gaps / period), 1)
    expected = nearest_multiple * period
    error = np.abs(gaps - expected) / expected
    return float(np.clip(1.0 - error, 0.0, 1.0).mean())


def _find_mix_in(onset_hops: np.ndarray, period: float) -> int | None:
    if onset_hops.size < MIX_IN_WINDOW_BEATS or period <= 0:
        return None

    gaps = np.diff(onset_hops).astype(np.float64)
    well_spaced = np.abs(gaps - period) <= _GRID_TOLERANCE * period

    needed = MIX_IN_WINDOW_BEATS - 1
    run = 0
    for i, ok in enumerate(well_spaced):
        run = run + 1 if ok else 0
        if run >= needed:
            return i - (needed - 1)

    return None
