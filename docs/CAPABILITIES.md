# 能力矩阵与实现方式

本脚手架不重复造轮子。QQ 能力主要来自协议端（NapCat / OneBot v11），少数能力需外部桥接，框架本身只做工程稳定性。

## 能力来源映射

| 能力类别 | 实现方式 | 是否需额外组件 |
|----------|----------|----------------|
| 私聊/群聊收发 | NapCat 原生 OneBot v11 | 否 |
| 群管（踢人/禁言/管理员/名片） | NapCat 原生 API | 否 |
| 戳一戳、表情回应 | NapCat 扩展 API | 否 |
| 群文件、合并转发 | NapCat go-cqhttp 兼容 API | 否 |
| OCR、AI 语音、改头像/签名/在线状态 | NapCat 扩展 API | 否 |
| 点赞好友 | NapCat 原生 API | 否 |
| QQ 空间：发说说/删/评论/点赞/转发/相册/好友动态/访客 | 外部 MIT 桥接服务（QZone Web API -> OneBot v11） | 可选，按需部署 |
| 发送图片/语音 | base64 / 本地文件路径 | 否 |
| 多模态看图 | LLM 视觉 API | 否 |
| 人格 / 长期记忆 | 部署方自定义模块 | 否 |

## 与常见方案的取舍

| 方案 | 运行时层数 | 协议 | 取舍 |
|------|-----------|------|------|
| 本脚手架 | 1 Python 进程 + 协议端 | MIT | 轻、宽松、可控；需自己写业务 |
| 某 DSH 插件生态 | 桌面运行时 + 网关 + 插件 | 各异 | 生态全，但重且绑定运行时 |
| AstrBot / LangBot | 框架进程 + 协议端 | AGPL / Apache | 开箱即用；前者协议传染，后者平台较重 |

## 可选组件：QQ 空间桥接

若需要空间能力，可部署一个 MIT 许可的 QZone 桥接服务，它把空间操作暴露成 OneBot v11 接口：

- 提供约 40 个 action：发/删说说、评论、点赞/取消、转发、相册、好友动态、访客、隐私设置等。
- 自带 NapCat 插件形式，可直接代理 REST 与事件流。
- 端口与鉴权按部署环境配置，注意与协议端端口隔离。
- 空间接口依赖 Web 端解析，数据不保证强一致，属正常现象。

本脚手架提供 `core/qzone_bridge.py`（action 白名单客户端）与 `modules/qzone.py`（可选模块），**不内置桥接服务本身**，保持核心轻量。

启用方式：

```python
from modules.qzone import QZoneModule, QZoneModuleConfig
from core.modules import ModuleRegistry

cfg = QZoneModuleConfig(
    base_url="http://127.0.0.1:5700",
    authorized_ids={10001},      # 允许使用空间能力的账号
    write_enabled=False,         # 写操作默认关，按需开启
    read_public=False,           # 读操作默认仅授权账号
)
registry.register(QZoneModule(cfg))
registry.enable("qzone")         # 模块默认禁用，需显式启用
```

安全默认：模块默认禁用；读操作默认仅授权账号；写操作需 `write_enabled=True` 且账号在授权列表；删除类动作预留二次确认钩子。权限在代码层校验，不依赖提示词约束。

## 说明

第三方协议登录存在账号风险，本项目不提供任何规避平台风控的手段，仅提供失败后的正确处理方式（告警 + 停手）。请遵守平台服务条款。
