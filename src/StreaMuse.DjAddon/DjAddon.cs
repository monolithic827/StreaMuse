using System.Collections.Concurrent;
using StreaMuse.DjAddon.Fetch;
using StreaMuse.DjAddon.Mixing;
using StreaMuse.Media;

namespace StreaMuse.DjAddon;

/// <summary>The decks. Holds a queue of requests, up to two playing tracks while one mixes into the
/// next, and a running estimate of the live source's beat grid; drops each track in on a beat and
/// trades the bass across rather than fading. See DjTransition for the shape of the blend.</summary>
public sealed class DjAddon : IDjAddon
{
    private const int RollingWindowSeconds = 8;

    // Calibrated against four real tracks rather than synthetic clicks (see CLAUDE.md): rolling 8s
    // windows of real music score a median of 96%, the weakest track a median of 39%, and white noise
    // 4.6%. 0.35 admits real music - including the awkward cases - with a wide margin over noise.
    private const double ConfidenceThreshold = 0.35;

    /// <summary>How much of a track's opening is read to establish its grid.</summary>
    private const int GridWindowSeconds = 30;

    /// <summary>How far two tempos may differ and still be blended without retiming. Tracks play at
    /// their recorded speed, so this is the whole budget - at 3% a sixteen-beat blend ends about half
    /// a beat out, which is the most that still reads as a mix.</summary>
    private const double TempoTolerance = 0.03;

    /// <summary>Beats the start is pushed forward by, so alignment is computed against a grid that is
    /// still valid once we get there rather than one we have already passed.</summary>
    private const int StartLeadBeats = 2;

    private const double BassCutHz = 220;

    /// <summary>One-pole smoothing for the bass handover. The swap is meant to be abrupt musically,
    /// but stepping a filter mix in one sample clicks, so it moves over a few milliseconds instead.</summary>
    private const double BassSmoothingMs = 8;

    private readonly Lock _sync = new();
    private readonly List<PendingTrack> _queue = [];
    private readonly ConcurrentQueue<float[]> _rollingInbox = new();

    private DjAddonContext _ctx = null!;
    private CancellationTokenSource? _rollingCts;

    private Deck? _incoming;
    private Deck? _outgoing;

    private long _streamFrames;
    private long _consumedFrames;

    private long _liveAnchorFrame;
    private long _livePeriodFrames;
    private double _liveBpm;
    private double _liveConfidence;

    private long _startFrame;
    private long _transitionPeriod;
    private bool _beatmatched;
    private Biquad? _liveHighPass;
    private double _liveBassSmoothed = 1;

    private sealed class PendingTrack
    {
        public required string Id { get; init; }
        public required string Query { get; init; }
        public string Title { get; set; } = "";
        public string Artist { get; set; } = "";
        public string Album { get; set; } = "";
        public string Status { get; set; } = "fetching";
        public float[]? Pcm { get; set; }
        public byte[]? Artwork { get; set; }
        public long ArtworkVersion { get; set; }
        public long BeatOffset { get; set; }
        public double SourceBpm { get; set; }
        public double SourceConfidence { get; set; }
    }

    /// <summary>A track that is currently making sound, with its own playhead and bass filter.</summary>
    private sealed class Deck(PendingTrack track, float[] pcm, long cursor, Biquad highPass, double bass)
    {
        public PendingTrack Track { get; } = track;
        public float[] Pcm { get; } = pcm;
        public long Cursor { get; set; } = cursor;
        public Biquad HighPass { get; } = highPass;
        public double BassSmoothed { get; set; } = bass;

        public bool Finished => Cursor >= Pcm.Length;
    }

    public void Initialize(DjAddonContext context)
    {
        _ctx = context;
        _rollingCts = new CancellationTokenSource();
        _ = Task.Run(() => RollingBpmLoopAsync(_rollingCts.Token));
    }

    public void Shutdown() => _rollingCts?.Cancel();

