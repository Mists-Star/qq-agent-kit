"""集成测试：用假客户端跑通主循环全链路，不依赖真实 QQ/网络。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import BotApp, BotConfig  # noqa: E402
from core.modules import Context, Module, ModuleRegistry  # noqa: E402


class FakeClient:
    def __init__(self, login_ok: bool = True):
        self.login_ok = login_ok
        self.sent: list[tuple[int, int, str]] = []

    @staticmethod
    def check_ws_dependency() -> str:
        return ""  # 模拟依赖就绪；真实检查见 tests/test_onebot.py

    def verify_login(self):
        return (True, "ok") if self.login_ok else (False, "login expired")

    def send_private_msg(self, user_id, text, **kw):
        self.sent.append((user_id, 0, text))
        return {"retcode": 0}

    def send_group_msg(self, group_id, text, **kw):
        self.sent.append((0, group_id, text))
        return {"retcode": 0}

    async def run_forever(self, *, on_reconnect=None):
        return None


class EchoModule(Module):
    name = "echo"

    async def on_private_message(self, ctx: Context):
        await ctx.reply(f"收到：{ctx.text}。完成。")


def make_app(tmp: Path, client: FakeClient, *, gen=None, registry=None) -> BotApp:
    cfg = BotConfig(base_dir=tmp, send_interval_sec=0)
    cfg.per_minute = 100
    return BotApp(cfg, client=client, generate_reply=gen, registry=registry)


def run(coro):
    return asyncio.run(coro)


class IntegrationTestCase(unittest.TestCase):
    def test_module_chain_to_send(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            reg = ModuleRegistry()
            reg.register(EchoModule())
            app = make_app(Path(d), client, registry=reg)
            evt = {"post_type": "message", "user_id": 1001, "group_id": 0, "raw_message": "你好"}
            run(app.on_event(evt))
            self.assertEqual(len(client.sent), 2, "两句话应切成两条发送")
            self.assertIn("收到：你好", client.sent[0][2])

    def test_llm_fallback_when_no_module_reply(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()

            async def gen(ctx):
                return "模型回复。第二段。"

            app = make_app(Path(d), client, gen=gen)
            evt = {"post_type": "message", "user_id": 1002, "group_id": 0, "raw_message": "hi"}
            run(app.on_event(evt))
            self.assertEqual(len(client.sent), 2, "应按句拆成两条")

    def test_bracket_not_torn_in_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()

            async def gen(ctx):
                return "这是【重要】的内容。"

            app = make_app(Path(d), client, gen=gen)
            evt = {"post_type": "message", "user_id": 1003, "group_id": 0, "raw_message": "q"}
            run(app.on_event(evt))
            self.assertTrue(any("【重要】" in s[2] for s in client.sent))

    def test_degenerate_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()

            async def gen(ctx):
                return "哈" * 30

            app = make_app(Path(d), client, gen=gen)
            evt = {"post_type": "message", "user_id": 1004, "group_id": 0, "raw_message": "x"}
            run(app.on_event(evt))
            self.assertEqual(client.sent, [], "退化输出不得发送")

    def test_rate_limit_drops(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            app = make_app(Path(d), client)
            # RateConfig 不可变：用严格档位重建 limiter 模拟低阈值
            from core.ratelimit import RateConfig, RateLimiter

            app.limiter = RateLimiter(RateConfig(per_minute=1, per_hour=10, per_day=100, ai_quota_day=100))
            evt = {"post_type": "message", "user_id": 1005, "group_id": 0, "raw_message": "hi"}

            async def gen(ctx):
                return "回复。"

            app.generate_reply = gen
            run(app.on_event(evt))
            run(app.on_event(evt))
            self.assertEqual(len(client.sent), 1, "超频控应被丢弃")

    def test_boot_fail_trips_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(login_ok=False)
            app = make_app(Path(d), client)
            result = run(app.boot())
            self.assertFalse(result.ok)
            self.assertTrue(app.guard.is_tripped)
            self.assertFalse(app._lock.acquired, "失败应释放锁")

    def test_non_message_event_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            app = make_app(Path(d), client)
            run(app.on_event({"post_type": "notice"}))
            self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
