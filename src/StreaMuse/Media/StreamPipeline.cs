using System.Diagnostics;
using System.IO.Pipes;
using StreaMuse.Capture;
using StreaMuse.Deps;
using StreaMuse.Dj;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;
using StreaMuse.Tunnel;

namespace StreaMuse.Media;

public sealed class StreamPipeline(
    AppSettings settings,
    StateHub hub,
    DependencyManager deps,
    DiscoveryService discovery,
    ArtworkStore artwork,
    CloudflaredTunnel tunnel,
    DjAddon dj)
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Stopwatch _clock = new();
    private readonly AudioPacer _audioPacer = new();
    private readonly LevelMeter _meter = new();

    private ProcessLoopbackCapture? _capture;
    private FfmpegEncoder? _encoder;
    private CancellationTokenSource? _session;
    private Task? _sessionTasks;
    private NamedPipeServerStream? _audioPipe;
    private NamedPipeServerStream? _videoPipe;
    private long _reportedShedSeconds;

    public bool Running => _session is { IsCancellationRequested: false };

    public void OnTargetChanged(ResolvedSource resolved)
    {
        if (!Running || _capture is null) return;
        if (!resolved.Detected || resolved.CaptureProcessId == _capture.ProcessId) return;

        hub.Log(LineLevel.Info,
            $"capture target moved to {resolved.ProcessName} · pid {resolved.CaptureProcessId} - reattaching");
        _ = ReattachAsync(resolved.CaptureProcessId);
    }

    /// <summary>Discovery raises a target only when it changes, so a dropped reattach never gets a
    /// second chance - the stream would keep running on filled silence.</summary>
    private async Task ReattachAsync(int processId)
    {
        await _gate.WaitAsync();
        try
        {
            if (!Running || _capture is null) return;
            if (await _capture.StartAsync(processId)) return;

            await AbortAsync("could not reattach audio capture");
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<bool> StartAsync()
    {
        await _gate.WaitAsync();
        try
        {
            if (Running) return true;

            if (deps.FfmpegPath is null)
            {
                Fail("ffmpeg is not available - check the Dependencies panel");
                return false;
            }

            var resolved = discovery.Current;
            if (!resolved.Detected)
            {
                Fail(resolved.StatusText);
                return false;
            }

            hub.SetEncoder(new EncoderState(StreamStatus.Starting, 0, settings.Fps, 0, 0, null));

            var session = new CancellationTokenSource();
            _session = session;

            HlsOutput.Prepare();
            _meter.Reset();
            _reportedShedSeconds = 0;

            var videoPacer = new VideoPacer(new CoverFrameRenderer(settings, artwork, hub, dj));
            var encoder = new FfmpegEncoder(hub);
            _encoder = encoder;

            // Servers must exist before ffmpeg opens them. The pacers get the locals, not the
            // fields, so a teardown that nulls them cannot fault a running write.
            var audioPipe = new NamedPipeServerStream(
                encoder.AudioPipeName, PipeDirection.Out, 1,
                PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 1 << 22, 1 << 22);

            var videoPipe = new NamedPipeServerStream(
                encoder.VideoPipeName, PipeDirection.Out, 1,
                PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 1 << 22, 1 << 22);

            _audioPipe = audioPipe;
            _videoPipe = videoPipe;

            var connected = Task.WhenAll(
                audioPipe.WaitForConnectionAsync(session.Token),
                videoPipe.WaitForConnectionAsync(session.Token));

            encoder.Exited += code =>
            {
                if (session.IsCancellationRequested) return;
                Fail($"encoder exited unexpectedly (code {code})");
                _ = StopAsync();
            };

            encoder.Start(deps.FfmpegPath, settings, Paths.HlsDir);

            try
            {
                await connected.WaitAsync(TimeSpan.FromSeconds(10), session.Token);
            }
            catch (Exception)
            {
                return await AbortAsync("ffmpeg never connected to the capture pipes");
            }

            var capture = new ProcessLoopbackCapture(hub);
            capture.SamplesAvailable += samples =>
            {
                _meter.Add(samples);
                _audioPacer.Push(samples);
            };
            _capture = capture;

            if (!await capture.StartAsync(resolved.CaptureProcessId))
            {
                return await AbortAsync("could not attach audio capture");
            }

            // Audio buffered before the clock started is stale and would overrun the buffer at once.
            _audioPacer.Reset();
            _clock.Restart();

            var token = session.Token;
            _sessionTasks = Task.WhenAll(
                Task.Run(() => RunPacerAsync("audio", () => _audioPacer.RunAsync(audioPipe, _clock, token, MixBlock), token)),
                Task.Run(() => RunPacerAsync("video", () => videoPacer.RunAsync(videoPipe, _clock, settings.Fps, token), token)),
                Task.Run(() => PublishTelemetryAsync(token)));

            hub.SetEncoder(new EncoderState(StreamStatus.Running, 0, settings.Fps, 0, 0, null));
            hub.Log(LineLevel.Info, $"streaming - {hub.Snapshot().LocalUrl}");

            if (settings.AutoTunnel) _ = tunnel.StartAsync();

            return true;
        }
        catch (Exception ex)
        {
            return await AbortAsync(ex.Message);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task StopAsync()
    {
        await _gate.WaitAsync();
        try
        {
            await StopCoreAsync();
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task<bool> AbortAsync(string message)
    {
        Fail(message);
        await StopCoreAsync();
        return false;
    }

    private async Task StopCoreAsync()
    {
        // Every failure path stops the stream, and several can fire for one fault (both pacers see
        // the broken pipe, then the encoder reports it exited). Only the first has anything to do.
        var session = Interlocked.Exchange(ref _session, null);
        if (session is null) return;

        session.Cancel();

        _capture?.Dispose();
        _capture = null;

        _encoder?.Stop();
        _encoder = null;

        await DrainSessionAsync();

        await DisposePipeAsync(_audioPipe);
        await DisposePipeAsync(_videoPipe);
        _audioPipe = null;
        _videoPipe = null;

        _clock.Stop();
        session.Dispose();

        if (hub.Encoder.Status != StreamStatus.Error)
        {
            hub.SetEncoder(new EncoderState(StreamStatus.Idle, 0, 0, 0, 0, null));
        }

        await tunnel.StopAsync();
        hub.Log(LineLevel.Info, "stream stopped");
    }

    /// <summary>Waits for the session's tasks to leave the pipes and the token alone. Bounded,
    /// because a write that ffmpeg never read stays blocked until its pipe goes away.</summary>
    private async Task DrainSessionAsync()
    {
        var tasks = Interlocked.Exchange(ref _sessionTasks, null);
        if (tasks is null) return;

        try
        {
            await tasks.WaitAsync(TimeSpan.FromSeconds(2));
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Warn, $"stream tasks did not end cleanly: {ex.Message}");
        }
    }

    /// <summary>A pacer returns only when it can no longer write, so an exit the session did not ask
    /// for would leave a stream reporting Running with frozen output. The stop is detached because a
    /// teardown draining this task holds the gate the stop needs.</summary>
    private async Task RunPacerAsync(string track, Func<Task> pacer, CancellationToken ct)
    {
        string? fault = null;
        try
        {
            await pacer();
        }
        catch (Exception ex)
        {
            fault = ex.Message;
        }

        if (ct.IsCancellationRequested) return;

        Fail(fault is null ? $"{track} pacing stopped" : $"{track} pacing stopped: {fault}");
        _ = StopAsync();
    }

    private async Task PublishTelemetryAsync(CancellationToken ct)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(100));
        var tick = 0;

        try
        {
            while (await timer.WaitForNextTickAsync(ct))
            {
                var (bars, peakDb) = _meter.Read();
                await hub.PublishMeterAsync(bars, peakDb, _audioPacer.HasSignal);

                if (++tick % 10 != 0) continue;

                var shed = _audioPacer.DroppedFrames / AudioPacer.SampleRate;
                if (shed > _reportedShedSeconds)
                {
                    _reportedShedSeconds = shed;
                    hub.Log(LineLevel.Warn, $"audio buffer overran - {shed}s shed to stay in sync");
                }

                hub.SetEncoder(new EncoderState(
                    StreamStatus.Running,
                    HlsOutput.MeasureBitrateKbps(),
                    settings.Fps,
                    _encoder?.DroppedFrames ?? 0,
                    _clock.Elapsed.TotalSeconds,
                    null));
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"telemetry stopped: {ex.Message}");
        }
    }

    /// <summary>Handed to the audio pacer, so it runs on the pacer's wall clock rather than on capture
    /// activity - see AudioPacer.RunAsync. A broken addon must never take the stream down with it, so
    /// a throw passes the block through unmixed.</summary>
    private float[] MixBlock(float[] block)
    {
        if (!settings.DjAddonEnabled) return block;

        try
        {
            return dj.Mix(block);
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Warn, $"DJ addon mix failed: {ex.Message}");
            return block;
        }
    }

    private void Fail(string message)
    {
        hub.Log(LineLevel.Error, message);
        hub.SetEncoder(new EncoderState(StreamStatus.Error, 0, 0, 0, 0, message));
    }

    private static async Task DisposePipeAsync(NamedPipeServerStream? pipe)
    {
        if (pipe is null) return;

        try
        {
            await pipe.DisposeAsync();
        }
        catch (Exception)
        {
        }
    }
}
