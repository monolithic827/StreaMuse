using System.Diagnostics;

namespace StreaMuse.Media;

/// <summary>Pushes exactly fps JPEG frames per second of wall clock. image2pipe timestamps by
/// frame index, so this is what locks video to audio.</summary>
public sealed class VideoPacer(CoverFrameRenderer renderer)
{
    public async Task RunAsync(Stream destination, Stopwatch clock, int fps, CancellationToken ct)
    {
        var interval = Math.Max(1, 1000 / Math.Max(fps, 1));
        var framesWritten = 0L;

        try
        {
            while (!ct.IsCancellationRequested)
            {
                var due = clock.ElapsedMilliseconds * fps / 1000 - framesWritten;

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
                    framesWritten++;
                }

                await destination.FlushAsync(ct);
                await Task.Delay(interval / 2 + 1, ct);
            }
        }
        catch (OperationCanceledException)
        {
        }
    }
}