    // The fetch runs for as long as the addon is loaded, not for as long as the HTTP request that
    // kicked it off - an ASP.NET Core RequestAborted token gets cancelled once the response is
    // written, which killed the yt-dlp/ffmpeg processes mid-download the first time this ran.
    public Task<DjRequestResult> RequestAsync(string query, CancellationToken ct)
    {
        var settings = _ctx.ReadSettings();
        if (!settings.Enabled)
            return Task.FromResult(new DjRequestResult(false, "DJ mixing is turned off in Settings", null));

        var pending = new PendingTrack { Id = Guid.NewGuid().ToString("N")[..8], Query = query, Title = query };

        lock (_sync) _queue.Add(pending);
        _ctx.StateChanged();

        var lifetime = _rollingCts?.Token ?? CancellationToken.None;
        _ = Task.Run(() => FetchAsync(pending, lifetime), CancellationToken.None);

        return Task.FromResult(new DjRequestResult(true, null, ToEntry(pending)));
    }

    /// <summary>Moves straight to whatever is next: if something is queued and ready it mixes in now,
    /// otherwise the playing track runs its closing transition back to the live source. Either way it
    /// is a mix, not a cut.</summary>
    public void SkipCurrent()
    {
        if (StartNext(handover: true)) return;

        lock (_sync)
        {
            if (_incoming is not { } deck) return;

            var outroSamples = _transitionPeriod * (long)DjTransition.TransitionBeats * _ctx.Channels;
            deck.Cursor = Math.Max(deck.Cursor, deck.Pcm.Length - outroSamples);
            deck.Track.Status = "mixing out";
        }

        _ctx.StateChanged();
    }

    public DjSnapshot Snapshot()
    {
        lock (_sync)
        {
            var deck = _incoming;
            var playing = deck?.Track;
            var queue = _queue.Where(t => t != playing && t != _outgoing?.Track).Select(ToEntry).ToList();

            double? confidence = playing is not null && _beatmatched
                ? Math.Round(Math.Min(_liveConfidence, playing.SourceConfidence) * 100, 0)
                : null;

            var perSecond = (double)(_ctx.SampleRate * _ctx.Channels);

            return new DjSnapshot(
                queue,
                playing is null ? null : ToEntry(playing),
                PhaseText(),
                confidence,
                playing?.Album ?? "",
                deck is null ? 0 : deck.Cursor / perSecond,
                deck is null ? 0 : deck.Pcm.Length / perSecond,
                playing?.ArtworkVersion ?? 0);
        }
    }

    public byte[]? CurrentArtwork()
    {
        lock (_sync) return _incoming?.Track.Artwork;
    }

