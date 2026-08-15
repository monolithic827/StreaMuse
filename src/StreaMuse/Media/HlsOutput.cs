using StreaMuse.Settings;

namespace StreaMuse.Media;

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

    /// <summary>Delivered bitrate over the last few 1 s segments, skipping the newest because
    /// ffmpeg may still be writing it. Measured from file sizes: HLS reports bitrate=N/A.</summary>
    public static int MeasureBitrateKbps()
    {
        try
        {
            var segments = new DirectoryInfo(Paths.HlsDir)
                .EnumerateFiles("seg_*.ts")
                .OrderByDescending(f => f.Name)
                .Skip(1)
                .Take(4)
                .ToList();

            if (segments.Count == 0) return 0;

            return (int)Math.Round(segments.Sum(f => f.Length) * 8.0 / segments.Count / 1000);
        }
        catch (Exception)
        {
            return 0;
        }
    }
}
