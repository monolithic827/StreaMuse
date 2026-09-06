"""The decks. Holds a queue of requests, up to two playing tracks while one mixes into the next, and
a running estimate of the live source's beat grid; drops each track in on a beat and trades the bass
across rather than fading. See transition.py for the shape of the blend.

mix() runs on AudioPacer's wall-clock tick and must never block - everything else (fetching,
decoding, beat analysis) happens ahead of time on background tasks. Every entry point here is on the
asyncio loop, so none of this state needs a lock.
"""

import asyncio
import collections
import math
import random

import numpy as np

from .. import state as state_module
from ..artwork import ArtworkStore
from . import beatgrid, harvester, key, pcm, transition
from .audition import audition
from .deck import Deck
from .fetch import SearchResult, YtDlpFetcher
from .sfx import SfxLibrary

FETCHING, AUDITIONING, READY, MIXING, MIXING_OUT = (
    "fetching", "auditioning", "ready", "mixing", "mixing out",
)

SAMPLE_RATE = 44100

#: How much of the outgoing stream the live beat grid is read from, and how often it is re-read.
ROLLING_WINDOW_SECONDS = 8
TICK_SECONDS = 2

#: How much of a track's opening is read to establish its grid.
GRID_WINDOW_SECONDS = 30

#: Beats the start is pushed forward by, so alignment is computed against a grid that is still valid
#: once we get there rather than one we have already passed.
START_LEAD_BEATS = 2.0

#: Chance a given transition gets a sound effect, once the cooldown below allows a roll at all.
SFX_CHANCE = 0.25
SFX_COOLDOWN_TRANSITIONS = 2
SFX_LATE_TOLERANCE_BEATS = 1.0

#: Rave mode's energy target ramps from START to PEAK over this many autonomous picks, then holds -
#: a set that opens at full intensity and never breathes reads as random, not as a set.
ENERGY_RAMP_TRACKS = 6
ENERGY_START = 0.35
ENERGY_PEAK = 0.85

#: How many of its own recent picks Rave mode won't repeat.
RAVE_RECENT_HISTORY = 8


