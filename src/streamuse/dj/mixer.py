"""The decks. Holds a queue of requests, up to two playing tracks while one mixes into the next, and
a running estimate of the live source's beat grid; drops each track in on a beat and trades the bass
across rather than fading. See transition.py for the shape of the blend.

mix() runs on AudioPacer's wall-clock tick and must never block - everything else (fetching,
decoding, beat analysis) happens ahead of time on background tasks.
"""

import asyncio
import collections
import math
import random
import threading

import numpy as np

from .. import state as state_module
from ..artwork import ArtworkStore
from . import beatgrid, harvester, key, pcm, transition
from .audition import audition
from .deck import Deck
from .fetch import SearchResult, YtDlpFetcher
from .sfx import SfxLibrary

FETCHING, AUDITIONING, READY, MIXING, MIXING_OUT, DONE, FAILED, REJECTED = (
    "fetching", "auditioning", "ready", "mixing", "mixing out", "done", "failed", "rejected",
)

_TERMINAL = (FAILED, REJECTED, DONE)

SAMPLE_RATE = 44100

ROLLING_WINDOW_SECONDS = 8
ROLLING_REFRESH_SECONDS = 2

#: How much of a track's opening is read to establish its grid.
GRID_WINDOW_SECONDS = 30

#: How far two tempos may differ and still be blended without retiming. Tracks play at their
#: recorded speed, so this is the whole budget.
TEMPO_TOLERANCE = 0.03

#: Beats the start is pushed forward by, so alignment is computed against a grid that is still valid
#: once we get there rather than one we have already passed.
START_LEAD_BEATS = 2.0

#: Chance a given transition gets a sound effect, once the cooldown below allows a roll at all.
SFX_CHANCE = 0.25
SFX_COOLDOWN_TRANSITIONS = 2
SFX_LATE_TOLERANCE_BEATS = 1.0
SFX_FADE_SECONDS = 0.005

#: Rave mode's energy target ramps from START to PEAK over this many autonomous picks, then holds -
#: a set that opens at full intensity and never breathes reads as random, not as a set.
ENERGY_RAMP_TRACKS = 6
ENERGY_START = 0.35
ENERGY_PEAK = 0.85

#: How many of its own recent picks Rave mode won't repeat.
RAVE_RECENT_HISTORY = 8


class PendingTrack:
    def __init__(self, entry_id: str, query: str, autonomous: bool = False) -> None:
        self.id = entry_id
        self.query = query
        self.title = query
        self.artist = ""
        self.album = ""
        self.status = FETCHING
        self.pcm: np.ndarray | None = None
        self.artwork: bytes | None = None
        self.mix_in_offset = 0
        self.source_bpm = 0.0
        self.source_confidence = 0.0
        #: A Rave-mode pick from the library rather than a real request - _start_next prefers any
        #: non-autonomous READY entry ahead of these, so a real request always jumps the queue.
        self.autonomous = autonomous
        self.camelot = ""
        self.video_id: str | None = None

    def to_entry(self) -> state_module.DjQueueEntry:
        return state_module.DjQueueEntry(self.id, self.query, self.title, self.artist, self.status)


class SfxVoice:
    def __init__(self, sfx_pcm: np.ndarray, start_frame: int) -> None:
        self.pcm = sfx_pcm
        self.start_frame = start_frame


