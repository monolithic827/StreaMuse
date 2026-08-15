using System.Runtime.InteropServices;

namespace StreaMuse.Sources;

/// <summary>One ToolHelp snapshot of every process's parent and name. Taken once per poll: WMI
/// costs ~100 ms per query, and Process.GetProcessById enumerates the whole table each call.</summary>
public sealed class ProcessTree
{
    private readonly Dictionary<int, (int Parent, string Name)> _entries = new();

    private ProcessTree()
    {
    }

    public static ProcessTree Snapshot()
    {
        var tree = new ProcessTree();
        var snapshot = CreateToolhelp32Snapshot(Th32CsSnapProcess, 0);
        if (snapshot == IntPtr.Zero || snapshot == InvalidHandle) return tree;

        try
        {
            var entry = new ProcessEntry32 { dwSize = (uint)Marshal.SizeOf<ProcessEntry32>() };
            if (!Process32First(snapshot, ref entry)) return tree;

            do
            {
                tree._entries[(int)entry.th32ProcessID] =
                    ((int)entry.th32ParentProcessID, Path.GetFileNameWithoutExtension(entry.szExeFile));
            }
            while (Process32Next(snapshot, ref entry));
        }
        finally
        {
            CloseHandle(snapshot);
        }

        return tree;
    }

    /// <summary>Executable name without extension, as Process.ProcessName reports it.</summary>
    public string? NameOf(int pid) => _entries.TryGetValue(pid, out var entry) ? entry.Name : null;

    public int ParentOf(int pid) => _entries.TryGetValue(pid, out var entry) ? entry.Parent : 0;

    /// <summary>Outermost ancestor sharing the same process name, so capture covers the whole app
    /// rather than the one child that happens to render audio.</summary>
    public int RootOfSameName(int pid)
    {
        var name = NameOf(pid);
        if (name is null) return pid;

        var current = pid;
        var guard = 0;

        while (guard++ < 32 && _entries.TryGetValue(current, out var entry) && entry.Parent > 0)
        {
            if (!string.Equals(NameOf(entry.Parent), name, StringComparison.OrdinalIgnoreCase)) break;
            current = entry.Parent;
        }

        return current;
    }

    /// <summary>True for this process or its children; our own WebView2 must never be a target.</summary>
    public bool IsSelfOrDescendant(int pid)
    {
        var self = Environment.ProcessId;
        var current = pid;
        var guard = 0;

        while (guard++ < 64 && current > 0)
        {
            if (current == self) return true;
            if (!_entries.TryGetValue(current, out var entry) || entry.Parent == current) break;
            current = entry.Parent;
        }

        return false;
    }

    private const uint Th32CsSnapProcess = 0x00000002;
    private static readonly IntPtr InvalidHandle = new(-1);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ProcessEntry32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;

        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32First(IntPtr snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32Next(IntPtr snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
}