class PendingTrack:
    def __init__(self, entry_id: str, query: str, autonomous: bool) -> None:
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
        self._harvesting = False

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

        self._sfx_pcm: np.ndarray | None = None
        self._sfx_start_frame = 0
        self._sfx_cooldown = 0
        self._sfx_roll = random.Random()

        self._live_pcm: collections.deque = collections.deque()
        self._live_frames = 0
        self._tick_task: asyncio.Task | None = None

        self._source_paused = False

        self.artwork = ArtworkStore()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Starts the mixer's background tick. Called once the app's event loop is running."""
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick_loop())

    # ---- requests -----------------------------------------------------------

    def request(self, query: str, video_id: str | None = None,
               autonomous: bool = False) -> state_module.DjQueueEntry:
        entry = PendingTrack(_new_id(), query, autonomous)
        entry.video_id = video_id
        self._queue.append(entry)
        self._publish()

        asyncio.create_task(self._fetch(entry, video_id))
        return entry.to_entry()

    async def search(self, query: str) -> list[SearchResult]:
        """Candidates for a query, for a request dropdown - picking one and calling request() with
        its video_id skips the search fetch() would otherwise redo, and fetches that exact track."""
        return await self._fetcher.search(query)

    # ---- the Rave-mode library ----------------------------------------------------

    async def harvest_live(self) -> None:
        """Walks forward through whatever's already playing on the selected source and learns each
        track - see harvester.py for why this needs no playlist API. Best run before the stream goes
        live: it's real playback, which push_audio only drops while no stream session exists."""
        await self._learn(
            "the live source",
            lambda: harvester.harvest_live_source(self._sources, self._hub, self._fetcher))

    async def harvest_playlist(self, url: str) -> None:
        await self._learn(
            "that playlist",
            lambda: harvester.harvest_youtube_playlist(self._deps, self._hub, url))

    async def _learn(self, what: str, harvest) -> None:
        """One pass at a time: two harvests would both drive the same receiver's 'next' and each
        read the other's skips as the playlist looping. Runs as a detached background task from the
        web handler, so it must log its own exceptions."""
        if self._harvesting:
            self._hub.warn("dj library: already learning - let that finish first")
            return

        self._harvesting = True
        try:
            added = self._library.add_candidates(await harvest())
            self._hub.info(f"dj library: {added} new tracks queued for analysis")
            await self._library.analyze_pending()
        except Exception as exc:
            self._hub.error(f"dj library: learning from {what} failed: {exc}")
        finally:
            self._harvesting = False

    def skip(self) -> None:
        """Moves straight to whatever is next: if something is queued and ready it mixes in now,
        otherwise the playing track runs its closing transition back to the live source. Either way
        it is a mix, not a cut."""
        if self._outgoing is not None:
            return  # already handing over; skipping again would need a third deck

        if self._start_next(handover=True):
            return

        deck = self._incoming
        if deck is None:
            return

        outro_samples = int(self._transition_period * self._transition_beats)
        deck.cursor = max(deck.cursor, deck.pcm.shape[0] - outro_samples)
        deck.entry.status = MIXING_OUT

        self._publish()
        self._evaluate_resume()

    # ---- fetch pipeline -------------------------------------------------------

    async def _fetch(self, entry: PendingTrack, video_id: str | None) -> None:
        try:
            fetched = await self._fetcher.fetch(entry.query, video_id)

            entry.title, entry.artist, entry.album = fetched.title, fetched.artist, fetched.album
            entry.artwork = fetched.artwork

            entry.status = AUDITIONING
            self._publish()

            result = audition(fetched.stereo, self._sample_rate)
            if not result.ok:
                self._hub.warn(f"dj: rejected \"{entry.title}\": {result.reason}")
                self._discard(entry)
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
            self._hub.error(f"dj: request \"{entry.query}\" failed: {exc}")
            self._discard(entry)

    def _discard(self, entry: PendingTrack) -> None:
        """A request that will never play leaves the queue rather than sitting in it as a permanent
        row nothing retires; the log is where the failure is reported. The library has to be told
        too, or the replacement Rave mode picks for the failure is the same track again - pick_next
        is deterministic and nothing else about its inputs changed."""
        if entry.video_id:
            self._library.mark_unplayable(entry.video_id)
        self._remove_from_queue(entry)
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
        # Only one handover at a time; a second would need a third deck and land off the grid.
        if self._outgoing is not None:
            return False
        if self._incoming is not None and not handover:
            return False

        ready = [e for e in self._queue if e.status == READY]
        # A real request always plays before an autonomous Rave-mode pick, even one queued earlier -
        # the pick is there to fill silence, not to make someone wait behind it.
        next_entry = next((e for e in ready if not e.autonomous), None) or next(iter(ready), None)
        if next_entry is None or next_entry.pcm is None or next_entry.pcm.shape[0] == 0:
            return False

        leaving = self._incoming

        # Beat-match against the outgoing track when there is one, since that is what the new track
        # has to sit on top of; against the live source otherwise. Both sides have to clear the
        # gate: matching to a tempo read at low confidence is guesswork, and lands worse than an
        # honest fade.
        leaving_readable = (leaving is not None and leaving.entry.source_bpm > 0
                            and leaving.entry.source_confidence >= beatgrid.CONFIDENCE_THRESHOLD)
        target_bpm = leaving.entry.source_bpm if leaving_readable else self._live_bpm

        period = ((self._sample_rate * 60.0 / target_bpm) if leaving_readable
                 else self._live_period_frames)

        readable = (next_entry.source_bpm > 0
                   and next_entry.source_confidence >= beatgrid.CONFIDENCE_THRESHOLD)

        self._beatmatched = bool(
            period > 0 and readable
            and (leaving_readable
                 or (leaving is None and self._live_confidence >= beatgrid.CONFIDENCE_THRESHOLD))
            and transition.tempos_agree(next_entry.source_bpm, target_bpm))

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

            message = (
                f"mixing in \"{next_entry.title}\" - {next_entry.source_bpm:.0f} against "
                f"{target_bpm:.0f} BPM, fading rather than dragging it into time"
                if readable and target_bpm > 0 else
                f"mixing in \"{next_entry.title}\" - no clear beat to lock to, fading instead")

        # Enter on the track's own mix-in point rather than wherever it happens to start - a DJ cues
        # a record from where its groove has actually locked in, not frame zero.
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

        if self._sfx_cooldown > 0:
            self._sfx_cooldown -= 1
            return
        if self._sfx_roll.random() >= SFX_CHANCE:
            return

        self._sfx_cooldown = SFX_COOLDOWN_TRANSITIONS
        asyncio.create_task(self._prepare_sfx(
            self._start_frame, SFX_LATE_TOLERANCE_BEATS * self._transition_period))

    async def _prepare_sfx(self, start_frame: float, tolerance_frames: float) -> None:
        sfx_pcm = await self._sfx.pick()
        if sfx_pcm is None or sfx_pcm.shape[0] == 0:
            return

        if self._stream_frames > start_frame + tolerance_frames:
            self._hub.warn("dj: sound effect missed its cue - decoding took too long, skipping it")
            return

        self._sfx_pcm = sfx_pcm
        self._sfx_start_frame = int(start_frame)
        self._hub.info(f"dj: sound effect scheduled ({sfx_pcm.shape[0] / self._sample_rate:.1f}s)")

    # ---- the hot path ---------------------------------------------------------

    def mix(self, live_chunk: bytes) -> bytes:
        live = pcm.s16le_to_float32(live_chunk)
        n = live.shape[0]
        block_start = self._stream_frames
        self._stream_frames += n
        self._remember_live(live)

        incoming = self._incoming
        if incoming is None:
            return live_chunk

        # The cue: a queued track comes in over the closing bars of this one, so the blend finishes
        # as it ends. Checked once per block rather than per sample.
        if self._outgoing is None:
            beats_left_now = (incoming.pcm.shape[0] - incoming.cursor) / self._transition_period
            if beats_left_now <= self._transition_beats + START_LEAD_BEATS:
                self._start_next(handover=True)
                incoming = self._incoming

        outgoing = self._outgoing
        departing = outgoing if (outgoing is not None and not outgoing.finished) else None

        out = live.astype(np.float64)
        active_start = min(n, max(0, int(self._start_frame) - block_start))

        if active_start > 0 and departing is not None:
            # Cued but not started: _start_frame is up to a phrase away, and the record on air keeps
            # playing until then. Without this the live source would come back for those beats -
            # audibly, and as silence, since it is paused by now.
            lead = departing.pcm[departing.cursor:departing.cursor + active_start]
            out[:lead.shape[0]] = lead
            departing.cursor += lead.shape[0]

        live_needed = departing is None

        if active_start < n:
            active_len = min(n - active_start, incoming.pcm.shape[0] - incoming.cursor)
            active_end = active_start + active_len

            frames_abs = block_start + np.arange(active_start, active_end)
            beats_in = (frames_abs - self._start_frame) / self._transition_period
            beats_left = ((incoming.pcm.shape[0] - incoming.cursor - np.arange(active_len))
                         / self._transition_period)

            live_gain, live_bass, track_gain, track_bass = transition.envelope(
                beats_in, beats_left, self._beatmatched, self._transition_beats, self._swap_beat)

            if departing is not None:
                # The outgoing deck can run out mid-block: it became outgoing on its own remaining
                # beats, which the incoming track's transition may outlast. Its tail is silence, not
                # a reason to leave the rest of the block unmixed.
                departing_raw = np.zeros((active_len, 2))
                available = departing.pcm[departing.cursor:departing.cursor + active_len]
                departing_raw[:available.shape[0]] = available
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

            mixed = departing_mixed * live_gain[:, None] + incoming_mixed * track_gain[:, None]
            self._add_sfx(mixed, frames_abs)
            out[active_start:active_end] = transition.soft_clip(mixed)

            incoming.cursor += active_len
            if departing is not None:
                departing.cursor += active_len

            live_needed = departing is None and live_gain[-1] > 0.0

        # The captured app is unused whenever a deck is departing, or while the envelope has the
        # live side at zero. It has to come back before the outro needs it, not once the deck
        # retires, or the outro crossfades into a source that is still paused.
        self._set_source_paused(not live_needed)

        self._retire_finished()
        return pcm.float32_to_s16le(out.astype(np.float32))

    def _add_sfx(self, mixed: np.ndarray, frames_abs: np.ndarray) -> None:
        """start_frame is a fixed absolute stream frame, so the clip position is just
        frame - start_frame; nothing here needs its own running cursor. The clip is already faded at
        both edges by SfxLibrary, so there is no gain to apply."""
        if self._sfx_pcm is None:
            return

        position = frames_abs - self._sfx_start_frame
        playing = (position >= 0) & (position < self._sfx_pcm.shape[0])
        mixed[playing] += self._sfx_pcm[position[playing]]

        if position[-1] >= self._sfx_pcm.shape[0]:
            self._sfx_pcm = None

    # ---- retirement and resume --------------------------------------------------

    def _retire_finished(self) -> None:
        """Drops a deck once it has stopped contributing: the outgoing one when its transition is
        over or its audio ran out, the incoming one when the track ends."""
        outgoing = self._outgoing
        retired = outgoing is not None and (
            outgoing.finished
            or self._stream_frames - self._start_frame > self._transition_period * self._transition_beats)
        if retired:
            self._remove_from_queue(outgoing.entry)
            self._outgoing = None

        incoming = self._incoming
        completed = incoming is not None and incoming.finished
        if completed:
            self._remove_from_queue(incoming.entry)
            self._incoming = None
            self._last_finished_camelot = incoming.entry.camelot
            self._last_finished_bpm = incoming.entry.source_bpm
            if incoming.entry.video_id:
                self._rave_recent.append(incoming.entry.video_id)

        if not (retired or completed):
            return

        self._evaluate_resume()

        if completed:
            self._publish()
            self._start_next(handover=False)

    def _remove_from_queue(self, entry: PendingTrack) -> None:
        self._queue = [e for e in self._queue if e is not entry]

    def _evaluate_resume(self) -> None:
        """Called whenever the queue may have run dry. In Rave mode that is the cue to pick its own
        next track - resuming the captured app would end the set, which is the opposite of what Rave
        mode is for."""
        incoming_entry = self._incoming.entry if self._incoming else None
        outgoing_entry = self._outgoing.entry if self._outgoing else None
        if any(e is not incoming_entry and e is not outgoing_entry for e in self._queue):
            return

        if self._settings.djMode == "rave" and self._autopick_next():
            return

        self._set_source_paused(False)

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

        self.request(f"{pick.artist} - {pick.title}".strip(" -") or pick.title,
                     video_id=pick.video_id, autonomous=True)
        self._rave_pick_count += 1
        return True

    def _set_source_paused(self, paused: bool) -> None:
        if self._source_paused == paused:
            return
        self._source_paused = paused
        asyncio.create_task(self._dispatch_pause(paused))

    async def _dispatch_pause(self, paused: bool) -> None:
        try:
            if await self._sources.control("pause" if paused else "resume"):
                self._hub.info("dj: paused the captured app - mixing has taken over" if paused
                              else "dj: handed back to the captured app")
        except Exception as exc:
            self._hub.warn(f"dj: could not control the captured app: {exc}")

    # ---- the live beat grid -------------------------------------------------

    def _remember_live(self, live: np.ndarray) -> None:
        """Keeps the last ROLLING_WINDOW_SECONDS of what actually went out. Fed from mix() rather
        than from push_audio so this buffer shares the stream's own timeline - push_audio sees audio
        before the pacer's jitter reserve, which would leave the grid's anchor a couple of hundred
        milliseconds ahead of the playhead a transition is scheduled against."""
        self._live_pcm.append(live)
        self._live_frames += live.shape[0]

        limit = ROLLING_WINDOW_SECONDS * self._sample_rate
        while self._live_frames - self._live_pcm[0].shape[0] >= limit:
            self._live_frames -= self._live_pcm.popleft().shape[0]

    def _refresh_live_grid(self) -> None:
        if not self._live_pcm:
            return

        # Read the playhead together with the window, before analyze() takes any time: the buffer's
        # last frame is the last frame mix() wrote, which is what makes the anchor below directly
        # comparable to the _start_frame a transition is scheduled against.
        window = np.concatenate(self._live_pcm, axis=0)[-ROLLING_WINDOW_SECONDS * self._sample_rate:]
        end_frame = self._stream_frames

        grid = beatgrid.analyze(pcm.to_mono(window), self._sample_rate)
        self._live_bpm = grid.bpm if grid else 0.0
        self._live_confidence = grid.confidence if grid else 0.0

        if grid and grid.period_frames > 0:
            # last_beat_frame, not a phase extrapolated from the window's start: the anchor is
            # projected forward, so it has to start from the most recent beat rather than one a
            # windowful of accumulated period error ago.
            self._live_anchor_frame = end_frame - window.shape[0] + grid.last_beat_frame
            self._live_period_frames = grid.period_frames

    async def _tick_loop(self) -> None:
        """The mixer's own tick, while a stream is running: keeps the live beat grid current, and in
        Rave mode starts the set when the decks have run dry. Nothing else would - a pick otherwise
        only happens as one track hands over to the next, so a freshly learned library would sit
        unused until someone made a manual request to prime it."""
        last_frames = -1
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)

                streaming = self._stream_frames != last_frames
                last_frames = self._stream_frames
                if not streaming:
                    continue

                self._refresh_live_grid()
                if self._incoming is None:
                    self._evaluate_resume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._hub.error(f"dj: the mixer's background tick stopped: {exc}")
                return

    # ---- state -------------------------------------------------------------

    def snapshot(self) -> state_module.DjState:
        deck = self._incoming
        playing = deck.entry if deck is not None else None
        outgoing_entry = self._outgoing.entry if self._outgoing is not None else None

        confidence = (round(min(self._live_confidence, playing.source_confidence) * 100, 0)
                     if playing is not None and self._beatmatched else None)

        return state_module.DjState(
            queue=[e.to_entry() for e in self._queue
                   if e is not playing and e is not outgoing_entry],
            nowMixing=playing.to_entry() if playing is not None else None,
            phaseText=self._phase_text(),
            confidencePercent=confidence,
            album=playing.album if playing is not None else "",
            positionSeconds=deck.cursor / self._sample_rate if deck is not None else 0.0,
            durationSeconds=deck.pcm.shape[0] / self._sample_rate if deck is not None else 0.0,
            artworkVersion=self.artwork.version if playing is not None else 0,
        )

    def _phase_text(self) -> str:
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
