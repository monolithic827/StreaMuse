namespace StreaMuse.DjAddon.Mixing;

/// <summary>Equal-power gain law: at t=0.5 both signals are attenuated by ~3dB rather than 6dB, so
/// the perceived loudness stays roughly constant through the middle of the fade.</summary>
public static class Crossfader
{
    public static (float Live, float Track) EqualPowerGains(double t)
    {
        t = Math.Clamp(t, 0, 1);
        var angle = t * Math.PI / 2;
        return ((float)Math.Cos(angle), (float)Math.Sin(angle));
    }
}
