using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Sources;

/// <summary>The capture target plus the metadata session that describes it.</summary>
public sealed record ResolvedSource(
    MusicSource EffectiveSource,
    int CaptureProcessId,
    string ProcessName,
    bool Active,
    SmtcSession? Metadata,
    string StatusText)
{
    public bool Detected => CaptureProcessId > 0;

    public static ResolvedSource None(MusicSource source, string reason) =>
        new(source, 0, "", false, null, reason);
}

/// <summary>Whether each source can be selected right now, and why not when it cannot.</summary>
public sealed record SourceAvailability(MusicSource Source, bool Available, string Reason);

/// <summary>Decides which process to capture. See CLAUDE.md for the selection rules.</summary>
public sealed class SourceResolver
{
    private static readonly string[] AppleProcessNames = ["applemusic", "itunes"];
    private static readonly string[] AppleAppIdHints = ["applemusic", "appleinc.applemusic", "itunes"];

    private static readonly string[] SpotifyProcessNames = ["spotify"];
    private static readonly string[] SpotifyAppIdHints = ["spotify"];

    /// <summary>Apple/Spotify are offered only while their desktop app is running.</summary>
    public IReadOnlyList<SourceAvailability> Availability()
    {
        var apple = FindProcessByNames(AppleProcessNames);
        var spotify = FindProcessByNames(SpotifyProcessNames);

        return
        [
            new(MusicSource.Apple, apple > 0, apple > 0 ? "" : "Apple Music desktop app is not running"),
            new(MusicSource.Spotify, spotify > 0, spotify > 0 ? "" : "Spotify desktop app is not running"),
            new(MusicSource.External, true, "")
        ];
    }

    public ResolvedSource Resolve(
        MusicSource requested,
        int manualProcessId,
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions)
    {
        var parents = ProcessTree.ParentMap();

        // Preference is left intact so installing the app later restores it.
        var available = Availability();
        var source = available.First(a => a.Source == requested).Available
            ? requested
            : MusicSource.External;

        return source == MusicSource.External
            ? ResolveExternal(manualProcessId, audioSessions, mediaSessions, parents)
            : ResolveDedicatedApp(source, audioSessions, mediaSessions, parents);
    }

    private ResolvedSource ResolveDedicatedApp(
        MusicSource source,
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions,
        Dictionary<int, int> parents)
    {
        var processNames = source == MusicSource.Apple ? AppleProcessNames : SpotifyProcessNames;
        var appIdHints = source == MusicSource.Apple ? AppleAppIdHints : SpotifyAppIdHints;
        var label = source == MusicSource.Apple ? "Apple Music" : "Spotify";

        var rendering = audioSessions.FirstOrDefault(s => MatchesAny(s.ProcessName, processNames));
        if (rendering is not null)
        {
            var root = ProcessTree.RootOfSameName(rendering.ProcessId, parents);
            return new ResolvedSource(
                source, root, rendering.ProcessName, rendering.Active,
                MatchByProcess(mediaSessions, root) ?? MatchByAppId(mediaSessions, appIdHints),
                rendering.Active
                    ? $"Listening to {label} · pid {root}"
                    : $"{label} attached but silent · pid {root}");
        }

        var idle = FindProcessByNames(processNames);
        if (idle > 0)
        {
            var root = ProcessTree.RootOfSameName(idle, parents);
            return new ResolvedSource(
                source, root, AudioSessionScanner.ProcessNameOf(root), false,
                MatchByProcess(mediaSessions, root) ?? MatchByAppId(mediaSessions, appIdHints),
                $"{label} open but not playing · pid {root}");
        }

        return ResolvedSource.None(source, $"{label} is no longer running");
    }

    private ResolvedSource ResolveExternal(
        int manualProcessId,
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions,
        Dictionary<int, int> parents)
    {
        if (manualProcessId > 0)
        {
            var chosen = audioSessions.FirstOrDefault(
                s => ProcessTree.RootOfSameName(s.ProcessId, parents) == manualProcessId);

            var name = chosen?.ProcessName ?? AudioSessionScanner.ProcessNameOf(manualProcessId);
            var metadata = MatchByProcess(mediaSessions, manualProcessId);
            var label = ProcessIdentity.FriendlyName(manualProcessId) is { Length: > 0 } friendly
                ? friendly
                : name;

            var what = metadata is null
                ? " · no track info reported"
                : $" - {metadata.Title}";

            return new ResolvedSource(
                MusicSource.External, manualProcessId, label, chosen?.Active ?? false,
                metadata, $"Capturing {label} · pid {manualProcessId}{what}");
        }

        var root = ElectAutoTarget(audioSessions, mediaSessions, parents);

        if (root == 0)
        {
            return ResolvedSource.None(
                MusicSource.External, "Nothing is playing audio - start playback, then pick a target");
        }

        var elected = audioSessions.First(s => ProcessTree.RootOfSameName(s.ProcessId, parents) == root);
        var session = MatchByProcess(mediaSessions, root);
        var autoLabel = ProcessIdentity.FriendlyName(root) is { Length: > 0 } autoFriendly
            ? autoFriendly
            : elected.ProcessName;

        var title = session is null ? " · no track info reported" : $" - {session.Title}";

        return new ResolvedSource(
            MusicSource.External, root, autoLabel, elected.Active, session,
            $"Auto · {autoLabel} · pid {root}{title}");
    }

