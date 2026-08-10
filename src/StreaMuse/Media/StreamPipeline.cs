using System.IO.Pipes;
using StreaMuse.Capture;
using StreaMuse.Deps;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;
using StreaMuse.Tunnel;

namespace StreaMuse.Media;

/// <summary>Orchestrates a streaming session: capture -> pacers -> ffmpeg -> HLS -> tunnel.</summary>
public sealed class StreamPipeline(
    AppSettings settings,
    StateHub hub,
    DependencyManager deps,
    DiscoveryService discovery,
    ArtworkStore artwork,
    CloudflaredTunnel tunnel)
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly StreamClock _clock = new();
    private readonly AudioPacer _audioPacer = new(hub);
    private readonly LevelMeter _meter = new();

    private ProcessLoopbackCapture? _capture;
    private FfmpegEncoder? _encoder;
    private VideoPacer? _videoPacer;
    private CancellationTokenSource? _session;
    private Task? _sessionTasks;
    private NamedPipeServerStream? _audioPipe;
    private NamedPipeServerStream? _videoPipe;
    private long _reportedShedSeconds;

    public bool Running => _session is { IsCancellationRequested: false };

    /// <summary>Restarts only the capture attachment when discovery points at a different process.</summary>
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

            Fail("could not reattach audio capture");
            await StopCoreAsync();
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
            _audioPacer.Reset();
            _meter.Reset();
            _reportedShedSeconds = 0;

            var renderer = new CoverFrameRenderer(settings, artwork, hub);
            var videoPacer = new VideoPacer(renderer, hub);
            _videoPacer = videoPacer;

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

            var audioConnected = audioPipe.WaitForConnectionAsync(session.Token);
            var videoConnected = videoPipe.WaitForConnectionAsync(session.Token);

            encoder.Exited += code =>
            {
                if (session.IsCancellationRequested) return;
                Fail($"encoder exited unexpectedly (code {code})");
                _ = StopAsync();
            };

            encoder.Start(deps.FfmpegPath, settings, Paths.HlsDir);

            using (var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(session.Token))
            {
                connectTimeout.CancelAfter(TimeSpan.FromSeconds(10));
                try
                {
                    await Task.WhenAll(audioConnected, videoConnected).WaitAsync(connectTimeout.Token);
                }
                catch (Exception)
                {
                    Fail("ffmpeg never connected to the capture pipes");
                    await StopCoreAsync();
                    return false;
                }
            }

            var capture = new ProcessLoopbackCapture(hub);
            capture.SamplesAvailable += samples =>
            {
                var array = samples.ToArray();
                _meter.Add(array);
                _audioPacer.Push(array);
            };
            _capture = capture;

            if (!await capture.StartAsync(resolved.CaptureProcessId))
            {
                Fail("could not attach audio capture");
                await StopCoreAsync();
                return false;
            }

            // Audio buffered before the clock started is stale and would overrun the buffer at once.
            _audioPacer.Reset();
            _clock.Restart();

            var token = session.Token;
            _sessionTasks = Task.WhenAll(
                Task.Run(() => _audioPacer.RunAsync(audioPipe, _clock, token)),
                Task.Run(() => RunVideoAsync(videoPacer, videoPipe, token)),
                Task.Run(() => PublishTelemetryAsync(token)));

            hub.SetEncoder(new EncoderState(StreamStatus.Running, 0, settings.Fps, 0, 0, null));
            hub.Log(LineLevel.Info, $"streaming - {hub.Snapshot().LocalUrl}");

            if (settings.AutoTunnel) _ = tunnel.StartAsync();

            return true;
        }
        catch (Exception ex)
        {
            Fail(ex.Message);
            await StopCoreAsync();
            return false;
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

    /// <summary>Teardown without the gate, for the paths that already hold it.</summary>
    private async Task StopCoreAsync()
    {
        var session = Interlocked.Exchange(ref _session, null);
        session?.Cancel();

        _capture?.Stop();
        _capture?.Dispose();
        _capture = null;

        _encoder?.Stop();
        _encoder = null;

        // The pacers write into the pipes and wait on the session token, so both outlive them: a
        // pipe disposed under a write throws ObjectDisposedException, and so does a Task.Delay
        // registering on a disposed source. ffmpeg is gone by here, so a blocked write has faulted.
        await DrainSessionAsync();

        await DisposePipeAsync(_audioPipe);
        await DisposePipeAsync(_videoPipe);
        _audioPipe = null;
        _videoPipe = null;

        _clock.Stop();
        _videoPacer = null;
        session?.Dispose();

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

    /// <summary>The pacer returns only when it can no longer write frames, so an exit the session
    /// did not ask for leaves a stream reporting Running with frozen video. The stop is detached
    /// because a teardown draining this task holds the gate the stop needs.</summary>
    private async Task RunVideoAsync(VideoPacer pacer, Stream destination, CancellationToken ct)
    {
        await pacer.RunAsync(destination, _clock, settings.Fps, ct);

        if (ct.IsCancellationRequested) return;

        Fail("video pacing stopped");
        _ = StopAsync();
    }

    /// <summary>Pushes the meter at ~10 Hz and encoder stats at 1 Hz.</summary>
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

                var progress = _encoder?.Progress;

                // Shed audio is a distinct failure from dropped video frames; keep them separate.
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
                    progress?.DroppedFrames ?? 0,
                    _clock.Elapsed.TotalSeconds,
                    null));
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex)
        {
            // Detached: an unlogged fault here silently freezes the meter and all statistics.
            hub.Log(LineLevel.Error, $"telemetry stopped: {ex.Message}");
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
            if (pipe.IsConnected) pipe.Disconnect();
        }
        catch (Exception)
        {
        }

        try
        {
            await pipe.DisposeAsync();
        }
        catch (Exception)
        {
        }
    }
}
