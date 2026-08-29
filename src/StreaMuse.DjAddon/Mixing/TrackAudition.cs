namespace StreaMuse.DjAddon.Mixing;

/// <summary>The pre-listen a DJ does in headphones before the crowd hears anything: check the fetched
/// track is actually playable, find where the music really starts, and refuse it outright if it isn't
/// worth playing. Without this a silent or near-silent download - a video whose audio track is empty,
/// a failed fetch that still produced a file - would crossfade the live source out into nothing, and
/// the first sign of trouble would be dead air on the stream.</summary>
public static class TrackAudition
{
    /// <summary>Below this peak the track is treated as unplayable rather than merely quiet.</summary>
    private const double MinPeakDb = -45;

    private const double MinSeconds = 5;

    /// <summary>Leading audio is skipped until it reaches this fraction of the track's own peak, so
    /// the mix starts on the music rather than on an intro of near-silence.</summary>
    private const double StartThresholdFraction = 0.08;

    public sealed record Result(
        bool Ok,
        string Reason,
        int StartSample,
        double PeakDb,
        double DurationSeconds);

    public static Result Audition(float[] pcm, int sampleRate, int channels)
    {
        var duration = pcm.Length / (double)(sampleRate * channels);
        if (duration < MinSeconds)
        {
            return new Result(false, $"only {duration:F1}s of audio - too short to mix", 0, double.NegativeInfinity, duration);
        }

        var peak = 0f;
        foreach (var sample in pcm)
        {
            var magnitude = Math.Abs(sample);
            if (magnitude > peak) peak = magnitude;
        }

        var peakDb = peak > 0 ? 20 * Math.Log10(peak) : double.NegativeInfinity;
        if (peakDb < MinPeakDb)
        {
            return new Result(false, $"silent download (peak {Describe(peakDb)})", 0, peakDb, duration);
        }

        return new Result(true, "ok", FindStart(pcm, channels, (float)(peak * StartThresholdFraction)), peakDb, duration);
    }

    /// <summary>First frame whose short window rises above the threshold, rounded down to a frame
    /// boundary so the returned index never splits a stereo pair.</summary>
    private static int FindStart(float[] pcm, int channels, float threshold)
    {
        for (var i = 0; i < pcm.Length; i++)
        {
            if (Math.Abs(pcm[i]) < threshold) continue;
            return i / channels * channels;
        }

        return 0;
    }

    private static string Describe(double db) => double.IsNegativeInfinity(db) ? "digital silence" : $"{db:F1} dB";
}
