using System.Diagnostics;
using System.Globalization;
using StreaMuse.App;
using StreaMuse.Capture;
using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Media;

public sealed class FfmpegEncoder(StateHub hub)
{
    private Process? _process;

    public string AudioPipeName { get; } = $"streamuse-audio-{Guid.NewGuid():N}";
    public string VideoPipeName { get; } = $"streamuse-video-{Guid.NewGuid():N}";

    /// <summary>From ffmpeg's -progress stream. Bitrate is not read from there: the HLS muxer
    /// reports N/A, so HlsOutput measures it from segment sizes instead.</summary>
    public long DroppedFrames { get; private set; }

    public event Action<int>? Exited;

    public void Start(string ffmpegPath, AppSettings settings, string outputDirectory)
    {
        hub.Log(LineLevel.Info, $"encoder starting - {settings.Width}x{settings.Height} @ {settings.Fps} fps, " +
                                $"v {settings.VideoBitrateKbps}k / a {settings.AudioBitrateKbps}k");

        var info = new ProcessStartInfo(ffmpegPath)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = false,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        foreach (var argument in BuildArguments(settings, outputDirectory)) info.ArgumentList.Add(argument);

        var process = new Process { StartInfo = info, EnableRaisingEvents = true };
        process.OutputDataReceived += OnProgressLine;
        process.ErrorDataReceived += OnLogLine;
        process.Exited += (_, _) =>
        {
            var code = SafeExitCode(process);
            hub.Log(code == 0 ? LineLevel.Info : LineLevel.Warn, $"encoder exited with code {code}");
            Exited?.Invoke(code);
        };

        process.Start();
        ChildProcessJob.Adopt(process);
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        _process = process;
    }

    private IReadOnlyList<string> BuildArguments(AppSettings settings, string outputDirectory)
    {
        var fps = settings.Fps.ToString(CultureInfo.InvariantCulture);
        var sampleRate = ProcessLoopbackCapture.SampleRate.ToString(CultureInfo.InvariantCulture);

        return
        [
            "-hide_banner", "-nostdin",
            "-loglevel", "level+warning",

            "-analyzeduration", "0",
            "-probesize", "32",
            "-thread_queue_size", "1024",
            "-f", "f32le",
            "-ar", sampleRate,
            "-ac", "2",
            "-i", $@"\\.\pipe\{AudioPipeName}",

            // probesize must still admit one whole JPEG.
            "-analyzeduration", "0",
            "-probesize", "5000000",
            "-thread_queue_size", "256",
            "-f", "image2pipe",
            "-framerate", fps,
            "-i", $@"\\.\pipe\{VideoPipeName}",

            "-map", "1:v:0", "-map", "0:a:0",

            "-vf", "scale=in_range=full:out_range=tv,format=yuv420p",
            "-color_range", "tv",

            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "stillimage",
            "-profile:v", "main",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-r", fps,
            "-g", fps,
            "-keyint_min", fps,
            "-sc_threshold", "0",
            "-b:v", $"{settings.VideoBitrateKbps}k",
            "-maxrate", $"{(int)(settings.VideoBitrateKbps * 1.25)}k",
            "-bufsize", $"{settings.VideoBitrateKbps * 2}k",

            "-c:a", "aac",
            "-b:a", $"{settings.AudioBitrateKbps}k",
            "-ar", sampleRate,
            "-ac", "2",

            "-fps_mode", "cfr",

            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "6",
            "-hls_delete_threshold", "2",
            "-hls_segment_type", "mpegts",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist+program_date_time",
            "-hls_segment_filename", Path.Combine(outputDirectory, "seg_%06d.ts"),
            Path.Combine(outputDirectory, "index.m3u8"),

            "-progress", "pipe:1",
            "-stats_period", "1"
        ];
    }

    public void Stop()
    {
        var process = Interlocked.Exchange(ref _process, null);
        if (process is null) return;

        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                process.WaitForExit(3000);
            }
        }
        catch (Exception)
        {
        }
        finally
        {
            process.Dispose();
        }
    }

    private void OnProgressLine(object? sender, DataReceivedEventArgs e)
    {
        if (e.Data is null || !e.Data.StartsWith("drop_frames=", StringComparison.Ordinal)) return;

        if (long.TryParse(e.Data.AsSpan("drop_frames=".Length), NumberStyles.Integer,
                CultureInfo.InvariantCulture, out var dropped))
        {
            DroppedFrames = dropped;
        }
    }

    private void OnLogLine(object? sender, DataReceivedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(e.Data)) return;

        var line = e.Data.Trim();

        // Range is handled in the filter chain; this warning is noise on every start.
        if (line.Contains("deprecated pixel format used", StringComparison.OrdinalIgnoreCase)) return;

        var level = line.Contains("[error]", StringComparison.OrdinalIgnoreCase) ||
                    line.Contains("[fatal]", StringComparison.OrdinalIgnoreCase)
            ? LineLevel.Error
            : LineLevel.Warn;

        hub.Log(level, $"ffmpeg: {Shorten(line)}");
    }

    private static int SafeExitCode(Process process)
    {
        try
        {
            return process.ExitCode;
        }
        catch (Exception)
        {
            return -1;
        }
    }

    private static string Shorten(string line) => line.Length <= 200 ? line : line[..200] + "…";
}
