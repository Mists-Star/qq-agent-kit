"""空间模块单测：默认禁用、分级授权、action 白名单。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.modules import Context  # noqa: E402
from core.qzone_bridge import QZoneBridge, QZoneConfig, QZoneError  # noqa: E402
from modules.qzone import QZoneModule, QZoneModuleConfig  # noqa: E402


class FakeBridge(QZoneBridge):
    def __init__(self, fail: bool = False):
        super().__init__(QZoneConfig())
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def call(self, action, params=None):
        self.calls.append((action, params or {}))
        if self.fail:
            raise QZoneError("bridge down")
        return {"retcode": 0, "data": {}}


def make_ctx(text: str, user_id: int = 1001):
    replies: list[str] = []

    async def reply(t):
        replies.append(t)

    return Context(text=text, user_id=user_id, reply_fn=reply), replies


def run(coro):
    return asyncio.run(coro)


class QZoneTestCase(unittest.TestCase):
    def test_module_default_disabled(self):
        m = QZoneModule()
        self.assertFalse(m.enabled)

    def test_write_blocked_when_not_authorized(self):
        bridge = FakeBridge()
        m = QZoneModule(QZoneModuleConfig(write_enabled=True, authorized_ids={9999}), bridge=bridge)
        ctx, out = make_ctx("/说说 测试内容", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertIn("未授权", out[0])
        self.assertEqual(bridge.calls, [], "未授权不得调用桥接")

    def test_write_blocked_when_write_disabled(self):
        bridge = FakeBridge()
        m = QZoneModule(QZoneModuleConfig(write_enabled=False, authorized_ids={1001}), bridge=bridge)
        ctx, out = make_ctx("/说说 测试内容", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertIn("未启用", out[0])

    def test_authorized_write_ok(self):
        bridge = FakeBridge()
        m = QZoneModule(QZoneModuleConfig(write_enabled=True, authorized_ids={1001}), bridge=bridge)
        ctx, out = make_ctx("/说说 今天天气不错", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertIn("已发布", out[0])
        self.assertEqual(bridge.calls[0][0], "send_msg")
        self.assertEqual(bridge.calls[0][1]["message"], "今天天气不错")

    def test_read_private_by_default(self):
        bridge = FakeBridge()
        m = QZoneModule(QZoneModuleConfig(read_public=False, authorized_ids={9999}), bridge=bridge)
        ctx, out = make_ctx("/空间动态", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertIn("未对该账号开放", out[0])

    def test_read_public_ok(self):
        bridge = FakeBridge()
        m = QZoneModule(QZoneModuleConfig(read_public=True), bridge=bridge)
        ctx, out = make_ctx("/空间动态", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertIn("成功", out[0])

    def test_bridge_failure_graceful(self):
        bridge = FakeBridge(fail=True)
        m = QZoneModule(QZoneModuleConfig(write_enabled=True, authorized_ids={1001}), bridge=bridge)
        ctx, out = make_ctx("/说说 内容", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertTrue(any("失败" in x for x in out))

    def test_non_command_ignored(self):
        bridge = FakeBridge()
        m = QZoneModule(QZoneModuleConfig(), bridge=bridge)
        ctx, out = make_ctx("普通聊天内容", user_id=1001)
        run(m.on_private_message(ctx))
        self.assertEqual(out, [])
        self.assertEqual(bridge.calls, [])


class BridgeWhitelistTestCase(unittest.TestCase):
    def test_unknown_action_rejected(self):
        b = QZoneBridge()
        with self.assertRaises(QZoneError):
            b.call("send_group_msg", {"group_id": 1})  # 非空间 action

    def test_is_write_action(self):
        b = QZoneBridge()
        self.assertTrue(b.is_write_action("send_msg"))
        self.assertFalse(b.is_write_action("get_friend_feeds"))
        with self.assertRaises(QZoneError):
            b.is_write_action("random_action")


if __name__ == "__main__":
    unittest.main(verbosity=2)
