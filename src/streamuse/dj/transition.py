"""The shape of the blend, as a function of how many beats into the transition we are - ported from
the reference (.NET) DjTransition/Crossfader/Biquad.

A volume crossfade is not how a mix is done: run two tracks at half volume and you get both
basslines and both kicks at once, which is mud, and the moment the fader is mid-travel everything
sounds thin. What a DJ actually does is bring the new track in with its bass killed - so the
incoming drums and melody sit on top of the outgoing low end without fighting it - then swap the
bass across on a downbeat, which is the moment the mix "lands", and only then let the old track go.
Both records stay at full level throughout; the low end is what changes hands.

The envelope is symmetric: the same function fades a track IN over its own opening beats and fades
it back OUT over its own closing beats (driven by beats_left, mirroring beats_in) - a track left
alone with nothing queued therefore mixes itself back out to live near its own natural end with no
separate "closing transition" object needed. Measured in beats rather than seconds, since the bass
swap has to land on a beat to sound deliberate.
"""

import math

import numpy as np

BASS_CUT_HZ = 220.0
BASS_SMOOTHING_MS = 8.0

CALM_TEMPO_BPM = 95.0
HARD_TEMPO_BPM = 140.0
TRANSITION_BEATS = 16.0
SWAP_BEAT = TRANSITION_BEATS / 2
HARD_TRANSITION_BEATS = 8.0

#: The fader-up at the start. One beat, not instant: a hard start clicks.
LEAD_IN_BEATS = 1.0

SOFT_CLIP_KNEE = 0.7


def transition_shape(bpm: float) -> tuple[float, float]:
    """How many beats a beatmatched handover takes, and where in it the bass swaps, for a track at
    this tempo. The swap point stays exactly halfway regardless of length, so a fast cut is a
    scaled-down version of the same shape, not a different one."""
    if bpm <= CALM_TEMPO_BPM:
        beats = TRANSITION_BEATS
    elif bpm >= HARD_TEMPO_BPM:
        beats = HARD_TRANSITION_BEATS
    else:
        t = (bpm - CALM_TEMPO_BPM) / (HARD_TEMPO_BPM - CALM_TEMPO_BPM)
        beats = TRANSITION_BEATS - t * (TRANSITION_BEATS - HARD_TRANSITION_BEATS)
    return beats, beats / 2


def tempos_agree(bpm: float, target: float, tolerance: float) -> bool:
    if bpm <= 0 or target <= 0:
        return False
    return any(abs(bpm * multiple - target) / target <= tolerance for multiple in (0.5, 1.0, 2.0))


