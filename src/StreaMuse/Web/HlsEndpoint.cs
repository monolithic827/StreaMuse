using Microsoft.Extensions.Primitives;
using StreaMuse.Settings;

namespace StreaMuse.Web;

/// <summary>The only thing reachable through the tunnel: /live/{key}/ playlists and segments.
/// Everything else on this port 404s. Never widen this. See CLAUDE.md.</summary>
public static class HlsEndpoint
{
    public static string LocalUrl(int publicPort, string streamKey) =>
        $"http://127.0.0.1:{publicPort}/live/{streamKey}/index.m3u8";

    /// <summary>The single name rule for everything the public port serves; shared with
    /// ListenerEndpoint so the two cannot drift apart. See CLAUDE.md.</summary>
    internal static bool IsSafeName(string name) =>
        name.Length > 0 &&
        !name.Contains('/') && !name.Contains('\\') && !name.Contains("..") &&
        !Path.IsPathRooted(name);

    public static void MapPublicHls(this WebApplication app, int publicPort, AppSettings settings)
    {
        app.Use(async (ctx, next) =>
        {
            if (ctx.Connection.LocalPort != publicPort)
            {
                await next();
                return;
            }

            var served = await TryServeAsync(ctx, settings);
            if (!served)
            {
                ctx.Response.StatusCode = StatusCodes.Status404NotFound;
                await ctx.Response.WriteAsync("Not found");
            }
        });
    }

    private static async Task<bool> TryServeAsync(HttpContext ctx, AppSettings settings)
    {
        if (!HttpMethods.IsGet(ctx.Request.Method) && !HttpMethods.IsHead(ctx.Request.Method)) return false;

        var path = ctx.Request.Path.Value ?? "";
        var expectedPrefix = $"/live/{settings.StreamKey}/";
        if (!path.StartsWith(expectedPrefix, StringComparison.Ordinal)) return false;

        var name = path[expectedPrefix.Length..];
        if (!IsSafeName(name)) return false;

        var isPlaylist = name.EndsWith(".m3u8", StringComparison.OrdinalIgnoreCase);
        var isSegment = name.EndsWith(".ts", StringComparison.OrdinalIgnoreCase);
        if (!isPlaylist && !isSegment) return false;

        var file = Path.Combine(Paths.HlsDir, name);
        if (!File.Exists(file)) return false;

        ctx.Response.Headers.AccessControlAllowOrigin = "*";
        ctx.Response.Headers.AccessControlAllowHeaders = "*";
        ctx.Response.ContentType = isPlaylist ? "application/vnd.apple.mpegurl" : "video/mp2t";

        ctx.Response.Headers.CacheControl = isPlaylist
            ? new StringValues("no-cache, no-store, must-revalidate")
            : new StringValues("public, max-age=3600, immutable");

        if (HttpMethods.IsHead(ctx.Request.Method))
        {
            ctx.Response.ContentLength = new FileInfo(file).Length;
            return true;
        }

        // Shared read: ffmpeg may still hold the file open.
        await using var stream = new FileStream(
            file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            bufferSize: 64 * 1024, useAsync: true);

        await stream.CopyToAsync(ctx.Response.Body, ctx.RequestAborted);
        return true;
    }
}
