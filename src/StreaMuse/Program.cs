using System.Net;
using Microsoft.Extensions.FileProviders;
using StreaMuse.App;
using StreaMuse.Deps;
using StreaMuse.Settings;
using StreaMuse.Media;
using StreaMuse.Sources;
using StreaMuse.Tunnel;
using StreaMuse.State;
using StreaMuse.Web;

namespace StreaMuse;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Contains("--probe"))
        {
            ConsoleHost.Attach();
            SourceProbe.RunAsync().GetAwaiter().GetResult();
            return;
        }

        if (args.Contains("--test-capture"))
        {
            ConsoleHost.Attach();
            Capture.CaptureSelfTest.RunAsync(args).GetAwaiter().GetResult();
            return;
        }

        ApplicationConfiguration.Initialize();

        Directory.CreateDirectory(Paths.ConfigDir);
        Directory.CreateDirectory(Paths.DataDir);
        Directory.CreateDirectory(Paths.HlsDir);

        var controlPort = PortFinder.Pick(7788);
        var publicPort = PortFinder.Pick(7789);

        var settings = AppSettings.Load();
        var hub = new StateHub { Settings = settings };
        var deps = new DependencyManager(hub);
        var artwork = new ArtworkStore();
        var discovery = new DiscoveryService(settings, hub, artwork);
        var tunnel = new CloudflaredTunnel(settings, hub, deps, publicPort);
        var pipeline = new StreamPipeline(settings, hub, deps, discovery, artwork, tunnel);

        discovery.TargetChanged += pipeline.OnTargetChanged;

        var app = BuildWebApp(
            args, settings, hub, deps, artwork, discovery, tunnel, pipeline, controlPort, publicPort);

        hub.SetLocalUrl(HlsEndpoint.LocalUrl(publicPort, settings.StreamKey));

        var lifetime = new CancellationTokenSource();
        app.RunAsync().ContinueWith(t =>
        {
            if (t.IsFaulted)
                hub.Log(LineLevel.Error, $"web host stopped: {t.Exception?.GetBaseException().Message}");
        }, TaskScheduler.Default);

        hub.Log(LineLevel.Info, $"control panel on 127.0.0.1:{controlPort}, HLS on 127.0.0.1:{publicPort}");

        _ = Task.Run(async () =>
        {
            try
            {
                await deps.EnsureAllAsync(lifetime.Token);
            }
            catch (Exception ex)
            {
                hub.Log(LineLevel.Error, $"dependency check failed: {ex.Message}");
            }
        }, lifetime.Token);

        _ = Task.Run(() => discovery.RunAsync(lifetime.Token), lifetime.Token);

        using var window = new MainWindow($"http://127.0.0.1:{controlPort}/", hub, settings);
        Application.Run(window);

        Shutdown(lifetime, pipeline, app, hub);
    }

    /// <summary>
    /// Runs with the window already gone and nothing above it to catch anything, so a step that
    /// throws here is a crash dialog rather than a message. Every step is attempted regardless of
    /// the ones before it: leaving ffmpeg or cloudflared running would keep the stream publicly live.
    /// </summary>
    private static void Shutdown(
        CancellationTokenSource lifetime,
        StreamPipeline pipeline,
        WebApplication app,
        StateHub hub)
    {
        Attempt(hub, "cancel background work", lifetime.Cancel);
        Attempt(hub, "stop the stream", () => pipeline.StopAsync().GetAwaiter().GetResult());
        Attempt(hub, "stop the web host",
            () => app.StopAsync(TimeSpan.FromSeconds(3)).GetAwaiter().GetResult());
    }

    private static void Attempt(StateHub hub, string what, Action step)
    {
        try
        {
            step();
        }
        catch (Exception ex)
        {
            hub.Log(LineLevel.Error, $"shutdown: could not {what} - {ex.Message}");
        }
    }

    private static WebApplication BuildWebApp(
        string[] args,
        AppSettings settings,
        StateHub hub,
        DependencyManager deps,
        ArtworkStore artwork,
        DiscoveryService discovery,
        CloudflaredTunnel tunnel,
        StreamPipeline pipeline,
        int controlPort,
        int publicPort)
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions
        {
            Args = args,
            ContentRootPath = AppContext.BaseDirectory
        });

        builder.Logging.ClearProviders();
        builder.WebHost.ConfigureKestrel(options =>
        {
            options.Listen(IPAddress.Loopback, controlPort);
            options.Listen(IPAddress.Loopback, publicPort);
        });

        builder.Services.AddSingleton(settings);
        builder.Services.AddSingleton(hub);
        builder.Services.AddSingleton(deps);
        builder.Services.AddSingleton(artwork);
        builder.Services.AddSingleton(discovery);
        builder.Services.AddSingleton(tunnel);
        builder.Services.AddSingleton(pipeline);

        var app = builder.Build();

        app.Environment.WebRootFileProvider =
            new ManifestEmbeddedFileProvider(typeof(Program).Assembly, "wwwroot");

        app.UseWebSockets(new WebSocketOptions { KeepAliveInterval = TimeSpan.FromSeconds(20) });

        // Order matters: the public-port filter terminates, so nothing below it is tunnel-reachable.
        app.MapPublicHls(publicPort, settings);

        app.MapControlApi(controlPort, publicPort);

        return app;
    }
}
