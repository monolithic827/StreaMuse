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

    /// <summary>SMTC hands back whatever the app supplied, so the type has to come from the bytes.
    /// Takes them rather than reading Bytes, or a track change between the two reads would label one
    /// image with another's type.</summary>
    public static string ContentTypeOf(byte[] bytes)
    {
        if (bytes.Length >= 3 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[2] == 0xFF) return "image/jpeg";
        if (bytes.Length >= 8 && bytes[0] == 0x89 && bytes[1] == 0x50) return "image/png";
        if (bytes.Length >= 12 && bytes[8] == 'W' && bytes[9] == 'E') return "image/webp";
        if (bytes.Length >= 6 && bytes[0] == 'G' && bytes[1] == 'I') return "image/gif";
        return "application/octet-stream";
    }

    private static long Fingerprint(byte[] bytes) =>
        (BitConverter.ToInt64(SHA256.HashData(bytes)) & long.MaxValue) | 1;
}
