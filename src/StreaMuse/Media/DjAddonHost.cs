using System.IO.Compression;
using System.Reflection;
using System.Runtime.Loader;
using StreaMuse.Settings;
using StreaMuse.State;

namespace StreaMuse.Media;

/// <summary>Scans %LOCALAPPDATA%\StreaMuse\plugins for an assembly implementing <see cref="IDjAddon"/>
/// and loads the first one it finds. With nothing installed, <see cref="Addon"/> stays null and every
/// other DJ code path in the main app is a no-op - that is the whole mechanism behind shipping the
/// mixing feature separately from the app.</summary>
public sealed class DjAddonHost
{
    private DjAddonContext? _context;
    private StateHub? _hub;

    public IDjAddon? Addon { get; private set; }

    public string? LoadedFrom { get; private set; }

    /// <summary>Held from the startup scan so a plugin installed later can be loaded without one -
    /// see <see cref="TryLoadInstalled"/>.</summary>
    public void TryLoad(DjAddonContext context, StateHub hub)
    {
        _context = context;
        _hub = hub;
        TryLoadInstalled();
    }

    /// <summary>Scans the plugins folder and loads the first assembly implementing IDjAddon. Does
    /// nothing once one is loaded: the addon's Mix sits on the live audio path, so replacing it under
    /// a running stream would mean tearing down mid-mix for no real gain over restarting.</summary>
    public bool TryLoadInstalled()
    {
        if (Addon is not null || _context is null || _hub is null) return false;

        Directory.CreateDirectory(Paths.PluginsDir);

        foreach (var path in Directory.EnumerateFiles(Paths.PluginsDir, "*.dll"))
        {
            if (TryLoadFrom(path, _context, _hub)) return true;
        }

        return false;
    }

    private bool TryLoadFrom(string path, DjAddonContext context, StateHub hub)
    {
        try
        {
            var assembly = new PluginLoadContext(path).LoadFromAssemblyPath(path);

            var type = assembly.GetTypes().FirstOrDefault(t =>
                typeof(IDjAddon).IsAssignableFrom(t) && !t.IsInterface && !t.IsAbstract);

            if (type is null) return false;

            var instance = (IDjAddon)Activator.CreateInstance(type)!;
            instance.Initialize(context);

            Addon = instance;
            LoadedFrom = Path.GetFileName(path);
            hub.Log(LineLevel.Info, $"DJ plugin loaded - {LoadedFrom}");
            return true;
        }
        catch (Exception ex)
        {
            // A DLL that is not a plugin at all is the common case here (a dependency sitting beside
            // the real one), so this is only worth a line when it looked like it should have worked.
            hub.Log(LineLevel.Warn, $"could not load {Path.GetFileName(path)}: {ex.Message}");
            return false;
        }
    }

    /// <summary>Resolves a plugin's own dependencies from beside it, so a plugin can carry libraries
    /// of its own (the DJ addon ships SoundTouch.Net). Not collectible: the addon's Mix runs on the
    /// audio path for the life of the stream, so there is nothing to gain from unloading and a
    /// collectible context only adds ways to fail.</summary>
    private sealed class PluginLoadContext : AssemblyLoadContext
    {
        private readonly AssemblyDependencyResolver _resolver;
        private readonly string _directory;

        public PluginLoadContext(string pluginPath)
            : base(Path.GetFileNameWithoutExtension(pluginPath), isCollectible: false)
        {
            _resolver = new AssemblyDependencyResolver(pluginPath);
            _directory = Path.GetDirectoryName(pluginPath)!;
        }

        protected override Assembly? Load(AssemblyName name)
        {
            // The host assembly must never be resolved from the plugins folder. IDjAddon has to be
            // the same type identity as the running app's, and a second copy of StreaMuse.dll beside
            // the plugin would be a distinct type to the CLR - the cast in TryLoadFrom would fail.
            // Returning null falls through to Default, which is the already-running host.
            if (name.Name == "StreaMuse") return null;

            var resolved = _resolver.ResolveAssemblyToPath(name);
            if (resolved is not null) return LoadFromAssemblyPath(resolved);

            // The resolver needs the plugin's .deps.json, which a hand-copied plugin may not have.
            var sibling = Path.Combine(_directory, name.Name + ".dll");
            return File.Exists(sibling) ? LoadFromAssemblyPath(sibling) : null;
        }
    }

    /// <summary>Installs a .dll or .zip into the plugins folder. Returns a message describing what
    /// happened, for the panel to show.</summary>
    public static string Install(string fileName, Stream content)
    {
        Directory.CreateDirectory(Paths.PluginsDir);

        var extension = Path.GetExtension(fileName).ToLowerInvariant();
        return extension switch
        {
            ".dll" => InstallSingle(fileName, content),
            ".zip" => InstallZip(content),
            _ => throw new InvalidOperationException($"{extension} is not a plugin - install a .dll or a .zip")
        };
    }

    private static string InstallSingle(string fileName, Stream content)
    {
        var target = SafeTarget(Path.GetFileName(fileName));
        using var file = File.Create(target);
        content.CopyTo(file);
        return $"installed {Path.GetFileName(target)}";
    }

    private static string InstallZip(Stream content)
    {
        using var archive = new ZipArchive(content, ZipArchiveMode.Read);
        var written = 0;

        foreach (var entry in archive.Entries)
        {
            // Directory entries have an empty name; skip them rather than trying to create a file.
            if (entry.Name.Length == 0) continue;

            // Flattened deliberately: entry.FullName is attacker-controlled and "../" in it would
            // otherwise write anywhere on disk (zip slip). Only the leaf name is ever used, and
            // SafeTarget re-checks that the result stays inside the plugins folder.
            var target = SafeTarget(entry.Name);
            entry.ExtractToFile(target, overwrite: true);
            written++;
        }

        if (written == 0) throw new InvalidOperationException("that zip contained no files");
        return $"installed {written} file{(written == 1 ? "" : "s")} from the zip";
    }

    /// <summary>Resolves a bare file name inside the plugins folder, refusing anything that escapes it
    /// or that carries a path of its own.</summary>
    private static string SafeTarget(string name)
    {
        if (name.Length == 0 || name.Contains('/') || name.Contains('\\') || Path.IsPathRooted(name))
        {
            throw new InvalidOperationException($"refusing suspicious plugin file name '{name}'");
        }

        var target = Path.GetFullPath(Path.Combine(Paths.PluginsDir, name));
        var root = Path.GetFullPath(Paths.PluginsDir) + Path.DirectorySeparatorChar;

        if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"refusing plugin file outside the plugins folder: '{name}'");
        }

        return target;
    }

    public static IReadOnlyList<string> Installed()
    {
        Directory.CreateDirectory(Paths.PluginsDir);
        return [.. Directory.EnumerateFiles(Paths.PluginsDir).Select(Path.GetFileName).OfType<string>().Order()];
    }

    public void Shutdown()
    {
        try
        {
            Addon?.Shutdown();
        }
        catch (Exception)
        {
        }
    }
}
