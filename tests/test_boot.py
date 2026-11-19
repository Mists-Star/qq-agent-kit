"""启动协调器单测：验证失败时停手、成功时重置、熔断态直接退出。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.boot import sync_login_with_guard  # noqa: E402
from core.guard import GuardConfig, LoginGuard, MemoryStateStore  # noqa: E402
from core.notifier import CallbackChannel, Notifier  # noqa: E402


class BootTestCase(unittest.TestCase):
    def setUp(self):
        self.alerts = []
        n = Notifier(
            channels=[CallbackChannel(lambda t, b, l, e: self.alerts.append((l, b)))],
            dedup_sec=999,
        )
        self.guard = LoginGuard(n, MemoryStateStore(), GuardConfig(max_failures=3, base_backoff_sec=0.01))

    def test_always_fail_stops_after_3(self):
        calls = []

        def always_fail():
            calls.append(1)
            return False, "login expired"

        result = sync_login_with_guard(self.guard, always_fail)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 3, "最多尝试 3 次就停")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.alerts), 1, "熔断告警一次")

    def test_success_on_second(self):
        seq = iter([(False, "e1"), (True, "")])

        def login():
            return next(seq)

        result = sync_login_with_guard(self.guard, login)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.guard.state.fail_count, 0, "成功后清零")

    def test_tripped_guard_never_calls_login(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure("x")
        calls = []

        def login():
            calls.append(1)
            return True, ""

        result = sync_login_with_guard(self.guard, login)
        self.assertFalse(result.ok)
        self.assertEqual(calls, [], "已熔断时绝不调用登录")
        self.assertIn("熔断", result.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
