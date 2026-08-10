using System.Buffers;
using StreaMuse.Media;
using StreaMuse.State;

namespace StreaMuse.Capture;

/// <summary>Writes exactly <see cref="SampleRate"/> frames per second of wall clock, filling
/// silence on underrun, so the encoder never starves. See CLAUDE.md.</summary>
public sealed class AudioPacer(StateHub hub) : IDisposable
{
    public const int SampleRate = ProcessLoopbackCapture.SampleRate;
    public const int Channels = ProcessLoopbackCapture.Channels;

    /// <summary>How much captured audio to hold back to absorb burstiness.</summary>
    private const int TargetLatencyMs = 200;

    /// <summary>Above this the buffer is trimmed; the source is running faster than the clock.</summary>
    private const int MaxLatencyMs = 600;

    private const int WriteIntervalMs = 20;

    private readonly Lock _sync = new();
    private readonly Queue<float[]> _pending = new();

    /// <summary>How far into the queue's head block the reader has already consumed.</summary>
    private int _headOffset;

    private int _pendingSamples;

    private long _framesWritten;
    private long _silenceFrames;
    private long _droppedFrames;

    /// <summary>Feeds the pacer. Safe to call from the capture thread.</summary>
    public void Push(float[] samples)
    {
        if (samples.Length == 0) return;

        lock (_sync)
        {
            _pending.Enqueue(samples);
            _pendingSamples += samples.Length;

            // Shed oldest rather than accumulate unbounded latency when the source outruns the clock.
            var maxSamples = MaxLatencyMs * SampleRate / 1000 * Channels;
            while (_pendingSamples > maxSamples && _pending.Count > 1)
            {
                var dropped = _pending.Dequeue();
                var lost = dropped.Length - _headOffset;
                _headOffset = 0;
                _pendingSamples -= lost;
                _droppedFrames += lost / Channels;
            }
        }
    }

    public long FramesWritten { get { lock (_sync) return _framesWritten; } }

    public long SilenceFrames { get { lock (_sync) return _silenceFrames; } }

    public long DroppedFrames { get { lock (_sync) return _droppedFrames; } }

    /// <summary>Whether the last second carried real audio rather than filled silence.</summary>
    public bool HasSignal { get; private set; }

    public void Reset()
    {
        lock (_sync)
        {
            _pending.Clear();
            _headOffset = 0;
            _pendingSamples = 0;
            _framesWritten = 0;
            _silenceFrames = 0;
            _droppedFrames = 0;
        }
    }

    /// <summary>Writes paced float32 samples until cancelled; the clock decides how much goes out.</summary>
    public async Task RunAsync(Stream destination, StreamClock clock, CancellationToken ct)
    {
        var byteBuffer = ArrayPool<byte>.Shared.Rent(SampleRate * Channels * sizeof(float) / 4);
        var scratch = ArrayPool<float>.Shared.Rent(SampleRate * Channels / 4);

        var silenceInWindow = 0L;
        var framesInWindow = 0L;
        var windowStart = clock.ElapsedMilliseconds;

        try
        {
            while (!ct.IsCancellationRequested)
            {
                var due = clock.ElapsedMilliseconds * SampleRate / 1000 - _framesWritten;

                if (due <= 0)
                {
                    await Task.Delay(WriteIntervalMs, ct);
                    continue;
                }

                var frames = (int)Math.Min(due, scratch.Length / Channels);
                var samplesNeeded = frames * Channels;
                var filled = Drain(scratch, samplesNeeded);

                if (filled < samplesNeeded)
                {
                    Array.Clear(scratch, filled, samplesNeeded - filled);
                    var silentFrames = (samplesNeeded - filled) / Channels;
                    silenceInWindow += silentFrames;

                    lock (_sync) _silenceFrames += silentFrames;
                }

                var byteCount = samplesNeeded * sizeof(float);
                if (byteBuffer.Length < byteCount)
                {
                    ArrayPool<byte>.Shared.Return(byteBuffer);
                    byteBuffer = ArrayPool<byte>.Shared.Rent(byteCount);
                }

                Buffer.BlockCopy(scratch, 0, byteBuffer, 0, byteCount);
                await destination.WriteAsync(byteBuffer.AsMemory(0, byteCount), ct);

                lock (_sync) _framesWritten += frames;
                framesInWindow += frames;

                if (clock.ElapsedMilliseconds - windowStart >= 1000)
                {
                    HasSignal = framesInWindow > 0 && silenceInWindow < framesInWindow / 2;
                    silenceInWindow = 0;
                    framesInWindow = 0;
                    windowStart = clock.ElapsedMilliseconds;
                }

                await Task.Delay(WriteIntervalMs, ct);
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException)
        {
            hub.Log(LineLevel.Warn, $"audio pipe closed: {ex.Message}");
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(byteBuffer);
            ArrayPool<float>.Shared.Return(scratch);
        }
    }

    /// <summary>Drains the jitter buffer, holding back <see cref="TargetLatencyMs"/> as reserve.</summary>
    private int Drain(float[] destination, int wanted)
    {
        lock (_sync)
        {
            var reserve = TargetLatencyMs * SampleRate / 1000 * Channels;
            var take = Math.Min(wanted, Math.Max(0, _pendingSamples - reserve));
            var written = 0;

            while (written < take && _pending.Count > 0)
            {
                var head = _pending.Peek();
                var headRemaining = head.Length - _headOffset;
                var chunk = Math.Min(headRemaining, take - written);

                head.AsSpan(_headOffset, chunk).CopyTo(destination.AsSpan(written));

                written += chunk;
                _headOffset += chunk;
                _pendingSamples -= chunk;

                if (_headOffset >= head.Length)
                {
                    _pending.Dequeue();
                    _headOffset = 0;
                }
            }

            return written;
        }
    }

    public void Dispose() => Reset();
}
