namespace StreaMuse.Dj.Mixing;

public readonly record struct MixEnvelope(float LiveGain, float LiveBass, float TrackGain, float TrackBass);

/// <summary>The shape of the blend, as a function of how many beats into the transition we are.
///
/// A volume crossfade is not how a mix is done: run two tracks at half volume and you get both
/// basslines and both kicks at once, which is mud, and the moment the fader is mid-travel everything
/// sounds thin. What a DJ actually does is bring the new track in with its bass killed - so the
/// incoming drums and melody sit on top of the outgoing low end without fighting it - then swap the
/// bass across on a downbeat, which is the moment the mix "lands", and only then let the old track
/// go. Both records stay at full level throughout; the low end is what changes hands.
///
/// The whole envelope is measured in beats rather than seconds because the swap has to happen on a
/// beat to sound deliberate. Without a usable beat grid on the live side there is nothing to lock to,
/// so this degrades to an equal-power fade and says so (<paramref name="beatmatched"/> false).</summary>
public static class DjTransition
{
    public const double TransitionBeats = 16;

    /// <summary>Where the low end changes hands - halfway, so the incoming track has had 4 bars on top
    /// before it takes over.</summary>
    public const double SwapBeat = 8;

    /// <summary>The fader-up at the start. One beat, not instant: a hard start clicks.</summary>
    private const double LeadInBeats = 1;

    public static MixEnvelope Envelope(double beatsIn, double beatsLeft, bool beatmatched)
    {
        if (!beatmatched) return Fade(beatsIn, beatsLeft);

        // Coming to the end of the track takes precedence: hand everything back to the live source.
        if (beatsLeft <= TransitionBeats) return Outro(beatsLeft);
        if (beatsIn >= TransitionBeats) return new MixEnvelope(0, 0, 1, 1);

        var trackGain = (float)Clamp01(beatsIn / LeadInBeats);

        // Before the swap the incoming track is high-passed: it rides on top of the live low end.
        if (beatsIn < SwapBeat) return new MixEnvelope(1, 1, trackGain, 0);

        // After it, the bass belongs to the incoming track and the live source walks out.
        var handover = Clamp01((beatsIn - SwapBeat) / (TransitionBeats - SwapBeat));
        return new MixEnvelope((float)(1 - handover), 0, 1, 1);
    }

    private static MixEnvelope Outro(double beatsLeft)
    {
        // Mirror of the intro: live comes back over the top first, then takes the bass back.
        if (beatsLeft > SwapBeat)
        {
            var returning = Clamp01((TransitionBeats - beatsLeft) / (TransitionBeats - SwapBeat));
            return new MixEnvelope((float)returning, 0, 1, 1);
        }

        return new MixEnvelope(1, 1, (float)Clamp01(beatsLeft / SwapBeat), 0);
    }

    /// <summary>No beat grid to lock to, so there is nothing to swap on. Equal-power both ways, full
    /// range on both sides - an honest fade rather than a beatmatch that would land wrong.</summary>
    private static MixEnvelope Fade(double beatsIn, double beatsLeft)
    {
        var t = Clamp01(Math.Min(beatsIn / TransitionBeats, beatsLeft / TransitionBeats));
        var (live, track) = Crossfader.EqualPowerGains(t);
        return new MixEnvelope(live, 1, track, 1);
    }

    private static double Clamp01(double value) => Math.Clamp(value, 0, 1);
}