    /// <summary>Called by the audio pacer on its wall clock (see AudioPacer.RunAsync and CLAUDE.md) -
    /// never from the capture callback, which stops firing when the source goes quiet. Does no I/O:
    /// fetching, decoding and retiming all happen ahead of time, and this only filters and sums
    /// already-decoded PCM.</summary>
    public float[] Mix(float[] liveSamples)
    {
        _rollingInbox.Enqueue(liveSamples);

        var channels = _ctx.Channels;
        var frames = liveSamples.Length / channels;
        var blockStart = _streamFrames;
        _streamFrames += frames;

        Deck? incoming;
        Deck? outgoing;
        lock (_sync)
        {
            incoming = _incoming;
            outgoing = _outgoing;
        }

        if (incoming is null) return liveSamples;
        if (blockStart + frames <= _startFrame) return liveSamples;

        var liveHighPass = _liveHighPass!;
        var period = (double)_transitionPeriod;
        var smoothing = 1 - Math.Exp(-1000.0 / (BassSmoothingMs * _ctx.SampleRate));
        var output = new float[liveSamples.Length];

        // The cue: a queued track comes in over the closing bars of this one, so the blend finishes as
        // it ends. Checked once per block rather than per sample - a 20 ms granularity on a decision
        // that then waits for the next phrase boundary anyway.
        if (outgoing is null &&
            (incoming.Pcm.Length - incoming.Cursor) / (double)channels / period
                <= DjTransition.TransitionBeats + StartLeadBeats &&
            StartNext(handover: true))
        {
            lock (_sync)
            {
                incoming = _incoming!;
                outgoing = _outgoing;
            }
        }

        for (var i = 0; i < frames; i++)
        {
            var offset = i * channels;

            if (blockStart + i < _startFrame || incoming.Finished)
            {
                Array.Copy(liveSamples, offset, output, offset, channels);
                continue;
            }

            var beatsIn = (blockStart + i - _startFrame) / period;
            var beatsLeft = (incoming.Pcm.Length - incoming.Cursor) / (double)channels / period;
            var envelope = DjTransition.Envelope(beatsIn, beatsLeft, _beatmatched);

            // Whatever is on the way out plays the "live" role in the envelope - the previous track
            // when one is still running, otherwise the captured source itself.
            var departing = outgoing is { Finished: false } ? outgoing : null;
            var departingFilter = departing?.HighPass ?? liveHighPass;

            var departingBass = departing?.BassSmoothed ?? _liveBassSmoothed;
            departingBass += (envelope.LiveBass - departingBass) * smoothing;
            if (departing is not null) departing.BassSmoothed = departingBass;
            else _liveBassSmoothed = departingBass;

            incoming.BassSmoothed += (envelope.TrackBass - incoming.BassSmoothed) * smoothing;

            for (var c = 0; c < channels; c++)
            {
                var departingRaw = departing is not null ? departing.Pcm[departing.Cursor + c] : liveSamples[offset + c];
                var incomingRaw = incoming.Pcm[incoming.Cursor + c];

                // Filter state has to advance every sample regardless of gain, or the low band jumps
                // when a side comes back in.
                var departingHigh = departingFilter.Process(c, departingRaw);
                var incomingHigh = incoming.HighPass.Process(c, incomingRaw);

                var departingMixed = departingHigh + (float)departingBass * (departingRaw - departingHigh);
                var incomingMixed = incomingHigh + (float)incoming.BassSmoothed * (incomingRaw - incomingHigh);

                output[offset + c] = SoftClip(
                    departingMixed * envelope.LiveGain + incomingMixed * envelope.TrackGain);
            }

            incoming.Cursor += channels;
            if (departing is not null) departing.Cursor += channels;
        }

        RetireFinished();
        return output;
    }

    /// <summary>Drops a deck once it has stopped contributing: the outgoing one when its transition is
    /// over or its audio ran out, the incoming one when the track ends.</summary>
    private void RetireFinished()
    {
        var completed = false;

        lock (_sync)
        {
            if (_outgoing is { } outgoing && (outgoing.Finished || _streamFrames - _startFrame >
                    _transitionPeriod * (long)DjTransition.TransitionBeats))
            {
                _queue.Remove(outgoing.Track);
                outgoing.Track.Status = "done";
                _outgoing = null;
            }

            if (_incoming is { Finished: true } incoming)
            {
                _queue.Remove(incoming.Track);
                incoming.Track.Status = "done";
                _incoming = null;
                completed = true;
            }
        }

        if (!completed) return;

        _ctx.StateChanged();
        StartNext(handover: false);
    }

