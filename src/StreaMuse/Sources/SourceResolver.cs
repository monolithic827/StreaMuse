using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Sources;

public sealed record ResolvedSource(
    MusicSource EffectiveSource,
    int CaptureProcessId,
    string ProcessName,
    SmtcSession? Metadata,
    string StatusText)
{
    public bool Detected => CaptureProcessId > 0;

    public static ResolvedSource None(MusicSource source, string reason) => new(source, 0, "", null, reason);
}

public sealed record SourceAvailability(MusicSource Source, bool Available, string Reason);

/// <summary>Decides which process to capture. See CLAUDE.md for the selection rules.</summary>
public sealed class SourceResolver
{
    private static readonly string[] AppleProcessNames = ["applemusic", "itunes"];
    private static readonly string[] AppleAppIdHints = ["applemusic", "appleinc.applemusic", "itunes"];

    /// <summary>Apple Music renders through AMPLibraryAgent, so it is tried first. See CLAUDE.md.</summary>
    private static readonly string[] AppleAudioProcessNames = ["amplibraryagent", "applemusic", "itunes"];

    private static readonly string[] SpotifyProcessNames = ["spotify"];
    private static readonly string[] SpotifyAppIdHints = ["spotify"];

    private const int AutoReleaseAfterQuietPolls = 5;

    private int _autoTarget;
    private int _autoQuietPolls;

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
        IReadOnlyList<SmtcSession> mediaSessions,
        ProcessTree tree)
    {
        // Preference is left intact so installing the app later restores it.
        var source = Availability().First(a => a.Source == requested).Available
            ? requested
            : MusicSource.External;

        return source == MusicSource.External
            ? ResolveExternal(manualProcessId, audioSessions, mediaSessions, tree)
            : ResolveDedicatedApp(source, audioSessions, mediaSessions, tree);
    }

    private static ResolvedSource ResolveDedicatedApp(
        MusicSource source,
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions,
        ProcessTree tree)
    {
        var processNames = source == MusicSource.Apple ? AppleAudioProcessNames : SpotifyProcessNames;
        var appIdHints = source == MusicSource.Apple ? AppleAppIdHints : SpotifyAppIdHints;
        var label = source == MusicSource.Apple ? "Apple Music" : "Spotify";

        var rendering = processNames
            .Select(name => audioSessions.FirstOrDefault(
                s => s.ProcessName.Contains(name, StringComparison.OrdinalIgnoreCase)))
            .FirstOrDefault(s => s is not null);

        var pid = rendering?.ProcessId ?? FindProcessByNames(processNames);
        if (pid == 0) return ResolvedSource.None(source, $"{label} is no longer running");

        var root = tree.RootOfSameName(pid);
        var status = rendering is null ? $"{label} open but not playing · pid {root}"
            : rendering.Active ? $"Listening to {label} · pid {root}"
            : $"{label} attached but silent · pid {root}";

        return new ResolvedSource(
            source, root, rendering?.ProcessName ?? tree.NameOf(root) ?? "unknown",
            MatchByProcess(mediaSessions, root) ?? MatchByAppId(mediaSessions, appIdHints),
            status);
    }

    private ResolvedSource ResolveExternal(
        int manualProcessId,
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions,
        ProcessTree tree)
    {
        if (manualProcessId > 0 && tree.NameOf(manualProcessId) is null)
        {
            return ResolvedSource.None(
                MusicSource.External, $"Process {manualProcessId} is no longer running - pick another target");
        }

        var root = manualProcessId > 0 ? manualProcessId : ElectAutoTarget(audioSessions, mediaSessions, tree);
        if (root == 0)
        {
            return ResolvedSource.None(
                MusicSource.External, "Nothing is playing audio - start playback, then pick a target");
        }

        var session = audioSessions.FirstOrDefault(s => tree.RootOfSameName(s.ProcessId) == root);
        var metadata = MatchByProcess(mediaSessions, root);
        var label = ProcessIdentity.FriendlyName(root, session?.ProcessName ?? tree.NameOf(root) ?? "unknown");
        var title = metadata is null ? " · no track info reported" : $" - {metadata.Title}";
        var prefix = manualProcessId > 0 ? "Capturing" : "Auto ·";

        return new ResolvedSource(
            MusicSource.External, root, label, metadata, $"{prefix} {label} · pid {root}{title}");
    }

    /// <summary>Elects the auto capture target: SMTC-playing first, loudness only as fallback.</summary>
    private int ElectAutoTarget(
        IReadOnlyList<AudioSession> audioSessions,
        IReadOnlyList<SmtcSession> mediaSessions,
        ProcessTree tree)
    {
        var candidates = audioSessions
            .Where(s => !tree.IsSelfOrDescendant(s.ProcessId))
            .GroupBy(s => tree.RootOfSameName(s.ProcessId))
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

    public static IReadOnlyList<CaptureTarget> CaptureTargets(
        IReadOnlyList<AudioSession> audioSessions, ProcessTree tree) =>
        audioSessions
            .Where(s => !tree.IsSelfOrDescendant(s.ProcessId))
            .GroupBy(s => tree.RootOfSameName(s.ProcessId))
            .Select(g => new { Pid = g.Key, Session = g.OrderByDescending(s => s.Active).First() })
            .Select(x => new CaptureTarget(
                x.Pid, ProcessIdentity.FriendlyName(x.Pid, x.Session.ProcessName), x.Session.Active))
            .OrderByDescending(t => t.Active)
            .ThenBy(t => t.Name, StringComparer.OrdinalIgnoreCase)
            .ToList();

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
