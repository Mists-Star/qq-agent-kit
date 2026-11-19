"""启动协调器 — 把 guard 的"绝不无限重试"真正落到启动流程里。

这个模块回答一个具体问题：
    开机自启 -> NapCat 登录 -> 失败时怎么办？

答：走 LoginGuard 状态机。快速登录失败 -> 退避 -> 最多 N 次 ->
    熔断 -> critical 通知 -> 停手，等人工扫码。绝不死循环重拉。

用法（伪代码）：
    guard = LoginGuard(notifier, FileStateStore(state_path))
    guard.restore()
    ok, reason = guard.can_attempt()
    if not ok:
        return  # 已熔断，notifier 已告警，直接退出
    for attempt in range(guard.config.max_failures):
        guard.begin()
        if napcat.quick_login():
            guard.record_success()
            break
        guard.record_failure(...)
        guard.wait_backoff()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .guard import GuardConfig, LoginGuard, LoginPhase


@dataclass
class BootResult:
    ok: bool
    phase: LoginPhase
    attempts: int
    reason: str


async def login_with_guard(
    guard: LoginGuard,
    try_login: Callable[[], Awaitable[tuple[bool, str]]],
    *,
    notify_scan_needed: Callable[[], Awaitable[None]] | None = None,
) -> BootResult:
    """带熔断的登录流程。任何情况下都不会无限重试。

    Args:
        guard: 已 restore() 的登录守护。
        try_login: 一次快速登录尝试，返回 (成功?, 错误信息)。
        notify_scan_needed: 熔断后可选补充动作（如拉起扫码 WebUI）。

    Returns:
        BootResult，含最终状态与尝试次数。
    """
    guard.restore()

    ok, reason = guard.can_attempt()
    if not ok:
        # 熔断态：guard 内部已发过 critical 告警，这里只做兜底补充
        if notify_scan_needed:
            await notify_scan_needed()
        return BootResult(False, guard.state.phase, guard.state.fail_count, reason)

    attempts = 0
    max_try = guard.config.max_failures
    while attempts < max_try:
        allowed, why = guard.can_attempt()
        if not allowed:
            break
        guard.begin()
        attempts += 1
        success, err = await try_login()
        if success:
            guard.record_success()
            return BootResult(True, LoginPhase.ONLINE, attempts, "登录成功")
        guard.record_failure(err)

        if guard.is_tripped:
            # 第 N 次失败：guard 已告警并熔断
            if notify_scan_needed:
                await notify_scan_needed()
            break
        wait = guard.backoff_seconds()
        await asyncio.sleep(wait)

    return BootResult(
        False,
        guard.state.phase,
        attempts,
        guard.state.last_error or "登录流程结束",
    )


def sync_login_with_guard(
    guard: LoginGuard,
    try_login: Callable[[], tuple[bool, str]],
    *,
    notify_scan_needed: Callable[[], None] | None = None,
) -> BootResult:
    """同步版，供同步启动脚本直接调用。"""
    guard.restore()
    ok, reason = guard.can_attempt()
    if not ok:
        if notify_scan_needed:
            notify_scan_needed()
        return BootResult(False, guard.state.phase, guard.state.fail_count, reason)

    attempts = 0
    while attempts < guard.config.max_failures:
        if not guard.can_attempt()[0]:
            break
        guard.begin()
        attempts += 1
        success, err = try_login()
        if success:
            guard.record_success()
            return BootResult(True, LoginPhase.ONLINE, attempts, "登录成功")
        guard.record_failure(err)
        if guard.is_tripped:
            if notify_scan_needed:
                notify_scan_needed()
            break
        guard.wait_backoff()
    return BootResult(False, guard.state.phase, attempts, guard.state.last_error or "登录失败")
