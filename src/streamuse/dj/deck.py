"""A track that is currently making sound, with its own playhead and bass filter."""

import numpy as np

from .transition import BASS_CUT_HZ, Biquad


class Deck:
    def __init__(self, entry, pcm: np.ndarray, sample_rate: int, cursor: int) -> None:
        self.entry = entry
        self.pcm = pcm
        self.cursor = cursor
        self.highpass = Biquad(BASS_CUT_HZ, sample_rate)
        self.bass_smoothed = 0.0

    @property
    def finished(self) -> bool:
        return self.cursor >= self.pcm.shape[0]