    private async Task FetchAsync(PendingTrack pending, CancellationToken ct)
    {
        try
        {
            var ffmpeg = _ctx.FfmpegPath();
            var ytDlp = _ctx.YtDlpPath();
            if (ffmpeg is null || ytDlp is null)
            {
                pending.Status = "failed";
                _ctx.LogError("DJ request failed: ffmpeg or yt-dlp is not available yet");
                _ctx.StateChanged();
                return;
            }

            var found = await YtDlpFetcher.FetchAsync(
                ytDlp, ffmpeg, pending.Query, _ctx.DataDir, _ctx.SampleRate, _ctx.Channels, ct);

            var title = found.Title;
            var fetched = found.Pcm;

            pending.Title = title;
            pending.Artist = found.Artist;
            pending.Album = found.Album;
            pending.Artwork = found.Artwork;
            pending.ArtworkVersion = VersionOf(found.Artwork);

            // Pre-listen before any of this can reach the stream.
            pending.Status = "auditioning";
            _ctx.StateChanged();

            var audition = TrackAudition.Audition(fetched, _ctx.SampleRate, _ctx.Channels);
            if (!audition.Ok)
            {
                pending.Status = "rejected";
                _ctx.LogWarn($"DJ rejected \"{title}\": {audition.Reason}");
                _ctx.StateChanged();
                return;
            }

            // Tracks are never retimed - they play at the speed they were recorded at. Alignment is
            // therefore only possible between tempos that already agree; see StartNext.
            var pcm = audition.StartSample > 0 ? fetched[audition.StartSample..] : fetched;
            var grid = ReadGrid(pcm);

            pending.Pcm = pcm;
            pending.BeatOffset = grid.PhaseFrames * _ctx.Channels;
            pending.SourceBpm = grid.Bpm;
            pending.SourceConfidence = grid.Confidence;

            var skipped = audition.StartSample / (double)(_ctx.SampleRate * _ctx.Channels);
            _ctx.LogInfo(
                $"DJ auditioned \"{title}\": {audition.DurationSeconds:F0}s, peak {audition.PeakDb:F1} dB" +
                (skipped > 0.1 ? $", skipped {skipped:F1}s of intro" : "") +
                (grid.Bpm > 0 ? $", {grid.Bpm:F0} BPM at {grid.Confidence:P0} confidence" : ", no clear tempo"));

            pending.Status = "ready";
            _ctx.StateChanged();

            // Only starts if nothing is playing. A request made mid-track waits for its cue near the
            // end of that track rather than interrupting it - see the check in Mix.
            StartNext(handover: false);
        }
        catch (Exception ex)
        {
            pending.Status = "failed";
            _ctx.LogError($"DJ request \"{pending.Query}\" failed: {ex.Message}");
            _ctx.StateChanged();
        }
    }

    /// <summary>Whether two tempos will hold together through a blend without either being retimed.
    /// A ratio near 1 obviously works; so do 2 and 1/2, where every second beat of the faster track
    /// lands on the slower one - a half-time record over a double-time one is a normal thing to mix.
    /// Anything else drifts apart, and the further into the blend, the worse it sounds.</summary>
    private static bool TemposAgree(double bpm, double target)
    {
        if (bpm <= 0 || target <= 0) return false;

        foreach (var multiple in new[] { 0.5, 1.0, 2.0 })
        {
            if (Math.Abs(bpm * multiple - target) / target <= TempoTolerance) return true;
        }

        return false;
    }

    /// <summary>Content hash, forced odd so it is never 0 - the panel reads 0 as "no artwork".</summary>
    private static long VersionOf(byte[]? artwork)
    {
        if (artwork is null || artwork.Length == 0) return 0;

        var hash = 1469598103934665603L;
        foreach (var b in artwork)
        {
            hash ^= b;
            hash *= 1099511628211L;
        }

        return (hash & 0x7FFFFFFFFFFFFFFEL) | 1L;
    }

    /// <summary>Reads the grid from the opening of the track rather than the whole file. Averaged over
    /// four minutes the beat-strength confidence is diluted by intros, breakdowns and outros - measured,
    /// deadmau5 scored 3% across the file and 63% on a window of it - and the opening is also the part
    /// whose phase we actually enter on.</summary>
    private BeatDetector.Grid ReadGrid(float[] pcm)
    {
        var window = Math.Min(pcm.Length, GridWindowSeconds * _ctx.SampleRate * _ctx.Channels);
        return BeatDetector.AnalyzeGrid(pcm[..window], _ctx.SampleRate);
    }

