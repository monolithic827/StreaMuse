"""Musical key, expressed as a Camelot wheel position - what actually determines whether two tracks
mix cleanly (adjacent numbers, or the same number's other letter, are compatible; nothing else is).

Same DSP tier as beatgrid.py: plain numpy, no new dependency. A chroma (pitch-class) profile is
correlated against the standard Krumhansl-Schmuckler major/minor templates - the same "well
established, verify against real material before trusting it" approach beatgrid.py itself was built
and calibrated with, since key detection genuinely can be wrong on ambiguous or atonal material.
"""

import numpy as np

FFT_SIZE = 4096
HOP_SIZE = 2048
MIN_FREQUENCY_HZ = 55.0

#: Krumhansl-Schmuckler tonal hierarchy - how strongly each scale degree is felt relative to a major
#: or minor tonic, in semitone order starting at the tonic. Standard published values, not derived
#: here.
_MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

#: Camelot number for each pitch class, major and minor separately - relative major/minor pairs
#: share a number by construction (e.g. C major and A minor are both "8"), which is the entire
#: point of the notation: adjacent numbers, or the same number's other letter, mix cleanly.
_MAJOR_CAMELOT = {0: "8B", 7: "9B", 2: "10B", 9: "11B", 4: "12B", 11: "1B",
                 6: "2B", 1: "3B", 8: "4B", 3: "5B", 10: "6B", 5: "7B"}
_MINOR_CAMELOT = {9: "8A", 4: "9A", 11: "10A", 6: "11A", 1: "12A", 8: "1A",
                 3: "2A", 10: "3A", 5: "4A", 0: "5A", 7: "6A", 2: "7A"}


def camelot_distance(a: str, b: str) -> int:
    """0 = same key, 1 = adjacent number or same number/other letter (compatible), higher = clashes
    more. Used by library.pick_next() to prefer a harmonically compatible next track."""
    if not a or not b:
        return 99
    number_a, letter_a = int(a[:-1]), a[-1]
    number_b, letter_b = int(b[:-1]), b[-1]

    if number_a == number_b and letter_a == letter_b:
        return 0
    if letter_a == letter_b:
        ring_distance = min((number_a - number_b) % 12, (number_b - number_a) % 12)
        return ring_distance
    if number_a == number_b:
        return 1
    return 6


def detect(mono: np.ndarray, sample_rate: int) -> str:
    """mono is float32 in [-1, 1]. Returns a Camelot position, or "" if the clip is too short to
    say anything."""
    n_frames = 1 + (mono.size - FFT_SIZE) // HOP_SIZE
    if n_frames < 2:
        return ""

    window = np.hanning(FFT_SIZE).astype(np.float32)
    starts = HOP_SIZE * np.arange(n_frames)[:, None]
    indices = starts + np.arange(FFT_SIZE)[None, :]
    frames = mono[indices] * window
    magnitudes = np.abs(np.fft.rfft(frames, axis=1)).sum(axis=0)

    freqs = np.fft.rfftfreq(FFT_SIZE, d=1.0 / sample_rate)
    # Below MIN_FREQUENCY_HZ a bin's pitch class is dominated by sub-bass energy that carries no
    # real harmonic information at this FFT resolution - excluding it keeps a heavy kick drum from
    # skewing the whole profile toward one pitch class.
    audible = freqs >= MIN_FREQUENCY_HZ
    with np.errstate(divide="ignore"):
        midi = 69 + 12 * np.log2(freqs[audible] / 440.0)
    pitch_classes = np.mod(np.round(midi), 12).astype(np.int64)

    chroma = np.zeros(12, dtype=np.float64)
    np.add.at(chroma, pitch_classes, magnitudes[audible])
    if chroma.sum() <= 0:
        return ""
    chroma /= chroma.sum()

    best_score = -np.inf
    best_root = 0
    best_major = True

    for root in range(12):
        rotated = np.roll(chroma, -root)
        major_score = _correlate(rotated, _MAJOR_PROFILE)
        minor_score = _correlate(rotated, _MINOR_PROFILE)

        if major_score > best_score:
            best_score, best_root, best_major = major_score, root, True
        if minor_score > best_score:
            best_score, best_root, best_major = minor_score, root, False

    table = _MAJOR_CAMELOT if best_major else _MINOR_CAMELOT
    return table[best_root]


def _correlate(chroma: np.ndarray, profile: np.ndarray) -> float:
    a, b = chroma - chroma.mean(), profile - profile.mean()
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else -np.inf
