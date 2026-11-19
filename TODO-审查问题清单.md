# 代码审查问题清单（2026-11-19 独立双审）— 已全部修复

> 两名独立审查者交叉审查 + 实测复现。原 48 测试全过但存在盲区，本轮修复后 66 测试全过。
> 分级：P0=核心承诺失效/P1=功能缺陷或安全/P2=隐患或一致性/P3=清理。
>
> **状态：以下 P0/P1/P2 问题均已修复并补回归测试**，详见 `tests/test_regressions.py`。
> 这份清单保留作为审查方法论的记录：为什么"测试全绿"不等于"没有问题"——
> 独立视角 + 实测复现才能发现自洽测试覆盖不到的盲区。

## P0 — 必须修

| # | 问题 | 位置 | 触发 | 修法 |
|---|------|------|------|------|
| P0-1 | `python -m core.boot` 是空操作（README 写的启动命令） | README.md:67 | 按文档启动 → 直接退出什么都没做 | 改成 `python bot.py` |
| P0-2 | `python bot.py` 未装 websockets 时静默无限重连，永不处理消息 | onebot.py:95,83-92 | 文档说"可都不装"→ 最小启动永远连不上 | 启动前显式检查依赖，缺失 fail-fast 明确报错；区分依赖错误与连接异常 |
| P0-3 | websockets ≥14 移除 `extra_headers`，无条件传入 → TypeError 被吞成无限重连 | onebot.py:98 | 按 requirements 装最新版 | 用 `additional_headers` + 兼容回退，或 pin `<14` |
| P0-4 | WebhookChannel `.format()` 花括号未转义 → 每次发送 KeyError，被 notify 吞掉 | notifier.py:104,108 | 任何 webhook 使用 | 用 `json.dumps` 构造 payload，转义花括号；接配置入口 |

## P1 — 重要

| # | 问题 | 位置 | 修法 |
|---|------|------|------|
| P1-1 | 配置模板 YAML 与加载器 JSON 完全不兼容；嵌套键全被静默忽略（`expect_uin` 不生效=连错号防护失效） | config.example.yaml / bot.py:50-58 | 统一格式：给真正可用的 config.example.json（扁平字段），加载校验未知键报错 |
| P1-2 | `_send` 同步阻塞 HTTP 卡死事件循环 | bot.py:167 | 改 async + `asyncio.to_thread` |
| P1-3 | `mark_offline()` 能从熔断态解锁且保留计数，导致静默再熔断无告警 | guard.py:238 | 仅 ONLINE 态可 mark_offline；WAIT_MANUAL 拒绝 |
| P1-4 | `create_task(on_event)` 无引用无异常回调无上限，LLM 异常静默丢失 | onebot.py:109 | task 集合 + done_callback 记录；有界并发 |
| P1-5 | WS 干净关闭（服务端主动关）零退避立即重连=高频风暴 | onebot.py:77-92 | 正常返回也走退避 |
| P1-6 | 退化检测固定 15 分块，重复单元长度 4/6/7/8/11 等漏检（`"abcd"*25` 实测漏） | degenerate.py:48 | 改滑动窗口/自相关/压缩率判定 |
| P1-7 | 同名模块重复注册导致钩子双执行、旧实例复活 | modules.py:57 | register 先移除同名旧钩子 |
| P1-8 | AI 专用额度 `ai_quota_day` 从未生效（无 is_ai=True 调用点） | bot.py:130 | generate_reply 前独立 check/consume(is_ai=True) |
| P1-9 | Web 面板存储型 XSS（告警正文直插 HTML） | panel/server.py:64 | `html.escape()` + CSP 头 |
| P1-10 | 空间二次确认是死代码：`_pending_confirm` 从不写入、`requires_confirm` 从不读取、无 delete 命令入口 | modules/qzone.py | 实现完整确认链路或删死代码与文档承诺 |
| P1-11 | `before_send`/`on_notice` 钩子注册但主循环从不 emit；文档 `from core.modules import Hook` 无法导入 | modules.py / bot.py / MODULES.md | 接进主循环或删文档承诺；修导入 |

## P2 — 隐患/一致性

| # | 问题 | 位置 |
|---|------|------|
| P2-1 | `stale_after_sec` 死配置（定义了从不读取） | guard.py:101 |
| P2-2 | `begin()` 熔断态反复调用，去重过期后周期性重复 critical 告警 | guard.py:186 |
| P2-3 | `check`/`consume` 分离 TOCTOU（当前不触发，建议 `check_and_consume`） | ratelimit.py |
| P2-4 | `_pending`/`_echo`/`_seq` 死代码（响应 echo 从不校验） | onebot.py |
| P2-5 | `call_api` 不校验 retcode，发送失败当成功（HTTP 200 但 retcode≠0） | onebot.py:40 / bot.py:167 |
| P2-6 | 畸形事件 `int("abc")` 抛 ValueError → task 静默死 | bot.py:126 |
| P2-7 | 切句器孤立引号/交叉括号导致栈永不清，整段不切 | splitter.py:45 |
| P2-8 | `boot()` try_login 抛异常泄漏锁 + guard 停在 QUICK_LOGIN | bot.py:117 |
| P2-9 | `_errors` 无界增长 | modules.py:55 |
| P2-10 | `from_dict` 只捕 ValueError，phase 为 dict/list 时 TypeError 崩溃；fail_count 字符串 +=1 崩溃 | guard.py:120 |
| P2-11 | 锁残留清理 TOCTOU（PID 复用误删他人锁） | lock.py:95 |
| P2-12 | `_seen` 无界增长 + 去重不分级（warning 抑制后续 critical） | notifier.py:152 |
| P2-13 | qzone_bridge retcode `"0"` 误判失败、`None` 误放行 | qzone_bridge.py:108 |
| P2-14 | 面板无鉴权，`--host 0.0.0.0` 暴露告警原文；alerts.jsonl 全量读入内存 | panel/server.py |
| P2-15 | `.gitignore` 未排除 `config.yaml`（README 指引创建这个含 token 的文件） | .gitignore |
| P2-16 | `tools/ci_status.py` key 入 URL query（日志泄漏风险，不硬编码） | ci_status.py:40 |

## P3 — 清理

1. `core/boot.py:101` 文档串提到不存在的 `autostart.py`
2. `docs/ARCHITECTURE.md` 数据流提到未实现的"合并窗"
3. README/USAGE 测试数 42 与实际 48 不符
4. `run(max_seconds=...)` 死参数
5. `docs/MODULES.md` 热重载承诺未实现

## 修复顺序
1. P0 全部（核心承诺）
2. P1 全部（功能/安全）
3. P2 高价值项（retcode、去重、gitignore、XSS、配置校验）
4. P3 文档一致性
