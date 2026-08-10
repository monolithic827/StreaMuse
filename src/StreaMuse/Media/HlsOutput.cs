using StreaMuse.Settings;

namespace StreaMuse.Media;

/// <summary>Manages the segment directory ffmpeg writes into.</summary>
public static class HlsOutput
{
    /// <summary>Clears the previous run so a reconnecting client cannot be handed stale segments.</summary>
    public static void Prepare()
    {
        Directory.CreateDirectory(Paths.HlsDir);

        foreach (var file in Directory.EnumerateFiles(Paths.HlsDir))
        {
            var extension = Path.GetExtension(file);
            if (!extension.Equals(".ts", StringComparison.OrdinalIgnoreCase) &&
                !extension.Equals(".m3u8", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            try
            {
                File.Delete(file);
            }
            catch (IOException)
            {
            }
        }
    }

    public static bool PlaylistExists => File.Exists(Path.Combine(Paths.HlsDir, "index.m3u8"));

    /// <summary>Delivered bitrate, measured from segment sizes because HLS reports bitrate=N/A.</summary>
    public static int MeasureBitrateKbps(int segmentSeconds = 1, int sampleCount = 4)
    {
        try
        {
            var segments = new DirectoryInfo(Paths.HlsDir)
                .EnumerateFiles("seg_*.ts")
                .OrderByDescending(f => f.Name)
                .Skip(1)                       // newest may still be being written
                .Take(sampleCount)
                .ToList();

            if (segments.Count == 0) return 0;

            var totalBits = segments.Sum(f => f.Length) * 8.0;
            var seconds = segments.Count * Math.Max(segmentSeconds, 1);
            return (int)Math.Round(totalBits / seconds / 1000);
        }
        catch (Exception)
        {
            return 0;
        }
    }
}
