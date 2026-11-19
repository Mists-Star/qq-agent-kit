"""额度档位系统测试：预设、覆盖、类型校验、边界。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest  # noqa: E402

from bot import BotConfig  # noqa: E402
from core.ratelimit import LIMIT_PRESETS, RateLimiter, resolve_rate_config  # noqa: E402


class LimitsTestCase(unittest.TestCase):
    def test_default_is_strict(self):
        cfg = BotConfig()
        self.assertEqual(cfg.limit_preset, "strict")
        r = cfg.resolve_rate_config()
        self.assertEqual((r.per_minute, r.ai_quota_day), (6, 30))
        self.assertEqual(r, LIMIT_PRESETS["strict"])

    def test_presets_ordered_strictest_to_loosest(self):
        s, b, r = (LIMIT_PRESETS[k] for k in ("strict", "balanced", "relaxed"))
        self.assertLess(s.per_minute, b.per_minute)
        self.assertLess(b.per_minute, r.per_minute)
        self.assertLess(s.ai_quota_day, b.ai_quota_day)

    def test_balanced_and_relaxed(self):
        self.assertEqual(resolve_rate_config("balanced").per_hour, 120)
        self.assertEqual(resolve_rate_config("relaxed").per_day, 2000)

    def test_unknown_preset_raises(self):
        with self.assertRaises(ValueError):
            resolve_rate_config("ultra")

    def test_override_single_field(self):
        r = resolve_rate_config("balanced", {"ai_quota_day": 5})
        self.assertEqual(r.per_minute, 20)  # 跟随 balanced
        self.assertEqual(r.ai_quota_day, 5)  # 被覆盖

    def test_override_string_number_coerced(self):
        r = resolve_rate_config("strict", {"per_minute": "10"})
        self.assertEqual(r.per_minute, 10)

    def test_bad_override_raises(self):
        with self.assertRaises(ValueError):
            resolve_rate_config("strict", {"per_minute": "abc"})
        with self.assertRaises(ValueError):
            resolve_rate_config("strict", {"per_minute": -1})

    def test_custom_falls_back_to_strict(self):
        r = resolve_rate_config("custom", {"per_minute": 10})
        self.assertEqual(r.per_minute, 10)
        self.assertEqual(r.per_hour, LIMIT_PRESETS["strict"].per_hour)

    def test_config_json_preset(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"limit_preset":"relaxed"}', encoding="utf-8")
            cfg = BotConfig.from_json(p)
            self.assertEqual(cfg.resolve_rate_config().per_minute, 60)

    def test_config_json_string_numbers_coerced(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"expect_uin":"123","max_failures":"5"}', encoding="utf-8")
            cfg = BotConfig.from_json(p)
            self.assertEqual(cfg.expect_uin, 123)
            self.assertEqual(cfg.max_failures, 5)

    def test_config_json_bad_type_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"expect_uin":"abc"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                BotConfig.from_json(p)
            p.write_text('{"ws_url":123}', encoding="utf-8")
            with self.assertRaises(ValueError):
                BotConfig.from_json(p)

    def test_config_json_negative_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"max_failures":-1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                BotConfig.from_json(p)

    def test_no_skip_general_bypass(self):
        # 旧 skip_general 组合已删除，is_ai 模式只查 AI 额度，普通频控独立
        lim = RateLimiter(resolve_rate_config("strict"))
        ok, _ = lim.check_and_consume(1, is_ai=True)
        self.assertTrue(ok)
        self.assertEqual(lim.active_users(), 1)
