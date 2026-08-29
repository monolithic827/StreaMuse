using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.FileProviders;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;

namespace StreaMuse.Web;

/// <summary>The listener page and its now-playing feed, served through the tunnel beside the
/// playlist. See CLAUDE.md before adding anything here.</summary>
public static class ListenerEndpoint
{
    /// <summary>The only files this port will serve, and the type each is sent as.</summary>
    private static readonly Dictionary<string, string> AssetTypes = new()
    {
        [".html"] = "text/html; charset=utf-8",
        [".css"] = "text/css; charset=utf-8",
        [".js"] = "text/javascript; charset=utf-8"
    };

    /// <summary>The files the page names with a version; the page itself is not one of them.</summary>
    private static readonly string[] AssetNames = ["listen.css", "listen.js"];

    private static string? _assetVersion;

    /// <summary>Everything the outside world is told, and only while the stream is running - the
    /// tunnel outlives the encoder, so a stopped stream must report nothing.</summary>
    private sealed record PublicNowPlaying(
        string Title,
        string Artist,
        string Album,
        bool Playing,
        double PositionSeconds,
        double DurationSeconds,
        string ArtworkVersion,
        bool Live);

    private static readonly PublicNowPlaying OffAir = new("", "", "", false, 0, 0, "0", false);

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
                "art" => await ServeArtAsync(ctx, hub, artwork),
                _ => await ServeAssetAsync(ctx, files, name)
            };

            if (!served) await next();
        });
    }

    private static bool IsLive(StateHub hub) => hub.Encoder.Status == StreamStatus.Running;

    private static async Task<bool> ServeAssetAsync(HttpContext ctx, IFileProvider files, string name)
    {
        if (!HlsEndpoint.IsSafeName(name)) return false;

        var extension = Path.GetExtension(name).ToLowerInvariant();
        if (!AssetTypes.TryGetValue(extension, out var contentType)) return false;

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

        ctx.Response.ContentType = contentType;
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

        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var name in AssetNames) hash.AppendData(Read(files.GetFileInfo($"listen/{name}")));

        return _assetVersion = Convert.ToHexString(hash.GetHashAndReset().AsSpan(0, 6));
    }

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

        var payload = IsLive(hub)
            ? new PublicNowPlaying(
                now.Title,
                now.Artist,
                now.Album,
                now.Playing,
                now.PositionSeconds,
                now.DurationSeconds,
                now.ArtworkVersion.ToString(CultureInfo.InvariantCulture),
                true)
            : OffAir;

        ctx.Response.ContentType = "application/json; charset=utf-8";
        ctx.Response.Headers.CacheControl = "no-store";

        return await SendAsync(ctx, JsonSerializer.SerializeToUtf8Bytes(payload, StateHub.Json));
    }

    private static async Task<bool> ServeArtAsync(HttpContext ctx, StateHub hub, ArtworkStore artwork)
    {
        if (!IsLive(hub)) return false;

        var (version, bytes) = artwork.Current;
        if (bytes is null || bytes.Length == 0) return false;

        ctx.Response.ContentType = ArtworkStore.ContentTypeOf(bytes);
        ctx.Response.Headers.XContentTypeOptions = "nosniff";

        // The cover can change between the poll that named a version and the fetch for it, and these
        // bytes are then not the ones that URL stands for. Caching them under it would outlive the
        // track by a year.
        ctx.Response.Headers.CacheControl =
            ctx.Request.Query["v"] == version.ToString(CultureInfo.InvariantCulture)
                ? "public, max-age=31536000, immutable"
                : "no-store";

        return await SendAsync(ctx, bytes);
    }
}
