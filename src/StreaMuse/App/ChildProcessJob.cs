using System.Diagnostics;
using System.Runtime.InteropServices;

namespace StreaMuse.App;

/// <summary>
/// Ties ffmpeg and cloudflared to our own lifetime. Windows kills a job's processes once its last
/// handle closes, which is the only thing that still covers an End task or a crash - there, none of
/// our teardown runs, and an outlived cloudflared keeps serving the tunnel it was given.
/// </summary>
public static class ChildProcessJob
{
    private static readonly IntPtr Job = Create();

    /// <summary>Adopts a child that has just started. Best effort: on every path where our own code
    /// runs, teardown stops these processes anyway.</summary>
    public static void Adopt(Process process)
    {
        if (Job == IntPtr.Zero) return;

        AssignProcessToJobObject(Job, process.Handle);
    }

    /// <summary>The handle is deliberately never closed - holding it for the life of the process is
    /// what arms the kill.</summary>
    private static IntPtr Create()
    {
        var job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) return IntPtr.Zero;

        var limits = new ExtendedLimitInformation();
        limits.Basic.LimitFlags = KillOnJobClose;

        var size = Marshal.SizeOf<ExtendedLimitInformation>();
        var buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(limits, buffer, false);
            if (SetInformationJobObject(job, ExtendedLimitInformationClass, buffer, (uint)size)) return job;
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }

        CloseHandle(job);
        return IntPtr.Zero;
    }

    private const uint KillOnJobClose = 0x2000;
    private const int ExtendedLimitInformationClass = 9;

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation Basic;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string? name);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
}
