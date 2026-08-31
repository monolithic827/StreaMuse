using StreaMuse.Dj.Audio;

namespace StreaMuse.Dj.Sfx;

/// <summary>A folder the user drops audio files into; the addon picks one at random each time it
/// wants a sound effect. Deliberately just a folder, not a manifest or a naming convention - the
/// whole point is "drop files in, it works," with nothing to configure per file.</summary>
public sealed class SfxLibrary(string directory, Func<string?> ffmpegPath)
{
    private static readonly string[] Extensions =
        [".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma"];

    private string? _lastPicked;

    /// <summary>Picks a file at random, decodes it, and returns it - or null if the folder is empty,
    /// ffmpeg isn't ready yet, or the pick failed to decode (a corrupt or unsupported file left in
    /// the folder costs nothing beyond that one trigger being silently skipped).</summary>
    public async Task<float[]?> PickAsync(int sampleRate, int channels, CancellationToken ct)
    {
        var ffmpeg = ffmpegPath();
        if (ffmpeg is null) return null;

        var path = Pick();
        if (path is null) return null;

        try
        {
            return await AudioDecoder.DecodeAsync(ffmpeg, path, sampleRate, channels, ct);
        }
        catch (Exception)
        {
            return null;
        }
    }

    private string? Pick()
    {
        List<string> files;
        try
        {
            files = Directory.EnumerateFiles(directory)
                .Where(f => Extensions.Contains(Path.GetExtension(f), StringComparer.OrdinalIgnoreCase))
                .ToList();
        }
        catch (IOException)
        {
            return null;
        }

        if (files.Count == 0) return null;

        // Avoid the same clip twice in a row when there is a choice - otherwise a two-file folder
        // reads as broken repetition rather than variety.
        var candidates = files.Count > 1 ? files.Where(f => f != _lastPicked).ToList() : files;

        // Random.Shared, not a private instance: PickAsync can run on two overlapping background
        // tasks if a decode is unusually slow and outlasts the cooldown that normally keeps triggers
        // from overlapping, and a plain Random instance is not safe to call from two threads at once.
        // Random.Shared is.
        var picked = candidates[Random.Shared.Next(candidates.Count)];
        _lastPicked = picked;
        return picked;
    }
}
