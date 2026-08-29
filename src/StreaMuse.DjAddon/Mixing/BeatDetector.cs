using SoundTouch;

namespace StreaMuse.DjAddon.Mixing;

/// <summary>Tempo and beat positions, from SoundTouch's <see cref="BpmDetect"/>.
///
/// This was hand-rolled first - energy flux, then a kick-band low-pass, then spectral flux over an
/// FFT - and each version had to be rebuilt after real music exposed it: the first two could not tell
/// Nirvana from white noise, and the octave correction walked to the top of its own search range on
/// two of four test tracks. SoundTouch's detector is the same idea done properly and, more to the
/// point, exercised by a great many more records than four. It also reports where the beats *are*,
/// with a strength per beat, which is what a transition has to land on - tempo alone is not enough.
///
/// The surface here is unchanged from the hand-rolled version, so the harness that measured that one
/// measures this one against the same tracks.</summary>
public static class BeatDetector
{
    /// <summary>Beats below this share of the strongest beat are dropped; SoundTouch returns weak
    /// candidates in the absence of clear beats and its own docs suggest filtering them.</summary>
    private const float WeakBeat = 0.4f;

    private const int MinimumSeconds = 2;

    /// <summary>Fewer strong beats than this is too little to call a grid from.</summary>
    private const int MinimumBeats = 4;

    /// <summary>How far a gap may sit from a whole number of periods and still count as on the grid.</summary>
    private const double GridTolerance = 0.12;

    /// <summary><see cref="PhaseFrames"/> is the offset from the start of the analysed buffer to the
    /// first beat and <see cref="PeriodFrames"/> the spacing between beats. <see cref="LastBeatFrames"/>
    /// is the most recent beat: live scheduling must anchor on it rather than extrapolate from the
    /// first, which multiplies any period error by every beat in the window - measured at ~50 ms out
    /// over 20s with the old detector, audible as a flam rather than one kick.</summary>
    public sealed record Grid(
        double Bpm, double Confidence, long PhaseFrames, long PeriodFrames, long LastBeatFrames);

    public static (double Bpm, double Confidence) Analyze(float[] interleavedStereo, int sampleRate)
    {
        var grid = AnalyzeGrid(interleavedStereo, sampleRate);
        return (grid.Bpm, grid.Confidence);
    }

    public static Grid AnalyzeGrid(float[] interleavedStereo, int sampleRate)
    {
        const int channels = 2;

        var frames = interleavedStereo.Length / channels;
        if (frames < MinimumSeconds * sampleRate) return Empty;

        var detect = new BpmDetect(channels, sampleRate);

        // InputSamples may disrupt the buffer it is given, and callers here reuse theirs.
        var scratch = new float[interleavedStereo.Length];
        Array.Copy(interleavedStereo, scratch, scratch.Length);

        // Fed in blocks rather than whole, as the library asks, to keep working memory bounded.
        const int blockFrames = 8192;
        for (var frame = 0; frame < frames; frame += blockFrames)
        {
            var take = Math.Min(blockFrames, frames - frame);
            detect.InputSamples(scratch.AsSpan(frame * channels, take * channels), take);
        }

        var bpm = detect.GetBpm();
        if (bpm <= 0) return Empty;

        var (first, last, confidence, period) = ReadBeats(detect, sampleRate, 60.0 / bpm);
        if (period <= 0) return Empty;

        return new Grid(60.0 / period, confidence, first, (long)Math.Round(period * sampleRate), last);
    }

    /// <summary>Pulls the beat list out and reduces it to what a transition needs: the first and last
    /// beat worth landing on, and how strongly the track carries a beat at all.</summary>
    private static (long First, long Last, double Confidence, double Period) ReadBeats(
        BpmDetect detect, int sampleRate, double periodSeconds)
    {
        var count = detect.GetBeats(Span<float>.Empty, Span<float>.Empty);
        if (count <= 0) return (0, 0, 0, 0);

        var positions = new float[count];
        var strengths = new float[count];
        count = detect.GetBeats(positions, strengths);
        if (count <= 0) return (0, 0, 0, 0);

        var strongest = 0f;
        for (var i = 0; i < count; i++) strongest = Math.Max(strongest, strengths[i]);
        if (strongest <= 0) return (0, 0, 0, 0);

        // Positions come back in seconds.
        var beats = new List<double>();
        for (var i = 0; i < count; i++)
        {
            if (strengths[i] >= strongest * WeakBeat) beats.Add(positions[i]);
        }

        if (beats.Count < MinimumBeats) return (0, 0, 0, 0);

        var first = (long)Math.Round(beats[0] * sampleRate);
        var last = (long)Math.Round(beats[^1] * sampleRate);

        return (first, last, Regularity(beats, periodSeconds), RefinePeriod(beats, periodSeconds));
    }

    /// <summary>Least-squares period through the detected beats, rather than 60/GetBpm.
    ///
    /// GetBpm is rounded enough that the period it implies is out by ~0.2%, which is nothing per beat
    /// and 17 ms across a sixteen-beat transition - the mix drifts off as it goes. Fitting a line
    /// through the beat positions uses the whole window to pin the spacing and takes that to under
    /// 2 ms. Beat *positions* are indexed by whole periods from the first beat, so a missed beat
    /// leaves a gap in the indices rather than distorting the slope.</summary>
    private static double RefinePeriod(List<double> beats, double periodSeconds)
    {
        double sumIndex = 0, sumPosition = 0, sumIndexSquared = 0, sumProduct = 0;
        var used = 0;

        foreach (var position in beats)
        {
            var index = Math.Round((position - beats[0]) / periodSeconds);
            if (index < 0) continue;

            sumIndex += index;
            sumPosition += position;
            sumIndexSquared += index * index;
            sumProduct += index * position;
            used++;
        }

        var denominator = used * sumIndexSquared - sumIndex * sumIndex;
        if (used < MinimumBeats || Math.Abs(denominator) < 1e-9) return periodSeconds;

        var slope = (used * sumProduct - sumIndex * sumPosition) / denominator;

        // A fit that disagrees wildly with the detector means the indexing went wrong, not that the
        // tempo is different; keep the detector's figure in that case.
        return slope > 0 && Math.Abs(slope - periodSeconds) / periodSeconds < 0.1 ? slope : periodSeconds;
    }

    /// <summary>How evenly the beats are spaced: the share of gaps that land on a whole number of
    /// periods.
    ///
    /// Measuring beat *strength* instead does not work, and fails in the most misleading direction -
    /// white noise scored 100%, because with no beat present every candidate comes back weak and
    /// similar, so any relative test passes them all. Spacing is the thing that distinguishes music
    /// from noise: real beats arrive on a grid, spurious ones arrive whenever.</summary>
    private static double Regularity(List<double> beats, double periodSeconds)
    {
        if (periodSeconds <= 0) return 0;

        var onGrid = 0;

        for (var i = 1; i < beats.Count; i++)
        {
            // Gaps may span more than one beat where a beat was missed, so measure against the
            // nearest whole multiple rather than against one period.
            var periods = (beats[i] - beats[i - 1]) / periodSeconds;
            var nearest = Math.Round(periods);
            if (nearest < 1) continue;

            if (Math.Abs(periods - nearest) / nearest <= GridTolerance) onGrid++;
        }

        return onGrid / (double)(beats.Count - 1);
    }

    private static Grid Empty => new(0, 0, 0, 0, 0);
}
