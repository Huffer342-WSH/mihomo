"""Hardware metadata via Python/native APIs and read-only files; no shell tools."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys


def _text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


def _sysctl(name):
    """Call the macOS native API directly, without launching sysctl."""
    try:
        query = ctypes.CDLL(None).sysctlbyname
        query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        query.restype = ctypes.c_int
        size = ctypes.c_size_t()
        if query(name.encode(), None, ctypes.byref(size), None, 0):
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if query(name.encode(), buffer, ctypes.byref(size), None, 0):
            return None
        return buffer.raw[:size.value]
    except (AttributeError, OSError):
        return None


def _cpu():
    cpu = {"model": None, "logical_count": os.cpu_count()}
    if platform.system() == "Linux":
        for line in (_text(Path("/proc/cpuinfo")) or "").splitlines():
            if line.split(":", 1)[0].strip() in ("model name", "Hardware"):
                cpu.update(model=line.split(":", 1)[1].strip(), model_source="/proc/cpuinfo")
                break
    elif platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                cpu["model"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
                cpu["model_source"] = "winreg ProcessorNameString"
        except (ImportError, OSError):
            pass
    elif platform.system() == "Darwin":
        raw = _sysctl("machdep.cpu.brand_string")
        if raw:
            cpu.update(model=raw.rstrip(b"\0").decode("utf-8", errors="replace"), model_source="native sysctlbyname")
    if hasattr(os, "sched_getaffinity"):
        try:
            cpu["available_count"] = len(os.sched_getaffinity(0))
        except OSError:
            pass
    return cpu


def _memory():
    memory = {
        "os_total_bytes": None,
        "modules": [], "modules_status": "not_exposed",
        "model_note": "DIMM part numbers are not exposed by the Python/native interfaces selected by this collector",
    }
    if platform.system() == "Linux":
        fields = dict(re.findall(r"^(\w+):\s+(\d+) kB$", _text(Path("/proc/meminfo")) or "", re.MULTILINE))
        for source, target in (("MemTotal", "os_total_bytes"),):
            if source in fields:
                memory[target] = int(fields[source]) * 1024
        memory["capacity_source"] = "/proc/meminfo"
    elif platform.system() == "Windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [(name, ctypes.c_uint64) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended_available")]
        try:
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            query = ctypes.windll.kernel32.GlobalMemoryStatusEx
            query.argtypes = [ctypes.POINTER(MemoryStatus)]
            query.restype = ctypes.c_int
            if query(ctypes.byref(status)):
                memory.update(os_total_bytes=status.total, capacity_source="native GlobalMemoryStatusEx")
        except (AttributeError, OSError):
            pass
    elif platform.system() == "Darwin":
        total = _sysctl("hw.memsize")
        if total:
            memory.update(os_total_bytes=int.from_bytes(total, sys.byteorder), capacity_source="native sysctlbyname hw.memsize")
    return memory


def _linux_devices(major_minor):
    start = Path("/sys/dev/block") / major_minor
    if not start.exists():
        return []
    pending, seen, found = [start.resolve()], set(), {}
    while pending:
        node = pending.pop().resolve()
        if node in seen:
            continue
        seen.add(node)
        # A partition's parent directory is the whole block device.
        if (node / "partition").exists():
            pending.append(node.parent)
            continue
        try:
            slaves = list((node / "slaves").iterdir())
        except OSError:
            slaves = []
        if slaves:
            pending.extend(slaves)
            continue
        model = _text(node / "device/model") or _text(node / "device/name")
        sectors = _text(node / "size")
        subsystem = node / "device/subsystem"
        transport = subsystem.resolve().name if subsystem.exists() else None
        found[node.name] = {
            "model": model,
            "size_bytes": int(sectors) * 512 if sectors and sectors.isdigit() else None,
            "transport": transport,
        }
    return [found[name] for name in sorted(found)]


def _linux_mount(path):
    best = {}
    for line in (_text(Path("/proc/self/mountinfo")) or "").splitlines():
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 5 or len(filesystem) < 2:
            continue
        decode = lambda value: re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)
        mount_point = decode(fields[4])
        if path.is_relative_to(mount_point) and len(mount_point) > len(best.get("mount_point", "")):
            best = {"mount_point": mount_point, "filesystem": filesystem[0], "source": decode(filesystem[1])}
    return best


def _storage(path):
    storage = {
        "devices": [], "device_status": "not_exposed",
        "scope": "OS-visible backing devices; virtual machines may not expose host hardware",
    }
    try:
        usage = shutil.disk_usage(path)
        storage["filesystem_capacity"] = {"total_bytes": usage.total}
    except OSError:
        pass
    if platform.system() == "Linux":
        storage["filesystem"] = _linux_mount(path).get("filesystem")
        try:
            device = path.stat().st_dev
            major_minor = f"{os.major(device)}:{os.minor(device)}"
            storage.update(devices=_linux_devices(major_minor), provider="Python stat + Linux sysfs/procfs")
        except OSError:
            pass
    elif platform.system() == "Windows":
        # Volume identity and filesystem type do not imply a physical disk model.
        try:
            library = ctypes.windll.kernel32
            volume = ctypes.create_unicode_buffer(32768)
            query = library.GetVolumePathNameW
            query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            query.restype = ctypes.c_int
            if query(str(path), volume, len(volume)):
                filesystem = ctypes.create_unicode_buffer(256)
                info = library.GetVolumeInformationW
                info.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
                info.restype = ctypes.c_int
                if info(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
                    storage.update(filesystem=filesystem.value)
            storage["provider"] = "shutil.disk_usage + native Windows volume APIs"
        except (AttributeError, OSError):
            pass
    if storage["devices"]:
        storage["device_status"] = "available" if all(item["model"] for item in storage["devices"]) else "model_not_exposed"
    return storage


def collect_hardware(data_path):
    """Return serializable metadata; unavailable model fields remain None."""
    path = Path(data_path).resolve()
    return {"cpu": _cpu(), "memory": _memory(), "storage": _storage(path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_path", nargs="?", default=".")
    args = parser.parse_args()
    print(json.dumps(collect_hardware(args.data_path), ensure_ascii=False, indent=2))
