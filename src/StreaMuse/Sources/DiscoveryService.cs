using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Sources;

/// <summary>Polls audio sessions and media metadata once a second; everything downstream reads
/// from what this publishes.</summary>
public sealed class DiscoveryService(
    AppSettings settings,
    StateHub hub,
    ArtworkStore artwork)
{
    private readonly AudioSessionScanner _scanner = new();
    private readonly SourceResolver _resolver = new();
    private readonly SmtcMetadataService _smtc = new(hub);

    private string _lastIdentity = "";
    private bool _retryArtwork;
    private ResolvedSource _current = ResolvedSource.None(MusicSource.External, "Starting up…");

    /// <summary>Latest resolution, read by the capture pipeline when it (re)starts.</summary>
    public ResolvedSource Current => _current;

    /// <summary>Raised when the capture target changes, so a running stream can retarget.</summary>
    public event Action<ResolvedSource>? TargetChanged;

    public async Task RunAsync(CancellationToken ct)
    {
        await _smtc.InitializeAsync();

        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(1));

        while (await SafeWaitAsync(timer, ct))
        {
            try
            {
                await PollAsync();
            }
            catch (Exception ex)
            {
                hub.Log(LineLevel.Warn, $"discovery poll failed: {ex.Message}");
            }
        }
    }

    private async Task PollAsync()
    {
        var audioSessions = _scanner.Scan();

        // Artwork is expensive; fetch it only when the track changes.
        var lightweight = await _smtc.ReadAllAsync(includeArtwork: false);
        var resolved = _resolver.Resolve(settings.Source, settings.ManualProcessId, audioSessions, lightweight);

        var identity = Identity(resolved.Metadata);
        if (identity != _lastIdentity || _retryArtwork)
        {
            var retrying = _retryArtwork;
            _lastIdentity = identity;

            var withArtwork = await _smtc.ReadAllAsync(includeArtwork: true);
            resolved = _resolver.Resolve(settings.Source, settings.ManualProcessId, audioSessions, withArtwork);

            // A thumbnail read often fails in the moment right after a track change. Hold the old
            // cover for one tick and try once more, rather than showing none for the whole track.
            _retryArtwork = !retrying && resolved.Metadata?.ArtworkFailed == true;

            if (!_retryArtwork && artwork.Set(resolved.Metadata?.Artwork) && resolved.Metadata is not null)
            {
                hub.Log(LineLevel.Info, $"now playing - {resolved.Metadata.Title} · {resolved.Metadata.Artist}");
            }
        }

        var previous = _current;
        _current = resolved;

        hub.SetSource(new SourceState(
            resolved.EffectiveSource.ToString().ToLowerInvariant(),
            resolved.Detected,
            resolved.Detected ? resolved.ProcessName : null,
            resolved.CaptureProcessId,
            resolved.StatusText,
            _resolver.Availability()
                .Select(a => new SourceOption(a.Source.ToString().ToLowerInvariant(), a.Available, a.Reason))
                .ToList(),
            SourceResolver.CaptureTargets(audioSessions)));

        hub.SetNowPlaying(ToNowPlaying(resolved.Metadata, artwork.Version));

        if (previous.CaptureProcessId != resolved.CaptureProcessId)
        {
            TargetChanged?.Invoke(resolved);
        }
    }

    private static NowPlaying ToNowPlaying(SmtcSession? session, long artworkVersion)
    {
        if (session is null || string.IsNullOrWhiteSpace(session.Title))
        {
            return new NowPlaying("Nothing playing", "-", "-", false, 0, 0, artworkVersion);
        }

        return new NowPlaying(
            session.Title,
            string.IsNullOrWhiteSpace(session.Artist) ? "-" : session.Artist,
            string.IsNullOrWhiteSpace(session.Album) ? "-" : session.Album,
            session.Playing,
            session.PositionSeconds,
            session.DurationSeconds,
            artworkVersion);
    }

    private static string Identity(SmtcSession? session) =>
        session is null ? "" : $"{session.AppId}|{session.Title}|{session.Artist}|{session.Album}";

    private static async Task<bool> SafeWaitAsync(PeriodicTimer timer, CancellationToken ct)
    {
        try
        {
            return await timer.WaitForNextTickAsync(ct);
        }
        catch (OperationCanceledException)
        {
            return false;
        }
    }
}
