# qq-agent-kit

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](requirements.txt)
[![Tests](https://github.com/Mists-Star/qq-agent-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/Mists-Star/qq-agent-kit/actions/workflows/tests.yml)

一个轻量、自托管、协议宽松的 QQ 机器人接入脚手架。基于 NapCat / OneBot v11，把"长期稳定运行"的工程问题一次性解决，把人格、记忆、业务能力留给部署方自己定义。

**核心特点：登录失败优先告警、绝不无限重试。** 这一条是硬规则，见 [SAFETY.md](docs/SAFETY.md)。

**跨平台**：核心逻辑在 Windows / macOS / Linux 均可运行（Python 3.10+，标准库为主）。协议端 NapCat 支持 Windows 原生、Mac 安装器、Docker 三种方式，框架只需通过 OneBot v11 连接——详见 [USAGE.md](USAGE.md)。

---

## 为什么有这个项目

市面上接 QQ 的机器人方案不少，但普遍存在三类问题：

1. **太重**：依赖一整套桌面运行时或商业平台，进程多、内存高、绑定强。
2. **协议不友好**：AGPL 等强传染协议，或闭源平台锁定。
3. **登录不稳**：登录态失效后守护脚本反复重拉，反而触发平台风控导致账号被限。

本项目走相反的路：

- **轻**：常驻只有一个 Python 进程 + 协议端；无桌面运行时、无托管平台。
- **宽松**：MIT 协议。
- **稳**：fail-fast 登录守护，失败即告警并停手，人工确认才恢复。
- **中立**：框架不内置任何人格/记忆内容，全部由部署方提供。

---

## 架构

```
QQ ──► NapCat (OneBot v11) ──► qq-agent-kit ──► LLM API ──► 回复 ──► QQ
                                    │
                                    ├─ core/guard.py      登录守护·熔断告警
                                    ├─ core/lock.py       单实例·防抢 Token
                                    ├─ core/splitter.py   安全切句
                                    ├─ core/degenerate.py 退化输出检测
                                    ├─ core/ratelimit.py  三层频控
                                    └─ modules/           可替换业务模块
```

各层只通过清晰接口通信，换协议端或换模型都不动核心。详见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 快速开始

> 完整步骤见 [USAGE.md](USAGE.md)（含配置、接模型、加模块、开空间、熔断恢复）。
> 参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
# 1. 环境
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
# 或 source .venv/bin/activate && pip install -r requirements.txt

# 2. 配置（复制模板后填写；可选，不配则用默认值）
cp config.example.json myconfig.json
cp persona/core.example.md persona/core.md      # 人格由部署方自己写

# 3. 起协议端：NapCat，WS 15701 / HTTP 15700，扫码登录

# 4. 启动
python bot.py                                   # 默认配置
# 或 python bot.py --config myconfig.json       # 用自定义配置
```

> 登录失败时不会反复重试：连续 3 次失败即熔断并告警，停手等待人工扫码。见 [SAFETY.md](docs/SAFETY.md)。

---

## 目录结构

```
qq-agent-kit/
├── LICENSE                 MIT
├── README.md
├── USAGE.md                完整使用指南
├── CONTRIBUTING.md         贡献指南
├── config.example.json     配置模板（JSON，无敏感信息）
├── requirements.txt
├── core/
│   ├── guard.py            ★ 登录守护状态机（fail-fast + 熔断）
│   ├── boot.py             ★ 启动协调器（熔断落到流程）
│   ├── notifier.py         ★ 多渠道告警（文件/控制台/系统气泡/webhook）
│   ├── lock.py             单实例锁
│   ├── splitter.py         安全切句（括号不撕裂）
│   ├── degenerate.py       退化重复检测
│   ├── ratelimit.py        三层频控 + AI 额度
│   ├── onebot.py           OneBot v11 协议客户端
│   ├── qzone_bridge.py     QQ 空间桥接客户端（可选·action 白名单）
│   └── __init__.py
├── bot.py                  主循环入口（串联所有核心）
├── tools/
│   └── ci_status.py        查询最近一次 CI 状态（可选工具）
├── modules/                可替换业务模块
│   └── qzone.py            QQ 空间模块（可选·默认禁用·分级授权）
├── persona/
│   └── core.example.md     人格模板示例（部署方自行填写）
├── panel/                  Web 状态面板（告警红标）
├── tests/                  单元测试
└── docs/
    ├── ARCHITECTURE.md
    ├── CAPABILITIES.md     能力矩阵与实现方式
    ├── SAFETY.md           风控与登录熔断设计
    └── MODULES.md          如何编写自己的模块
```

---

## 测试

```bash
python -m unittest discover -s tests -v
```

共 48+ 个测试，覆盖：登录熔断全流程、告警去重与分级、重启后状态保持、人工确认解锁、退避上限、启动协调器、安全切句、退化检测（多周期）、频控与 AI 额度、空间模块分级授权与 action 白名单、主循环全链路集成。

查看线上 CI 状态（无需安装 gh）：

```bash
python tools/ci_status.py                          # 匿名 GitHub API
GITHUB_TOKEN=xxx python tools/ci_status.py         # 带 token（推荐）
# 无法直连 GitHub 时，可设置 DEVHUB_API / DEVHUB_KEY 走兼容的加速转发端点
```

---

## 许可

MIT。可自由使用、修改、商用、再分发，仅需保留许可声明。

本项目不含任何第三方平台账号，不提供任何绕过平台风控的手段。第三方协议登录存在账号风险，请自行评估。
