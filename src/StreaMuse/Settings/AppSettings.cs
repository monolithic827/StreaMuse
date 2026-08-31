using System.Text.Json;
using System.Text.Json.Serialization;

namespace StreaMuse.Settings;

/// <summary>Apple/Spotify mean the desktop app; External means an explicitly picked process.</summary>
public enum MusicSource
{
    Apple,
    Spotify,
    External
}

public enum TunnelMode
{
    Quick,
    Named
}

/// <summary>Auto follows the Windows app theme.</summary>
public enum AppTheme
{
    Auto,
    Dark,
    Light
}

/// <summary>User-facing configuration. Encoder fields are read only when the stream starts.</summary>
public sealed class AppSettings
{
    public MusicSource Source { get; set; } = MusicSource.External;

    /// <summary>Path component of the public playlist: /live/{StreamKey}/index.m3u8.</summary>
    public string StreamKey { get; set; } = "parlour";

    public int Width { get; set; } = 1280;
    public int Height { get; set; } = 720;
    public int Fps { get; set; } = 10;
    public int VideoBitrateKbps { get; set; } = 400;
    public int AudioBitrateKbps { get; set; } = 320;

    public bool TextOverlay { get; set; } = true;

    public TunnelMode TunnelMode { get; set; } = TunnelMode.Quick;

    public string NamedTunnelToken { get; set; } = "";

    public string NamedTunnelHostname { get; set; } = "";

    public bool AutoTunnel { get; set; } = true;

    /// <summary>Explicit PID override; 0 means "let SourceResolver decide".</summary>
    public int ManualProcessId { get; set; }

    public bool LogExpanded { get; set; }

    public AppTheme Theme { get; set; } = AppTheme.Auto;

    /// <summary>Whether plugins/StreaMuse.DjAddon.dll should be loaded and wired into the pipeline.
    /// Has no effect at all when that file isn't present.</summary>
    public bool DjAddonEnabled { get; set; }

    public double CrossfadeSeconds { get; set; } = 8;

    /// <summary>Off by default: an unrequested air horn on a live stream is a worse first impression
    /// than a quiet one - see CLAUDE.md.</summary>
    public bool DjSfxEnabled { get; set; }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        Converters = { new JsonStringEnumConverter() }
    };

    public static string SettingsPath { get; } = Path.Combine(Paths.ConfigDir, "settings.json");

    public static AppSettings Load()
    {
        try
        {
            if (File.Exists(SettingsPath))
            {
                var json = File.ReadAllText(SettingsPath);
                var loaded = JsonSerializer.Deserialize<AppSettings>(json, JsonOptions);
                if (loaded is not null) return loaded.Normalized();
            }
        }
        catch (Exception)
        {
            // A corrupt file must never stop startup.
        }

        return new AppSettings();
    }

    public void Save()
    {
        Directory.CreateDirectory(Paths.ConfigDir);
        var tmp = SettingsPath + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(this, JsonOptions));
        File.Move(tmp, SettingsPath, overwrite: true);
    }

    /// <summary>Clamps anything a hand-edited file (or a stale schema) could have made invalid.</summary>
    public AppSettings Normalized()
    {
        StreamKey = SanitizeKey(StreamKey);
        Width = Math.Clamp(EvenUp(Width), 256, 3840);
        Height = Math.Clamp(EvenUp(Height), 256, 2160);
        Fps = Math.Clamp(Fps, 1, 30);
        VideoBitrateKbps = Math.Clamp(VideoBitrateKbps, 100, 9000);
        AudioBitrateKbps = Math.Clamp(AudioBitrateKbps, 64, 512);
        CrossfadeSeconds = Math.Clamp(CrossfadeSeconds, 2, 30);
        return this;
    }

    private static int EvenUp(int v) => v % 2 == 0 ? v : v + 1;

    private static string SanitizeKey(string key)
    {
        var cleaned = new string((key ?? "").Where(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_').ToArray());
        return cleaned.Length == 0 ? "live" : cleaned[..Math.Min(cleaned.Length, 64)];
    }
}

/// <summary>Well-known directories. Config is roaming, binaries and HLS output are machine-local.</summary>
public static class Paths
{
    public static string ConfigDir { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "StreaMuse");

    public static string DataDir { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "StreaMuse");

    public static string BinDir { get; } = Path.Combine(DataDir, "bin");

    public static string HlsDir { get; } = Path.Combine(DataDir, "hls");

    /// <summary>Where DjAddonHost looks for StreaMuse.DjAddon.dll.</summary>
    public static string PluginsDir { get; } = Path.Combine(DataDir, "plugins");

    /// <summary>Scratch space for the DJ addon's downloaded/decoded audio.</summary>
    public static string DjCacheDir { get; } = Path.Combine(DataDir, "dj-cache");

    /// <summary>Drop-in library the DJ addon picks sound effects from at random. Unlike DjCacheDir,
    /// this is a library the user maintains, not scratch space the app owns.</summary>
    public static string DjSfxDir { get; } = Path.Combine(DataDir, "dj-sfx");
}
