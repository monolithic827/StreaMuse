namespace StreaMuse.Media;

/// <summary>Everything the DJ mixer needs from the rest of the app, handed once at startup. Paths are
/// Funcs because ffmpeg/yt-dlp may still be downloading when the mixer is constructed.</summary>
public sealed class DjAddonContext(
    int sampleRate,
    int channels,
    string dataDir,
    Func<string?> ffmpegPath,
    Func<string?> ytDlpPath,
    Action<string> logInfo,
    Action<string> logWarn,
    Action<string> logError,
    Func<DjAddonSettings> readSettings,
    Action stateChanged,
    Func<Task<bool>> pauseSource,
    Func<Task<bool>> resumeSource,
    string sfxDir)
{
    public int SampleRate { get; } = sampleRate;
    public int Channels { get; } = channels;
    public string DataDir { get; } = dataDir;
    public Func<string?> FfmpegPath { get; } = ffmpegPath;
    public Func<string?> YtDlpPath { get; } = ytDlpPath;
    public Action<string> LogInfo { get; } = logInfo;
    public Action<string> LogWarn { get; } = logWarn;
    public Action<string> LogError { get; } = logError;
    public Func<DjAddonSettings> ReadSettings { get; } = readSettings;

    /// <summary>The addon calls this after anything that changes Snapshot() so the host can
    /// rebroadcast state without the panel having to poll for it.</summary>
    public Action StateChanged { get; } = stateChanged;

    /// <summary>Pauses/resumes the captured app - a no-op returning false for any source but Apple
    /// Music or Spotify, where "the app" is unambiguous. See DiscoveryService.TryPauseSourceAsync.</summary>
    public Func<Task<bool>> PauseSource { get; } = pauseSource;

    public Func<Task<bool>> ResumeSource { get; } = resumeSource;

    /// <summary>Folder the addon scans for its own drop-in sound effect files.</summary>
    public string SfxDir { get; } = sfxDir;
}

/// <summary>Config the mixer reads live from AppSettings, copied out to keep DjAddon itself free of a
/// direct AppSettings dependency.</summary>
public sealed record DjAddonSettings(bool Enabled, double CrossfadeSeconds, bool SfxEnabled);

public sealed record DjRequestResult(bool Accepted, string? Error, DjQueueEntry? Entry);

public sealed record DjQueueEntry(string Id, string Query, string Title, string Artist, string Status);

/// <summary>Serializable state the panel renders. Null while DJ mixing is disabled in Settings, which
/// is what makes the DJ card feature-detected rather than always shown.</summary>
public sealed record DjSnapshot(
    IReadOnlyList<DjQueueEntry> Queue,
    DjQueueEntry? NowMixing,
    string PhaseText,
    double? ConfidencePercent,
    string Album,
    double PositionSeconds,
    double DurationSeconds,
    /// <summary>Content-derived and forced odd, so it doubles as the cache key in /api/dj/art?v= and
    /// can never be 0, which the panel reads as "no artwork". Same rule as ArtworkStore.Version, and
    /// for the same reason: a counter restarting each run serves the previous run's covers.</summary>
    long ArtworkVersion);