    /// <summary>The next 4-beat boundary of the live source's grid, a couple of beats out so the
    /// schedule is not already in the past by the time the pacer reaches it.</summary>
    private long NextPhraseOnLiveGrid(long period)
    {
        var earliest = _streamFrames + StartLeadBeats * period;
        var beats = Math.Ceiling((earliest - _liveAnchorFrame) / (double)period / 4) * 4;
        return _liveAnchorFrame + (long)(beats * period);
    }

    /// <summary>The next 4-beat boundary of the *outgoing track's own* grid. Coming in on a phrase of
    /// the record that is leaving is what makes the blend land musically rather than merely on a beat;
    /// starting the instant the cue fires would drop the new track mid-bar.</summary>
    private long NextPhraseOnDeck(Deck deck, long period, int channels)
    {
        var position = deck.Cursor / channels;
        var beatsPlayed = (position - deck.Track.BeatOffset / channels) / (double)period;
        var phrase = Math.Ceiling((beatsPlayed + StartLeadBeats) / 4) * 4;

        return _streamFrames + (long)Math.Max(0, (phrase - beatsPlayed) * period);
    }

    /// <summary>Starts the next ready track. With <paramref name="handover"/> it mixes over whatever is
    /// playing - which is what the cue near the end of a track, and Skip, both want; without it, it
    /// only starts when the decks are empty, so a request made mid-track waits its turn. Returns false
    /// when there is nothing ready to start.</summary>
    private bool StartNext(bool handover)
    {
        string message;

        lock (_sync)
        {
            // Only one handover at a time; a second would need a third deck and land off the grid.
            if (_outgoing is not null) return false;
            if (_incoming is not null && !handover) return false;

            var next = _queue.FirstOrDefault(t => t.Status == "ready");
            if (next?.Pcm is not { Length: > 0 } pcm) return false;

            var channels = _ctx.Channels;
            var leaving = _incoming;

            // Beat-match against the outgoing track when there is one, since that is what the new
            // track has to sit on top of; against the live source otherwise. Both sides have to clear
            // the gate: matching to a tempo read at 16% confidence is guesswork, and lands worse than
            // an honest fade.
            var leavingReadable = leaving is not null && leaving.Track.SourceBpm > 0 &&
                                  leaving.Track.SourceConfidence >= ConfidenceThreshold;

            var period = leavingReadable
                ? (long)(60.0 / leaving!.Track.SourceBpm * _ctx.SampleRate)
                : _livePeriodFrames;

            var readable = next.SourceBpm > 0 && next.SourceConfidence >= ConfidenceThreshold;
            var targetBpm = leavingReadable ? leaving!.Track.SourceBpm : _liveBpm;

            // Both grids readable *and* the tempos already compatible. Without retiming there is no
            // way to pull two different tempos together, so a blend between them would walk out of
            // time as it ran; a fade is the honest answer for those.
            _beatmatched = period > 0 && readable &&
                           (leavingReadable || (leaving is null && _liveConfidence >= ConfidenceThreshold)) &&
                           TemposAgree(next.SourceBpm, targetBpm);

            if (_beatmatched)
            {
                _transitionPeriod = period;
                _startFrame = leaving is null
                    ? NextPhraseOnLiveGrid(period)
                    : NextPhraseOnDeck(leaving, period, channels);

                message = leaving is null
                    ? $"dropping \"{next.Title}\" on the beat at {next.SourceBpm:F0} BPM"
                    : $"mixing \"{next.Title}\" in over the end of \"{leaving.Track.Title}\" - both at {next.SourceBpm:F0} BPM";
            }
            else
            {
                var seconds = Math.Max(1.0, _ctx.ReadSettings().CrossfadeSeconds);
                _transitionPeriod = (long)(seconds * _ctx.SampleRate / DjTransition.TransitionBeats);
                _startFrame = _streamFrames;

                var target = leavingReadable ? leaving!.Track.SourceBpm : _liveBpm;
                message = readable && target > 0
                    ? $"mixing in \"{next.Title}\" - {next.SourceBpm:F0} against {target:F0} BPM, fading rather than dragging it into time"
                    : $"mixing in \"{next.Title}\" - no clear beat to lock to, fading instead";
            }

            // Enter the track on one of its own beats, so beat one of the mix is beat one of both.
            var cursor = _beatmatched ? Math.Min(next.BeatOffset, pcm.Length - channels) : 0;

            if (leaving is not null) leaving.Track.Status = "mixing out";
            _outgoing = leaving;
            _liveHighPass ??= Biquad.HighPass(_ctx.SampleRate, BassCutHz, 0.707, channels);
            _liveBassSmoothed = 1;

            next.Status = "mixing";
            _incoming = new Deck(
                next, pcm, cursor, Biquad.HighPass(_ctx.SampleRate, BassCutHz, 0.707, channels), 0);
        }

        _ctx.LogInfo(message);
        _ctx.StateChanged();
        return true;
    }

