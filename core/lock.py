"""单实例锁 — 防止多源夹心启动导致 Token 抢占。

实战教训（来自社区踩坑帖）：
    开机自启项 -> 看门狗 -> 注入版 launcher -> 抢 Token ->
    无头版被平台踢下线 -> 回退二维码 -> 卡死。多启动源是常见诱因。

设计：
    - 文件锁 + PID 身份，进程存活用 ctypes 校验（不是 os.kill(pid,0)，沙箱会假阴性）
    - Windows 下 os.remove 抛 WinError 32 = 真被占用（金标准）
    - 死 PID 的残留锁自动接管（有限重试）
    - 进程名二次校验，防 PID 复用误伤
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    """Windows 用 OpenProcess 判断，比 kill(pid,0) 可靠。"""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


def _is_python_process(pid: int) -> bool:
    """进程名二次校验；取不到就保守返回 True（宁可拒启不误伤）。"""
    if sys.platform != "win32":
        return True
    try:
        import subprocess

        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.lower()
        return "python" in out or not out.strip()
    except Exception:
        return True


class SingleInstanceLock:
    def __init__(self, path: Path, *, max_takeover: int = 3):
        self.path = path
        self.max_takeover = max_takeover
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(self.max_takeover + 1):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.close(fd)
                self.acquired = True
                return True
            except OSError as exc:
                if getattr(exc, "winerror", None) == 32:
                    # 真被别的进程占用
                    if self._holder_alive():
                        return False
                    self._remove_residual()
                    continue
                raise
        return False

    def _holder_alive(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except Exception:
            return False
        return _pid_alive(pid) and _is_python_process(pid)

    def _remove_residual(self) -> None:
        try:
            os.remove(self.path)
        except OSError:
            time.sleep(0.15)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
