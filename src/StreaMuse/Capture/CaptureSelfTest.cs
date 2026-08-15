using System.Diagnostics;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;

namespace StreaMuse.Capture;

/// <summary>`--test-capture [seconds]`: records the resolved source to WAV through the pacer and
/// reports peak, filled silence and drift. Catches silent captures and mis-pacing early.</summary>
public static class CaptureSelfTest
{
    public static async Task RunAsync(string[] args)
    {
        var seconds = 10;
        var index = Array.IndexOf(args, "--test-capture");
        if (index >= 0 && index + 1 < args.Length && int.TryParse(args[index + 1], out var parsed))
        {
            seconds = Math.Clamp(parsed, 2, 600);
        }

        var hub = new StateHub();
        var settings = AppSettings.Load();
        var artwork = new ArtworkStore();
        var discovery = new DiscoveryService(settings, hub, artwork);

        using var cts = new CancellationTokenSource();
        var discoveryTask = Task.Run(() => discovery.RunAsync(cts.Token));

        Console.WriteLine($"Resolving source ({settings.Source})…");
        await Task.Delay(1500);

        var resolved = discovery.Current;
        Console.WriteLine($"  target : {resolved.StatusText}");

        if (!resolved.Detected)
        {
            Console.WriteLine("  no capture target - start playback and try again");
            await cts.CancelAsync();
            return;
        }

        Console.WriteLine($"  pid    : {resolved.CaptureProcessId} ({resolved.ProcessName})");
        Console.WriteLine();

        var pacer = new AudioPacer();
        var meter = new LevelMeter();
        using var capture = new ProcessLoopbackCapture(hub);

        capture.SamplesAvailable += samples =>
        {
            meter.Add(samples);
            pacer.Push(samples);
        };

        if (!await capture.StartAsync(resolved.CaptureProcessId))
        {
            await cts.CancelAsync();
            return;
        }

        var output = Path.Combine(Paths.DataDir, "capture-test.wav");
        Console.WriteLine($"Recording {seconds}s to {output} …");

        var clock = Stopwatch.StartNew();

        await using (var wav = new WavWriter(output, AudioPacer.SampleRate, AudioPacer.Channels))
        {
            using var recordCts = new CancellationTokenSource(TimeSpan.FromSeconds(seconds));
            await pacer.RunAsync(wav.Stream, clock, recordCts.Token);
        }

        clock.Stop();
        capture.Stop();
        await cts.CancelAsync();

        try
        {
            await discoveryTask;
        }
        catch (OperationCanceledException)
        {
        }

        var writtenSeconds = pacer.FramesWritten / (double)AudioPacer.SampleRate;
        var silentSeconds = pacer.SilenceFrames / (double)AudioPacer.SampleRate;
        var (_, peakDb) = meter.Read();

        Console.WriteLine();
        Console.WriteLine("=== result ===");
        Console.WriteLine($"  wall clock     : {clock.Elapsed.TotalSeconds:F2}s");
        Console.WriteLine($"  audio written  : {writtenSeconds:F2}s");
        Console.WriteLine($"  drift          : {(writtenSeconds - clock.Elapsed.TotalSeconds) * 1000:F0} ms");
        Console.WriteLine($"  silence filled : {silentSeconds:F2}s " +
                          $"({silentSeconds / Math.Max(writtenSeconds, 0.01) * 100:F1}%)");
        Console.WriteLine($"  dropped        : {pacer.DroppedFrames / (double)AudioPacer.SampleRate:F2}s");
        Console.WriteLine($"  peak           : {(peakDb is null ? "-inf" : peakDb.Value.ToString("F1"))} dBFS");
        Console.WriteLine();
        Console.WriteLine(peakDb is null or < -80
            ? "  VERDICT: silent - the target rendered nothing capturable (wrong pid, or protected audio)."
            : "  VERDICT: audio captured. Play the WAV to confirm it is the right source.");
    }
}

/// <summary>Float32 WAV writer: header written up front, lengths patched on dispose.</summary>
public sealed class WavWriter : IAsyncDisposable
{
    private readonly FileStream _file;
    private readonly int _sampleRate;
    private readonly int _channels;

    public WavWriter(string path, int sampleRate, int channels)
    {
        _sampleRate = sampleRate;
        _channels = channels;

        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        _file = File.Create(path);
        WriteHeader(0);
    }

    public Stream Stream => _file;

    private void WriteHeader(int dataBytes)
    {
        var blockAlign = _channels * sizeof(float);
        var resume = _file.Position;

        _file.Position = 0;

        using (var writer = new BinaryWriter(_file, System.Text.Encoding.ASCII, leaveOpen: true))
        {
            writer.Write("RIFF"u8);
            writer.Write(36 + dataBytes);
            writer.Write("WAVE"u8);
            writer.Write("fmt "u8);
            writer.Write(16);
            writer.Write((short)3);                       // IEEE float
            writer.Write((short)_channels);
            writer.Write(_sampleRate);
            writer.Write(_sampleRate * blockAlign);
            writer.Write((short)blockAlign);
            writer.Write((short)32);
            writer.Write("data"u8);
            writer.Write(dataBytes);
        }

        _file.Position = Math.Max(resume, 44);
    }

    public async ValueTask DisposeAsync()
    {
        await _file.FlushAsync();
        WriteHeader((int)(_file.Length - 44));
        await _file.DisposeAsync();
    }
}
