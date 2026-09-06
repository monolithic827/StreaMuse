"""The learned track library Rave DJ mode picks its own next track from - built from a harvested or
read playlist (see harvester.py), analyzed in the background, and never touched by Radio DJ mode.

Only ever driven from the asyncio loop (harvesting and analysis are both background tasks created on
it), so nothing here needs a lock."""

import asyncio
import json
from dataclasses import dataclass

import numpy as np

from . import beatgrid, key as key_module, pcm
from .fetch import YtDlpFetcher

CLIP_SECONDS = 30

#: A candidate must clear this to be considered pickable at all - the same floor request() already
#: gates a beatmatch on, so an autonomous pick is never trusted further than a listener's own would be.
CONFIDENCE_FLOOR = beatgrid.CONFIDENCE_THRESHOLD

#: How often analysis progress is written back. Learning a playlist takes long enough that closing
#: the app partway through it is a normal thing to do, and the harvest that produced the candidates
#: was real playback nobody wants to sit through twice.
CHECKPOINT_EVERY = 5

_PERSISTED = ("video_id", "title", "artist", "bpm", "confidence", "camelot", "energy", "analyzed")


@dataclass
class LibraryEntry:
    video_id: str
    title: str = ""
    artist: str = ""
    bpm: float = 0.0
    confidence: float = 0.0
    camelot: str = ""
    energy: float = 0.0
    analyzed: bool = False
    #: Deliberately not persisted: a track that failed to fetch or play this run is skipped for the
    #: rest of it, but a run later is a fair chance to try again.
    unplayable: bool = False


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
                entry = LibraryEntry(**{k: v for k, v in item.items() if k in _PERSISTED})
                self._entries[entry.video_id] = entry
            except TypeError:
                continue

    def _save(self) -> None:
        """Writes unanalyzed candidates too, so an interrupted learning pass resumes where it
        stopped rather than starting from the harvest again."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{field: getattr(e, field) for field in _PERSISTED}
                   for e in self._entries.values()]
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- building the library ---------------------------------------------------

    def add_candidates(self, candidates: list[tuple[str, str, str]]) -> int:
        """candidates: (video_id, title, artist) triples. Returns how many were actually new -
        already-known video_ids are left alone rather than re-queued for analysis."""
        added = 0
        for video_id, title, artist in candidates:
            if video_id in self._entries:
                continue
            self._entries[video_id] = LibraryEntry(video_id, title, artist)
            added += 1
        if added:
            self._save()
        return added

    async def analyze_pending(self) -> None:
        """Analyzes every candidate that hasn't been analyzed yet, bounded to
        settings.djLibraryConcurrency concurrent fetch+decode+analyze tasks - not unbounded, since
        that many simultaneous yt-dlp downloads reads as automated abuse to YouTube's own side, and
        would starve the live mix() path for CPU besides."""
        pending = [e for e in self._entries.values() if not e.analyzed and not e.unplayable]
        if not pending:
            return

        semaphore = asyncio.Semaphore(max(1, self._settings.djLibraryConcurrency))
        total = len(pending)
        done = 0

        async def worker(entry: LibraryEntry) -> None:
            nonlocal done
            async with semaphore:
                await self._analyze_one(entry)
            done += 1
            if done % CHECKPOINT_EVERY == 0 or done == total:
                self._save()
                self._hub.info(f"dj library: learned {done}/{total}")

        await asyncio.gather(*(worker(entry) for entry in pending))
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
            entry.unplayable = True
            self._hub.warn(f"dj library: could not learn \"{entry.title}\": {exc}")

    # ---- picking -----------------------------------------------------------

    def mark_unplayable(self, video_id: str) -> None:
        """Takes a track out of the running for the rest of the run. Without this an entry that
        always fails to fetch, or that audition always rejects, is picked again the moment its own
        failure asks for a replacement - the same track, forever, since pick_next is deterministic
        and nothing else about the inputs has changed."""
        entry = self._entries.get(video_id)
        if entry is not None:
            entry.unplayable = True

    def pick_next(self, after_camelot: str, after_bpm: float, recent, target_energy: float
                 ) -> LibraryEntry | None:
        """The best next track relative to whatever just finished (after_bpm <= 0 means starting
        cold, nothing to match against): prefers a harmonically compatible key and a close tempo,
        ranked by distance from target_energy so a set can build or hold rather than picking at
        random. Never repeats one of the caller's recent picks."""
        candidates = [e for e in self._entries.values()
                     if e.analyzed and not e.unplayable and e.confidence >= CONFIDENCE_FLOOR
                     and e.video_id not in recent]
        if not candidates:
            return None

        def score(entry: LibraryEntry) -> float:
            # Starting cold there is no tempo to match; camelot_distance already answers the same
            # for every candidate when after_camelot is empty, so only energy separates them.
            tempo_distance = 0.0
            if after_bpm > 0:
                ratio = entry.bpm / after_bpm
                tempo_distance = min(abs(ratio - 1.0), abs(ratio - 0.5), abs(ratio - 2.0))

            return (key_module.camelot_distance(after_camelot, entry.camelot) * 2.0
                   + tempo_distance * 4.0 + abs(entry.energy - target_energy))

        return min(candidates, key=score)


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
