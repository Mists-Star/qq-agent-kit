"""单实例锁测试：正常获取/释放、活进程拒启、死 PID 接管、跨平台冲突信号。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.lock import SingleInstanceLock  # noqa: E402


class LockTestCase(unittest.TestCase):
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state" / "bot.lock"  # 父目录不存在，验证自动创建
            lock = SingleInstanceLock(p)
            self.assertTrue(lock.acquire())
            self.assertTrue(p.exists())
            self.assertEqual(int(p.read_text().strip()), os.getpid())
            lock.release()
            self.assertFalse(p.exists())

    def test_live_holder_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.lock"
            p.write_text(f"{os.getpid()}\n", encoding="utf-8")  # 当前进程 = 活的
            # 进程名校验通过（当前确实是 python）
            lock = SingleInstanceLock(p)
            self.assertFalse(lock.acquire(), "活进程持有的锁应拒绝")

    def test_dead_pid_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.lock"
            p.write_text("99999999\n", encoding="utf-8")  # 不存在的 PID
            lock = SingleInstanceLock(p)
            self.assertTrue(lock.acquire(), "死 PID 的残留锁应能接管")
            self.assertEqual(int(p.read_text().strip()), os.getpid())
            lock.release()

    def test_corrupt_lock_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.lock"
            p.write_text("garbage\n", encoding="utf-8")
            lock = SingleInstanceLock(p)
            self.assertTrue(lock.acquire(), "损坏的锁文件应能接管")
            lock.release()

    def test_context_manager(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.lock"
            with SingleInstanceLock(p) as lock:
                self.assertTrue(lock.acquired)
                self.assertTrue(p.exists())
            self.assertFalse(p.exists(), "with 退出应释放锁")

    def test_posix_fileexistserror_signal(self):
        """模拟 POSIX：O_EXCL 冲突抛 FileExistsError（无 winerror 属性），应走占用判断而非崩溃。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.lock"
            p.write_text(f"{os.getpid()}\n", encoding="utf-8")
            lock = SingleInstanceLock(p)
            # 无论平台，FileExistsError 分支都应返回 False（活持有者）
            with patch.object(lock, "_holder_alive", return_value=True):
                self.assertFalse(lock.acquire())


if __name__ == "__main__":
    unittest.main(verbosity=2)
