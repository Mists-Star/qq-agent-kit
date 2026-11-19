"""模块内核 — 加载、开关、替换、出错隔离。

模块是业务能力的载体。核心只提供钩子分发与异常隔离，不含任何业务逻辑。

设计：
    - 每个模块注册到特定钩子；事件到达时按注册顺序调用。
    - 单模块异常被捕获并记录，不影响其它模块和主循环。
    - 可停用模块（停用后其钩子不再被调用）。
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Context:
    """事件上下文。模块通过它访问能力与回复。"""

    text: str = ""
    user_id: int = 0
    group_id: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    reply_fn: Callable[[str], Awaitable[None]] | None = None

    async def reply(self, text: str) -> None:
        if self.reply_fn:
            await self.reply_fn(text)


class Module:
    """模块基类。子类覆盖需要的钩子即可。"""

    name: str = "unnamed"
    enabled: bool = True

    async def on_private_message(self, ctx: Context) -> None: ...
    async def on_group_message(self, ctx: Context) -> None: ...
    async def on_notice(self, ctx: Context) -> None: ...
    async def before_send(self, ctx: Context) -> None: ...
    async def on_startup(self) -> None: ...
    async def on_shutdown(self) -> None: ...


HookFn = Callable[..., Awaitable[None]]


class ModuleRegistry:
    def __init__(self):
        self._modules: dict[str, Module] = {}
        self._hooks: dict[str, list[tuple[str, HookFn]]] = defaultdict(list)
        self._errors: list[tuple[str, str, str]] = []

    def register(self, module: Module) -> None:
        self._modules[module.name] = module
        for hook in (
            "on_private_message",
            "on_group_message",
            "on_notice",
            "before_send",
            "on_startup",
            "on_shutdown",
        ):
            fn = getattr(module, hook, None)
            if fn is not None and callable(fn):
                self._hooks[hook].append((module.name, fn))

    def enable(self, name: str, enabled: bool = True) -> None:
        if name in self._modules:
            self._modules[name].enabled = enabled

    async def emit(self, hook: str, ctx: Context | None = None) -> None:
        for name, fn in list(self._hooks.get(hook, [])):
            mod = self._modules.get(name)
            if not mod or not mod.enabled:
                continue
            try:
                if ctx is None:
                    await fn()
                else:
                    await fn(ctx)
            except Exception as exc:
                # 单模块异常隔离：记录但不外抛
                self._errors.append((name, hook, f"{type(exc).__name__}: {exc}"))
                print(f"[module {name}.{hook}] {exc}", file=sys.stderr)

    @property
    def errors(self) -> list[tuple[str, str, str]]:
        return list(self._errors)
