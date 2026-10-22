import ctypes
import os
import sys
import threading
import tracemalloc
from pathlib import Path


def process_metrics():
    values = {"process_threads": threading.active_count()}
    if tracemalloc.is_tracing():
        values["heap_used_bytes"] = tracemalloc.get_traced_memory()[0]
    # Current resident memory, not ru_maxrss (which can never decrease).
    try:
        if sys.platform == "darwin":
            class TimeValue(ctypes.Structure):
                _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]
            class Info(ctypes.Structure):
                _fields_ = [("virtual", ctypes.c_uint64), ("resident", ctypes.c_uint64), ("resident_max", ctypes.c_uint64),
                            ("user", TimeValue), ("system", TimeValue), ("policy", ctypes.c_int), ("suspend", ctypes.c_int)]
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            # mach_task_self_ is an exported variable, not a function.
            task = ctypes.c_uint.in_dll(lib, "mach_task_self_").value
            lib.task_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            lib.task_info.restype = ctypes.c_int
            info, count = Info(), ctypes.c_uint(ctypes.sizeof(Info) // ctypes.sizeof(ctypes.c_int))
            if lib.task_info(task, 20, ctypes.byref(info), ctypes.byref(count)) == 0:
                values["process_rss_bytes"] = info.resident
            values["open_fds"] = len(os.listdir("/dev/fd"))
        elif sys.platform.startswith("linux"):
            values["process_rss_bytes"] = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            values["open_fds"] = len(os.listdir("/proc/self/fd"))
            values["process_threads"] = len(os.listdir("/proc/self/task"))
    except (OSError, ValueError, AttributeError):
        pass
    return values
