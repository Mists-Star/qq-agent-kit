# 编写自己的模块

业务能力全部放在 `modules/`，框架核心不含任何人格、记忆或对话逻辑。

## 最小模块

```python
# modules/hello.py
from core.modules import Module, Context

class HelloModule(Module):
    name = "hello"

    async def on_private_message(self, ctx):
        if ctx.text == "/hi":
            await ctx.reply("你好")
```

模块通过钩子接入事件流：

| 钩子 | 触发时机 |
|------|----------|
| `on_private_message(ctx)` | 收到私聊 |
| `on_group_message(ctx)` | 收到群聊 |
| `on_notice(ctx)` | 收到通知类事件 |
| `before_send(ctx)` | 发送前，可改写最终内容 |
| `on_startup()` / `on_shutdown()` | 生命周期 |

## 约定

- 模块不得读取框架私有状态，只通过 `ctx` 访问能力。
- 模块异常必须被隔离，不能拖垮主循环（由模块内核负责）。
- 人格文本放在 `persona/`，由部署方维护，框架只读不改。

## 热重载

当前支持：注册、按名替换（同名注册会移除旧钩子）、开关（enable/disable）。热重载与定时任务调度尚未实现；停用模块后其钩子不再触发，但 buf 中已入队的任务需调用方自行清理。
