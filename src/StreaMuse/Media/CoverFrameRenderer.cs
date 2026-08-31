using SkiaSharp;
using StreaMuse.Dj;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;

namespace StreaMuse.Media;

/// <summary>Draws the video track as JPEG frames pushed down a pipe, so the picture can change
/// mid-stream without restarting ffmpeg and breaking the playlist.</summary>
public sealed class CoverFrameRenderer(AppSettings settings, ArtworkStore artwork, StateHub hub, DjAddon dj)
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
        var (now, artBytes, artVersion, isRequest) = CurrentSource();
        var progress = now.DurationSeconds > 0
            ? Math.Clamp(now.PositionSeconds / now.DurationSeconds, 0, 1)
            : 0;

        // Quantised so a static track does not force a re-encode every frame.
        var signature = string.Join('|',
            artVersion, settings.Width, settings.Height, settings.TextOverlay, isRequest,
            now.Title, now.Artist, now.Album, (int)(progress * 600));

        if (_lastFrame is not null && signature == _lastSignature) return _lastFrame;

        _lastFrame = RenderFrame(now, progress, artBytes, artVersion, isRequest);
        _lastSignature = signature;
        return _lastFrame;
    }

    /// <summary>What the frame shows: the DJ's own track while one is actually mixing, the regular
    /// SMTC now-playing otherwise. This matters beyond just "the DJ has its own cover" - once a DJ
    /// track fully takes over, DjAddon pauses Apple Music/Spotify (see CLAUDE.md), so their SMTC
    /// metadata goes stale for as long as the DJ track plays. Without this the video would keep
    /// showing the paused track's cover while a different song plays audibly underneath it.</summary>
    private (NowPlaying Now, byte[]? Artwork, long ArtworkVersion, bool IsRequest) CurrentSource()
    {
        if (dj.SnapshotWithArtwork() is ({ NowMixing: { } mixing } snapshot, var djArt))
        {
            var now = new NowPlaying(
                mixing.Title, mixing.Artist, snapshot.Album, true,
                snapshot.PositionSeconds, snapshot.DurationSeconds, snapshot.ArtworkVersion);

            return (now, djArt, snapshot.ArtworkVersion, true);
        }

        return (hub.NowPlaying, artwork.Bytes, artwork.Version, false);
    }

    private byte[] RenderFrame(NowPlaying now, double progress, byte[]? artBytes, long artVersion, bool isRequest)
    {
        var width = settings.Width;
        var height = settings.Height;

        EnsureArtworkDecoded(artBytes, artVersion);

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

        if (overlay) DrawText(canvas, now, progress, artBox, width, height, isRequest);

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

    private void DrawText(
        SKCanvas canvas, NowPlaying now, double progress, SKRect artBox, int width, int height, bool isRequest)
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

        // Viewers can't otherwise tell a request from whatever the stream would normally be playing -
        // the two look identical once it's dropped in.
        if (isRequest) DrawRequestedTag(canvas, left, artBox.Top, width, height);

        // Pushed down when the tag is showing - the title font's cap-height reaches close enough to
        // its own baseline that at the normal position it visibly touched the pill above it.
        var y = artBox.Top + height * (isRequest ? 0.135f : 0.10f);

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

    /// <summary>A small filled pill above the title, sat in the gap between the art box's top and
    /// where the title itself starts (10% of height down) - there is room for one without pushing
    /// anything else.</summary>
    private static void DrawRequestedTag(SKCanvas canvas, float left, float top, int width, int height)
    {
        const string label = "REQUESTED";
        using var font = new SKFont(SKTypeface.FromFamilyName("Segoe UI Semibold"), height * 0.028f);

        var padX = width * 0.012f;
        var tagHeight = height * 0.045f;
        var tagTop = top + height * 0.025f;
        var rect = new SKRect(left, tagTop, left + font.MeasureText(label) + padX * 2, tagTop + tagHeight);

        using var fill = new SKPaint { Color = new SKColor(0x94, 0xBC, 0xE3), IsAntialias = true };
        canvas.DrawRoundRect(rect, tagHeight / 2, tagHeight / 2, fill);

        using var text = new SKPaint { Color = new SKColor(0x10, 0x12, 0x14), IsAntialias = true };
        canvas.DrawText(label, rect.Left + padX, rect.MidY + height * 0.010f, SKTextAlign.Left, font, text);
    }

    private void EnsureArtworkDecoded(byte[]? bytes, long version)
    {
        if (version == _decodedVersion) return;

        _decodedVersion = version;
        _decoded?.Dispose();
        _decoded = null;

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
