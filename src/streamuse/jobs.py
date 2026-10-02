"""Ties ffmpeg, cloudflared, go-librespot and yt-dlp to our own lifetime. Windows kills a job's
processes once its last handle closes, which is the only thing that still covers an End task or a
crash - there none of our teardown runs, and an outlived cloudflared keeps serving the tunnel it
was given."""

import asyncio
import contextlib
import ctypes
import subprocess
import time
from ctypes import wintypes

_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA_TERMINATE = 0x0100 | 0x0001
_PROCESS_TERMINATE = 0x0001
_SNAPSHOT_PROCESSES = 0x2

END_TIMEOUT = 3

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


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


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


async def end(process) -> None:
    """Stops yt-dlp.exe, which is a PyInstaller one-file exe: a bootloader that unpacks to temp and
    runs the real program as a child of its own. Killing the bootloader leaves that child running
    and the unpacked folder behind for good - measured, both. Ending the child instead lets the
    bootloader see it exit, clean up and follow within half a second. Its first few hundred
    milliseconds are the unpacking itself, with no child to end yet, so this waits for one."""
    deadline = time.monotonic() + END_TIMEOUT
    while process.returncode is None and not _end_children(process.pid):
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(process.wait(), END_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(OSError, ProcessLookupError):
            process.kill()


def _end_children(pid: int) -> bool:
    children = _same_image_children(pid)
    for child in children:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, child)
        if handle:
            _kernel32.TerminateProcess(wintypes.HANDLE(handle), 1)
            _kernel32.CloseHandle(wintypes.HANDLE(handle))
    return bool(children)


def _same_image_children(pid: int) -> list[int]:
    """Parent ids outlive their process and get reused, so a child only counts when it runs the
    same image as the process it claims as its parent - which a bootloader's child always does."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_SNAPSHOT_PROCESSES, 0)
    if snapshot == -1:
        return []

    entries = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = _kernel32.Process32FirstW(wintypes.HANDLE(snapshot), ctypes.byref(entry))
        while more:
            entries.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            more = _kernel32.Process32NextW(wintypes.HANDLE(snapshot), ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(wintypes.HANDLE(snapshot))

    image = next((name for process, _, name in entries if process == pid), None)
    return [process for process, parent, name in entries if parent == pid and name == image]
