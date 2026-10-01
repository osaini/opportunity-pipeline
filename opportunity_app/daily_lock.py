"""The lock a daily run holds, shared by the scheduled run (daily.py) and a manual refresh (refresh.py).

Standard library only, with ROOT from the package, so the scheduled run that exits at once does not load the
pipeline and the automation stack to take a lock.
"""

from __future__ import annotations

import re
import sys

from . import ROOT

DAILY_LOCK_PATH = ROOT / "data" / "daily-run.lock"
# pipeline.py's exit code when some sources were unreachable (EX_TEMPFAIL).
TEMPFAIL_EXIT = 75


class DailyRunMutex:
    """The lock a daily run holds for its length.

    On Windows it is the named mutex scripts/run-daily.ps1 takes. A Win32 mutex
    belongs to the thread that acquired it, so acquire and release must happen
    on the same thread. Elsewhere it is a file lock that opportunity_app.daily
    takes the same way.
    """

    def __init__(self) -> None:
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return self._acquire_file_lock()
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        # Must match $mutexName in run-daily.ps1.
        name = "Local\\internship-pipeline-daily-" + re.sub(r"[\\/:]", "_", str(ROOT))
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True
        # WAIT_OBJECT_0 or WAIT_ABANDONED (a killed daily run) both grant ownership.
        if kernel32.WaitForSingleObject(handle, 0) in (0x0, 0x80):
            self._handle = (kernel32, handle)
            return True
        kernel32.CloseHandle(handle)
        return False

    def _acquire_file_lock(self) -> bool:
        # macOS and Linux: opportunity_app.daily and a manual refresh share an
        # advisory lock, which the kernel drops if the holder dies.
        import fcntl

        DAILY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = DAILY_LOCK_PATH.open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = ("file", handle)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        if self._handle[0] == "file":
            self._handle[1].close()
            self._handle = None
            return
        kernel32, handle = self._handle
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
        self._handle = None
