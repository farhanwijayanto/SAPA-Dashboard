import os
import platform
import shutil
import ctypes
from datetime import datetime, timezone


def _read_meminfo_linux() -> tuple[int | None, int | None]:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            data = f.read().splitlines()
        mem_total_kb = None
        mem_avail_kb = None
        for line in data:
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
        total = mem_total_kb * 1024 if mem_total_kb is not None else None
        avail = mem_avail_kb * 1024 if mem_avail_kb is not None else None
        return total, avail
    except Exception:
        return None, None


def _read_meminfo_windows() -> tuple[int | None, int | None]:
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
    except Exception:
        return None, None


def get_system_metrics(disk_path: str | None = None) -> dict:
    disk_path = disk_path or os.getenv("SYSTEM_DISK_PATH") or ("/" if platform.system() != "Windows" else "C:\\")
    disk_total, disk_used, disk_free = shutil.disk_usage(disk_path)

    mem_total = None
    mem_avail = None
    system = platform.system()
    if system == "Linux":
        mem_total, mem_avail = _read_meminfo_linux()
    elif system == "Windows":
        mem_total, mem_avail = _read_meminfo_windows()

    return {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "disk_path": disk_path,
        "disk_total_bytes": int(disk_total),
        "disk_used_bytes": int(disk_used),
        "disk_free_bytes": int(disk_free),
        "memory_total_bytes": int(mem_total) if mem_total is not None else None,
        "memory_available_bytes": int(mem_avail) if mem_avail is not None else None,
    }
