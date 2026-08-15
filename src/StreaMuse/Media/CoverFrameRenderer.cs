using SkiaSharp;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;

namespace StreaMuse.Media;

/// <summary>Draws the video track as JPEG frames pushed down a pipe, so the picture can change
/// mid-stream without restarting ffmpeg and breaking the playlist.</summary>
public sealed class CoverFrameRenderer(AppSettings settings, ArtworkStore artwork, StateHub hub)
{
    private const int JpegQuality = 88;

    private SKBitmap? _decoded;
    private long _decodedVersion = -1;
    private static readonly SKColor DefaultBackground = new(0x1D, 0x1F, 0x20);
    private SKColor _background = DefaultBackground;

    private byte[]? _lastFrame;
    private string _lastSignature = "";

    /// <summary>Current frame as JPEG, re-rendered only when something visible changed.</summary>
    public byte[] Render()
    {
        var now = hub.NowPlaying;
        var progress = now.DurationSeconds > 0
            ? Math.Clamp(now.PositionSeconds / now.DurationSeconds, 0, 1)
            : 0;

        // Quantised so a static track does not force a re-encode every frame.
        var signature = string.Join('|',
            artwork.Version, settings.Width, settings.Height, settings.TextOverlay,
            now.Title, now.Artist, now.Album, (int)(progress * 600));

        if (_lastFrame is not null && signature == _lastSignature) return _lastFrame;

        _lastFrame = RenderFrame(now, progress);
        _lastSignature = signature;
        return _lastFrame;
    }

    private byte[] RenderFrame(NowPlaying now, double progress)
    {
        var width = settings.Width;
        var height = settings.Height;

        EnsureArtworkDecoded();

        using var surface = SKSurface.Create(new SKImageInfo(width, height, SKColorType.Rgba8888));
        var canvas = surface.Canvas;
        canvas.Clear(_background);

        var art = _decoded;
        var overlay = settings.TextOverlay;

        var artBox = overlay
            ? SquareIn(new SKRect(0, 0, width, height), 0.62f, alignLeft: true)
            : SquareIn(new SKRect(0, 0, width, height), 0.86f, alignLeft: false);

        if (art is not null)
        {
            DrawBackdrop(canvas, art, width, height);
            canvas.DrawBitmap(art, Fit(art.Width, art.Height, artBox), new SKSamplingOptions(SKFilterMode.Linear));
        }
        else
        {
            DrawArtPlaceholder(canvas, artBox);
        }

        if (overlay) DrawText(canvas, now, progress, artBox, width, height);

        using var image = surface.Snapshot();
        using var data = image.Encode(SKEncodedImageFormat.Jpeg, JpegQuality);
        return data.ToArray();
    }

    /// <summary>A blurred, darkened blow-up of the art so the frame is never flat black.</summary>
    private static void DrawBackdrop(SKCanvas canvas, SKBitmap art, int width, int height)
    {
        using var paint = new SKPaint
        {
            IsAntialias = true,
            ImageFilter = SKImageFilter.CreateBlur(40, 40)
        };

        var scale = Math.Max(width / (float)art.Width, height / (float)art.Height) * 1.4f;
        var w = art.Width * scale;
        var h = art.Height * scale;
        var dest = new SKRect((width - w) / 2, (height - h) / 2, (width + w) / 2, (height + h) / 2);

        canvas.DrawBitmap(art, dest, new SKSamplingOptions(SKFilterMode.Linear), paint);

        using var shade = new SKPaint { Color = new SKColor(0x10, 0x12, 0x14, 0xC0) };
        canvas.DrawRect(new SKRect(0, 0, width, height), shade);
    }

    private static void DrawArtPlaceholder(SKCanvas canvas, SKRect box)
    {
        using var fill = new SKPaint { Color = new SKColor(0x2B, 0x2B, 0x2D), IsAntialias = true };
        canvas.DrawRect(box, fill);

        using var stroke = new SKPaint
        {
            Color = new SKColor(0x59, 0x80, 0xA6),
            IsAntialias = true,
            Style = SKPaintStyle.Stroke,
            StrokeWidth = 2
        };

        canvas.DrawRect(box, stroke);
    }

