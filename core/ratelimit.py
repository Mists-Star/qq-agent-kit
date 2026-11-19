"""三层频控 + AI 专用额度 — 防刷屏（风控高危特征）且防额度失控。

设计：
    - 滑动窗口：分钟 / 小时 / 日三层，每层独立阈值。
    - AI 额度独立核算：AI 流式分片不计入普通消息频率，但计入 AI 专用额度。
    - 并发安全：用 threading.Lock。
    - 档位预设：strict / balanced / relaxed / custom，默认最严（strict）。
      部署方可选档，并用显式字段逐项覆盖。

档位（每账号）：
    strict   最严（默认）：6/分  40/时  200/日   AI 30/日
    balanced 均衡        ：20/分 120/时 600/日   AI 150/日
    relaxed  宽松        ：60/分 400/时 2000/日  AI 600/日
    custom   自定义      ：完全由显式字段决定（未给的用 strict 兜底）

数值来源：config.limit_preset + 可选逐项覆盖（见 BotConfig.resolve_rate_config）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class RateConfig:
    per_minute: int = 6
    per_hour: int = 40
    per_day: int = 200
    ai_quota_day: int = 30

    def validate(self) -> None:
        """校验所有阈值为非负整数（0 = 禁用该层）。"""
        for name in ("per_minute", "per_hour", "per_day", "ai_quota_day"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{name} 必须是非负整数，收到 {v!r}")


# 档位预设（不可变）
LIMIT_PRESETS: dict[str, RateConfig] = {
    "strict": RateConfig(per_minute=6, per_hour=40, per_day=200, ai_quota_day=30),
    "balanced": RateConfig(per_minute=20, per_hour=120, per_day=600, ai_quota_day=150),
    "relaxed": RateConfig(per_minute=60, per_hour=400, per_day=2000, ai_quota_day=600),
}
DEFAULT_PRESET = "strict"


def resolve_rate_config(preset: str, overrides: dict[str, Any] | None = None) -> RateConfig:
    """按档位 + 覆盖项解析出最终配置。

    Args:
        preset: strict/balanced/relaxed/custom；未知档位报错。
        overrides: 键为 RateConfig 字段名，值为 None 表示不覆盖。
    """
    key = (preset or DEFAULT_PRESET).strip().lower()
    if key == "custom":
        base = LIMIT_PRESETS[DEFAULT_PRESET]  # 自定义未给项用最严兜底
    elif key in LIMIT_PRESETS:
        base = LIMIT_PRESETS[key]
    else:
        raise ValueError(f"未知额度档位 {preset!r}，可选: {sorted(LIMIT_PRESETS) + ['custom']}")

    values: dict[str, Any] = {}
    for name in ("per_minute", "per_hour", "per_day", "ai_quota_day"):
        override = (overrides or {}).get(name)
        if override is None:
            values[name] = getattr(base, name)
        else:
            # 字符串型数字强转（JSON 里加引号很常见）
            try:
                values[name] = int(override)
            except (ValueError, TypeError):
                raise ValueError(f"{name} 必须是整数，收到 {override!r}")

    cfg = RateConfig(**values)
    cfg.validate()
    return cfg


class RateLimiter:
    def __init__(self, config: RateConfig | None = None, clock=None):
        self.config = config or RateConfig()
        self.config.validate()
        self._clock = clock or time.time
        self._buckets: dict[tuple[int, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, user_id: int, *, is_ai: bool = False) -> tuple[bool, str]:
        """只检查不扣减。普通三层 + 可选 AI 额度。"""
        now = self._clock()
        with self._lock:
            return self._check_locked(user_id, is_ai=is_ai, now=now)

    def consume(self, user_id: int, *, is_ai: bool = False) -> None:
        """扣减一次（普通或 AI）。"""
        now = self._clock()
        with self._lock:
            if is_ai:
                self._bucket(user_id, "ai_day").append(now)
            else:
                for key in ("min", "hour", "day"):
                    self._bucket(user_id, key).append(now)

    def check_and_consume(self, user_id: int, *, is_ai: bool = False) -> tuple[bool, str]:
        """原子检查+扣减。

        is_ai=True 只查/扣 AI 额度（入站普通频控已单独通过），
        避免旧 skip_general 组合造成的绕过与误用。
        """
        now = self._clock()
        with self._lock:
            ok, reason = self._check_locked(user_id, is_ai=is_ai, now=now)
            if not ok:
                return False, reason
            if is_ai:
                self._bucket(user_id, "ai_day").append(now)
            else:
                for key in ("min", "hour", "day"):
                    self._bucket(user_id, key).append(now)
            return True, "ok"

    def _check_locked(self, user_id: int, *, is_ai: bool, now: float) -> tuple[bool, str]:
        if is_ai:
            d = self._bucket(user_id, "ai_day")
            self._prune(d, now, 86400)
            if len(d) >= self.config.ai_quota_day:
                return False, "AI 专用额度已用尽"
            return True, "ok"
        # 普通消息：三层滑动窗
        if self._count(user_id, "min", 60, now) >= self.config.per_minute:
            return False, "每分钟次数超限"
        if self._count(user_id, "hour", 3600, now) >= self.config.per_hour:
            return False, "每小时次数超限"
        if self._count(user_id, "day", 86400, now) >= self.config.per_day:
            return False, "每日次数超限"
        return True, "ok"

    def reset(self, user_id: int | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._buckets.clear()
            else:
                for key in ("min", "hour", "day", "ai_day"):
                    self._buckets.pop((user_id, key), None)

    def active_users(self) -> int:
        """当前有记录的账号数（观测用）。"""
        with self._lock:
            return len({uid for uid, _ in self._buckets})

    def _bucket(self, user_id: int, key: str) -> deque[float]:
        # 用普通 dict + get，避免 defaultdict 为每次查询都建空桶
        k = (user_id, key)
        d = self._buckets.get(k)
        if d is None:
            d = deque()
            self._buckets[k] = d
        return d

    def _count(self, user_id: int, key: str, window: int, now: float) -> int:
        d = self._bucket(user_id, key)
        self._prune(d, now, window)
        # 过期后清空桶并删除 key，防长期运行内存无界增长
        if not d:
            self._buckets.pop((user_id, key), None)
        return len(d)

    @staticmethod
    def _prune(d: deque[float], now: float, window: int) -> None:
        cutoff = now - window
        while d and d[0] < cutoff:
            d.popleft()
