"""转到已经建好的虚拟环境，再启动 MCP。不安装依赖。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    candidates = (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    )
    python = next((item for item in candidates if item.is_file()), None)
    if python is None:
        sys.stderr.write(
            "没有找到虚拟环境。请先在项目目录执行：\n"
            "macOS：python3.13 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt "
            "&& .venv/bin/python -m playwright install chromium\n"
            "Windows：py -3.13 -m venv .venv && .venv\\Scripts\\python.exe -m pip install -r requirements.txt "
            "&& .venv\\Scripts\\python.exe -m playwright install chromium\n"
        )
        raise SystemExit(1)
    os.chdir(ROOT)
    handoff(str(python))


def handoff(executable: str, platform: str | None = None) -> None:
    """用虚拟环境里的解释器接上当前进程的标准输入输出。

    macOS 用 execv 替换当前进程。Windows 的 execv 会先退出 Cursor 启动的那个进程，
    stdio 连接随即被关掉，所以这里改成一直等待子进程。
    """

    argv = [executable, "-m", "seller_auto.mcp_server"]
    system = platform or os.name
    if system == "nt":
        raise SystemExit(_wait_on_windows(argv))
    os.execv(executable, argv)


def _wait_on_windows(argv: list[str]) -> int:
    import ctypes

    job = _windows_kill_on_close_job()
    process = subprocess.Popen(argv)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    if job is not None:
        kernel32.AssignProcessToJobObject(job, int(process._handle))
    try:
        return process.wait()
    finally:
        if job is not None:
            kernel32.CloseHandle(job)


def _windows_kill_on_close_job() -> int | None:
    """子进程放进作业对象。启动器被结束时，作业句柄关闭，子进程一起退出。"""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("_pad_after_flags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("_pad_after_active", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    return int(job)


if __name__ == "__main__":
    main()