    /// <summary>Elects the auto capture target: SMTC-playing first, loudness only as fallback.</summary>
    private int ElectAutoTarget(
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions,
        Dictionary<int, int> parents)
    {
        var candidates = audioSessions
            .Where(s => !ProcessTree.IsSelfOrDescendant(s.ProcessId, parents))
            .GroupBy(s => ProcessTree.RootOfSameName(s.ProcessId, parents))
            .Select(g => new { Root = g.Key, Active = g.Any(s => s.Active), Peak = g.Max(s => s.Peak) })
            .ToList();

        if (candidates.Count == 0)
        {
            _autoTarget = 0;
            return 0;
        }

        var playingMedia = candidates
            .Where(c => mediaSessions.Any(m => m.Playing && ProcessIdentity.Matches(c.Root, m.AppId)))
            .ToList();

        if (playingMedia.Count > 0)
        {
            _autoQuietPolls = 0;

            // Stay put when the incumbent is one of them, so two media apps do not trade places.
            if (playingMedia.All(c => c.Root != _autoTarget))
            {
                _autoTarget = playingMedia
                    .OrderByDescending(c => c.Active)
                    .ThenByDescending(c => c.Peak)
                    .First().Root;
            }

            return _autoTarget;
        }

        var incumbent = candidates.FirstOrDefault(c => c.Root == _autoTarget);
        if (incumbent is not null)
        {
            if (incumbent.Active)
            {
                _autoQuietPolls = 0;
                return _autoTarget;
            }

            _autoQuietPolls++;
            if (_autoQuietPolls < AutoReleaseAfterQuietPolls || !candidates.Any(c => c.Active))
            {
                return _autoTarget;
            }
        }

        _autoTarget = candidates
            .OrderByDescending(c => c.Active)
            .ThenByDescending(c => c.Peak)
            .First().Root;

        _autoQuietPolls = 0;
        return _autoTarget;
    }

    private const int AutoReleaseAfterQuietPolls = 5;

    private int _autoTarget;
    private int _autoQuietPolls;

    /// <summary>Live audio sessions offered as capture targets in the UI.</summary>
    public static IReadOnlyList<CaptureTarget> CaptureTargets(IReadOnlyList<AudioSession> audioSessions)
    {
        var parents = ProcessTree.ParentMap();

        return audioSessions
            .Where(s => !ProcessTree.IsSelfOrDescendant(s.ProcessId, parents))
            .GroupBy(s => ProcessTree.RootOfSameName(s.ProcessId, parents))
            .Select(g => new { Pid = g.Key, Session = g.OrderByDescending(s => s.Active).First() })
            .Select(x => new CaptureTarget(
                x.Pid,
                ProcessIdentity.FriendlyName(x.Pid) is { Length: > 0 } friendly
                    ? friendly
                    : x.Session.ProcessName,
                x.Session.Active))
            .OrderByDescending(t => t.Active)
            .ThenBy(t => t.Name, StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    private static SmtcSession? MatchByAppId(IReadOnlyList<SmtcSession> sessions, string[] hints) =>
        sessions.FirstOrDefault(s => hints.Any(
            h => s.AppId.Contains(h, StringComparison.OrdinalIgnoreCase)));

    /// <summary>The media session belonging to the captured process, or none - never a guess.</summary>
    private static SmtcSession? MatchByProcess(IReadOnlyList<SmtcSession> sessions, int processId) =>
        sessions
            .Where(s => ProcessIdentity.Matches(processId, s.AppId))
            // A failed property read yields an empty title; prefer a sibling that still has one.
            .OrderByDescending(s => !string.IsNullOrWhiteSpace(s.Title))
            .ThenByDescending(s => s.Playing)
            .FirstOrDefault();

    private static bool MatchesAny(string processName, string[] candidates) =>
        candidates.Any(c => processName.Contains(c, StringComparison.OrdinalIgnoreCase));

    private static int FindProcessByNames(string[] names)
    {
        foreach (var name in names)
        {
            try
            {
                var found = System.Diagnostics.Process.GetProcessesByName(name);
                try
                {
                    if (found.Length > 0) return found[0].Id;
                }
                finally
                {
                    foreach (var p in found) p.Dispose();
                }
            }
            catch (Exception)
            {
            }
        }

        return 0;
    }
}
