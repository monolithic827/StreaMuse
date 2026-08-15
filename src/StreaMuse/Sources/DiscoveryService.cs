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
    private readonly SourceResolver _resolver = new();
    private readonly SmtcMetadataService _smtc = new(hub);

    /// <summary>How long a track's artwork is chased for, in polls, before giving up on it.</summary>
    private const int ArtworkPolls = 8;

    private string _lastIdentity = "";
    private int _artworkPolls;
    private ResolvedSource _current = ResolvedSource.None(MusicSource.External, "Starting up…");

    public ResolvedSource Current => _current;

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
        var tree = ProcessTree.Snapshot();
        var audioSessions = AudioSessionScanner.Scan(tree);

        // Artwork is expensive, so it is read only while a track change is being chased.
        var mediaSessions = await _smtc.ReadAllAsync(includeArtwork: _artworkPolls > 0);
        var resolved = _resolver.Resolve(settings.Source, settings.ManualProcessId, audioSessions, mediaSessions, tree);

        var identity = Identity(resolved.Metadata);
        var changed = identity != _lastIdentity;
        if (changed)
        {
            _lastIdentity = identity;

            if (_artworkPolls == 0)
            {
                mediaSessions = await _smtc.ReadAllAsync(includeArtwork: true);
                resolved = _resolver.Resolve(settings.Source, settings.ManualProcessId, audioSessions, mediaSessions, tree);
            }

            _artworkPolls = ArtworkPolls;
        }

        if (_artworkPolls > 0)
        {
            var found = resolved.Metadata?.Artwork;
            var replaced = (found is not null || changed) && artwork.Set(found);

            if (replaced && found is not null)
            {
                hub.Log(LineLevel.Info, $"now playing - {resolved.Metadata!.Title} · {resolved.Metadata.Artist}");
            }

            _artworkPolls = found is null ? _artworkPolls - 1 : 0;
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
            SourceResolver.CaptureTargets(audioSessions, tree)));

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
