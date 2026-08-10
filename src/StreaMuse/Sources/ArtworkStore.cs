using System.Security.Cryptography;

namespace StreaMuse.Sources;

/// <summary>Current album art. The version tells the renderer when to rebuild and is the cache key
/// the panel fetches /api/art with.</summary>
public sealed class ArtworkStore
{
    private readonly Lock _sync = new();
    private byte[]? _bytes;
    private long _version;

    public long Version { get { lock (_sync) return _version; } }

    public byte[]? Bytes { get { lock (_sync) return _bytes; } }

    /// <summary>Returns true when the artwork actually changed.</summary>
    public bool Set(byte[]? bytes)
    {
        var version = bytes is null || bytes.Length == 0 ? 0 : Fingerprint(bytes);

        lock (_sync)
        {
            if (version == _version) return false;

            _bytes = bytes;
            _version = version;
            return true;
        }
    }

    // Derived from the bytes, never a counter: /api/art is served immutable into a WebView2 cache
    // that outlives the process, so a counter restarting at 1 would hand the panel the previous
    // run's image. Forced positive and non-zero - the panel reads 0 as "no artwork".
    private static long Fingerprint(byte[] bytes) =>
        (BitConverter.ToInt64(SHA256.HashData(bytes)) & long.MaxValue) | 1;
}
