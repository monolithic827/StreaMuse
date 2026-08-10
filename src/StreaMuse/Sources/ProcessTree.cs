using System.Diagnostics;
using System.Runtime.InteropServices;

namespace StreaMuse.Sources;

/// <summary>Parent/child lookups via ToolHelp; WMI costs ~100 ms per query and this is polled
/// every second.</summary>
public static class ProcessTree
{
    /// <summary>Every pid on the machine mapped to its parent pid.</summary>
    public static Dictionary<int, int> ParentMap()
    {
        var map = new Dictionary<int, int>();
        var snapshot = CreateToolhelp32Snapshot(Th32CsSnapProcess, 0);
        if (snapshot == IntPtr.Zero || snapshot == InvalidHandle) return map;

        try
        {
            var entry = new ProcessEntry32 { dwSize = (uint)Marshal.SizeOf<ProcessEntry32>() };
            if (!Process32First(snapshot, ref entry)) return map;

            do
            {
                map[(int)entry.th32ProcessID] = (int)entry.th32ParentProcessID;
            }
            while (Process32Next(snapshot, ref entry));
        }
        finally
        {
            CloseHandle(snapshot);
        }

        return map;
    }

    /// <summary>Outermost ancestor sharing the same process name, so capture covers the whole app
    /// rather than the one child that happens to render audio.</summary>
    public static int RootOfSameName(int pid, Dictionary<int, int>? parents = null)
    {
        parents ??= ParentMap();

        var name = NameOf(pid);
        if (name is null) return pid;

        var current = pid;
        var guard = 0;

        while (guard++ < 32 && parents.TryGetValue(current, out var parent) && parent > 0)
        {
            if (!string.Equals(NameOf(parent), name, StringComparison.OrdinalIgnoreCase)) break;
            current = parent;
        }

        return current;
    }

    /// <summary>True for this process or its children; our own WebView2 must never be a target.</summary>
    public static bool IsSelfOrDescendant(int pid, Dictionary<int, int>? parents = null)
    {
        parents ??= ParentMap();

        var self = Environment.ProcessId;
        var current = pid;
        var guard = 0;

        while (guard++ < 64 && current > 0)
        {
            if (current == self) return true;
            if (!parents.TryGetValue(current, out var parent) || parent == current) break;
            current = parent;
        }

        return false;
    }

    private static string? NameOf(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            return process.ProcessName;
        }
        catch (Exception)
        {
            return null;
        }
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
