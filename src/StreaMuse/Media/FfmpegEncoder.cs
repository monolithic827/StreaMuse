using System.Diagnostics;
using System.Globalization;
using System.Text.RegularExpressions;
using StreaMuse.App;
using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Media;

/// <summary>Numbers scraped from ffmpeg's -progress stream.</summary>
public sealed record EncoderProgress(int BitrateKbps, long Frames, long DroppedFrames, double OutTimeSeconds);

/// <summary>Owns the ffmpeg child process muxing the two paced pipes into HLS.</summary>
public sealed partial class FfmpegEncoder(StateHub hub)
{
    private Process? _process;

    public string AudioPipeName { get; } = $"streamuse-audio-{Guid.NewGuid():N}";
    public string VideoPipeName { get; } = $"streamuse-video-{Guid.NewGuid():N}";

    public EncoderProgress Progress { get; private set; } = new(0, 0, 0, 0);

    public bool Running => _process is { HasExited: false };

    public event Action<int>? Exited;

    public void Start(string ffmpegPath, AppSettings settings, string outputDirectory)
    {
        var arguments = BuildArguments(settings, outputDirectory);
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

        foreach (var argument in arguments) info.ArgumentList.Add(argument);

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

    public IReadOnlyList<string> BuildArguments(AppSettings settings, string outputDirectory)
    {
        var audioPipe = $@"\\.\pipe\{AudioPipeName}";
        var videoPipe = $@"\\.\pipe\{VideoPipeName}";
        var fps = settings.Fps;

        return
        [
            "-hide_banner", "-nostdin",
            "-loglevel", "level+warning",

            // Probing must stay off: the default 5s probe starves the audio pipe (CLAUDE.md).
            "-analyzeduration", "0",
            "-probesize", "32",
            "-thread_queue_size", "1024",
            "-f", "f32le",
            "-ar", ProcessLoopbackSampleRate.ToString(CultureInfo.InvariantCulture),
            "-ac", "2",
            "-i", audioPipe,

            // probesize must still admit one whole JPEG.
            "-analyzeduration", "0",
            "-probesize", "5000000",
            "-thread_queue_size", "256",
            "-f", "image2pipe",
            "-framerate", fps.ToString(CultureInfo.InvariantCulture),
            "-i", videoPipe,

            "-map", "1:v:0", "-map", "0:a:0",

            // JPEG input is full-range and would otherwise leak out as yuvj420p.
            "-vf", "scale=in_range=full:out_range=tv,format=yuv420p",
            "-color_range", "tv",

            // Fixed GOP: one keyframe per second, matching -hls_time 1.
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "stillimage",
            "-profile:v", "main",
            "-level", "3.1",
            "-pix_fmt", "yuv420p",
            "-r", fps.ToString(CultureInfo.InvariantCulture),
            "-g", fps.ToString(CultureInfo.InvariantCulture),
            "-keyint_min", fps.ToString(CultureInfo.InvariantCulture),
            "-sc_threshold", "0",
            "-b:v", $"{settings.VideoBitrateKbps}k",
            "-maxrate", $"{(int)(settings.VideoBitrateKbps * 1.25)}k",
            "-bufsize", $"{settings.VideoBitrateKbps * 2}k",

            "-c:a", "aac",
            "-b:a", $"{settings.AudioBitrateKbps}k",
            "-ar", "48000",
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
        if (string.IsNullOrEmpty(e.Data)) return;

        var separator = e.Data.IndexOf('=');
        if (separator <= 0) return;

        var key = e.Data[..separator];
        var value = e.Data[(separator + 1)..];

        var current = Progress;

        Progress = key switch
        {
            // HLS reports N/A; the real figure comes from HlsOutput.MeasureBitrateKbps.
            "bitrate" => current with { BitrateKbps = ParseBitrate(value) },
            "frame" => current with { Frames = ParseLong(value, current.Frames) },
            "drop_frames" => current with { DroppedFrames = ParseLong(value, current.DroppedFrames) },
            "out_time_us" => current with
            {
                OutTimeSeconds = ParseLong(value, 0) / 1_000_000.0
            },
            _ => current
        };
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

    private const int ProcessLoopbackSampleRate = 48_000;

    /// <summary>ffmpeg reports bitrate as e.g. "4380.2kbits/s", or "N/A" before the first frame.</summary>
    private static int ParseBitrate(string value)
    {
        var match = BitrateRegex().Match(value);
        return match.Success && double.TryParse(match.Groups[1].Value, NumberStyles.Float,
            CultureInfo.InvariantCulture, out var parsed)
            ? (int)Math.Round(parsed)
            : 0;
    }

    private static long ParseLong(string value, long fallback) =>
        long.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : fallback;

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

    [GeneratedRegex(@"([\d.]+)\s*kbits/s")]
    private static partial Regex BitrateRegex();
}
