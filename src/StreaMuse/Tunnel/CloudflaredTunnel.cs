using System.Diagnostics;
using System.Text.RegularExpressions;
using StreaMuse.App;
using StreaMuse.Deps;
using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Tunnel;

/// <summary>Publishes the HLS port through Cloudflare. Quick tunnels need no account but get a
/// random hostname, printed on stderr; named tunnels take a token and keep a stable one.</summary>
public sealed partial class CloudflaredTunnel(AppSettings settings, StateHub hub, DependencyManager deps, int publicPort)
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Process? _process;

    public bool Running => _process is { HasExited: false };

    public async Task<bool> StartAsync()
    {
        await _gate.WaitAsync();
        try
        {
            if (Running) return true;

            if (deps.CloudflaredPath is null)
            {
                Fail("cloudflared is not available - check the Dependencies panel");
                return false;
            }

            var named = settings.TunnelMode == TunnelMode.Named;
            if (named && string.IsNullOrWhiteSpace(settings.NamedTunnelToken))
            {
                Fail("named tunnel selected but no token is configured");
                return false;
            }

            hub.SetTunnel(new TunnelState(TunnelStatus.Starting, null, null));

            var info = new ProcessStartInfo(deps.CloudflaredPath)
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            foreach (var argument in BuildArguments(named)) info.ArgumentList.Add(argument);

            var process = new Process { StartInfo = info, EnableRaisingEvents = true };
            process.OutputDataReceived += (_, e) => Inspect(e.Data);
            process.ErrorDataReceived += (_, e) => Inspect(e.Data);
            process.Exited += (_, _) =>
            {
                // A stopped process can report late; it must not mark a newer tunnel down.
                if (!ReferenceEquals(_process, process)) return;
                hub.SetTunnel(new TunnelState(TunnelStatus.Off, null, null));
                hub.Log(LineLevel.Warn, "cloudflared exited");
            };

            process.Start();
            ChildProcessJob.Adopt(process);
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            _process = process;

            if (named)
            {
                var host = settings.NamedTunnelHostname.Trim();
                var url = string.IsNullOrWhiteSpace(host)
                    ? null
                    : $"https://{host}/live/{settings.StreamKey}/index.m3u8";

                hub.SetTunnel(new TunnelState(TunnelStatus.Up, url, null));
                hub.Log(LineLevel.Info, url is null
                    ? "named tunnel running - set the hostname in Settings to show its URL"
                    : $"named tunnel running - {url}");
            }

            return true;
        }
        catch (Exception ex)
        {
            Fail(ex.Message);
            return false;
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task StopAsync()
    {
        await _gate.WaitAsync();
        try
        {
            var process = Interlocked.Exchange(ref _process, null);
            if (process is null) return;

            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                    process.WaitForExit(3000);
                }
            }
            catch (Exception)
            {
            }
            finally
            {
                process.Dispose();
            }

            hub.SetTunnel(new TunnelState(TunnelStatus.Off, null, null));
        }
        finally
        {
            _gate.Release();
        }
    }

    private IReadOnlyList<string> BuildArguments(bool named) => named
        ?
        [
            "tunnel", "--no-autoupdate", "run",
            "--token", settings.NamedTunnelToken.Trim()
        ]
        :
        [
            "tunnel", "--no-autoupdate",
            "--url", $"http://127.0.0.1:{publicPort}"
        ];

    /// <summary>Watches cloudflared's output for the assigned quick-tunnel hostname.</summary>
    private void Inspect(string? line)
    {
        if (string.IsNullOrWhiteSpace(line)) return;

        var match = QuickTunnelUrlRegex().Match(line);
        if (match.Success && hub.Tunnel.PublicUrl is null)
        {
            var url = $"{match.Value}/live/{settings.StreamKey}/index.m3u8";
            hub.SetTunnel(new TunnelState(TunnelStatus.Up, url, null));
            hub.Log(LineLevel.Info, $"tunnel up - {url}");
            return;
        }

        if (line.Contains("ERR ", StringComparison.Ordinal) ||
            line.Contains("error", StringComparison.OrdinalIgnoreCase))
        {
            hub.Log(LineLevel.Warn, $"cloudflared: {Shorten(line)}");
        }
    }

    private void Fail(string message)
    {
        hub.Log(LineLevel.Error, $"tunnel: {message}");
        hub.SetTunnel(new TunnelState(TunnelStatus.Error, null, message));
    }

    private static string Shorten(string line) =>
        line.Length <= 200 ? line.Trim() : line.Trim()[..200] + "…";

    [GeneratedRegex(@"https://[a-z0-9-]+\.trycloudflare\.com")]
    private static partial Regex QuickTunnelUrlRegex();
}
