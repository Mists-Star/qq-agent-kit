"""OneBot 协议层真实逻辑测试（此前被 FakeClient 完全绕过）。

覆盖：retcode 校验、依赖检查、账号脱敏、版本兼容的 header 参数选择。
WS 连接用 mock 的 websockets 模块，不依赖真实协议端。
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.onebot import OneBotClient, OneBotConfig, _mask_uin  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


async def noop_event(evt):
    return None


class OneBotTestCase(unittest.TestCase):
    def setUp(self):
        self.cfg = OneBotConfig(http_url="http://127.0.0.1:15700")

    def test_retcode_zero_ok(self):
        c = OneBotClient(self.cfg, noop_event)
        with patch("urllib.request.urlopen", return_value=FakeResponse({"retcode": 0, "data": {}})):
            self.assertEqual(c.call_api("get_login_info")["retcode"], 0)

    def test_retcode_string_zero_ok(self):
        c = OneBotClient(self.cfg, noop_event)
        with patch("urllib.request.urlopen", return_value=FakeResponse({"retcode": "0", "data": {}})):
            self.assertEqual(c.call_api("x")["retcode"], "0")

    def test_retcode_nonzero_raises(self):
        c = OneBotClient(self.cfg, noop_event)
        with patch("urllib.request.urlopen", return_value=FakeResponse({"retcode": 100, "message": "bad"})):
            with self.assertRaises(RuntimeError):
                c.call_api("send_private_msg")

    def test_verify_login_masked_and_match(self):
        c = OneBotClient(self.cfg, noop_event)
        with patch("urllib.request.urlopen", return_value=FakeResponse({"retcode": 0, "data": {"user_id": 123456789}})):
            ok, msg = c.verify_login()
        self.assertTrue(ok)
        self.assertNotIn("123456789", msg, "日志/告警不得含完整账号")
        self.assertIn("***", msg)

    def test_verify_login_mismatch_masked(self):
        cfg = OneBotConfig(expect_uin=987654321)
        c = OneBotClient(cfg, noop_event)
        with patch("urllib.request.urlopen", return_value=FakeResponse({"retcode": 0, "data": {"user_id": 123456789}})):
            ok, msg = c.verify_login()
        self.assertFalse(ok)
        self.assertNotIn("123456789", msg)
        self.assertNotIn("987654321", msg)

    def test_mask_uin_short(self):
        self.assertEqual(_mask_uin("123"), "***")

    def test_check_ws_dependency_missing(self):
        # 模拟 websockets 不存在
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "websockets":
                raise ImportError("no websockets")
            return real_import(name, *a, **kw)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            err = OneBotClient.check_ws_dependency()
        self.assertIn("websockets", err)

    def test_check_ws_dependency_ok(self):
        # 环境可能没装 websockets：用真实模块 mock 一个"就绪"状态来验证通过分支
        fake_ws = types.ModuleType("websockets")
        fake_ws.__version__ = "14.0"
        fake_ws.connect = MagicMock()
        # connect 的内省返回一个带 additional_headers 的签名
        def fake_connect(*a, **kw):
            pass

        import inspect

        def fake_sig(obj):
            return inspect.Signature([
                inspect.Parameter("url", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                inspect.Parameter("additional_headers", inspect.Parameter.KEYWORD_ONLY, default=None),
            ])

        fake_ws.connect = fake_connect
        with patch.dict(sys.modules, {"websockets": fake_ws}), patch.object(inspect, "signature", side_effect=fake_sig):
            err = OneBotClient.check_ws_dependency()
        self.assertEqual(err, "", f"模拟依赖就绪应返回空，实际: {err}")

    def test_call_api_sends_echo_and_token(self):
        cfg = OneBotConfig(http_url="http://x", access_token="tok")
        c = OneBotClient(cfg, noop_event)
        captured = {}

        def fake_urlopen(req, timeout=10):
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            return FakeResponse({"retcode": 0})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            c.call_api("send_msg", {"message": "hi"})
        self.assertTrue(captured["body"]["echo"])
        self.assertEqual(captured["auth"], "Bearer tok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
