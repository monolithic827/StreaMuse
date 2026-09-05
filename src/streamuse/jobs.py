"""Ties ffmpeg, cloudflared and go-librespot to our own lifetime. Windows kills a job's processes
once its last handle closes, which is the only thing that still covers an End task or a crash -
there none of our teardown runs, and an outlived cloudflared keeps serving the tunnel it was given."""

import ctypes
import subprocess
from ctypes import wintypes

_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA_TERMINATE = 0x0100 | 0x0001

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create() -> int:
    """The handle is deliberately never closed - holding it for the life of the process is what
    arms the kill."""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0

    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE

    if _kernel32.SetInformationJobObject(
        wintypes.HANDLE(job), _EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        return job

    _kernel32.CloseHandle(wintypes.HANDLE(job))
    return 0


_JOB = _create()


def adopt(process: subprocess.Popen) -> None:
    """Adopts a child that has just started. Best effort: on every path where our own code runs,
    teardown stops these processes anyway."""
    if not _JOB:
        return

    # Popen keeps its handle only on Windows and closes it on wait(); reopen by pid so an adopt
    # racing a wait cannot pass a dead handle.
    handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA_TERMINATE, False, process.pid)
    if not handle:
        return
    try:
        _kernel32.AssignProcessToJobObject(wintypes.HANDLE(_JOB), wintypes.HANDLE(handle))
    finally:
        _kernel32.CloseHandle(wintypes.HANDLE(handle))
