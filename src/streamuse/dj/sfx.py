"""A folder the user drops audio files into; picks one at random each time DjMixer wants a sound
effect. Deliberately just a folder, not a manifest or a naming convention - the whole point is "drop
files in, it works," with nothing to configure per file. The chance/cooldown decision of *whether* to
trigger one lives in DjMixer, not here - this only picks and decodes."""

import asyncio
import random
import subprocess
from pathlib import Path

import numpy as np

from .. import jobs, paths

CREATE_NO_WINDOW = 0x08000000
DECODE_TIMEOUT = 10
SAMPLE_RATE = 44100

FADE_SECONDS = 0.005

SFX_DIR = paths.DATA_DIR / "dj-sfx"
_EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma")


class SfxLibrary:
    def __init__(self, deps) -> None:
        self._deps = deps
        self._last_picked: Path | None = None

    async def pick(self) -> np.ndarray | None:
        """Picks a file, decodes it, and returns it - or None if the folder is empty, ffmpeg isn't
        ready, or the pick failed to decode (a corrupt file left in the folder costs nothing beyond
        that one trigger being silently skipped)."""
        if self._deps.ffmpeg is None:
            return None

        path = self._choose()
        if path is None:
            return None

        try:
            return await self._decode(path)
        except Exception:
            return None

    def _choose(self) -> Path | None:
        if not SFX_DIR.is_dir():
            return None
        files = [p for p in SFX_DIR.iterdir() if p.suffix.lower() in _EXTENSIONS]
        if not files:
            return None

        # Avoid the same clip twice in a row when there is a choice - otherwise a two-file folder
        # reads as broken repetition rather than variety.
        candidates = [f for f in files if f != self._last_picked] if len(files) > 1 else files
        picked = random.choice(candidates)
        self._last_picked = picked
        return picked

    async def _decode(self, path: Path) -> np.ndarray | None:
        process = await asyncio.create_subprocess_exec(
            self._deps.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        jobs.adopt(process)

        try:
            raw, _ = await asyncio.wait_for(process.communicate(), DECODE_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            return None

        if not raw:
            return None

        floats = np.frombuffer(raw, dtype="<f4")
        usable = floats.size - floats.size % 2
        stereo = floats[:usable].reshape(-1, 2).copy()
        return _fade(stereo, int(FADE_SECONDS * SAMPLE_RATE))


def _fade(stereo: np.ndarray, fade_frames: int) -> np.ndarray:
    fade_frames = min(fade_frames, stereo.shape[0] // 2)
    if fade_frames <= 0:
        return stereo

    ramp_in = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)[:, None]
    stereo[:fade_frames] *= ramp_in
    stereo[-fade_frames:] *= ramp_in[::-1]
    return stereo
