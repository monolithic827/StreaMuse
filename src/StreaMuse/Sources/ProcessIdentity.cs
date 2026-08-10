using System.Collections.Concurrent;
using System.Diagnostics;

namespace StreaMuse.Sources;

/// <summary>
/// Decides whether a media session belongs to a process. App ids and executables name apps
/// differently (Helium runs as chrome.exe), so version resources bridge them. See CLAUDE.md.
/// </summary>
public static class ProcessIdentity
{
    private sealed record Identity(string Name, string Product, string Description);

    // Keyed by process instance, not pid: Windows recycles pids, and a stale entry would bind one
    // app's SMTC metadata to whatever inherited its pid.
    private static readonly ConcurrentDictionary<(int Pid, long Started), Identity> Cache = new();

    /// <summary>Shortest token length worth comparing; below this, matches are coincidence.</summary>
    private const int MinTokenLength = 3;

    public static bool Matches(int processId, string appId)
    {
        if (processId <= 0 || string.IsNullOrWhiteSpace(appId)) return false;

        var identity = Describe(processId);
        var candidates = new[] { identity.Name, identity.Product, identity.Description };
        var tokens = AppIdTokens(appId);

        foreach (var candidate in candidates)
        {
            var cleaned = Clean(candidate);
            if (cleaned.Length < MinTokenLength) continue;

            foreach (var token in tokens)
            {
                if (token.Length < MinTokenLength) continue;

                // Either direction: "Chrome" vs "Google Chrome", "Spotify.exe" vs "spotify".
                if (token.Contains(cleaned, StringComparison.OrdinalIgnoreCase) ||
                    cleaned.Contains(token, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
        }

        return false;
    }

    /// <summary>A human-readable name for the process, preferring its product name.</summary>
    public static string FriendlyName(int processId)
    {
        var identity = Describe(processId);
        return !string.IsNullOrWhiteSpace(identity.Product) ? identity.Product : identity.Name;
    }

    private static Identity Describe(int processId)
    {
        try
        {
            using var process = Process.GetProcessById(processId);
            return Cache.GetOrAdd((processId, process.StartTime.Ticks), _ => Read(process));
        }
        catch (Exception)
        {
            // Not cached: a process we cannot open now may be readable on the next poll.
            return new Identity("", "", "");
        }
    }

    private static Identity Read(Process process)
    {
        try
        {
            var info = process.MainModule?.FileVersionInfo;
            return new Identity(process.ProcessName, info?.ProductName ?? "", info?.FileDescription ?? "");
        }
        catch (Exception)
        {
            // MainModule throws for higher-integrity processes; the name still helps.
            return new Identity(process.ProcessName, "", "");
        }
    }

    /// <summary>Splits Family_hash!AppId or Helium.HASH style ids into comparable pieces.</summary>
    private static string[] AppIdTokens(string appId)
    {
        var trimmed = appId.Trim();
        var head = trimmed.Split('!')[0].Split('_')[0];

        return
        [
            Clean(trimmed),
            Clean(head),
            Clean(head.Split('.')[0])
        ];
    }

    private static string Clean(string value) =>
        value.Replace(".exe", "", StringComparison.OrdinalIgnoreCase).Trim();
}
