using System.Diagnostics;
using System.Globalization;

namespace StreaMuse.DjAddon.Fetch;

/// <summary>Turns a free-text request into decoded PCM and the tags that go with it. Searches
/// music.youtube.com rather than youtube.com: the music service answers with track, artist and album
/// as separate fields and square cover art, where the video site gives a title like "… (Official Music
/// Video Remastered)" and a 16:9 screenshot. Downloading arbitrary tracks this way is against
/// YouTube's ToS - see README/CLAUDE.md.</summary>
public static class YtDlpFetcher
{
    /// <summary>Unlikely enough in a title to split on, since yt-dlp prints one line per field only if
    /// you ask for several --print arguments and their order is then harder to keep straight.</summary>
    private const string Separator = " |#| ";

    public sealed record Track(
        string Title,
        string Artist,
        string Album,
        double DurationSeconds,
        byte[]? Artwork,
        float[] Pcm);

    public static async Task<Track> FetchAsync(
        string ytDlpPath, string ffmpegPath, string query, string cacheDir, int sampleRate, int channels,
        CancellationToken ct)
    {
        Directory.CreateDirectory(cacheDir);
        var id = Guid.NewGuid().ToString("N");
        var outputTemplate = Path.Combine(cacheDir, id + ".%(ext)s");

        var tags = await RunYtDlpAsync(ytDlpPath, query, outputTemplate, ct);
        var downloaded = Directory.EnumerateFiles(cacheDir, id + ".*").FirstOrDefault();

        if (downloaded is null) throw new InvalidOperationException("yt-dlp did not produce an audio file");

        var rawPath = Path.Combine(cacheDir, id + ".raw");
        try
        {
            await DecodeToPcmAsync(ffmpegPath, downloaded, rawPath, sampleRate, channels, ct);

            var bytes = await File.ReadAllBytesAsync(rawPath, ct);
            var pcm = new float[bytes.Length / sizeof(float)];
            Buffer.BlockCopy(bytes, 0, pcm, 0, pcm.Length * sizeof(float));

            var artwork = await DownloadArtworkAsync(tags.ThumbnailUrl, ct);

            return new Track(tags.Title, tags.Artist, tags.Album, tags.Duration, artwork, pcm);
        }
        finally
        {
            TryDelete(downloaded);
            TryDelete(rawPath);
        }
    }

    private sealed record Tags(string Title, string Artist, string Album, double Duration, string? ThumbnailUrl);

    private static async Task<Tags> RunYtDlpAsync(
        string ytDlpPath, string query, string outputTemplate, CancellationToken ct)
    {
        var psi = new ProcessStartInfo(ytDlpPath)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        foreach (var arg in new[]
        {
            // --print implies --simulate on its own; without --no-simulate this prints the tags and
            // downloads nothing at all, which looks like success (exit 0) right up until the caller
            // finds no file on disk.
            "-f", "bestaudio", "--no-playlist", "--no-simulate", "--playlist-items", "1",
            "-o", outputTemplate,
            "--print",
            string.Join(Separator,
                "%(track,title)s", "%(artist,uploader)s", "%(album)s", "%(duration)s", "%(thumbnail)s"),
            "https://music.youtube.com/search?q=" + Uri.EscapeDataString(query)
        })
        {
            psi.ArgumentList.Add(arg);
        }

        using var process = Process.Start(psi) ?? throw new InvalidOperationException("could not start yt-dlp");
        var stdoutTask = process.StandardOutput.ReadToEndAsync(ct);
        var stderrTask = process.StandardError.ReadToEndAsync(ct);
        await process.WaitForExitAsync(ct);

        var stdout = await stdoutTask;
        var stderr = await stderrTask;

        if (process.ExitCode != 0) throw new InvalidOperationException($"yt-dlp failed: {stderr.Trim()}");

        var line = stdout
            .Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .LastOrDefault(l => l.Contains(Separator));

        if (line is null) return new Tags(query, "", "", 0, null);

        var fields = line.Split(Separator);
        return new Tags(
            Clean(fields.ElementAtOrDefault(0)) ?? query,
            Clean(fields.ElementAtOrDefault(1)) ?? "",
            Clean(fields.ElementAtOrDefault(2)) ?? "",
            double.TryParse(Clean(fields.ElementAtOrDefault(3)), CultureInfo.InvariantCulture, out var seconds)
                ? seconds
                : 0,
            Clean(fields.ElementAtOrDefault(4)));
    }

    /// <summary>yt-dlp writes the string "NA" for a field a video does not carry.</summary>
    private static string? Clean(string? field)
    {
        var trimmed = field?.Trim();
        return string.IsNullOrEmpty(trimmed) || trimmed == "NA" ? null : trimmed;
    }

    /// <summary>Cover art is a nicety, so a failure here costs the request nothing.</summary>
    private static async Task<byte[]?> DownloadArtworkAsync(string? url, CancellationToken ct)
    {
        if (url is null) return null;

        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
            return await http.GetByteArrayAsync(url, ct);
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static async Task DecodeToPcmAsync(
        string ffmpegPath, string input, string output, int sampleRate, int channels, CancellationToken ct)
    {
        var psi = new ProcessStartInfo(ffmpegPath)
        {
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        foreach (var arg in new[]
        {
            "-y", "-i", input,
            "-ar", sampleRate.ToString(CultureInfo.InvariantCulture),
            "-ac", channels.ToString(CultureInfo.InvariantCulture),
            "-f", "f32le", output
        })
        {
            psi.ArgumentList.Add(arg);
        }

        using var process = Process.Start(psi) ?? throw new InvalidOperationException("could not start ffmpeg");
        var stderr = await process.StandardError.ReadToEndAsync(ct);
        await process.WaitForExitAsync(ct);

        if (process.ExitCode != 0) throw new InvalidOperationException($"ffmpeg decode failed: {stderr.Trim()}");
    }

    private static void TryDelete(string? path)
    {
        if (path is null) return;

        try
        {
            if (File.Exists(path)) File.Delete(path);
        }
        catch (IOException)
        {
        }
    }
}
