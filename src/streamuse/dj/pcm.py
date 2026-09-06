"""s16le <-> float32 conversion shared by fetch, audition, the deck and the mixer."""

import numpy as np

CHANNELS = 2


def s16le_to_float32(data: bytes) -> np.ndarray:
    """Interleaved stereo bytes -> shape (frames, 2) float32 in [-1, 1]."""
    ints = np.frombuffer(data, dtype="<i2")
    usable = ints.size - ints.size % CHANNELS
    return (ints[:usable].astype(np.float32) / 32768.0).reshape(-1, CHANNELS)


def float32_to_s16le(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    ints = (clipped * 32767.0).astype("<i2")
    return ints.tobytes()


def to_mono(stereo: np.ndarray) -> np.ndarray:
    return stereo.mean(axis=1)
