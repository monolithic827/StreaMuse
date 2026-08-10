using StreaMuse.State;

namespace StreaMuse.Media;

/// <summary>Pushes exactly fps JPEG frames per second of wall clock. image2pipe timestamps by
/// frame index, so this is what locks video to audio.</summary>
public sealed class VideoPacer(CoverFrameRenderer renderer, StateHub hub)
{
    public long FramesWritten { get; private set; }

    public void Reset() => FramesWritten = 0;

    public async Task RunAsync(Stream destination, StreamClock clock, int fps, CancellationToken ct)
    {
        var interval = Math.Max(1, 1000 / Math.Max(fps, 1));

        try
        {
            while (!ct.IsCancellationRequested)
            {
                var due = clock.ElapsedMilliseconds * fps / 1000 - FramesWritten;

                if (due <= 0)
                {
                    await Task.Delay(interval / 2 + 1, ct);
                    continue;
                }

                // Cap catch-up so a stall cannot dump hundreds of frames at once.
                var frames = (int)Math.Min(due, fps);
                var jpeg = renderer.Render();

                for (var i = 0; i < frames; i++)
                {
                    await destination.WriteAsync(jpeg, ct);
                    FramesWritten++;
                }

                await destination.FlushAsync(ct);
                await Task.Delay(interval / 2 + 1, ct);
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (IOException ex)
        {
            hub.Log(LineLevel.Warn, $"video pipe closed: {ex.Message}");
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"video pacer failed: {ex.Message}");
        }
    }
}
