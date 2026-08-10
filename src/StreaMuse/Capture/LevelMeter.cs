namespace StreaMuse.Capture;

/// <summary>Reduces samples to the 34-bar meter the panel draws, plus a peak reading.</summary>
public sealed class LevelMeter(int bars = 34)
{
    private readonly float[] _bars = new float[bars];
    private readonly Lock _sync = new();

    private int _cursor;
    private float _windowPeak;
    private float _windowSum;
    private int _windowCount;
    private float _decayedPeak;

    /// <summary>Samples per bar - roughly 25 ms at 48 kHz stereo, so a full sweep is ~0.85 s.</summary>
    private const int SamplesPerBar = 48_000 * 2 / 40;

    public void Add(ReadOnlySpan<float> samples)
    {
        lock (_sync)
        {
            foreach (var sample in samples)
            {
                var magnitude = Math.Abs(sample);
                if (magnitude > _windowPeak) _windowPeak = magnitude;

                _windowSum += sample * sample;
                _windowCount++;

                if (_windowCount < SamplesPerBar) continue;

                var rms = MathF.Sqrt(_windowSum / _windowCount);
                _bars[_cursor] = rms;
                _cursor = (_cursor + 1) % _bars.Length;

                if (_windowPeak > _decayedPeak) _decayedPeak = _windowPeak;
                else _decayedPeak *= 0.92f;

                _windowPeak = 0;
                _windowSum = 0;
                _windowCount = 0;
            }
        }
    }

    /// <summary>Bar heights as percentages, oldest first. Peak is null rather than -Infinity when
    /// silent: System.Text.Json refuses to serialize infinity and the throw is swallowed.</summary>
    public (float[] Bars, double? PeakDb) Read()
    {
        lock (_sync)
        {
            var ordered = new float[_bars.Length];
            for (var i = 0; i < _bars.Length; i++)
            {
                var value = _bars[(_cursor + i) % _bars.Length];
                ordered[i] = (float)Math.Round(ToPercent(value), 1);
            }

            var peakDb = _decayedPeak <= 0.00001f ? (double?)null : 20 * Math.Log10(_decayedPeak);
            return (ordered, peakDb);
        }
    }

    public void Reset()
    {
        lock (_sync)
        {
            Array.Clear(_bars);
            _cursor = 0;
            _windowPeak = 0;
            _windowSum = 0;
            _windowCount = 0;
            _decayedPeak = 0;
        }
    }

    /// <summary>Maps linear amplitude onto 0-100 across a 60 dB window.</summary>
    private static double ToPercent(float amplitude)
    {
        if (amplitude <= 0.000_1f) return 4;

        var db = 20 * Math.Log10(amplitude);
        var normalized = (db + 60) / 60;
        return Math.Clamp(normalized * 100, 4, 100);
    }
}
