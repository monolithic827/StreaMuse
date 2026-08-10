using System.Runtime.InteropServices;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using StreaMuse.State;

namespace StreaMuse.Capture;

/// <summary>Captures what one process and its children render, via WASAPI process loopback.
/// Activation is async (BuildAsync) and WithFormat is mandatory: GetMixFormat is E_NOTIMPL here.</summary>
public sealed class ProcessLoopbackCapture(StateHub hub) : IDisposable
{
    public const int SampleRate = 48_000;
    public const int Channels = 2;

    private WasapiRecorder? _recorder;
    private int _pid;

    /// <summary>Interleaved 32-bit float samples, in capture order.</summary>
    public event Action<ReadOnlyMemory<float>>? SamplesAvailable;

    public bool Running => _recorder is not null;

    public int ProcessId => _pid;

    public async Task<bool> StartAsync(int processId)
    {
        Stop();

        try
        {
            var recorder = await new WasapiRecorderBuilder()
                .WithProcessLoopback((uint)processId, ProcessLoopbackMode.IncludeTargetProcessTree)
                .WithFormat(WaveFormat.CreateIeeeFloatWaveFormat(SampleRate, Channels))
                .WithEventSync()
                .BuildAsync();

            recorder.DataAvailable += OnDataAvailable;
            recorder.RecordingStopped += OnRecordingStopped;
            recorder.StartRecording();

            _recorder = recorder;
            _pid = processId;

            hub.Log(LineLevel.Info, $"capture attached to pid {processId} - 48 kHz / 2 ch float");
            return true;
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"could not attach capture to pid {processId}: {ex.Message}");
            _recorder = null;
            _pid = 0;
            return false;
        }
    }

    public void Stop()
    {
        var recorder = Interlocked.Exchange(ref _recorder, null);
        if (recorder is null) return;

        recorder.DataAvailable -= OnDataAvailable;
        recorder.RecordingStopped -= OnRecordingStopped;

        try
        {
            recorder.StopRecording();
        }
        catch (Exception)
        {
        }

        try
        {
            recorder.Dispose();
        }
        catch (Exception)
        {
        }

        _pid = 0;
    }

    /// <summary>The span is valid only for this call; copy before it leaves the capture thread.</summary>
    private void OnDataAvailable(
        ReadOnlySpan<byte> buffer,
        AudioClientBufferFlags flags,
        long devicePosition,
        long qpcPosition)
    {
        if (buffer.Length == 0) return;

        var samples = MemoryMarshal.Cast<byte, float>(buffer);
        var copy = new float[samples.Length];

        // A silent packet still carries duration; zeroing keeps the pacer's timeline intact.
        if ((flags & AudioClientBufferFlags.Silent) == 0) samples.CopyTo(copy);

        SamplesAvailable?.Invoke(copy);
    }

    private void OnRecordingStopped(object? sender, StoppedEventArgs e)
    {
        if (e.Exception is not null)
        {
            hub.Log(LineLevel.Warn, $"capture stopped: {e.Exception.Message}");
        }
    }

    public void Dispose() => Stop();
}
