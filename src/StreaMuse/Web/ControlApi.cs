using System.Text.Json;
using StreaMuse.Deps;
using StreaMuse.Settings;
using StreaMuse.Sources;
using StreaMuse.State;

namespace StreaMuse.Web;

/// <summary>Loopback-only control surface. Never exposed through the tunnel.</summary>
public static class ControlApi
{
    public static void MapControlApi(this WebApplication app, int controlPort, int publicPort)
    {
        var hub = app.Services.GetRequiredService<StateHub>();
        var deps = app.Services.GetRequiredService<DependencyManager>();
        var settings = app.Services.GetRequiredService<AppSettings>();
        var artwork = app.Services.GetRequiredService<ArtworkStore>();
        var discovery = app.Services.GetRequiredService<DiscoveryService>();
        var pipeline = app.Services.GetRequiredService<Media.StreamPipeline>();
        var tunnel = app.Services.GetRequiredService<Tunnel.CloudflaredTunnel>();

        // Explicit port guard so a future listener change cannot silently widen the surface.
        app.Use(async (ctx, next) =>
        {
            if (ctx.Connection.LocalPort != controlPort)
            {
                ctx.Response.StatusCode = StatusCodes.Status404NotFound;
                return;
            }

            await next();
        });

        app.UseDefaultFiles();
        app.UseStaticFiles();

        app.MapGet("/api/state", () => Results.Json(hub.Snapshot(), StateHub.Json));

        // Versioned so the browser can cache each image forever.
        app.MapGet("/api/art", (HttpContext ctx) =>
        {
            var bytes = artwork.Bytes;
            if (bytes is null || bytes.Length == 0) return Results.NotFound();

            ctx.Response.Headers.CacheControl = "public, max-age=31536000, immutable";
            return Results.File(bytes, ArtworkStore.ContentTypeOf(bytes));
        });

        app.MapPost("/api/settings", async (HttpContext ctx) =>
        {
            var incoming = await JsonSerializer.DeserializeAsync<AppSettings>(ctx.Request.Body, StateHub.Json);
            if (incoming is null) return Results.BadRequest();

            Apply(settings, incoming.Normalized());
            settings.Save();

            // The stream key is part of the URL, and the endpoint reads the key live, so a URL
            // built at startup 404s after a key change. This also rebroadcasts the saved settings.
            hub.SetLocalUrl(HlsEndpoint.LocalUrl(publicPort, settings.StreamKey));
            hub.Log(LineLevel.Info, "settings saved - encoder changes apply on next start");
            return Results.Json(settings, StateHub.Json);
        });

        app.MapPost("/api/stream/start", async () => await pipeline.StartAsync()
            ? Results.Ok()
            : Results.Problem(hub.Encoder.Error ?? "could not start the stream"));

        app.MapPost("/api/stream/stop", async () =>
        {
            await pipeline.StopAsync();
            return Results.Ok();
        });

        app.MapPost("/api/tunnel/start", async () => await tunnel.StartAsync()
            ? Results.Ok()
            : Results.Problem(hub.Tunnel.Error ?? "could not start the tunnel"));

        app.MapPost("/api/tunnel/stop", async () =>
        {
            await tunnel.StopAsync();
            return Results.Ok();
        });

        app.MapPost("/api/deps/refresh", async () =>
        {
            await deps.EnsureAllAsync();
            return Results.Ok();
        });

        app.Map("/ws", async (HttpContext ctx) =>
        {
            if (!ctx.WebSockets.IsWebSocketRequest)
            {
                ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
                return;
            }

            using var socket = await ctx.WebSockets.AcceptWebSocketAsync();
            await hub.AcceptSocketAsync(socket, ctx.RequestAborted);
        });
    }

    /// <summary>Copies incoming values onto the live settings instance shared across the app.</summary>
    private static void Apply(AppSettings target, AppSettings source)
    {
        target.Source = source.Source;
        target.StreamKey = source.StreamKey;
        target.Width = source.Width;
        target.Height = source.Height;
        target.Fps = source.Fps;
        target.VideoBitrateKbps = source.VideoBitrateKbps;
        target.AudioBitrateKbps = source.AudioBitrateKbps;
        target.TextOverlay = source.TextOverlay;
        target.TunnelMode = source.TunnelMode;
        target.NamedTunnelToken = source.NamedTunnelToken;
        target.NamedTunnelHostname = source.NamedTunnelHostname;
        target.AutoTunnel = source.AutoTunnel;
        target.ManualProcessId = source.ManualProcessId;
        target.LogExpanded = source.LogExpanded;
        target.Theme = source.Theme;
    }
}
