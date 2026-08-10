using System.Diagnostics;

namespace StreaMuse.Media;

/// <summary>The single time reference for a session. Pacing both tracks from it is what keeps
/// them in sync, since each demuxer timestamps by how much data it has received.</summary>
public sealed class StreamClock
{
    private readonly Stopwatch _stopwatch = new();

    public void Restart() => _stopwatch.Restart();

    public void Stop() => _stopwatch.Stop();

    public long ElapsedMilliseconds => _stopwatch.ElapsedMilliseconds;

    public TimeSpan Elapsed => _stopwatch.Elapsed;

    public bool Running => _stopwatch.IsRunning;
}
