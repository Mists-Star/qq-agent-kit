"""审查发现问题的回归测试 — 覆盖此前测试盲区。

每条对应 TODO-审查问题清单.md 中的一个编号。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import BotApp, BotConfig  # noqa: E402
from core.degenerate import DegenerateDetector  # noqa: E402
from core.guard import GuardConfig, LoginGuard, LoginPhase, MemoryStateStore  # noqa: E402
from core.modules import Module, ModuleRegistry  # noqa: E402
from core.notifier import CallbackChannel, Notifier, WebhookChannel  # noqa: E402
from core.qzone_bridge import QZoneBridge, QZoneError  # noqa: E402
from tests.test_integration import FakeClient  # noqa: E402


def make_notifier():
    alerts: list[tuple[str, str, str]] = []
    n = Notifier(
        channels=[CallbackChannel(lambda t, b, l, e: alerts.append((t, b, l)))],
        dedup_sec=999,
    )
    return n, alerts


class RegressTestCase(unittest.TestCase):
    # P0-4: WebhookChannel JSON 必合法
    def test_p0_webhook_payload_valid_json(self):
        ch = WebhookChannel(url="http://example.invalid")
        payload = ch.build_payload("标题", "正文含\"引号\"与{花括号}\n换行", "critical")
        obj = json.loads(payload)  # 不抛 = 合法
        self.assertEqual(obj["msgtype"], "text")
        self.assertIn("引号", obj["text"]["content"])

    # P2-12: 去重分级，critical 不被 warning 抑制
    def test_p2_dedup_level_escalation(self):
        n, alerts = make_notifier()
        n.notify("x", "warn", "warning", fingerprint="same")
        n.notify("x", "crit", "critical", fingerprint="same")
        self.assertEqual(len(alerts), 2, "级别升级应穿透去重")
        self.assertEqual(alerts[-1][2], "critical")

    # P2-12: 去重表有上限
    def test_p2_dedup_bounded(self):
        n, _ = make_notifier()
        n.max_seen = 10
        for i in range(100):
            n.notify("x", str(i), fingerprint=f"fp{i}")
        self.assertLessEqual(len(n._seen), 10)

    # P1-1: 配置加载未知键报错
    def test_p1_config_unknown_key_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"ws_url":"x","typo_key":1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                BotConfig.from_json(p)

    # P1-1: 配置加载合法字段
    def test_p1_config_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"ws_url":"ws://x","expect_uin":123}', encoding="utf-8")
            cfg = BotConfig.from_json(p)
            self.assertEqual(cfg.expect_uin, 123)
            self.assertEqual(cfg.ws_url, "ws://x")

    # P1-3: mark_offline 不能从熔断态解锁
    def test_p1_mark_offline_cannot_unlock_trip(self):
        n, _ = make_notifier()
        g = LoginGuard(n, MemoryStateStore(), GuardConfig(max_failures=2))
        for _ in range(2):
            g.begin()
            g.record_failure()
        self.assertTrue(g.is_tripped)
        g.mark_offline("kicked")
        self.assertTrue(g.is_tripped, "mark_offline 不得解除熔断")

    # P2-2: begin 熔断态不周期告警
    def test_p2_begin_tripped_no_repeat_alert(self):
        n, alerts = make_notifier()
        g = LoginGuard(n, MemoryStateStore(), GuardConfig(max_failures=2))
        for _ in range(2):
            g.begin()
            g.record_failure()
        count = len(alerts)
        for _ in range(5):
            g.begin()
        self.assertEqual(len(alerts), count, "熔断态 begin 不应重复告警")

    # P2-10: from_dict 鲁棒性
    def test_p2_from_dict_malformed(self):
        from core.guard import GuardState

        s = GuardState.from_dict({"phase": {"bad": 1}, "fail_count": "oops"})
        self.assertEqual(s.phase, LoginPhase.IDLE)
        self.assertEqual(s.fail_count, 0)

    # P1-7: 同名模块替换不双执行
    def test_p1_module_replace_no_double_hook(self):
        calls = []

        class M(Module):
            name = "dup"

            def __init__(self, tag):
                self.tag = tag

            async def on_startup(self):
                calls.append(self.tag)

        reg = ModuleRegistry()
        reg.register(M("old"))
        reg.register(M("new"))
        asyncio.run(reg.emit("on_startup"))
        self.assertEqual(calls, ["new"], f"同名模块应只执行新实例: {calls}")

    # P2-13: retcode 字符串 "0" 应成功
    def test_p2_retcode_string_zero_ok(self):
        bridge = QZoneBridge()
        with patch.object(bridge, "call", wraps=bridge.call):
            import urllib.request

            class FakeResp:
                status = 200

                def read(self):
                    return b'{"retcode":"0","data":{}}'

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            with patch.object(urllib.request, "urlopen", return_value=FakeResp()):
                result = bridge.call("get_status")
                self.assertEqual(result["retcode"], "0")

    # P1-6: 退化多周期
    def test_p1_degenerate_various_periods(self):
        d = DegenerateDetector()
        for unit in ("abcd", "abcde", "ab", "中文重复句子", "abcdefghi"):
            with self.subTest(unit=unit):
                self.assertTrue(d.is_degenerate(unit * 12))

    # P1-10: 空间二次确认链路
    def test_p1_qzone_confirm_flow(self):
        from modules.qzone import QZoneModule, QZoneModuleConfig
        from core.modules import Context

        class FB:
            def __init__(self):
                self.calls = []

            def call(self, action, params=None):
                self.calls.append((action, params))
                return {"retcode": 0}

        fb = FB()
        m = QZoneModule(QZoneModuleConfig(write_enabled=True, authorized_ids={1}), bridge=fb)
        out = []

        async def reply(t):
            out.append(t)

        ctx = Context(text="/删除说说 abc123", user_id=1, reply_fn=reply)
        asyncio.run(m.on_private_message(ctx))
        self.assertEqual(fb.calls, [], "危险动作不应立即执行")
        self.assertIn("确认", out[-1])

        ctx2 = Context(text="/确认", user_id=1, reply_fn=reply)
        asyncio.run(m.on_private_message(ctx2))
        self.assertEqual(fb.calls[0][0], "delete_msg", "确认后应执行")

    # P1-N6: 日志轮转
    def test_p1_log_rotation(self):
        from core.notifier import _atomic_write_jsonl

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "alerts.jsonl"
            for i in range(20):
                _atomic_write_jsonl(p, {"i": i, "pad": "x" * 20000}, max_bytes=1024)
            backup = p.with_suffix(p.suffix + ".1")
            self.assertTrue(p.exists())
            self.assertTrue(backup.exists(), "超限应轮转生成 .1")
            self.assertLess(p.stat().st_size, 100000, "主文件应被轮转清空")

    # P1-2: _send 异步不阻塞（to_thread 路径）
    def test_p1_send_is_async(self):
        self.assertTrue(asyncio.iscoroutinefunction(BotApp._send))

    # P2-6: 畸形事件不崩
    def test_p2_malformed_event_no_crash(self):
        self.assertEqual(BotApp._as_int("abc"), 0)
        self.assertEqual(BotApp._as_int(None), 0)
        self.assertEqual(BotApp._as_int("123"), 123)


if __name__ == "__main__":
    unittest.main(verbosity=2)
