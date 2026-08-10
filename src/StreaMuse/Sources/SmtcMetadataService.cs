using System.Runtime.InteropServices.WindowsRuntime;
using StreaMuse.State;
using Windows.Media.Control;
using Windows.Storage.Streams;

namespace StreaMuse.Sources;

/// <summary>What one app is reporting to the Windows media transport controls.</summary>
public sealed record SmtcSession(
    string AppId,
    string Title,
    string Artist,
    string Album,
    bool Playing,
    double PositionSeconds,
    double DurationSeconds,
    byte[]? Artwork,
    bool ArtworkFailed);

/// <summary>Now-playing metadata from the Windows media transport controls.</summary>
public sealed class SmtcMetadataService(StateHub hub)
{
    private GlobalSystemMediaTransportControlsSessionManager? _manager;

    public async Task<bool> InitializeAsync()
    {
        try
        {
            _manager = await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
            return true;
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"media transport controls unavailable: {ex.Message}");
            return false;
        }
    }

    /// <summary>All sessions currently registered with the system, newest artwork included.</summary>
    public async Task<IReadOnlyList<SmtcSession>> ReadAllAsync(bool includeArtwork)
    {
        if (_manager is null) return [];

        var results = new List<SmtcSession>();

        try
        {
            foreach (var session in _manager.GetSessions())
            {
                var read = await ReadOneAsync(session, includeArtwork);
                if (read is not null) results.Add(read);
            }
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Warn, $"could not read media sessions: {ex.Message}");
        }

        return results;
    }

    private static async Task<SmtcSession?> ReadOneAsync(
        GlobalSystemMediaTransportControlsSession session, bool includeArtwork)
    {
        try
        {
            var props = await session.TryGetMediaPropertiesAsync();
            var playback = session.GetPlaybackInfo();
            var timeline = session.GetTimelineProperties();

            byte[]? artwork = null;
            var wanted = includeArtwork && props?.Thumbnail is not null;
            if (wanted)
            {
                artwork = await ReadThumbnailAsync(props!.Thumbnail);
            }

            var playing = playback?.PlaybackStatus ==
                          GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing;

            var appId = session.SourceAppUserModelId ?? "";
            var title = string.IsNullOrWhiteSpace(props?.Title) ? "" : props!.Title;

            return new SmtcSession(
                appId,
                title,
                props?.Artist ?? "",
                props?.AlbumTitle ?? "",
                playing,
                PositionSeconds(timeline, playing, playback?.PlaybackRate),
                (timeline.EndTime - timeline.StartTime).TotalSeconds,
                artwork,
                wanted && artwork is null);
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>Position advanced to now. Position is pushed only on play/pause/seek, so it must
    /// be extrapolated from LastUpdatedTime or the progress bar sits frozen.</summary>
    private static double PositionSeconds(
        GlobalSystemMediaTransportControlsSessionTimelineProperties timeline,
        bool playing,
        double? playbackRate)
    {
        var position = timeline.Position - timeline.StartTime;

        if (playing && timeline.LastUpdatedTime > DateTimeOffset.MinValue)
        {
            var since = DateTimeOffset.UtcNow - timeline.LastUpdatedTime;

            // An implausible gap means a stale or skewed timestamp; a stalled bar beats a wrong one.
            if (since > TimeSpan.Zero && since < TimeSpan.FromHours(1))
            {
                position += since * (playbackRate is > 0 ? playbackRate.Value : 1.0);
            }
        }

        var duration = timeline.EndTime - timeline.StartTime;
        if (duration > TimeSpan.Zero && position > duration) position = duration;

        return position > TimeSpan.Zero ? position.TotalSeconds : 0;
    }

    private static async Task<byte[]?> ReadThumbnailAsync(IRandomAccessStreamReference reference)
    {
        try
        {
            using var stream = await reference.OpenReadAsync();
            if (stream.Size == 0 || stream.Size > 16 * 1024 * 1024) return null;

            var buffer = new Windows.Storage.Streams.Buffer((uint)stream.Size);
            await stream.ReadAsync(buffer, (uint)stream.Size, InputStreamOptions.None);
            return buffer.ToArray();
        }
        catch (Exception)
        {
            return null;
        }
    }
}
