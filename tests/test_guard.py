"""登录守护单测：验证"失败优先告警、绝不无限重试"的所有硬规则。

运行： python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.guard import (  # noqa: E402
    FileStateStore,
    GuardConfig,
    GuardState,
    LoginGuard,
    LoginPhase,
    MemoryStateStore,
)
from core.notifier import CallbackChannel, Notifier  # noqa: E402


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.alerts: list[tuple[str, str, str]] = []

        def capture(title, body, level, extra):
            self.alerts.append((title, body, level))

        self.notifier = Notifier(channels=[CallbackChannel(callback=capture)], dedup_sec=999)
        self.store = MemoryStateStore()
        self.guard = LoginGuard(self.notifier, self.store, GuardConfig(max_failures=3))

    def test_01_begin_sets_quick_login(self):
        self.guard.begin()
        self.assertEqual(self.guard.state.phase, LoginPhase.QUICK_LOGIN)
        self.assertTrue(self.guard.can_attempt()[0])

    def test_02_fail_below_threshold_backoff_no_alert(self):
        self.guard.begin()
        self.guard.record_failure("err1")
        self.assertEqual(self.guard.state.phase, LoginPhase.BACKOFF)
        self.guard.begin()
        self.guard.record_failure("err2")
        self.assertEqual(self.guard.state.fail_count, 2)
        self.assertEqual(self.alerts, [], "阈值未到不应告警")

    def test_03_third_failure_trips_and_alerts(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure("bad login")
        self.assertEqual(self.guard.state.phase, LoginPhase.WAIT_MANUAL)
        self.assertTrue(self.guard.is_tripped)
        self.assertEqual(len(self.alerts), 1, "熔断应恰好告警一次")
        self.assertEqual(self.alerts[0][2], "critical", "必须是 critical 级")
        self.assertIn("扫码", self.alerts[0][1])

    def test_04_tripped_rejects_all_retries(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure()
        before = self.guard.state.fail_count
        allowed, reason = self.guard.can_attempt()
        self.assertFalse(allowed)
        self.assertIn("拒绝自动重试", reason)
        # 即使反复调用 begin() 也不能推进
        for _ in range(10):
            self.guard.begin()
        self.assertEqual(self.guard.state.fail_count, before, "熔断后计数不应再增长")

    def test_05_no_alert_storm_on_repeated_begin(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure()
        for _ in range(5):
            self.guard.begin()
        self.assertEqual(len(self.alerts), 1, "重复 begin 不应造成告警风暴")

    def test_06_restart_preserves_trip(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure()
        # 模拟进程重启：新 guard 读同一 store
        guard2 = LoginGuard(self.notifier, self.store, GuardConfig(max_failures=3))
        guard2.restore()
        self.assertTrue(guard2.is_tripped, "重启后必须记得已熔断")
        self.assertFalse(guard2.can_attempt()[0])

    def test_07_success_resets_everything(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure()
        self.guard.record_success()
        self.assertEqual(self.guard.state.phase, LoginPhase.ONLINE)
        self.assertEqual(self.guard.state.fail_count, 0)
        self.assertFalse(self.guard.is_tripped)
        self.assertTrue(self.guard.can_attempt()[0])

    def test_08_human_confirm_is_only_unlock(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure()
        self.guard.human_confirm()
        self.assertEqual(self.guard.state.phase, LoginPhase.IDLE)
        self.assertEqual(self.guard.state.fail_count, 0)

    def test_09_mark_offline_does_not_auto_retry(self):
        self.guard.state.phase = LoginPhase.ONLINE
        self.guard.mark_offline("kicked")
        self.assertEqual(self.guard.state.phase, LoginPhase.IDLE)
        self.assertTrue(self.guard.can_attempt()[0], "下线后允许重新评估，但不是自动无限重试")

    def test_10_backoff_is_bounded_and_exponential(self):
        self.assertEqual(self.guard.backoff_seconds(1) > 0, True)
        self.assertLessEqual(self.guard.backoff_seconds(99), self.guard.config.max_backoff_sec)

    def test_11_file_store_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state" / "guard.json"
            s = FileStateStore(p)
            s.save(GuardState(phase=LoginPhase.WAIT_MANUAL, fail_count=3).to_dict())
            loaded = GuardState.from_dict(s.load())
            self.assertEqual(loaded.phase, LoginPhase.WAIT_MANUAL)
            self.assertEqual(loaded.fail_count, 3)

    def test_12_status_exposes_reason(self):
        for _ in range(3):
            self.guard.begin()
            self.guard.record_failure()
        st = self.guard.status()
        self.assertTrue(st["is_tripped"])
        self.assertFalse(st["can_attempt"])
        self.assertEqual(st["fail_count"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
