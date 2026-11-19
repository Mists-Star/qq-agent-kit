"""登录守护状态机 — 登录失败优先告警，绝不无限重试。

这是整个脚手架最重要的模块，针对的正是这类事故：

    异常关机 -> 管家修复杀进程 -> 登录态失效 ->
    快速登录失败 -> 守护脚本死循环重拉 -> 触发平台风控 -> 账号被临时冻结

硬规则（不可协商）：
    1. 快速登录连续失败 N 次（默认 3）立即熔断，进入 WAIT_MANUAL。
    2. 熔断的第一时间就发出 critical 级告警，不等不拖。
    3. 熔断状态下**拒绝一切自动重试**，只有人工确认（扫码/点按钮）才解除。
    4. 熔断状态持久化：进程重启后仍然记得"已熔断"，不会从 0 重新死磕。
    5. 每次失败之间指数退避 + 抖动，避免高频请求本身变成风控特征。

状态机：
    IDLE ──boot──> QUICK_LOGIN ──ok──> ONLINE
                      │ fail
                      ▼
                  BACKOFF ──fail_count<N──> QUICK_LOGIN
                      │ fail_count>=N
                      ▼
                  WAIT_MANUAL ──(notify critical)──> 等人工扫码
                      │ human_confirm()
                      ▼
                  QUICK_LOGIN（计数清零）

所有 IO（存储、通知、时钟）都可注入，纯逻辑可单测。
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .notifier import Notifier


class LoginPhase(str, Enum):
    IDLE = "idle"
    QUICK_LOGIN = "quick_login"
    BACKOFF = "backoff"
    WAIT_MANUAL = "wait_manual"
    ONLINE = "online"


class StateStore(Protocol):
    """持久化协议。默认用文件实现，测试可传内存实现。"""

    def load(self) -> dict[str, Any]:
        ...

    def save(self, data: dict[str, Any]) -> None:
        ...


class FileStateStore:
    """原子写文件存储：tmp + os.replace，防崩溃写坏。"""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self.path)


class MemoryStateStore:
    """测试用内存存储。"""

    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data or {}

    def load(self) -> dict[str, Any]:
        return dict(self.data)

    def save(self, data: dict[str, Any]) -> None:
        self.data = dict(data)


@dataclass
class GuardConfig:
    max_failures: int = 3            # 连续失败几次后熔断
    base_backoff_sec: float = 2.0    # 退避基数（指数增长）
    max_backoff_sec: float = 60.0    # 退避上限
    jitter: float = 0.3              # 抖动比例 ±30%
    stale_after_sec: float = 1800.0  # 熔断状态超过 30 分钟才允许自动清（默认不清）
    alert_tag: str = "QQ Bot"        # 告警标题前缀，部署方可自行改成自己的机器人名


@dataclass
class GuardState:
    phase: LoginPhase = LoginPhase.IDLE
    fail_count: int = 0
    last_error: str = ""
    last_attempt_ts: float = 0.0
    tripped_ts: float = 0.0          # 熔断时刻
    notified: bool = False           # 本轮熔断是否已告警（防重复）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GuardState":
        d = dict(d)
        try:
            d["phase"] = LoginPhase(d.get("phase", "idle"))
        except ValueError:
            d["phase"] = LoginPhase.IDLE
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})


class LoginGuard:
    """登录守护状态机。

    Args:
        notifier: 告警通道（熔断时 critical 级通知）。
        store: 状态持久化（默认文件）。
        config: 策略参数。
        clock: 时钟函数，测试可注入假时钟。
        sleeper: 睡眠函数，测试可注入 no-op。
    """

    def __init__(
        self,
        notifier: Notifier,
        store: StateStore,
        config: GuardConfig | None = None,
        clock: Any = None,
        sleeper: Any = None,
    ):
        self.notifier = notifier
        self.store = store
        self.config = config or GuardConfig()
        self._clock = clock or time.time
        self._sleeper = sleeper or time.sleep
        self.state = GuardState.from_dict(store.load())

    # ---------- 持久化 ----------
    def _persist(self) -> None:
        self.store.save(self.state.to_dict())

    def restore(self) -> GuardState:
        """从存储重新载入（进程重启后调用）。熔断状态会被保留。"""
        self.state = GuardState.from_dict(self.store.load())
        return self.state

    # ---------- 状态判定 ----------
    @property
    def is_tripped(self) -> bool:
        return self.state.phase == LoginPhase.WAIT_MANUAL

    def can_attempt(self) -> tuple[bool, str]:
        """是否可以发起一次登录尝试。返回 (是否允许, 原因)。"""
        if self.is_tripped:
            return False, "已熔断：等待人工扫码确认，拒绝自动重试"
        return True, "ok"

    def backoff_seconds(self, fail_count: int | None = None) -> float:
        """指数退避 + 抖动。"""
        n = self.state.fail_count if fail_count is None else fail_count
        raw = self.config.base_backoff_sec * (2 ** max(0, n - 1))
        capped = min(raw, self.config.max_backoff_sec)
        jitter = capped * self.config.jitter
        # 抖动后再夹一次上限：否则 capped=60 + 30% 会突破 max_backoff_sec
        return max(0.0, min(capped + random.uniform(-jitter, jitter), self.config.max_backoff_sec))

    # ---------- 状态迁移 ----------
    def begin(self) -> GuardState:
        """开始一次登录流程。熔断态下直接拒绝。"""
        ok, reason = self.can_attempt()
        if not ok:
            self._alert_tripped(reason=reason)
            return self.state
        self.state.phase = LoginPhase.QUICK_LOGIN
        self.state.last_attempt_ts = self._clock()
        self._persist()
        return self.state

    def record_failure(self, error: str = "") -> GuardState:
        """记录一次快速登录失败。达到阈值立即熔断并告警。"""
        self.state.fail_count += 1
        self.state.last_error = error or "quick login failed"
        self.state.last_attempt_ts = self._clock()

        if self.state.fail_count >= self.config.max_failures:
            self._trip()
        else:
            self.state.phase = LoginPhase.BACKOFF
            self._persist()
        return self.state

    def wait_backoff(self) -> None:
        """阻塞式退避（供简单调用方使用；异步场景请用 backoff_seconds 自行 await）。"""
        if self.state.phase != LoginPhase.BACKOFF:
            return
        self._sleeper(self.backoff_seconds())

    def record_success(self) -> GuardState:
        """登录成功：清零所有失败状态。"""
        self.state.phase = LoginPhase.ONLINE
        self.state.fail_count = 0
        self.state.last_error = ""
        self.state.tripped_ts = 0.0
        self.state.notified = False
        self._persist()
        self.notifier.clear_dedup("login_guard:tripped")
        return self.state

    def human_confirm(self) -> GuardState:
        """人工确认（已扫码 / 已处理）。唯一能解除熔断的入口。"""
        self.state.phase = LoginPhase.IDLE
        self.state.fail_count = 0
        self.state.last_error = ""
        self.state.tripped_ts = 0.0
        self.state.notified = False
        self._persist()
        self.notifier.clear_dedup("login_guard:tripped")
        return self.state

    def mark_offline(self, error: str = "connection lost") -> GuardState:
        """运行中被踢下线。不直接重试，交由调用方走 begin() 重新评估。"""
        self.state.phase = LoginPhase.IDLE
        self.state.last_error = error
        self._persist()
        return self.state

    # ---------- 熔断与告警 ----------
    def _trip(self) -> None:
        self.state.phase = LoginPhase.WAIT_MANUAL
        self.state.tripped_ts = self._clock()
        self._persist()
        if not self.state.notified:
            self._alert_tripped(
                f"连续 {self.state.fail_count} 次快速登录失败，已停止自动重试。"
                f"最后错误：{self.state.last_error}。请在手机端完成验证后扫码登录。"
            )
            self.state.notified = True
            self._persist()

    def _alert_tripped(self, reason: str = "") -> None:
        body = reason or (
            f"登录已熔断：连续 {self.state.fail_count} 次失败。"
            "已停止自动重试（防止触发平台风控）。请人工扫码后确认恢复。"
        )
        self.notifier.notify(
            title=f"[{self.config.alert_tag}] 登录失败，需要处理",
            body=body,
            level="critical",
            fingerprint="login_guard:tripped",
            extra={"phase": self.state.phase.value, "fail_count": self.state.fail_count},
        )

    # ---------- 可观测 ----------
    def status(self) -> dict[str, Any]:
        d = self.state.to_dict()
        d["is_tripped"] = self.is_tripped
        d["can_attempt"] = self.can_attempt()[0]
        return d
