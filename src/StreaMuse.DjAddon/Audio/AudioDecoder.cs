using System.Diagnostics;
using System.Globalization;

namespace StreaMuse.DjAddon.Audio;

/// <summary>Decodes any file ffmpeg understands to raw interleaved float32 PCM. Shared by
/// YtDlpFetcher (a downloaded track) and SfxLibrary (a dropped-in sound effect) - both just need
/// "arbitrary audio file in, PCM at the mix's rate out", so this is the one place that owns the
/// ffmpeg invocation rather than each caller shelling out its own copy.</summary>
public static class AudioDecoder
{
    public static async Task<float[]> DecodeAsync(
        string ffmpegPath, string inputPath, int sampleRate, int channels, CancellationToken ct)
    {
        var rawPath = Path.Combine(Path.GetTempPath(), $"streamuse-dj-{Guid.NewGuid():N}.raw");

        try
        {
            var psi = new ProcessStartInfo(ffmpegPath)
            {
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            foreach (var arg in new[]
            {
                "-y", "-i", inputPath,
                "-ar", sampleRate.ToString(CultureInfo.InvariantCulture),
                "-ac", channels.ToString(CultureInfo.InvariantCulture),
                "-f", "f32le", rawPath
            })
            {
                psi.ArgumentList.Add(arg);
            }

            using var process = Process.Start(psi) ?? throw new InvalidOperationException("could not start ffmpeg");
            var stderr = await process.StandardError.ReadToEndAsync(ct);
            await process.WaitForExitAsync(ct);

            if (process.ExitCode != 0) throw new InvalidOperationException($"ffmpeg decode failed: {stderr.Trim()}");

            var bytes = await File.ReadAllBytesAsync(rawPath, ct);
            var pcm = new float[bytes.Length / sizeof(float)];
            Buffer.BlockCopy(bytes, 0, pcm, 0, pcm.Length * sizeof(float));
            return pcm;
        }
        finally
        {
            try
            {
                if (File.Exists(rawPath)) File.Delete(rawPath);
            }
            catch (IOException)
            {
            }
        }
    }
}