    /// <summary>Keeps the live beat grid current. Runs off the pacer's own sample timeline - the frame
    /// counter here and the one in Mix count the same blocks - so the anchor it publishes is directly
    /// comparable to the playhead a transition is scheduled against.</summary>
    private async Task RollingBpmLoopAsync(CancellationToken ct)
    {
        var ring = new List<float>();
        var channels = _ctx.Channels;
        var maxSamples = RollingWindowSeconds * _ctx.SampleRate * channels;

        try
        {
            while (!ct.IsCancellationRequested)
            {
                while (_rollingInbox.TryDequeue(out var chunk))
                {
                    ring.AddRange(chunk);
                    _consumedFrames += chunk.Length / channels;
                }

                if (ring.Count > maxSamples) ring.RemoveRange(0, ring.Count - maxSamples);

                if (ring.Count >= _ctx.SampleRate * channels * 2)
                {
                    var grid = BeatDetector.AnalyzeGrid([.. ring], _ctx.SampleRate);
                    _liveBpm = grid.Bpm;
                    _liveConfidence = grid.Confidence;

                    if (grid.PeriodFrames > 0)
                    {
                        // LastBeatFrames, not PhaseFrames: the anchor is extrapolated forward, so it
                        // has to start from the most recent beat rather than one a windowful of
                        // accumulated period error ago. See BeatDetector.Grid.
                        _liveAnchorFrame = _consumedFrames - ring.Count / channels + grid.LastBeatFrames;
                        _livePeriodFrames = grid.PeriodFrames;
                    }
                }

                await Task.Delay(TimeSpan.FromSeconds(2), ct);
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private string PhaseText()
    {
        var playing = _incoming?.Track;

        if (playing is null) return _queue.Count == 0 ? "Nothing queued" : $"{_queue.Count} queued";
        if (_outgoing is { } outgoing) return $"mixing \"{playing.Title}\" over \"{outgoing.Track.Title}\"";

        return _beatmatched
            ? $"beatmatched - \"{playing.Title}\" at {playing.SourceBpm:F0} BPM"
            : $"mixing in \"{playing.Title}\"";
    }

    /// <summary>Keeps the sum inside the rails. Both decks deliberately run at full level through a
    /// bass swap, and modern masters arrive already at or above 0 dBFS - measured, two of the test
    /// tracks peaked at +2.6 and +1.8 dB - so their sum clips hard without this. Below the threshold
    /// the signal is untouched; above it the curve bends smoothly rather than flattening, which is
    /// what a mixer's headroom does and what stops a clipped mix sounding like a fault.</summary>
    private static float SoftClip(float sample)
    {
        const float threshold = 0.7f;

        var magnitude = Math.Abs(sample);
        if (magnitude <= threshold) return sample;

        var excess = (magnitude - threshold) / (1 - threshold);
        var shaped = threshold + (1 - threshold) * Math.Tanh(excess);

        return sample < 0 ? (float)-shaped : (float)shaped;
    }

    private static DjQueueEntry ToEntry(PendingTrack track) =>
        new(track.Id, track.Query, track.Title, track.Artist, track.Status);
}
