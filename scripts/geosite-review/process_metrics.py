"""Per-child OS high-water memory and CPU counters, without polling or injection."""
import ctypes
import os
import subprocess
import sys
import time


def metric_description():
    if os.name == "nt":
        return {"resident": "Windows PROCESS_MEMORY_COUNTERS.PeakWorkingSetSize (bytes)",
                "commit": "Windows PROCESS_MEMORY_COUNTERS.PeakPagefileUsage (bytes)",
                "collector": "GetProcessMemoryInfo/GetProcessTimes after exit, process handle retained"}
    if sys.platform == "linux":
        return {"resident": "Linux wait4(pid).ru_maxrss * 1024 (bytes)",
                "collector": "per-child wait4, not cumulative RUSAGE_CHILDREN"}
    raise RuntimeError("peak-memory measurement currently supports Windows and Linux")


def _windows_counters(process):
    from ctypes import wintypes as w
    class Counters(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage")]
    memory_info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    memory_info.argtypes = [w.HANDLE, ctypes.POINTER(Counters), w.DWORD]
    memory_info.restype = w.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    # Popen retains its Windows process handle after wait(). Query before the
    # object is released; no PID lookup races with process termination.
    handle = int(process._handle)
    if not memory_info(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    get_times = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
    get_times.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    get_times.restype = w.BOOL
    creation, exit_time, kernel, user = (w.FILETIME() for _ in range(4))
    if not get_times(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    ticks = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
    return {"peak_resident_bytes": counters.PeakWorkingSetSize,
            "peak_commit_bytes": counters.PeakPagefileUsage,
            "cpu_ms": (ticks(kernel) + ticks(user)) / 10000}


def measure_process(command, *, cwd=None, env=None, stdout=None):
    """Return wall time, CPU time and lifetime peak memory for exactly this child."""
    metric_description()  # Fail before launching on unsupported platforms.
    start = time.perf_counter_ns()
    with subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT) as process:
        if os.name == "nt":
            returncode = process.wait()
            elapsed = (time.perf_counter_ns() - start) / 1e6
            counters = _windows_counters(process)
        else:
            _, status, usage = os.wait4(process.pid, 0)
            elapsed = (time.perf_counter_ns() - start) / 1e6
            returncode = process.returncode = os.waitstatus_to_exitcode(status)
            counters = {"peak_resident_bytes": usage.ru_maxrss * 1024,
                        "cpu_ms": (usage.ru_utime + usage.ru_stime) * 1000}
        if counters["peak_resident_bytes"] <= 0:
            raise RuntimeError("OS returned no peak resident memory; refusing a zero-memory result")
        return {"returncode": returncode, "load_ms": elapsed, **counters}
