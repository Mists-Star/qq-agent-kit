"""三层频控 + AI 专用额度 — 防刷屏（风控高危特征）且防额度失控。

设计：
    - 滑动窗口：分钟 / 小时 / 日三层，每层独立阈值。
    - AI 额度独立核算：AI 流式分片不计入普通消息频率，但计入 AI 专用额度。
    - 并发安全：用 threading.Lock（asyncio 场景在事件循环内调用即可）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class RateConfig:
    per_minute: int = 30
    per_hour: int = 200
    per_day: int = 1000
    ai_quota_day: int = 500


class RateLimiter:
    def __init__(self, config: RateConfig | None = None, clock=None):
        self.config = config or RateConfig()
        self._clock = clock or time.time
        self._buckets: dict[tuple[int, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, user_id: int, *, is_ai: bool = False) -> tuple[bool, str]:
        """检查是否放行。返回 (是否放行, 原因)。不消耗额度。"""
        now = self._clock()
        with self._lock:
            if self._count(user_id, "min", 60, now) >= self.config.per_minute:
                return False, "每分钟次数超限"
            if self._count(user_id, "hour", 3600, now) >= self.config.per_hour:
                return False, "每小时次数超限"
            if self._count(user_id, "day", 86400, now) >= self.config.per_day:
                return False, "每日次数超限"
            if is_ai:
                d = self._buckets[(user_id, "ai_day")]
                self._prune(d, now, 86400)
                if len(d) >= self.config.ai_quota_day:
                    return False, "AI 专用额度已用尽"
        return True, "ok"

    def consume(self, user_id: int, *, is_ai: bool = False) -> None:
        """消耗一次额度（check 通过后调用）。"""
        now = self._clock()
        with self._lock:
            for key, window in (("min", 60), ("hour", 3600), ("day", 86400)):
                self._buckets[(user_id, key)].append(now)
            if is_ai:
                self._buckets[(user_id, "ai_day")].append(now)

    def reset(self, user_id: int | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._buckets.clear()
            else:
                for key in ("min", "hour", "day", "ai_day"):
                    self._buckets.pop((user_id, key), None)

    def _count(self, user_id: int, key: str, window: int, now: float) -> int:
        d = self._buckets[(user_id, key)]
        self._prune(d, now, window)
        return len(d)

    @staticmethod
    def _prune(d: deque[float], now: float, window: int) -> None:
        cutoff = now - window
        while d and d[0] < cutoff:
            d.popleft()
