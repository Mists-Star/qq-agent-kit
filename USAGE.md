# 使用指南

这份文档讲清楚一件事：怎么把这个脚手架从空目录跑到能收发消息。

---

## 0. 前置准备

| 项 | 说明 |
|----|------|
| Python | 3.10+（用了 `X | Y` 类型语法） |
| 协议端 | NapCat / LLOneBot 等任意 OneBot v11 实现，先扫码登录好 |
| 模型 API | 可选。框架不内置模型，需自己提供 `generate_reply` 回调 |

核心模块（登录守护、切句、频控、单实例）**零第三方依赖**，标准库即可运行。
只有用到异步 WS 或系统气泡时才需要可选依赖：

```bash
python -m venv .venv
.venv\Scripts\pip install websockets notifypy   # 按需，可都不装
```

---

## 1. 最小启动（不含模型，只验证链路）

```bash
# 在项目根目录
python bot.py
```

此时：
- 读取默认配置（WS `127.0.0.1:15701` / HTTP `127.0.0.1:15700`）
- 校验登录号，成功后进入事件循环
- 因为没接 `generate_reply`，只跑模块（默认没有启用的业务模块），不会自动回复

要让它回复，用下面第 3 步接一个生成回调。

---

## 2. 配置文件（可选）

默认配置写在 `bot.py` 的 `BotConfig` 里。要覆盖，复制模板改字段，然后用 `--config` 指定：

```bash
cp config.example.yaml myconfig.json   # 注意：--config 读 JSON，不是 YAML
python bot.py --config myconfig.json
```

一个最小 JSON 例子：

```json
{
  "ws_url": "ws://127.0.0.1:15701",
  "http_url": "http://127.0.0.1:15700",
  "expect_uin": 123456789,
  "max_failures": 3,
  "alert_tag": "My Bot",
  "per_minute": 20
}
```

字段含义见 `config.example.yaml` 注释。

> `expect_uin` 强烈建议填——它会核对实际登录的号，防止连错账号触发风控。

---

## 3. 接自己的模型（generate_reply）

框架不碰模型和密钥。在自己代码里注入一个异步回调：

```python
import asyncio
from pathlib import Path
from bot import BotApp, BotConfig
from core.modules import Context

async def my_generate(ctx: Context) -> str | None:
    # 这里换成你自己的模型调用，例如：
    # return await call_my_llm(ctx.text)
    return f"你说的是：{ctx.text}。"

async def main():
    cfg = BotConfig(base_dir=Path("."))
    app = BotApp(cfg, generate_reply=my_generate)
    await app.run()   # 内含熔断登录，失败即告警停手

asyncio.run(main())
```

`generate_reply` 返回的字符串会经过：退化检测 → 安全切句 → 逐句发送。你不用自己处理括号拆分或死循环输出。

---

## 4. 加业务模块

模块放在 `modules/`，继承 `Module` 并覆盖需要的钩子：

```python
from core.modules import Context, Module

class GreetModule(Module):
    name = "greet"

    async def on_private_message(self, ctx: Context):
        if ctx.text == "/hi":
            await ctx.reply("你好")
```

注册：

```python
from core.modules import ModuleRegistry
from modules.greet import GreetModule

reg = ModuleRegistry()
reg.register(GreetModule())
app = BotApp(cfg, registry=reg, generate_reply=my_generate)
```

详见 `docs/MODULES.md`。

---

## 5. 开启 QQ 空间（可选）

空间能力靠外部 MIT 桥接服务，框架只做客户端。步骤：

1. 部署一个把 QZone Web API 暴露成 OneBot v11 action 的桥接服务（默认端口 5700），完成登录。
2. 启用模块：

```python
from modules.qzone import QZoneModule, QZoneModuleConfig

cfg = QZoneModuleConfig(
    base_url="http://127.0.0.1:5700",
    authorized_ids={123456789},   # 允许使用的账号
    write_enabled=False,          # 写操作默认关，确认要发说说再开
)
reg.register(QZoneModule(cfg))
reg.enable("qzone")               # 模块默认禁用，必须显式启用
```

3. 账号私聊发命令：`/空间状态` 测连通，`/说说 <内容>` 发布（需 `write_enabled=True`）。

详见 `docs/CAPABILITIES.md`。

---

## 6. 登录失败会怎样（重点）

这是本脚手架的核心行为，务必理解：

```
快速登录失败 1~2 次 -> 退避重试（指数+抖动，非高频）
第 3 次仍失败      -> 熔断：停手，不再自动重试
                   -> 发 critical 告警（logs/alerts.jsonl + 控制台）
进程重启           -> 熔断状态从 state/guard.json 恢复，不会从头死磕
人工处理           -> 手机端完成验证 + 扫码，然后调用 guard.human_confirm() 解除
```

**你不需要也不应该**写"失败就无限重连"的守护脚本——那正是触发平台风控的典型原因。

告警在哪看：
- `logs/alerts.jsonl`：每行一条 JSON
- Web 面板：`python -m panel.server`，浏览器打开 `http://127.0.0.1:8765`，熔断显示红标

解除熔断（人工确认后）：

```python
from core.guard import FileStateStore, LoginGuard
from core.notifier import default_notifier

g = LoginGuard(default_notifier(Path(".")), FileStateStore(Path("state/guard.json")))
g.human_confirm()
```

---

## 7. 启动方式（由你决定）

本脚手架**不内置任何开机自启/进程守护逻辑**，启动方式完全交给部署方。可选：

- 手动 `python bot.py`
- 系统服务 / 计划任务
- 容器 `CMD ["python", "bot.py"]`

这样做的原因：启动环境的差异（异常关机后的自动修复、多启动源抢登录态）是账号风控的高发诱因，交给部署方按自己的环境处理最稳妥。见 `docs/SAFETY.md`。

---

## 8. 跑测试

```bash
python -m unittest discover -s tests -v
```

当前约 42 个测试，覆盖：登录熔断、告警去重、重启保持、切句括号、退化检测、频控、空间授权、主循环全链路。

---

## 常见问题

**Q：为什么发了消息没回复？**
A：检查三处——模块是否 `enabled`、账号是否过频控、`generate_reply` 是否注入并返回了内容。

**Q：为什么登录失败后不动了？**
A：这是设计行为（熔断）。看 `logs/alerts.jsonl`，按第 6 步人工确认恢复。

**Q：能换协议端吗？**
A：能。只要对方实现 OneBot v11，改 `ws_url` / `http_url` 即可，`core/onebot.py` 不用改。

**Q：空间功能报 action not allowed？**
A：该 action 不在白名单。白名单在 `core/qzone_bridge.py` 的 `ACTION_SPECS`，按桥接服务实际支持的 action 增删。
