"""bot.py — 主循环入口，把核心模块串成一条可运行的链路。

链路：
    启动协调(guard 熔断) -> 单实例锁 -> OneBot 客户端 -> 事件循环
    -> 频控 -> 模块分发 -> 可选 LLM 生成 -> 安全切句 -> 退化检测 -> 发送

不碰任何启动项/注册表/进程管理。启动方式由部署方自行决定。

LLM 是可注入的：框架不内置任何模型或密钥，通过 generate_reply 回调接入。
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.boot import BootResult, login_with_guard
from core.degenerate import DegenerateDetector
from core.guard import FileStateStore, GuardConfig, LoginGuard
from core.lock import SingleInstanceLock
from core.modules import Context, ModuleRegistry
from core.notifier import ConsoleChannel, FileChannel, Notifier
from core.onebot import OneBotClient, OneBotConfig
from core.ratelimit import RateConfig, RateLimiter
from core.splitter import safe_sentence_cut

GenerateReply = Callable[[Context], Awaitable[str | None]]


@dataclass
class BotConfig:
    """主循环配置。可用 --config 指向一个 JSON 文件覆盖。"""

    base_dir: Path = Path(".")
    ws_url: str = "ws://127.0.0.1:15701"
    http_url: str = "http://127.0.0.1:15700"
    access_token: str = ""
    expect_uin: int = 0
    max_failures: int = 3
    alert_tag: str = "QQ Bot"
    per_minute: int = 30
    per_hour: int = 200
    per_day: int = 1000
    ai_quota_day: int = 500
    send_interval_sec: float = 0.05  # 句间间隔，降低高频发送特征

    @classmethod
    def from_json(cls, path: Path) -> "BotConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = cls()
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        for k, v in data.items():
            if k in allowed:
                setattr(cfg, k, v)
        return cfg


class BotApp:
    def __init__(
        self,
        config: BotConfig,
        *,
        registry: ModuleRegistry | None = None,
        generate_reply: GenerateReply | None = None,
        client: OneBotClient | None = None,
    ):
        self.config = config
        base = config.base_dir
        self.notifier = Notifier(
            channels=[FileChannel(path=base / "logs" / "alerts.jsonl"), ConsoleChannel()]
        )
        self.guard = LoginGuard(
            self.notifier,
            FileStateStore(base / "state" / "guard.json"),
            GuardConfig(max_failures=config.max_failures, alert_tag=config.alert_tag),
        )
        self.limiter = RateLimiter(
            RateConfig(
                per_minute=config.per_minute,
                per_hour=config.per_hour,
                per_day=config.per_day,
                ai_quota_day=config.ai_quota_day,
            )
        )
        self.registry = registry or ModuleRegistry()
        self.generate_reply = generate_reply
        self.detector = DegenerateDetector()
        self.client = client or OneBotClient(
            OneBotConfig(
                ws_url=config.ws_url,
                http_url=config.http_url,
                access_token=config.access_token,
                expect_uin=config.expect_uin,
            ),
            on_event=self.on_event,
        )
        self._lock = SingleInstanceLock(base / "state" / "bot.lock")

    # ---------- 启动 ----------
    async def boot(self) -> BootResult:
        """带熔断的登录。失败即告警停手，不无限重试。"""
        if not self._lock.acquire():
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 启动中止",
                body="已有实例在运行（单实例锁），本次启动已退出。",
                level="warning",
                fingerprint="boot:duplicate",
            )
            return BootResult(False, self.guard.state.phase, 0, "已有实例在运行")

        async def try_login() -> tuple[bool, str]:
            return self.client.verify_login()

        result = await login_with_guard(self.guard, try_login)
        if not result.ok:
            self._lock.release()
        return result

    # ---------- 事件处理 ----------
    async def on_event(self, evt: dict[str, Any]) -> None:
        if evt.get("post_type") != "message":
            return
        user_id = int(evt.get("user_id", 0) or 0)
        group_id = int(evt.get("group_id", 0) or 0)
        text = str(evt.get("raw_message", "") or "")

        ok, reason = self.limiter.check(user_id)
        if not ok:
            return  # 频控拒绝，静默丢弃（不告警，避免噪声）
        self.limiter.consume(user_id)

        replies: list[str] = []

        async def collect(t: str) -> None:
            replies.append(t)

        ctx = Context(text=text, user_id=user_id, group_id=group_id, raw=evt, reply_fn=collect)
        hook = "on_group_message" if group_id else "on_private_message"
        await self.registry.emit(hook, ctx)

        if not replies and self.generate_reply:
            generated = await self.generate_reply(ctx)
            if generated:
                replies.append(generated)

        for text_out in replies:
            await self.deliver(user_id, group_id, text_out)

    async def deliver(self, user_id: int, group_id: int, text: str) -> None:
        """安全切句 + 退化检测 + 逐句发送。"""
        if self.detector.is_degenerate(text):
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 已拦截退化输出",
                body=self.detector.reason(text)[:200],
                level="warning",
                fingerprint="reply:degenerate",
            )
            return
        for sentence in safe_sentence_cut(text):
            self._send(user_id, group_id, sentence)
            if self.config.send_interval_sec > 0:
                await asyncio.sleep(self.config.send_interval_sec)

    def _send(self, user_id: int, group_id: int, text: str) -> None:
        try:
            if group_id:
                self.client.send_group_msg(group_id, text)
            else:
                self.client.send_private_msg(user_id, text)
        except Exception as exc:
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 消息发送失败",
                body=f"{type(exc).__name__}: {exc}"[:200],
                level="warning",
                fingerprint="reply:send_fail",
            )

    # ---------- 运行 ----------
    async def run(self, *, max_seconds: float | None = None) -> None:
        result = await self.boot()
        if not result.ok:
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 未上线",
                body=result.reason,
                level="critical",
                fingerprint="boot:not_online",
            )
            return
        await self.registry.emit("on_startup")
        try:
            await self.client.run_forever(
                on_reconnect=lambda n: self.notifier.notify(
                    title=f"[{self.config.alert_tag}] 连接断开",
                    body=f"第 {n} 次重连中（退避进行，非高频重试）",
                    level="warning",
                    fingerprint="onebot:reconnect",
                )
            )
        finally:
            await self.registry.emit("on_shutdown")
            self.guard.mark_offline()
            self._lock.release()


def load_config(path: Path | None) -> BotConfig:
    return BotConfig.from_json(path) if path else BotConfig()


async def amain(config: BotConfig) -> None:
    await BotApp(config).run()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="qq-agent-kit 主循环")
    ap.add_argument("--config", type=Path, default=None, help="JSON 配置文件路径")
    ns = ap.parse_args(argv)
    try:
        asyncio.run(amain(load_config(ns.config)))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
