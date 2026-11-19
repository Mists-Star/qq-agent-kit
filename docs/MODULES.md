# 编写自己的模块

业务能力全部放在 `modules/`，框架核心不含任何人格、记忆或对话逻辑。

## 最小模块

```python
# modules/hello.py
from core.modules import Module, Hook

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

模块内核支持开关、替换与热重载：注册的工具、钩子、定时任务都挂在模块名下，停用时会一并撤销。（接口版本化，详见后续版本。）
