using System.Diagnostics;
using StreaMuse.State;

namespace StreaMuse.Sources;

/// <summary>`--probe`: dumps every render session and media session, with the app ids and pids
/// the machine actually reports. First stop when a source is not detected.</summary>
public static class SourceProbe
{
    public static async Task RunAsync()
    {
        var hub = new StateHub();

        Console.WriteLine("=== WASAPI render sessions (default output device) ===");
        foreach (var session in new AudioSessionScanner().Scan())
        {
            Console.WriteLine(
                $"  pid={session.ProcessId,-7} {session.ProcessName,-24} " +
                $"active={session.Active,-5} peak={session.Peak:F3}  \"{session.DisplayName}\"");
            Console.WriteLine($"      parent : {ParentDescription(session.ProcessId)}");
        }

        Console.WriteLine();
        Console.WriteLine("=== System media transport control sessions ===");

        var smtc = new SmtcMetadataService(hub);
        if (!await smtc.InitializeAsync())
        {
            Console.WriteLine("  (unavailable)");
            return;
        }

        foreach (var session in await smtc.ReadAllAsync(includeArtwork: true))
        {
            Console.WriteLine($"  appId  : {session.AppId}");
            Console.WriteLine($"    title : {session.Title}");
            Console.WriteLine($"    artist: {session.Artist}");
            Console.WriteLine($"    album : {session.Album}");
            Console.WriteLine(
                $"    state : playing={session.Playing} " +
                $"pos={session.PositionSeconds:F1}s dur={session.DurationSeconds:F1}s " +
                $"art={(session.Artwork is null ? "none" : session.Artwork.Length + " bytes")}");
        }
    }

    private static string ParentDescription(int pid)
    {
        var parents = ProcessTree.ParentMap();
        if (!parents.TryGetValue(pid, out var parent) || parent <= 0) return "unknown";

        var root = ProcessTree.RootOfSameName(pid, parents);

        try
        {
            using var process = Process.GetProcessById(parent);
            return $"pid={parent} {process.ProcessName}   (same-name root: pid={root})";
        }
        catch (Exception)
        {
            return $"pid={parent} (gone)   (same-name root: pid={root})";
        }
    }
}
