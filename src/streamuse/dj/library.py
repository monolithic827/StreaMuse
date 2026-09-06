"""The learned track library Rave DJ mode picks its own next track from - built from a harvested or
read playlist (see harvester.py), analyzed in the background, and never touched by Radio DJ mode.

Only ever driven from the asyncio loop (harvesting and analysis are both background tasks created on
it), unlike DjMixer itself - nothing here is called from a receiver thread, so no threading.Lock is
needed the way DjMixer's own state requires one for observe_live()."""

import asyncio
import json

import numpy as np

from . import beatgrid, key as key_module, pcm
from .fetch import YtDlpFetcher

CLIP_SECONDS = 30

#: A candidate must clear this to be considered pickable at all - the same floor request() already
#: gates a beatmatch on, so an autonomous pick is never trusted further than a listener's own would be.
CONFIDENCE_FLOOR = beatgrid.CONFIDENCE_THRESHOLD


class LibraryEntry:
    def __init__(self, video_id: str, title: str, artist: str, source: str) -> None:
        self.video_id = video_id
        self.title = title
        self.artist = artist
        self.source = source
        self.bpm = 0.0
        self.confidence = 0.0
        self.camelot = ""
        self.energy = 0.0
        self.analyzed = False
        self.failed = False

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id, "title": self.title, "artist": self.artist,
            "source": self.source, "bpm": self.bpm, "confidence": self.confidence,
            "camelot": self.camelot, "energy": self.energy, "analyzed": self.analyzed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LibraryEntry":
        entry = cls(data["video_id"], data.get("title", ""), data.get("artist", ""),
                    data.get("source", "harvested"))
        entry.bpm = data.get("bpm", 0.0)
        entry.confidence = data.get("confidence", 0.0)
        entry.camelot = data.get("camelot", "")
        entry.energy = data.get("energy", 0.0)
        entry.analyzed = data.get("analyzed", False)
        return entry


class TrackLibrary:
    def __init__(self, settings, hub, deps, path) -> None:
        self._settings = settings
        self._hub = hub
        self._fetcher = YtDlpFetcher(deps, hub)
        self._path = path

        self._entries: dict[str, LibraryEntry] = {}
        self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw:
            try:
                entry = LibraryEntry.from_dict(item)
                self._entries[entry.video_id] = entry
            except (KeyError, TypeError):
                continue

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        payload = [e.to_dict() for e in self._entries.values() if e.analyzed]
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- building the library ---------------------------------------------------

    def add_candidates(self, candidates: list[tuple[str, str, str]], source: str) -> int:
        """candidates: (video_id, title, artist) triples. Returns how many were actually new -
        already-known video_ids are left alone rather than re-queued for analysis."""
        added = 0
        for video_id, title, artist in candidates:
            if video_id in self._entries:
                continue
            self._entries[video_id] = LibraryEntry(video_id, title, artist, source)
            added += 1
        return added

    async def analyze_pending(self) -> None:
        """Analyzes every candidate that hasn't been analyzed yet, bounded to
        settings.djLibraryConcurrency concurrent fetch+decode+analyze tasks - not unbounded, since
        that many simultaneous yt-dlp downloads reads as automated abuse to YouTube's own side, and
        would starve the live mix() path for CPU besides."""
        pending = [e for e in self._entries.values() if not e.analyzed and not e.failed]
        if not pending:
            return

        limit = max(1, self._settings.djLibraryConcurrency)
        semaphore = asyncio.Semaphore(limit)
        total = len(pending)
        done = 0

        async def worker(entry: LibraryEntry) -> None:
            nonlocal done
            async with semaphore:
                await self._analyze_one(entry)
            done += 1
            if done % 5 == 0 or done == total:
                self._hub.info(f"dj library: learned {done}/{total}")

        await asyncio.gather(*(worker(entry) for entry in pending))
        self._save()
        self._hub.info(f"dj library: {sum(1 for e in self._entries.values() if e.analyzed)} "
                       "tracks ready to pick from")

    async def _analyze_one(self, entry: LibraryEntry) -> None:
        try:
            stereo = await self._fetcher.fetch_clip(entry.video_id, CLIP_SECONDS)
            mono = pcm.to_mono(stereo)

            grid = beatgrid.analyze(mono, beatgrid.SAMPLE_RATE)
            entry.bpm = grid.bpm if grid else 0.0
            entry.confidence = grid.confidence if grid else 0.0
            entry.camelot = key_module.detect(mono, beatgrid.SAMPLE_RATE)
            entry.energy = _energy_score(stereo)
            entry.analyzed = True
        except Exception as exc:
            entry.failed = True
            self._hub.warn(f"dj library: could not learn \"{entry.title}\": {exc}")

    # ---- picking -----------------------------------------------------------

    def pick_next(self, after_camelot: str, after_bpm: float, recent, target_energy: float
                 ) -> LibraryEntry | None:
        """The best next track relative to whatever just finished (after_bpm <= 0 means starting
        cold, nothing to match against): prefers a harmonically compatible key and a close tempo,
        ranked by distance from target_energy so a set can build or hold rather than picking at
        random. Never repeats one of the caller's recent picks."""
        candidates = [e for e in self._entries.values()
                     if e.analyzed and e.confidence >= CONFIDENCE_FLOOR
                     and e.video_id not in recent]
        if not candidates:
            return None

        if after_bpm <= 0:
            return min(candidates, key=lambda e: abs(e.energy - target_energy))

        def score(entry: LibraryEntry) -> float:
            key_distance = key_module.camelot_distance(after_camelot, entry.camelot)
            tempo_ratio = entry.bpm / after_bpm
            tempo_distance = min(abs(tempo_ratio - 1.0), abs(tempo_ratio - 0.5), abs(tempo_ratio - 2.0))
            energy_distance = abs(entry.energy - target_energy)
            return key_distance * 2.0 + tempo_distance * 4.0 + energy_distance

        return min(candidates, key=score)

    def get(self, video_id: str) -> LibraryEntry | None:
        return self._entries.get(video_id)

    @property
    def analyzed_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.analyzed)

    @property
    def total_count(self) -> int:
        return len(self._entries)


def _energy_score(stereo: np.ndarray) -> float:
    """A rough 0-1 loudness/brightness blend - not claiming to model perceived energy precisely, just
    enough to tell a driving track from a mellow one for set-pacing purposes."""
    mono = pcm.to_mono(stereo)
    if mono.size == 0:
        return 0.0

    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.size)))
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / beatgrid.SAMPLE_RATE)
    centroid = float(np.sum(freqs * spectrum) / np.sum(spectrum)) if spectrum.sum() > 0 else 0.0

    loudness = min(1.0, rms * 4.0)
    brightness = min(1.0, centroid / 4000.0)
    return 0.6 * loudness + 0.4 * brightness