def equal_power_gains(t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """At t=0.5 both signals are attenuated by ~3dB rather than 6dB, so perceived loudness stays
    roughly constant through the fade. The endpoints are special-cased rather than left to cos/sin,
    which can never land on an exact zero (cos(pi/2) is ~6e-17, not 0) - a residue that rounds away
    in any display but that a `live_gain <= 0` pause check would never see as true."""
    clipped = np.clip(t, 0.0, 1.0)
    angle = clipped * (math.pi / 2)
    live = np.where(clipped >= 1.0, 0.0, np.where(clipped <= 0.0, 1.0, np.cos(angle)))
    track = np.where(clipped <= 0.0, 0.0, np.where(clipped >= 1.0, 1.0, np.sin(angle)))
    return live, track


def envelope(beats_in: np.ndarray, beats_left: np.ndarray, beatmatched: bool,
            transition_beats: float, swap_beat: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (live_gain, live_bass, track_gain, track_bass) arrays, one value per sample. "live"
    here means whatever is departing - the captured source or an outgoing deck - and "track" means
    whatever is arriving; the caller decides which physical signal plays which role."""
    if not beatmatched:
        return _fade(beats_in, beats_left, transition_beats)

    n = beats_in.shape[0]
    live_gain = np.empty(n)
    live_bass = np.empty(n)
    track_gain = np.empty(n)
    track_bass = np.empty(n)

    # Coming to the end of the track takes precedence: hand everything back to whoever's next.
    outro = beats_left <= transition_beats
    full = (~outro) & (beats_in >= transition_beats)
    mid = (~outro) & (~full)

    if np.any(outro):
        bl = beats_left[outro]
        returning_zone = bl > swap_beat
        returning = np.clip((transition_beats - bl) / (transition_beats - swap_beat), 0.0, 1.0)
        tail_gain = np.clip(bl / swap_beat, 0.0, 1.0)

        live_gain[outro] = np.where(returning_zone, returning, 1.0)
        live_bass[outro] = np.where(returning_zone, 0.0, 1.0)
        track_gain[outro] = np.where(returning_zone, 1.0, tail_gain)
        track_bass[outro] = np.where(returning_zone, 1.0, 0.0)

    if np.any(full):
        live_gain[full] = 0.0
        live_bass[full] = 0.0
        track_gain[full] = 1.0
        track_bass[full] = 1.0

    if np.any(mid):
        bi = beats_in[mid]
        before_swap = bi < swap_beat
        track_g = np.clip(bi / LEAD_IN_BEATS, 0.0, 1.0)
        handover = np.clip((bi - swap_beat) / (transition_beats - swap_beat), 0.0, 1.0)

        # Before the swap the incoming track is high-passed: it rides on top of the live low end.
        # After it, the bass belongs to the incoming track and the departing side walks out.
        live_gain[mid] = np.where(before_swap, 1.0, 1.0 - handover)
        live_bass[mid] = np.where(before_swap, 1.0, 0.0)
        track_gain[mid] = np.where(before_swap, track_g, 1.0)
        track_bass[mid] = np.where(before_swap, 0.0, 1.0)

    return live_gain, live_bass, track_gain, track_bass


def _fade(beats_in: np.ndarray, beats_left: np.ndarray, transition_beats: float):
    """No beat grid to lock to, so there is nothing to swap on. Equal-power both ways, full range on
    both sides - an honest fade rather than a beatmatch that would land wrong."""
    t = np.minimum(beats_in / transition_beats, beats_left / transition_beats)
    live_gain, track_gain = equal_power_gains(t)
    return live_gain, np.ones_like(t), track_gain, np.ones_like(t)


def soft_clip(block: np.ndarray) -> np.ndarray:
    """tanh soft-knee above 0.7 magnitude - two full-level decks plus a real download (routinely
    peaking a couple dB over 0 dBFS already) will otherwise clip on the sum."""
    magnitude = np.abs(block)
    over = magnitude > SOFT_CLIP_KNEE
    if not np.any(over):
        return block

    shaped = block.copy()
    span = 1.0 - SOFT_CLIP_KNEE
    excess = magnitude[over] - SOFT_CLIP_KNEE
    shaped[over] = np.sign(block[over]) * (SOFT_CLIP_KNEE + span * np.tanh(excess / span))
    return shaped


def smooth_toward(start: float, target: np.ndarray, coefficient: float) -> tuple[np.ndarray, float]:
    """One-pole smoothing of a running value toward a per-sample target - used for the bass-gain
    handover, whose *target* steps instantly at the swap beat while the filter mix itself is meant
    to move over a few milliseconds so it doesn't click. A true per-sample recurrence, so it can't be
    vectorized in closed form when the target varies arbitrarily; the loop only ever runs over one
    pacer tick's worth of samples (~880 at 44.1kHz)."""
    n = target.shape[0]
    out = np.empty(n)
    value = start
    for i in range(n):
        value += (target[i] - value) * coefficient
        out[i] = value
    return out, value


class Biquad:
    """RBJ high-pass biquad, one filter state per channel; coefficients are fixed at construction
    since the cutoff never changes for the addon's life."""

    def __init__(self, cutoff_hz: float, sample_rate: int, q: float = 0.707) -> None:
        omega = 2 * math.pi * cutoff_hz / sample_rate
        alpha = math.sin(omega) / (2 * q)
        cos_omega = math.cos(omega)

        b0 = (1 + cos_omega) / 2
        b1 = -(1 + cos_omega)
        b2 = (1 + cos_omega) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_omega
        a2 = 1 - alpha

        self.b0, self.b1, self.b2 = b0 / a0, b1 / a0, b2 / a0
        self.a1, self.a2 = a1 / a0, a2 / a0

        self._x1 = np.zeros(2, dtype=np.float64)
        self._x2 = np.zeros(2, dtype=np.float64)
        self._y1 = np.zeros(2, dtype=np.float64)
        self._y2 = np.zeros(2, dtype=np.float64)

    def process(self, block: np.ndarray) -> np.ndarray:
        """block is (n, 2) float32/float64. Direct Form I, sample by sample: filter state has to
        advance every sample regardless of gain, or the low band jumps when a side comes back in, so
        this runs for as long as a deck is playing, not just during a transition."""
        out = np.empty(block.shape, dtype=np.float64)
        x1, x2, y1, y2 = self._x1, self._x2, self._y1, self._y2
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2

        for i in range(block.shape[0]):
            x0 = block[i].astype(np.float64)
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            out[i] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0

        self._x1, self._x2, self._y1, self._y2 = x1, x2, y1, y2
        return out
