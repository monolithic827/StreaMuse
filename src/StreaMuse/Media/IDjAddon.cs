namespace StreaMuse.Media;

/// <summary>The entire boundary between the main app and the optional DJ addon. The addon assembly
/// is loaded from plugins/ at runtime (see DjAddonHost) and never compiled against by anything but
/// this file, so nothing here may change in a way an already-built addon DLL couldn't tolerate
/// without a matching contract version.</summary>
public interface IDjAddon
{
    void Initialize(DjAddonContext context);

    Task<DjRequestResult> RequestAsync(string query, CancellationToken ct);

    void SkipCurrent();

    DjSnapshot Snapshot();

    /// <summary>Called from the capture callback with every batch of live samples; returns what
    /// actually reaches AudioPacer.Push. Runs on the capture thread, so it must never block on I/O -
    /// fetching and decoding happen ahead of time in RequestAsync, not here.</summary>
    float[] Mix(float[] liveSamples);

    /// <summary>Cover art for the track playing now, as the bytes the service returned. Served over
    /// HTTP rather than embedded in the snapshot, so it stays out of every state broadcast - see
    /// ArtworkStore for the same reasoning on the host side.</summary>
    byte[]? CurrentArtwork();

    /// <summary>Snapshot and artwork read together, for a caller - the video overlay - that needs both
    /// to describe the same track. Calling Snapshot() and CurrentArtwork() separately lets a transition
    /// land between the two reads and pair one track's title with another's cover for a frame.</summary>
    (DjSnapshot Snapshot, byte[]? Artwork) SnapshotWithArtwork();

    void Shutdown();
}

/// <summary>Everything the addon needs from the host, handed once at load time. Paths are Funcs
/// because ffmpeg/yt-dlp may still be downloading when the addon loads.</summary>
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

/// <summary>Config the addon reads live from AppSettings, copied out so the addon assembly never
/// needs a reference to the host's Settings project.</summary>
public sealed record DjAddonSettings(bool Enabled, double CrossfadeSeconds, bool SfxEnabled);

public sealed record DjRequestResult(bool Accepted, string? Error, DjQueueEntry? Entry);

public sealed record DjQueueEntry(string Id, string Query, string Title, string Artist, string Status);

/// <summary>Serializable state the panel renders. The host never sends this at all when no addon is
/// loaded, which is what makes the DJ card feature-detected rather than settings-detected.</summary>
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
