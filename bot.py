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
        if not isinstance(data, dict):
            raise ValueError(f"配置文件必须是 JSON 对象: {path}")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = set(data) - allowed
        if unknown:
            # 未知键明确报错，不静默忽略（否则拼错 expect_uin 也毫无提示）
            raise ValueError(f"未知配置项: {sorted(unknown)}（可用项: {sorted(allowed)}）")
        cfg = cls()
        for k, v in data.items():
            if k == "base_dir":
                v = Path(v)
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
            on_event_error=self._on_event_error,
        )
        self._lock = SingleInstanceLock(base / "state" / "bot.lock")

    # ---------- 启动 ----------
    def _on_event_error(self, exc: Exception, evt: dict[str, Any]) -> None:
        """事件处理异常上报：不静默吞，写告警。"""
        self.notifier.notify(
            title=f"[{self.config.alert_tag}] 事件处理异常",
            body=f"{type(exc).__name__}: {exc}"[:300],
            level="warning",
            fingerprint=f"event:error:{type(exc).__name__}",
        )

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

        # 依赖错误 fail-fast：不把"缺库/版本不对"当成连接异常去无限重连
        try:
            dep_err = self.client.check_ws_dependency()
        except Exception as exc:
            dep_err = f"依赖检查失败: {exc}"
        if dep_err:
            self._lock.release()
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 启动失败·依赖",
                body=dep_err,
                level="critical",
                fingerprint="boot:dependency",
            )
            return BootResult(False, self.guard.state.phase, 0, dep_err)

        async def try_login() -> tuple[bool, str]:
            return self.client.verify_login()

        try:
            result = await login_with_guard(self.guard, try_login)
        except Exception as exc:
            # 任何未预期异常也要释放锁，避免残留死锁
            self._lock.release()
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 启动异常",
                body=f"{type(exc).__name__}: {exc}"[:300],
                level="critical",
                fingerprint="boot:exception",
            )
            return BootResult(False, self.guard.state.phase, 0, f"启动异常: {exc}")
        if not result.ok:
            self._lock.release()
        return result

    # ---------- 事件处理 ----------
    @staticmethod
    def _as_int(value: Any) -> int:
        """容错整数转换：畸形事件字段归零，不让 ValueError 杀掉 task。"""
        try:
            return int(value or 0)
        except (ValueError, TypeError):
            return 0

    async def on_event(self, evt: dict[str, Any]) -> None:
        post_type = evt.get("post_type")
        if post_type == "notice":
            # 通知类事件交给模块钩子（此前 on_notice 注册了却从不 emit）
            await self.registry.emit("on_notice", Context(raw=evt))
            return
        if post_type != "message":
            return
        user_id = self._as_int(evt.get("user_id"))
        group_id = self._as_int(evt.get("group_id"))
        text = str(evt.get("raw_message", "") or "")

        ok, reason = self.limiter.check_and_consume(user_id)
        if not ok:
            return  # 频控拒绝，静默丢弃（不告警，避免噪声）

        replies: list[str] = []

        async def collect(t: str) -> None:
            replies.append(t)

        ctx = Context(text=text, user_id=user_id, group_id=group_id, raw=evt, reply_fn=collect)
        hook = "on_group_message" if group_id else "on_private_message"
        await self.registry.emit(hook, ctx)

        if not replies and self.generate_reply:
            # AI 专用额度独立核算：与入站消息频控分开，真正防成本失控
            ai_ok, ai_reason = self.limiter.check_and_consume(user_id, is_ai=True, skip_general=True)
            if not ai_ok:
                self.notifier.notify(
                    title=f"[{self.config.alert_tag}] AI 额度已用尽",
                    body=ai_reason,
                    level="warning",
                    fingerprint="ai:quota_exhausted",
                )
            else:
                generated = await self.generate_reply(ctx)
                if generated:
                    replies.append(generated)

        for text_out in replies:
            await self.deliver(user_id, group_id, text_out)

    async def deliver(self, user_id: int, group_id: int, text: str) -> None:
        """before_send 改写 -> 退化检测 -> 安全切句 -> 逐句异步发送。"""
        # 给模块最后一次改写/拦截出站内容的机会
        out_ctx = Context(text=text, user_id=user_id, group_id=group_id)
        await self.registry.emit("before_send", out_ctx)
        text = out_ctx.text
        if not text:
            return

        if self.detector.is_degenerate(text):
            self.notifier.notify(
                title=f"[{self.config.alert_tag}] 已拦截退化输出",
                body=self.detector.reason(text)[:200],
                level="warning",
                fingerprint="reply:degenerate",
            )
            return
        for sentence in safe_sentence_cut(text):
            await self._send(user_id, group_id, sentence)
            if self.config.send_interval_sec > 0:
                await asyncio.sleep(self.config.send_interval_sec)

    async def _send(self, user_id: int, group_id: int, text: str) -> None:
        """同步 HTTP 调用丢到线程池，不阻塞 asyncio 事件循环。"""
        try:
            if group_id:
                await asyncio.to_thread(self.client.send_group_msg, group_id, text)
            else:
                await asyncio.to_thread(self.client.send_private_msg, user_id, text)
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