    private void DrawText(SKCanvas canvas, NowPlaying now, double progress, SKRect artBox, int width, int height)
    {
        var left = artBox.Right + width * 0.045f;
        var right = width - width * 0.05f;
        var available = right - left;
        if (available < 80) return;

        using var titleFont = new SKFont(SKTypeface.FromFamilyName("Segoe UI Semibold"), height * 0.075f);
        using var bodyFont = new SKFont(SKTypeface.FromFamilyName("Segoe UI"), height * 0.045f);
        using var smallFont = new SKFont(SKTypeface.FromFamilyName("Segoe UI"), height * 0.033f);

        using var bright = new SKPaint { Color = SKColors.White, IsAntialias = true };
        using var muted = new SKPaint { Color = new SKColor(0xFF, 0xFF, 0xFF, 0xB0), IsAntialias = true };
        using var faint = new SKPaint { Color = new SKColor(0xFF, 0xFF, 0xFF, 0x70), IsAntialias = true };
        using var accent = new SKPaint { Color = new SKColor(0x94, 0xBC, 0xE3), IsAntialias = true };

        var y = artBox.Top + height * 0.10f;

        canvas.DrawText(Ellipsize(now.Title, titleFont, available), left, y, SKTextAlign.Left, titleFont, bright);
        y += height * 0.085f;

        canvas.DrawText(Ellipsize(now.Artist, bodyFont, available), left, y, SKTextAlign.Left, bodyFont, muted);
        y += height * 0.06f;

        canvas.DrawText(Ellipsize(now.Album, smallFont, available), left, y, SKTextAlign.Left, smallFont, faint);

        var barY = artBox.Bottom - height * 0.06f;
        var barHeight = Math.Max(2f, height * 0.006f);

        using var track = new SKPaint { Color = new SKColor(0xFF, 0xFF, 0xFF, 0x33), IsAntialias = true };
        canvas.DrawRect(new SKRect(left, barY, right, barY + barHeight), track);
        canvas.DrawRect(new SKRect(left, barY, left + available * (float)progress, barY + barHeight), accent);

        var elapsed = FormatClock(now.PositionSeconds);
        var total = now.DurationSeconds > 0 ? FormatClock(now.DurationSeconds) : "--:--";
        var totalWidth = smallFont.MeasureText(total);

        canvas.DrawText(elapsed, left, barY + height * 0.045f, SKTextAlign.Left, smallFont, faint);
        canvas.DrawText(total, right - totalWidth, barY + height * 0.045f, SKTextAlign.Left, smallFont, faint);
    }

    private void EnsureArtworkDecoded()
    {
        var version = artwork.Version;
        if (version == _decodedVersion) return;

        _decodedVersion = version;
        _decoded?.Dispose();
        _decoded = null;

        var bytes = artwork.Bytes;
        if (bytes is null || bytes.Length == 0) return;

        try
        {
            _decoded = SKBitmap.Decode(bytes);
            if (_decoded is not null) _background = DominantColor(_decoded);
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Warn, $"could not decode album art: {ex.Message}");
        }
    }

    /// <summary>Average of a downsampled copy, darkened - good enough as a backdrop tint.</summary>
    private static SKColor DominantColor(SKBitmap bitmap)
    {
        using var small = bitmap.Resize(new SKImageInfo(16, 16), new SKSamplingOptions(SKFilterMode.Linear));
        if (small is null) return DefaultBackground;

        long r = 0, g = 0, b = 0;
        var count = 0;

        for (var x = 0; x < small.Width; x++)
        for (var y = 0; y < small.Height; y++)
        {
            var pixel = small.GetPixel(x, y);
            r += pixel.Red;
            g += pixel.Green;
            b += pixel.Blue;
            count++;
        }

        if (count == 0) return DefaultBackground;

        return new SKColor(
            (byte)(r / count * 0.35),
            (byte)(g / count * 0.35),
            (byte)(b / count * 0.35));
    }

    private static SKRect SquareIn(SKRect area, float fraction, bool alignLeft)
    {
        var side = Math.Min(area.Width, area.Height) * fraction;
        var top = area.MidY - side / 2;
        var leftEdge = alignLeft ? area.Left + area.Height * 0.09f : area.MidX - side / 2;
        return new SKRect(leftEdge, top, leftEdge + side, top + side);
    }

    private static SKRect Fit(int sourceWidth, int sourceHeight, SKRect box)
    {
        var scale = Math.Min(box.Width / sourceWidth, box.Height / sourceHeight);
        var w = sourceWidth * scale;
        var h = sourceHeight * scale;
        return new SKRect(box.MidX - w / 2, box.MidY - h / 2, box.MidX + w / 2, box.MidY + h / 2);
    }

    private static string Ellipsize(string text, SKFont font, float maxWidth)
    {
        if (string.IsNullOrEmpty(text) || font.MeasureText(text) <= maxWidth) return text;

        var trimmed = text;
        while (trimmed.Length > 1 && font.MeasureText(trimmed + "…") > maxWidth)
        {
            trimmed = trimmed[..^1];
        }

        return trimmed + "…";
    }

    private static string FormatClock(double seconds)
    {
        if (double.IsNaN(seconds) || seconds < 0) seconds = 0;
        var span = TimeSpan.FromSeconds(seconds);
        return span.TotalHours >= 1
            ? $"{(int)span.TotalHours}:{span.Minutes:00}:{span.Seconds:00}"
            : $"{span.Minutes}:{span.Seconds:00}";
    }
}
