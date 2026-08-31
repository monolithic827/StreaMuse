namespace StreaMuse.DjAddon.Mixing;

/// <summary>Equal-power gain law: at t=0.5 both signals are attenuated by ~3dB rather than 6dB, so
/// the perceived loudness stays roughly constant through the middle of the fade.</summary>
public static class Crossfader
{
    public static (float Live, float Track) EqualPowerGains(double t)
    {
        t = Math.Clamp(t, 0, 1);

        // The endpoints are special-cased rather than left to Cos/Sin, which can never land on an
        // exact zero here - Math.Cos(Math.PI/2) is ~6.12e-17, not 0, since pi/2 has no exact binary
        // representation. That residue survives the cast to float. It rounds away to "0.000" in any
        // display or log, which is exactly what let it hide: DjAddon.Mix checks `LiveGain <= 0f` to
        // decide the live source is genuinely unused and safe to pause (see CLAUDE.md) - a tiny
        // positive residue means that check is never true, so Apple Music/Spotify was never paused
        // through this path. The beatmatched envelope in DjTransition was unaffected only because it
        // returns literal 0/1, not a trig result - that was luck, not a property of this function.
        if (t <= 0) return (1f, 0f);
        if (t >= 1) return (0f, 1f);

        var angle = t * Math.PI / 2;
        return ((float)Math.Cos(angle), (float)Math.Sin(angle));
    }
}
