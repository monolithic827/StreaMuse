using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace StreaMuse.State;

/// <summary>
/// Holds current app state, fans it out to connected UI sockets, and keeps the rolling log.
/// Every mutation goes through here so the UI and the REST API always agree.
/// </summary>
public sealed class StateHub
{
    private const int LogCapacity = 200;

    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,

        // Must match ControlApi: first paint comes from /api/state, updates from this socket.
        Converters = { new JsonStringEnumConverter() }
    };

    private readonly Lock _sync = new();
    private readonly LinkedList<LogLine> _log = new();
    private readonly ConcurrentDictionary<Guid, Client> _sockets = new();

    /// <summary>A socket plus its send gate - WebSockets reject overlapping SendAsync calls.</summary>
    private sealed class Client(WebSocket socket)
    {
        public WebSocket Socket { get; } = socket;
        public SemaphoreSlim SendGate { get; } = new(1, 1);
    }

    private SourceState _source = new("external", false, null, 0, "Looking for a source…", [], []);
    private NowPlaying _nowPlaying = new("Nothing playing", "-", "-", false, 0, 0, 0);
    private EncoderState _encoder = new(StreamStatus.Idle, 0, 0, 0, 0, null);
    private TunnelState _tunnel = new(TunnelStatus.Off, null, null);
    private IReadOnlyList<DependencyView> _deps = [];
    private string? _localUrl;

    /// <summary>Supplies the settings object embedded in each snapshot.</summary>
    public Func<object>? SettingsProvider { get; set; }

    public void SetSource(SourceState value) => Mutate(() => _source = value);

    public void SetNowPlaying(NowPlaying value) => Mutate(() => _nowPlaying = value);

    public void SetEncoder(EncoderState value) => Mutate(() => _encoder = value);

    public void SetTunnel(TunnelState value) => Mutate(() => _tunnel = value);

    public void SetDependencies(IReadOnlyList<DependencyView> value) => Mutate(() => _deps = value);

    public void SetLocalUrl(string? value) => Mutate(() => _localUrl = value);

    public void Log(LineLevel level, string message)
    {
        LogLine line;

        lock (_sync)
        {
            line = new LogLine(DateTime.Now.ToString("HH:mm:ss"), level.ToString().ToLowerInvariant(), message);
            _log.AddFirst(line);
            while (_log.Count > LogCapacity) _log.RemoveLast();
        }

        Console.WriteLine($"[{line.Time}] {line.Level,-5} {line.Message}");
        _ = BroadcastAsync(new { type = "log", line });
    }

    public EncoderState Encoder { get { lock (_sync) return _encoder; } }

    public TunnelState Tunnel { get { lock (_sync) return _tunnel; } }

    public SourceState Source { get { lock (_sync) return _source; } }

    public NowPlaying NowPlaying { get { lock (_sync) return _nowPlaying; } }

    public StateSnapshot Snapshot()
    {
        lock (_sync)
        {
            return new StateSnapshot(
                _source,
                _nowPlaying,
                _encoder,
                _tunnel,
                _deps,
                [.. _log],
                _localUrl,
                Environment.MachineName.ToLowerInvariant(),
                SettingsProvider?.Invoke() ?? new { });
        }
    }

    /// <summary>Pushes the 34-bar meter separately so it never forces a full state re-serialize.</summary>
    public Task PublishMeterAsync(float[] bars, double? peakDb, bool signal) =>
        BroadcastAsync(new { type = "meter", bars, peakDb, signal });

    public async Task AcceptSocketAsync(WebSocket socket, CancellationToken ct)
    {
        var id = Guid.NewGuid();
        var client = new Client(socket);
        _sockets[id] = client;

        try
        {
            await SendAsync(client, Snapshot(), CancellationToken.None);

            // Push-only; reading just holds the socket open until the client leaves.
            var buffer = new byte[1024];
            while (socket.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                var result = await socket.ReceiveAsync(buffer, ct);
                if (result.MessageType == WebSocketMessageType.Close) break;
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch (WebSocketException)
        {
        }
        finally
        {
            _sockets.TryRemove(id, out _);
        }
    }

    private void Mutate(Action change)
    {
        lock (_sync) change();
        _ = BroadcastAsync(Snapshot());
    }

    private async Task BroadcastAsync(object payload)
    {
        if (_sockets.IsEmpty) return;

        var bytes = JsonSerializer.SerializeToUtf8Bytes(payload, Json);

        foreach (var (id, client) in _sockets)
        {
            if (client.Socket.State != WebSocketState.Open)
            {
                _sockets.TryRemove(id, out _);
                continue;
            }

            try
            {
                await SendRawAsync(client, bytes, CancellationToken.None);
            }
            catch (Exception)
            {
                _sockets.TryRemove(id, out _);
            }
        }
    }

    private static Task SendAsync(Client client, object payload, CancellationToken ct) =>
        SendRawAsync(client, JsonSerializer.SerializeToUtf8Bytes(payload, Json), ct);

    private static async Task SendRawAsync(Client client, byte[] bytes, CancellationToken ct)
    {
        await client.SendGate.WaitAsync(ct);
        try
        {
            await client.Socket.SendAsync(bytes, WebSocketMessageType.Text, true, ct);
        }
        finally
        {
            client.SendGate.Release();
        }
    }
}
