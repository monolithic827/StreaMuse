using System.IO.Compression;
using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Deps;

public sealed record DependencyStatus(string Name, bool Present, string? Path, string? Detail);

/// <summary>
/// Resolves ffmpeg and cloudflared, downloading official release builds into
/// %LOCALAPPDATA%\StreaMuse\bin when they are not already on PATH or in that folder.
/// </summary>
public sealed class DependencyManager(StateHub hub)
{
    private const string FfmpegUrl =
        "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip";

    private const string CloudflaredUrl =
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe";

    private readonly SemaphoreSlim _gate = new(1, 1);

    public string? FfmpegPath { get; private set; }
    public string? CloudflaredPath { get; private set; }

    public IReadOnlyList<DependencyStatus> Snapshot() =>
    [
        new("ffmpeg", FfmpegPath is not null, FfmpegPath, FfmpegPath is null ? "not found" : null),
        new("cloudflared", CloudflaredPath is not null, CloudflaredPath, CloudflaredPath is null ? "not found" : null)
    ];

    /// <summary>Resolve both tools, downloading anything missing. Safe to call repeatedly.</summary>
    public async Task EnsureAllAsync(CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct);
        try
        {
            Directory.CreateDirectory(Paths.BinDir);
            FfmpegPath = await EnsureFfmpegAsync(ct);
            CloudflaredPath = await EnsureCloudflaredAsync(ct);
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task<string?> EnsureFfmpegAsync(CancellationToken ct)
    {
        var existing = Resolve("ffmpeg.exe");
        if (existing is not null) return existing;

        var target = Path.Combine(Paths.BinDir, "ffmpeg.exe");
        hub.Log(LineLevel.Info, "ffmpeg not found - downloading BtbN build (~100 MB)");

        var zip = Path.Combine(Path.GetTempPath(), $"streamuse-ffmpeg-{Guid.NewGuid():N}.zip");
        try
        {
            await DownloadAsync(FfmpegUrl, zip, "ffmpeg", ct);

            using (var archive = ZipFile.OpenRead(zip))
            {
                // The archive nests everything under ffmpeg-master-latest-win64-gpl/bin/.
                var entry = archive.Entries.FirstOrDefault(
                    e => e.FullName.EndsWith("bin/ffmpeg.exe", StringComparison.OrdinalIgnoreCase));

                if (entry is null)
                {
                    hub.Log(LineLevel.Error, "ffmpeg archive did not contain bin/ffmpeg.exe");
                    return null;
                }

                entry.ExtractToFile(target, overwrite: true);
            }

            hub.Log(LineLevel.Info, $"ffmpeg installed to {target}");
            return target;
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"ffmpeg download failed: {ex.Message}");
            return null;
        }
        finally
        {
            TryDelete(zip);
        }
    }

    private async Task<string?> EnsureCloudflaredAsync(CancellationToken ct)
    {
        var existing = Resolve("cloudflared.exe");
        if (existing is not null) return existing;

        var target = Path.Combine(Paths.BinDir, "cloudflared.exe");
        hub.Log(LineLevel.Info, "cloudflared not found - downloading");

        try
        {
            await DownloadAsync(CloudflaredUrl, target, "cloudflared", ct);
            hub.Log(LineLevel.Info, $"cloudflared installed to {target}");
            return target;
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"cloudflared download failed: {ex.Message}");
            TryDelete(target);
            return null;
        }
    }

    /// <summary>Look in our own bin folder first, then anywhere on PATH.</summary>
    private static string? Resolve(string exe)
    {
        var local = Path.Combine(Paths.BinDir, exe);
        if (File.Exists(local)) return local;

        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var candidate = Path.Combine(dir.Trim('"'), exe);
                if (File.Exists(candidate)) return candidate;
            }
            catch (ArgumentException)
            {
                // PATH can contain entries with invalid path characters; skip them.
            }
        }

        return null;
    }

    private async Task DownloadAsync(string url, string destination, string label, CancellationToken ct)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(15) };
        http.DefaultRequestHeaders.UserAgent.ParseAdd("StreaMuse/1.0");

        using var response = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, ct);
        response.EnsureSuccessStatusCode();

        var total = response.Content.Headers.ContentLength;
        var tmp = destination + ".part";

        await using (var src = await response.Content.ReadAsStreamAsync(ct))
        await using (var dst = File.Create(tmp))
        {
            var buffer = new byte[81920];
            long read = 0;
            var lastReport = -1;
            int n;

            while ((n = await src.ReadAsync(buffer, ct)) > 0)
            {
                await dst.WriteAsync(buffer.AsMemory(0, n), ct);
                read += n;

                if (total is > 0)
                {
                    var pct = (int)(read * 100 / total.Value);
                    if (pct >= lastReport + 10)
                    {
                        lastReport = pct - pct % 10;
                        hub.Log(LineLevel.Info, $"{label} download {lastReport}%");
                    }
                }
            }
        }

        File.Move(tmp, destination, overwrite: true);
    }

    private static void TryDelete(string path)
    {
        try
        {
            if (File.Exists(path)) File.Delete(path);
        }
        catch (IOException)
        {
        }
    }
}