class DjMixer:
    def __init__(self, settings, hub, deps, sources, library, sample_rate: int = SAMPLE_RATE) -> None:
        self._settings = settings
        self._hub = hub
        self._deps = deps
        self._sources = sources
        self._library = library
        self._sample_rate = sample_rate

        self._fetcher = YtDlpFetcher(deps, hub)
        self._sfx = SfxLibrary(deps)

        self._rave_recent: collections.deque = collections.deque(maxlen=RAVE_RECENT_HISTORY)
        self._rave_pick_count = 0
        self._last_finished_camelot = ""
        self._last_finished_bpm = 0.0

        self._lock = threading.Lock()
        self._queue: list[PendingTrack] = []

        self._incoming: Deck | None = None
        self._outgoing: Deck | None = None

        self._stream_frames = 0
        self._live_anchor_frame = 0.0
        self._live_period_frames = 0.0
        self._live_bpm = 0.0
        self._live_confidence = 0.0

        self._start_frame = 0.0
        self._transition_period = 0.0
        self._transition_beats = transition.TRANSITION_BEATS
        self._swap_beat = transition.SWAP_BEAT
        self._beatmatched = False

        self._live_highpass: transition.Biquad | None = None
        self._live_bass_smoothed = 1.0
        self._bass_smoothing_coefficient = 1 - math.exp(
            -1000.0 / (transition.BASS_SMOOTHING_MS * sample_rate))

        self._sfx_voice: SfxVoice | None = None
        self._sfx_cooldown = 0
        self._sfx_roll = random.Random()

        self._live_pcm: list[np.ndarray] = []
        self._live_pcm_frames = 0
        self._rolling_task: asyncio.Task | None = None

        self._source_paused = False

        self.artwork = ArtworkStore()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Starts the rolling live-grid analysis. Called once the app's event loop is running."""
        if self._rolling_task is None:
            self._rolling_task = asyncio.create_task(self._rolling_bpm_loop())

    # ---- requests -----------------------------------------------------------

    def request(self, query: str, video_id: str | None = None,
               autonomous: bool = False) -> tuple[bool, str, state_module.DjQueueEntry | None]:
        query = query.strip()
        if not query:
            return False, "empty request", None

        entry = PendingTrack(_new_id(), query, autonomous)
        entry.video_id = video_id
        with self._lock:
            self._queue.append(entry)
        self._publish()

        asyncio.create_task(self._fetch(entry, video_id))
        return True, "", entry.to_entry()

    async def search(self, query: str) -> list[SearchResult]:
        """Candidates for a query, for a request dropdown - picking one and calling request() with
        its video_id skips the search fetch() would otherwise redo, and fetches that exact track."""
        query = query.strip()
        return await self._fetcher.search(query) if query else []

    # ---- the Rave-mode library ----------------------------------------------------

    async def harvest_live(self) -> None:
        """Walks forward through whatever's already playing on the selected source and learns each
        track - see harvester.py for why this needs no playlist API. Best run before the stream goes
        live: it's real playback, which push_audio only drops while no stream session exists. Runs as
        a detached background task from the web handler, so it must log its own exceptions."""
        try:
            candidates = await harvester.harvest_live_source(self._sources, self._hub, self._fetcher)
            added = self._library.add_candidates(candidates, "harvested")
            self._hub.info(f"dj library: {added} new tracks queued for analysis")
            await self._library.analyze_pending()
        except Exception as exc:
            self._hub.error(f"dj library: learning from the live source failed: {exc}")

    async def harvest_playlist(self, url: str) -> None:
        """Same background-task exception discipline as harvest_live."""
        try:
            candidates = await harvester.harvest_youtube_playlist(self._deps, self._hub, url)
            added = self._library.add_candidates(candidates, "playlist")
            self._hub.info(f"dj library: {added} new tracks queued for analysis")
            await self._library.analyze_pending()
        except Exception as exc:
            self._hub.error(f"dj library: learning that playlist failed: {exc}")

    def skip(self) -> None:
        """Moves straight to whatever is next: if something is queued and ready it mixes in now,
        otherwise the playing track runs its closing transition back to the live source. Either way
        it is a mix, not a cut."""
        if self._start_next(handover=True):
            return

        with self._lock:
            deck = self._incoming
            if deck is None:
                return
            outro_samples = int(self._transition_period * self._transition_beats)
            deck.cursor = max(deck.cursor, deck.pcm.shape[0] - outro_samples)
            deck.entry.status = MIXING_OUT

        self._publish()
        self._evaluate_resume()

    # ---- fetch pipeline -------------------------------------------------------

    async def _fetch(self, entry: PendingTrack, video_id: str | None = None) -> None:
        try:
            fetched = await self._fetcher.fetch(entry.query, video_id)

            entry.title, entry.artist, entry.album = fetched.title, fetched.artist, fetched.album
            entry.artwork = fetched.artwork

            entry.status = AUDITIONING
            self._publish()

            result = audition(fetched.stereo, self._sample_rate)
            if not result.ok:
                entry.status = REJECTED
                self._hub.warn(f"dj: rejected \"{entry.title}\": {result.reason}")
                self._publish()
                self._evaluate_resume()
                return

            trimmed = fetched.stereo[result.start_frame:] if result.start_frame > 0 else fetched.stereo
            grid, camelot = self._read_grid(trimmed)

            entry.pcm = trimmed
            entry.mix_in_offset = grid.mix_in_frame if grid else 0
            entry.source_bpm = grid.bpm if grid else 0.0
            entry.source_confidence = grid.confidence if grid else 0.0
            entry.camelot = camelot

            skipped = result.start_frame / self._sample_rate
            mix_in_seconds = entry.mix_in_offset / self._sample_rate
            bpm_text = (f"{entry.source_bpm:.0f} BPM at {entry.source_confidence:.0%} confidence"
                       if entry.source_bpm > 0 else "no clear tempo")
            self._hub.info(
                f"dj: auditioned \"{entry.title}\": {result.duration_seconds:.0f}s, "
                f"peak {result.peak_db:.1f} dB"
                + (f", skipped {skipped:.1f}s of intro" if skipped > 0.1 else "")
                + f", {bpm_text}"
                + (f", cues in at {mix_in_seconds:.1f}s once the groove locks in"
                   if mix_in_seconds > 0.1 else ""))

            entry.status = READY
            self._publish()

            # Only starts if nothing is playing. A request made mid-track waits for its cue near the
            # end of that track rather than interrupting it - see the cue check in mix().
            self._start_next(handover=False)
        except Exception as exc:
            entry.status = FAILED
            self._hub.error(f"dj: request \"{entry.query}\" failed: {exc}")
            self._publish()
            self._evaluate_resume()

    def _read_grid(self, pcm_data: np.ndarray) -> tuple[beatgrid.BeatGrid | None, str]:
        """Reads the grid (and, for Rave mode's harmonic picking, the key) from the opening of the
        track rather than the whole file - averaged over a full track, beat-strength confidence is
        diluted by intros, breakdowns and outros, and the opening is also the part whose phase we
        actually enter on."""
        window = pcm_data[: int(GRID_WINDOW_SECONDS * self._sample_rate)]
        mono = pcm.to_mono(window)
        return beatgrid.analyze(mono, self._sample_rate), key.detect(mono, self._sample_rate)

    # ---- handover scheduling ---------------------------------------------------

    def _start_next(self, handover: bool) -> bool:
        """Starts the next ready track. With handover it mixes over whatever is playing - which is
        what the cue near the end of a track, and skip(), both want; without it, it only starts when
        the decks are empty, so a request made mid-track waits its turn."""
        with self._lock:
            # Only one handover at a time; a second would need a third deck and land off the grid.
            if self._outgoing is not None:
                return False
            if self._incoming is not None and not handover:
                return False

            ready = [e for e in self._queue if e.status == READY]
            # A real request always plays before an autonomous Rave-mode pick, even one queued
            # earlier - the pick is there to fill silence, not to make someone wait behind it.
            next_entry = next((e for e in ready if not e.autonomous), None) or next(iter(ready), None)
            if next_entry is None or next_entry.pcm is None or next_entry.pcm.shape[0] == 0:
                return False

            leaving = self._incoming

            # Beat-match against the outgoing track when there is one, since that is what the new
            # track has to sit on top of; against the live source otherwise. Both sides have to clear
            # the gate: matching to a tempo read at low confidence is guesswork, and lands worse than
            # an honest fade.
            leaving_readable = (leaving is not None and leaving.entry.source_bpm > 0
                                and leaving.entry.source_confidence >= beatgrid.CONFIDENCE_THRESHOLD)

            period = ((self._sample_rate * 60.0 / leaving.entry.source_bpm) if leaving_readable
                     else self._live_period_frames)

            readable = (next_entry.source_bpm > 0
                       and next_entry.source_confidence >= beatgrid.CONFIDENCE_THRESHOLD)
            target_bpm = leaving.entry.source_bpm if leaving_readable else self._live_bpm

            self._beatmatched = bool(
                period > 0 and readable
                and (leaving_readable
                     or (leaving is None and self._live_confidence >= beatgrid.CONFIDENCE_THRESHOLD))
                and transition.tempos_agree(next_entry.source_bpm, target_bpm, TEMPO_TOLERANCE))

            if self._beatmatched:
                self._transition_beats, self._swap_beat = transition.transition_shape(
                    next_entry.source_bpm)
                self._transition_period = period
                self._start_frame = (self._next_phrase_on_live_grid(period) if leaving is None
                                     else self._next_phrase_on_deck(leaving, period))

                message = (f"dropping \"{next_entry.title}\" on the beat at "
                          f"{next_entry.source_bpm:.0f} BPM" if leaving is None else
                          f"mixing \"{next_entry.title}\" in over the end of "
                          f"\"{leaving.entry.title}\" - both at {next_entry.source_bpm:.0f} BPM")
            else:
                self._transition_beats = transition.TRANSITION_BEATS
                self._swap_beat = transition.SWAP_BEAT

                seconds = max(1.0, self._settings.djCrossfadeSeconds)
                self._transition_period = seconds * self._sample_rate / self._transition_beats
                self._start_frame = float(self._stream_frames)

                target = leaving.entry.source_bpm if leaving_readable else self._live_bpm
                message = (
                    f"mixing in \"{next_entry.title}\" - {next_entry.source_bpm:.0f} against "
                    f"{target:.0f} BPM, fading rather than dragging it into time"
                    if readable and target > 0 else
                    f"mixing in \"{next_entry.title}\" - no clear beat to lock to, fading instead")

            # Enter on the track's own mix-in point rather than wherever it happens to start - a DJ
            # cues a record from where its groove has actually locked in, not frame zero.
            cursor = min(next_entry.mix_in_offset, next_entry.pcm.shape[0] - 1)

            if leaving is not None:
                leaving.entry.status = MIXING_OUT
            self._outgoing = leaving
            if self._live_highpass is None:
                self._live_highpass = transition.Biquad(transition.BASS_CUT_HZ, self._sample_rate)
            self._live_bass_smoothed = 1.0

            next_entry.status = MIXING
            self._incoming = Deck(next_entry, next_entry.pcm, self._sample_rate, cursor)
            self.artwork.set(next_entry.artwork)

        self._hub.info(f"dj: {message}")
        self._publish()
        self._try_trigger_sfx()
        return True

    def _next_phrase_on_live_grid(self, period: float) -> float:
        """The next 4-beat boundary of the live source's grid, a couple of beats out so the schedule
        is not already in the past by the time the pacer reaches it."""
        earliest = self._stream_frames + START_LEAD_BEATS * period
        beats = math.ceil((earliest - self._live_anchor_frame) / period / 4) * 4
        return self._live_anchor_frame + beats * period

    def _next_phrase_on_deck(self, deck: Deck, period: float) -> float:
        """The next 4-beat boundary of the *outgoing track's own* grid. Coming in on a phrase of the
        record that is leaving is what makes the blend land musically rather than merely on a beat."""
        beats_played = (deck.cursor - deck.entry.mix_in_offset) / period
        phrase = math.ceil((beats_played + START_LEAD_BEATS) / 4) * 4
        return self._stream_frames + max(0.0, (phrase - beats_played) * period)

    def _try_trigger_sfx(self) -> None:
        """Rolls the dice for a sound effect on the transition that just started. Picking and
        decoding run in the background - _start_next is sometimes called from inside mix()'s cue
        check, so nothing here can block the audio thread."""
        if not self._settings.djSfxEnabled:
            return

        with self._lock:
            if self._sfx_cooldown > 0:
                self._sfx_cooldown -= 1
                return
            if self._sfx_roll.random() >= SFX_CHANCE:
                return
            self._sfx_cooldown = SFX_COOLDOWN_TRANSITIONS
            start_frame = self._start_frame
            tolerance_frames = SFX_LATE_TOLERANCE_BEATS * self._transition_period

        asyncio.create_task(self._prepare_sfx(start_frame, tolerance_frames))

    async def _prepare_sfx(self, start_frame: float, tolerance_frames: float) -> None:
        sfx_pcm = await self._sfx.pick()
        if sfx_pcm is None or sfx_pcm.shape[0] == 0:
            return

        if self._stream_frames > start_frame + tolerance_frames:
            self._hub.warn("dj: sound effect missed its cue - decoding took too long, skipping it")
            return

        with self._lock:
            self._sfx_voice = SfxVoice(sfx_pcm, int(start_frame))
        seconds = sfx_pcm.shape[0] / self._sample_rate
        self._hub.info(f"dj: sound effect scheduled ({seconds:.1f}s)")

    # ---- the hot path ---------------------------------------------------------

    def mix(self, live_chunk: bytes) -> bytes:
        live = pcm.s16le_to_float32(live_chunk)
        n = live.shape[0]
        block_start = self._stream_frames
        self._stream_frames += n

        with self._lock:
            incoming = self._incoming
            outgoing = self._outgoing
            sfx = self._sfx_voice

        if incoming is None:
            return pcm.float32_to_s16le(live)
        if block_start + n <= self._start_frame:
            return pcm.float32_to_s16le(live)

        # The cue: a queued track comes in over the closing bars of this one, so the blend finishes
        # as it ends. Checked once per block rather than per sample.
        if outgoing is None:
            beats_left_now = (incoming.pcm.shape[0] - incoming.cursor) / self._transition_period
            if beats_left_now <= self._transition_beats + START_LEAD_BEATS and self._start_next(True):
                with self._lock:
                    incoming = self._incoming
                    outgoing = self._outgoing

        out = live.astype(np.float64)
        active_start = max(0, int(self._start_frame) - block_start)
        live_gain_tail = None

        if active_start < n:
            departing = outgoing if (outgoing is not None and not outgoing.finished) else None

            remaining_capacity = incoming.pcm.shape[0] - incoming.cursor
            if departing is not None:
                # Defensive: the outgoing deck's own remaining buffer should always cover at least
                # the rest of this transition by construction (it only becomes "outgoing" once its
                # own beatsLeft already exceeded the incoming track's transition length), but this
                # keeps a corrupted/short buffer from ever slicing out of bounds below.
                remaining_capacity = min(remaining_capacity, departing.pcm.shape[0] - departing.cursor)
            active_len = max(0, min(n - active_start, remaining_capacity))

            if active_len > 0:
                out = out.copy()
                active_end = active_start + active_len
                frames_abs = block_start + np.arange(active_start, active_end)
                beats_in = (frames_abs - self._start_frame) / self._transition_period
                beats_left = ((incoming.pcm.shape[0] - incoming.cursor - np.arange(active_len))
                             / self._transition_period)

                live_gain, live_bass, track_gain, track_bass = transition.envelope(
                    beats_in, beats_left, self._beatmatched, self._transition_beats, self._swap_beat)

                if departing is not None:
                    departing_raw = departing.pcm[departing.cursor:departing.cursor + active_len].astype(np.float64)
                    departing_filter = departing.highpass
                    departing_bass_start = departing.bass_smoothed
                else:
                    departing_raw = live[active_start:active_end].astype(np.float64)
                    departing_filter = self._live_highpass
                    departing_bass_start = self._live_bass_smoothed

                incoming_raw = incoming.pcm[incoming.cursor:incoming.cursor + active_len].astype(np.float64)

                departing_high = departing_filter.process(departing_raw)
                incoming_high = incoming.highpass.process(incoming_raw)

                departing_bass_arr, departing_bass_end = transition.smooth_toward(
                    departing_bass_start, live_bass, self._bass_smoothing_coefficient)
                incoming_bass_arr, incoming_bass_end = transition.smooth_toward(
                    incoming.bass_smoothed, track_bass, self._bass_smoothing_coefficient)

                if departing is not None:
                    departing.bass_smoothed = departing_bass_end
                else:
                    self._live_bass_smoothed = departing_bass_end
                incoming.bass_smoothed = incoming_bass_end

                departing_mixed = departing_high + departing_bass_arr[:, None] * (departing_raw - departing_high)
                incoming_mixed = incoming_high + incoming_bass_arr[:, None] * (incoming_raw - incoming_high)

                sfx_contribution, sfx_retire = self._sfx_contribution(sfx, frames_abs, active_len)

                mixed = (departing_mixed * live_gain[:, None] + incoming_mixed * track_gain[:, None]
                        + sfx_contribution)
                mixed = transition.soft_clip(mixed)

                out[active_start:active_end] = mixed

                incoming.cursor += active_len
                if departing is not None:
                    departing.cursor += active_len
                if sfx_retire:
                    with self._lock:
                        if self._sfx_voice is sfx:
                            self._sfx_voice = None

                live_gain_tail = float(live_gain[-1])

        # Fully on DJ content once the departing side has nothing left to contribute - the captured
        # app's audio is unused from here until this track's own outro needs it again.
        if live_gain_tail is not None and live_gain_tail <= 0.0:
            self._maybe_pause_source()

        self._retire_finished()
        return pcm.float32_to_s16le(out.astype(np.float32))

    def _sfx_contribution(self, sfx: SfxVoice | None, frames_abs: np.ndarray,
                          active_len: int) -> tuple[np.ndarray, bool]:
        """Gain for the sound effect at each absolute frame - 0 before it starts or after it ends,
        ramped over a few ms at both edges so a one-shot never clicks in or out. start_frame is a
        fixed absolute stream frame, so the clip position is just frame - start_frame; nothing here
        needs its own running cursor."""
        if sfx is None:
            return np.zeros((active_len, 2)), False

        position = frames_abs - sfx.start_frame
        finished = bool(position[-1] >= sfx.pcm.shape[0] - 1)

        active = (position >= 0) & (position < sfx.pcm.shape[0])
        if not np.any(active):
            return np.zeros((active_len, 2)), finished

        fade = max(1, int(SFX_FADE_SECONDS * self._sample_rate))
        remaining = sfx.pcm.shape[0] - position
        gain = np.clip(np.minimum(position / fade, remaining / fade), 0.0, 1.0)

        contribution = np.zeros((active_len, 2))
        indices = position[active].astype(np.int64)
        contribution[active] = sfx.pcm[indices] * gain[active, None]
        return contribution, finished

    # ---- retirement and resume --------------------------------------------------

    def _retire_finished(self) -> None:
        """Drops a deck once it has stopped contributing: the outgoing one when its transition is
        over or its audio ran out, the incoming one when the track ends."""
        retired = False
        completed = False

        with self._lock:
            outgoing = self._outgoing
            if outgoing is not None and (
                    outgoing.finished
                    or self._stream_frames - self._start_frame > self._transition_period * self._transition_beats):
                self._remove_from_queue(outgoing.entry)
                outgoing.entry.status = DONE
                self._outgoing = None
                retired = True

            incoming = self._incoming
            if incoming is not None and incoming.finished:
                self._remove_from_queue(incoming.entry)
                incoming.entry.status = DONE
                self._incoming = None
                retired = True
                completed = True
                self._last_finished_camelot = incoming.entry.camelot
                self._last_finished_bpm = incoming.entry.source_bpm
                if incoming.entry.video_id:
                    self._rave_recent.append(incoming.entry.video_id)

        if not retired:
            return

        self._evaluate_resume()

        if not completed:
            return

        self._publish()
        self._start_next(handover=False)

    def _remove_from_queue(self, entry: PendingTrack) -> None:
        self._queue = [e for e in self._queue if e is not entry]

    def _evaluate_resume(self) -> None:
        """Resumes the captured app as soon as nothing is left to hand off to - eagerly, since
        resuming early costs nothing (live audio the envelope does not want yet is simply multiplied
        by zero) where resuming late means dead air right when the outro needs it. In Rave mode,
        "nothing left to hand off to" is instead the cue to pick its own next track - resuming the
        captured app would end the set, which is the opposite of what Rave mode is for."""
        with self._lock:
            incoming_entry = self._incoming.entry if self._incoming else None
            outgoing_entry = self._outgoing.entry if self._outgoing else None
            has_successor = any(
                e is not incoming_entry and e is not outgoing_entry and e.status not in _TERMINAL
                for e in self._queue)

        if has_successor:
            return

        if self._settings.djMode == "rave" and self._autopick_next():
            return

        self._maybe_resume_source()

    def _autopick_next(self) -> bool:
        """Queues the library's own choice for what plays next. A real request queued after this
        still plays first (see _start_next's non-autonomous preference) - this only fills silence
        that would otherwise fall back to the captured app. Returns False when the library has
        nothing pickable yet, so the caller can fall back to resuming the live source instead."""
        target_energy = ENERGY_START + (ENERGY_PEAK - ENERGY_START) * min(
            1.0, self._rave_pick_count / ENERGY_RAMP_TRACKS)

        pick = self._library.pick_next(
            self._last_finished_camelot, self._last_finished_bpm, self._rave_recent, target_energy)
        if pick is None:
            return False

        title = f"{pick.artist} - {pick.title}".strip(" -") or pick.title
        accepted, _, _ = self.request(title, video_id=pick.video_id, autonomous=True)
        if accepted:
            self._rave_pick_count += 1
        return accepted

    # ---- pause/resume of the live source ---------------------------------------

    def _maybe_pause_source(self) -> None:
        # Unlocked fast path: this is called on nearly every ~20ms block for a track's entire steady
        # body, and after the first call _source_paused is already true for the rest of it - the
        # locked check underneath still makes the actual decision, this only skips the lock.
        if self._source_paused:
            return

        with self._lock:
            if self._source_paused:
                return
            self._source_paused = True

        asyncio.create_task(self._dispatch_pause(True, "paused the captured app - mixing has taken over"))

    def _maybe_resume_source(self) -> None:
        with self._lock:
            if not self._source_paused:
                return
            self._source_paused = False

        asyncio.create_task(self._dispatch_pause(False, "resumed the captured app - nothing left queued"))

    async def _dispatch_pause(self, pause: bool, message: str) -> None:
        try:
            if await self._sources.control("pause" if pause else "resume"):
                self._hub.info(f"dj {message}")
        except Exception as exc:
            self._hub.warn(f"dj: could not control the captured app: {exc}")

    # ---- rolling live beat grid -------------------------------------------------

    def observe_live(self, chunk: bytes) -> None:
        """Feeds the rolling live-grid analysis. Called from push_audio, which can run on a receiver
        thread as well as the loop."""
        if not chunk:
            return
        stereo = pcm.s16le_to_float32(chunk)
        with self._lock:
            self._live_pcm.append(stereo)
            self._live_pcm_frames += stereo.shape[0]
            limit = ROLLING_WINDOW_SECONDS * self._sample_rate * 2
            while self._live_pcm_frames > limit and len(self._live_pcm) > 1:
                dropped = self._live_pcm.pop(0)
                self._live_pcm_frames -= dropped.shape[0]

    async def _rolling_bpm_loop(self) -> None:
        """Keeps the live beat grid current. Runs off the pacer's own sample timeline, so the anchor
        it publishes is directly comparable to the playhead a transition is scheduled against."""
        while True:
            try:
                await asyncio.sleep(ROLLING_REFRESH_SECONDS)
                window_frames = ROLLING_WINDOW_SECONDS * self._sample_rate

                with self._lock:
                    if self._live_pcm_frames < 2 * self._sample_rate:
                        continue
                    stereo = np.concatenate(self._live_pcm, axis=0)[-window_frames:]
                    consumed = self._live_pcm_frames

                grid = beatgrid.analyze(pcm.to_mono(stereo), self._sample_rate)
                with self._lock:
                    self._live_bpm = grid.bpm if grid else 0.0
                    self._live_confidence = grid.confidence if grid else 0.0
                    if grid and grid.period_frames > 0:
                        # last_beat_frame, not a phase extrapolated from the window's start: the
                        # anchor is projected forward, so it has to start from the most recent beat
                        # rather than one a windowful of accumulated period error ago.
                        self._live_anchor_frame = (
                            consumed - stereo.shape[0] + grid.last_beat_frame)
                        self._live_period_frames = grid.period_frames
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._hub.error(f"dj: live beat analysis stopped: {exc}")
                return

    # ---- state -------------------------------------------------------------

    def snapshot(self) -> state_module.DjState:
        with self._lock:
            deck = self._incoming
            playing = deck.entry if deck is not None else None
            outgoing_entry = self._outgoing.entry if self._outgoing is not None else None

            queue_view = [e.to_entry() for e in self._queue if e is not playing and e is not outgoing_entry]

            confidence = (round(min(self._live_confidence, playing.source_confidence) * 100, 0)
                         if playing is not None and self._beatmatched else None)

            per_second = float(self._sample_rate)
            position = deck.cursor / per_second if deck is not None else 0.0
            duration = deck.pcm.shape[0] / per_second if deck is not None else 0.0
            artwork_version = self.artwork.version if playing is not None else 0
            album = playing.album if playing is not None else ""
            now_mixing = playing.to_entry() if playing is not None else None
            phase_text = self._phase_text()

        return state_module.DjState(
            queue=queue_view,
            nowMixing=now_mixing,
            phaseText=phase_text,
            confidencePercent=confidence,
            album=album,
            positionSeconds=position,
            durationSeconds=duration,
            artworkVersion=artwork_version,
        )

    def _phase_text(self) -> str:
        """Caller already holds self._lock."""
        playing = self._incoming.entry if self._incoming is not None else None

        if playing is None:
            count = len(self._queue)
            return "Nothing queued" if count == 0 else f"{count} queued"

        if self._outgoing is not None:
            return f"mixing \"{playing.title}\" over \"{self._outgoing.entry.title}\""

        return (f"beatmatched - \"{playing.title}\" at {playing.source_bpm:.0f} BPM"
               if self._beatmatched else f"mixing in \"{playing.title}\"")

    def _publish(self) -> None:
        self._hub.set_dj(self.snapshot())


def _new_id() -> str:
    return f"{random.getrandbits(32):08x}"
