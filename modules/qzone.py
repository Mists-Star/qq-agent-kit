"""QQ 空间模块（可选）— 默认禁用，写操作需授权。

启用方式：注册模块后调用 enable()，并配置 authorized_ids（允许的账号）。
本模块依赖 core/qzone_bridge.py 的外部桥接服务，不内置任何服务本身。

安全默认（对齐「开放模块」原则）：
    - 模块 enabled 默认 False，不显式启用则完全不工作。
    - 读操作：默认仅授权账号可用（read_public=False）。
    - 写操作：必须授权账号，且 requires_confirm 的动作需要二次确认。
    - 权限校验在代码层，不依赖任何提示词约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.modules import Context, Module
from core.qzone_bridge import QZoneBridge, QZoneConfig, QZoneError


@dataclass
class QZoneModuleConfig:
    base_url: str = "http://127.0.0.1:5700"
    access_token: str = ""
    authorized_ids: set[int] = field(default_factory=set)  # 允许使用空间能力的账号
    read_public: bool = False            # 读操作是否对所有账号开放
    write_enabled: bool = False          # 写操作总开关
    requires_confirm: set[str] = field(
        default_factory=lambda: {"delete_msg", "delete_comment", "delete_album", "delete_photo"}
    )


class QZoneModule(Module):
    name = "qzone"
    enabled = False  # 默认禁用

    def __init__(self, config: QZoneModuleConfig | None = None, *, bridge: QZoneBridge | None = None):
        self.config = config or QZoneModuleConfig()
        self.bridge = bridge or QZoneBridge(
            QZoneConfig(base_url=self.config.base_url, access_token=self.config.access_token)
        )
        self._pending_confirm: dict[int, tuple[str, dict]] = {}

    # ---------- 权限 ----------
    def _authorized(self, user_id: int) -> bool:
        return user_id in self.config.authorized_ids

    def _can_read(self, user_id: int) -> bool:
        return self.config.read_public or self._authorized(user_id)

    def _can_write(self, user_id: int) -> bool:
        return self.config.write_enabled and self._authorized(user_id)

    # ---------- 命令 ----------
    async def on_private_message(self, ctx: Context) -> None:
        text = ctx.text.strip()
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/说说":
            await self._post(ctx, arg)
        elif cmd == "/空间动态":
            await self._feeds(ctx)
        elif cmd == "/日记":
            await self._post(ctx, arg)
        elif cmd == "/空间状态":
            await self._status(ctx)
        elif cmd in ("/确认", "/取消"):
            await self._confirm(ctx, cmd == "/确认")
        elif cmd == "/空间帮助":
            await self._help(ctx)

    async def _post(self, ctx: Context, content: str) -> None:
        if not self._can_write(ctx.user_id):
            await ctx.reply("空间写操作未启用或账号未授权。")
            return
        if not content:
            await ctx.reply("请提供说说内容，例如：/说说 今天天气不错")
            return
        # 删除类动作需二次确认，这里只做发布；保留确认钩子供扩展
        try:
            result = self.bridge.call("send_msg", {"message": content})
            await ctx.reply("已发布到空间。")
        except QZoneError as exc:
            await ctx.reply(f"发布失败：{exc}")

    async def _feeds(self, ctx: Context) -> None:
        if not self._can_read(ctx.user_id):
            await ctx.reply("空间读取未对该账号开放。")
            return
        try:
            data = self.bridge.friend_feeds(num=10)
            await ctx.reply(f"好友动态读取成功（{len(str(data))} 字节）。")
        except QZoneError as exc:
            await ctx.reply(f"读取失败：{exc}")

    async def _status(self, ctx: Context) -> None:
        if not self._can_read(ctx.user_id):
            await ctx.reply("空间状态未对该账号开放。")
            return
        try:
            self.bridge.health()
            await ctx.reply("空间桥接服务在线。")
        except QZoneError as exc:
            await ctx.reply(f"桥接服务不可用：{exc}")

    async def _confirm(self, ctx: Context, agree: bool) -> None:
        """二次确认处理（占位）：危险动作先登记，确认后再执行。"""
        pending = self._pending_confirm.pop(ctx.user_id, None)
        if not pending:
            await ctx.reply("没有待确认的操作。")
            return
        if not agree:
            await ctx.reply("已取消。")
            return
        action, params = pending
        if not self._can_write(ctx.user_id):
            await ctx.reply("账号无写权限。")
            return
        try:
            self.bridge.call(action, params)
            await ctx.reply("已执行。")
        except QZoneError as exc:
            await ctx.reply(f"执行失败：{exc}")

    async def _help(self, ctx: Context) -> None:
        await ctx.reply(
            "空间命令：/说说 <内容> · /日记 <内容> · /空间动态 · /空间状态 · /确认 · /取消。"
            "写操作需启用并授权。"
        )
