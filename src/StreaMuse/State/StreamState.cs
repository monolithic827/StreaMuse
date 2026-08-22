using System.Text.Json.Serialization;

namespace StreaMuse.State;

/// <summary>Named LineLevel rather than LogLevel to stay clear of Microsoft.Extensions.Logging.</summary>
public enum LineLevel
{
    Info,
    Warn,
    Error
}

public sealed record LogLine(string Time, string Level, string Message);

public sealed record NowPlaying(
    string Title,
    string Artist,
    string Album,
    bool Playing,
    double PositionSeconds,
    double DurationSeconds,
    long ArtworkVersion);

public sealed record SourceOption(string Source, bool Available, string Reason);

public sealed record CaptureTarget(int Pid, string Name, bool Active);

public sealed record SourceState(
    // The source actually in use. Differs from the stored preference when that app is not running.
    string Source,
    bool Detected,
    string? ProcessName,
    int ProcessId,
    string StatusText,
    IReadOnlyList<SourceOption> Options,
    IReadOnlyList<CaptureTarget> Targets);

public sealed record EncoderState(
    string Status,
    int BitrateKbps,
    int Fps,
    long DroppedFrames,
    double UptimeSeconds,
    string? Error);

public sealed record TunnelState(
    string Status,
    string? PublicUrl,
    string? Error);

public sealed record DependencyView(string Name, string? Path)
{
    public bool Present => Path is not null;
}

/// <summary>Immutable snapshot sent to the UI. Level meter and log lines ship as separate messages.</summary>
public sealed record StateSnapshot(
    SourceState Source,
    NowPlaying NowPlaying,
    EncoderState Encoder,
    TunnelState Tunnel,
    IReadOnlyList<DependencyView> Dependencies,
    IReadOnlyList<LogLine> Log,
    string? LocalUrl,
    object Settings)
{
    [JsonPropertyName("type")]
    public string Type => "state";
}

public static class StreamStatus
{
    public const string Idle = "idle";
    public const string Starting = "starting";
    public const string Running = "running";
    public const string Error = "error";
}

public static class TunnelStatus
{
    public const string Off = "off";
    public const string Starting = "starting";
    public const string Up = "up";
    public const string Error = "error";
}
