namespace StreaMuse.DjAddon.Mixing;

/// <summary>A second-order IIR section (RBJ cookbook coefficients), one filter state per channel.
/// Used to split a track into low and high bands so the two decks can trade the bass between them -
/// see DjTransition for why that trade is what makes a mix sound like a mix.</summary>
public sealed class Biquad
{
    private readonly double _b0, _b1, _b2, _a1, _a2;
    private readonly double[] _x1, _x2, _y1, _y2;

    private Biquad(double b0, double b1, double b2, double a1, double a2, int channels)
    {
        (_b0, _b1, _b2, _a1, _a2) = (b0, b1, b2, a1, a2);
        _x1 = new double[channels];
        _x2 = new double[channels];
        _y1 = new double[channels];
        _y2 = new double[channels];
    }

    public static Biquad LowPass(double sampleRate, double cutoffHz, double q, int channels)
    {
        var w0 = 2 * Math.PI * cutoffHz / sampleRate;
        var cos = Math.Cos(w0);
        var alpha = Math.Sin(w0) / (2 * q);

        var a0 = 1 + alpha;
        return new Biquad(
            (1 - cos) / 2 / a0,
            (1 - cos) / a0,
            (1 - cos) / 2 / a0,
            -2 * cos / a0,
            (1 - alpha) / a0,
            channels);
    }

    public static Biquad HighPass(double sampleRate, double cutoffHz, double q, int channels)
    {
        var w0 = 2 * Math.PI * cutoffHz / sampleRate;
        var cos = Math.Cos(w0);
        var alpha = Math.Sin(w0) / (2 * q);

        var a0 = 1 + alpha;
        return new Biquad(
            (1 + cos) / 2 / a0,
            -(1 + cos) / a0,
            (1 + cos) / 2 / a0,
            -2 * cos / a0,
            (1 - alpha) / a0,
            channels);
    }

    public float Process(int channel, float input)
    {
        var output = _b0 * input + _b1 * _x1[channel] + _b2 * _x2[channel]
                     - _a1 * _y1[channel] - _a2 * _y2[channel];

        _x2[channel] = _x1[channel];
        _x1[channel] = input;
        _y2[channel] = _y1[channel];
        _y1[channel] = output;

        return (float)output;
    }
}
