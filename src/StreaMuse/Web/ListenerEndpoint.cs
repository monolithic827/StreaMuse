using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.FileProviders;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;

namespace StreaMuse.Web;

/// <summary>
/// The listener page and its now-playing feed, served through the tunnel beside the playlist.
/// GET-only, and it must never serialize StateSnapshot: that carries the tunnel token, local paths,
/// pids and the log. Everything here is built from a record declared below instead, so a field added
/// to the panel's state cannot become public by accident. See CLAUDE.md.
/// </summary>
public static class ListenerEndpoint
{
    private static readonly string[] AssetExtensions = [".html", ".css", ".js"];

    /// <summary>The files the page names with a version; the page itself is not one of them.</summary>
    private static readonly string[] AssetNames = ["listen.css", "listen.js"];

    private static string? _assetVersion;

    /// <summary>Everything the outside world is told. Title, artist, album and cover are already
    /// rendered into the video, so this adds no information a listener does not have.</summary>
    private sealed record PublicNowPlaying(
        string Title,
        string Artist,
        string Album,
        bool Playing,
        double PositionSeconds,
        double DurationSeconds,
        long ArtworkVersion,
        bool Live);

    /// <summary>Must be mapped before MapPublicHls, which terminates for this port. Anything not
    /// handled here falls through to it and 404s.</summary>
    public static void MapListenerUi(this WebApplication app, int publicPort, AppSettings settings)
    {
        var hub = app.Services.GetRequiredService<StateHub>();
        var artwork = app.Services.GetRequiredService<ArtworkStore>();
        var files = app.Environment.WebRootFileProvider;

        app.Use(async (ctx, next) =>
        {
            var method = ctx.Request.Method;
            if (ctx.Connection.LocalPort != publicPort ||
                (!HttpMethods.IsGet(method) && !HttpMethods.IsHead(method)))
            {
                await next();
                return;
            }

            var prefix = $"/live/{settings.StreamKey}/";
            var path = ctx.Request.Path.Value ?? "";

            // The page's own URLs are relative, so it only resolves them from the directory form.
            // This link gets pasted around by hand, and without the redirect the slashless form is a
            // bare 404.
            if (path.Length == prefix.Length - 1 && prefix.StartsWith(path, StringComparison.Ordinal))
            {
                ctx.Response.Redirect(prefix);
                return;
            }

            if (!path.StartsWith(prefix, StringComparison.Ordinal))
            {
                await next();
                return;
            }

            var name = path[prefix.Length..];

            var served = name switch
            {
                "" => await ServeAssetAsync(ctx, files, "listen.html"),
                "now" => await ServeNowAsync(ctx, hub),
                "art" => await ServeArtAsync(ctx, artwork),
                _ => await ServeAssetAsync(ctx, files, name)
            };

            if (!served) await next();
        });
    }

    /// <summary>Serves the page's own files only. The lookup is confined to the listen/ subtree, so
    /// the control panel's index.html and app.js next to it stay unreachable from this port.</summary>
    private static async Task<bool> ServeAssetAsync(HttpContext ctx, IFileProvider files, string name)
    {
        if (!HlsEndpoint.IsSafeName(name)) return false;

        var extension = Path.GetExtension(name).ToLowerInvariant();
        if (!AssetExtensions.Contains(extension)) return false;

        var file = files.GetFileInfo($"listen/{name}");
        if (!file.Exists) return false;

        var body = Read(file);
        var isPage = extension == ".html";

        // The page names its stylesheet and script with the version below; only it needs rewriting.
        if (isPage)
        {
            body = Encoding.UTF8.GetBytes(
                Encoding.UTF8.GetString(body).Replace("{v}", AssetVersion(files), StringComparison.Ordinal));
        }

        ctx.Response.ContentType = extension switch
        {
            ".html" => "text/html; charset=utf-8",
            ".css" => "text/css; charset=utf-8",
            _ => "text/javascript; charset=utf-8"
        };

        // The page must be revalidated to hand out current asset URLs; the assets it names carry a
        // version in the URL, so they can be held forever. Cloudflare rewrites a no-cache on .css
        // and .js to its own 4h TTL, which is why busting by URL is the only thing that works here.
        ctx.Response.Headers.CacheControl = isPage
            ? "no-cache"
            : "public, max-age=31536000, immutable";

        return await SendAsync(ctx, body);
    }

    private static byte[] Read(IFileInfo file)
    {
        using var stream = file.CreateReadStream();
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }

    /// <summary>Content-derived, so a rebuild changes the URL the page asks for and nothing between
    /// here and the browser can serve the previous build's script against this build's feed.</summary>
    private static string AssetVersion(IFileProvider files)
    {
        if (_assetVersion is not null) return _assetVersion;

        var bytes = new List<byte>();
        foreach (var name in AssetNames) bytes.AddRange(Read(files.GetFileInfo($"listen/{name}")));

        return _assetVersion = Convert.ToHexString(SHA256.HashData(bytes.ToArray()).AsSpan(0, 6));
    }

    /// <summary>HEAD must answer exactly as GET does minus the body, or a link shared into a chat
    /// app whose unfurler probes with HEAD looks dead.</summary>
    private static async Task<bool> SendAsync(HttpContext ctx, byte[] body)
    {
        ctx.Response.ContentLength = body.Length;

        if (HttpMethods.IsHead(ctx.Request.Method)) return true;

        await ctx.Response.Body.WriteAsync(body, ctx.RequestAborted);
        return true;
    }

    private static async Task<bool> ServeNowAsync(HttpContext ctx, StateHub hub)
    {
        var now = hub.NowPlaying;

        var payload = new PublicNowPlaying(
            now.Title,
            now.Artist,
            now.Album,
            now.Playing,
            now.PositionSeconds,
            now.DurationSeconds,
            now.ArtworkVersion,
            hub.Encoder.Status == StreamStatus.Running);

        ctx.Response.ContentType = "application/json; charset=utf-8";
        ctx.Response.Headers.CacheControl = "no-store";

        return await SendAsync(ctx, JsonSerializer.SerializeToUtf8Bytes(payload, StateHub.Json));
    }

    /// <summary>Cached forever against ArtworkStore's content-derived version, exactly as the panel's
    /// /api/art is. See CLAUDE.md on why that key must not become a counter.</summary>
    private static async Task<bool> ServeArtAsync(HttpContext ctx, ArtworkStore artwork)
    {
        var bytes = artwork.Bytes;
        if (bytes is null || bytes.Length == 0) return false;

        ctx.Response.ContentType = ArtworkStore.ContentTypeOf(bytes);
        ctx.Response.Headers.CacheControl = "public, max-age=31536000, immutable";

        return await SendAsync(ctx, bytes);
    }
}
