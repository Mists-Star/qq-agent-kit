"""OneBot v11 客户端 — 纯协议层，WS 收事件 + HTTP 调接口，自动重连 + 退避。

只做协议，不含任何人格/业务逻辑。换协议端（NapCat / LLOneBot）时本文件不用改。

硬规则：
    - 重连必须退避，不为一秒一次的高频重连（那是风控特征）。
    - get_login_info 核对登录号，防止连错小号。
    - 所有 action 统一走 call_api，便于打点与失败注入。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Awaitable


def _mask_uin(uin: Any) -> str:
    """账号号脱敏：保留前3后3，中间打码（告警会落盘/经面板展示）。"""
    s = str(uin)
    if len(s) <= 6:
        return "*" * len(s)
    return f"{s[:3]}***{s[-3:]}"


@dataclass
class OneBotConfig:
    ws_url: str = "ws://127.0.0.1:15701"
    http_url: str = "http://127.0.0.1:15700"
    access_token: str = ""
    expect_uin: int = 0              # 期望登录的 QQ 号，0=不校验
    reconnect_base_sec: float = 1.0
    reconnect_max_sec: float = 30.0


class OneBotClient:
    def __init__(
        self,
        config: OneBotConfig,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        on_event_error: Callable[[Exception, dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.on_event = on_event
        self.on_event_error = on_event_error
        self._seq = 0

    # ---------- HTTP action ----------
    def call_api(self, action: str, params: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
        payload = {"action": action, "params": params or {}}
        self._seq += 1
        payload["echo"] = str(self._seq)
        headers = {"Content-Type": "application/json"}
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        req = urllib.request.Request(
            f"{self.config.http_url}/{action}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # HTTP 200 不代表 action 成功：retcode≠0 一律视为失败，交给上层告警
        rc = data.get("retcode", 0)
        try:
            rc_num = int(rc)
        except (ValueError, TypeError):
            rc_num = -1
        if rc_num != 0:
            raise RuntimeError(f"{action} 返回 retcode={rc}: {data.get('message', data.get('wording', ''))}")
        return data

    def send_private_msg(self, user_id: int, text: str, **kw: Any) -> dict[str, Any]:
        return self.call_api("send_private_msg", {"user_id": user_id, "message": text, **kw})

    def send_group_msg(self, group_id: int, text: str, **kw: Any) -> dict[str, Any]:
        return self.call_api("send_group_msg", {"group_id": group_id, "message": text, **kw})

    def verify_login(self) -> tuple[bool, str]:
        """核对登录号，返回 (是否匹配, 说明)。"""
        try:
            data = self.call_api("get_login_info")
            uin = int(data.get("data", {}).get("user_id", 0))
        except Exception as exc:
            return False, f"get_login_info 失败: {exc}"
        if self.config.expect_uin and uin != self.config.expect_uin:
            # 账号号脱敏：告警会落盘并经面板读取，不写完整号码
            return False, f"登录号 {_mask_uin(uin)} != 期望 {_mask_uin(self.config.expect_uin)}"
        return True, f"登录号 {_mask_uin(uin)}"

    # ---------- WS 事件循环 ----------
    @staticmethod
    def check_ws_dependency() -> str:
        """启动前检查 WS 依赖；缺失或版本不兼容返回人类可读错误，否则返回空串。

        依赖错误是配置问题，必须 fail-fast，不能当成连接异常去无限重连。
        """
        try:
            import websockets  # type: ignore[import-not-found]
        except ImportError:
            return (
                "缺少依赖 websockets：请执行 pip install websockets "
                "（WS 事件接收必需；HTTP action 单独可用）"
            )
        # 探测 connect 支持的关键字参数（13+ 改名 additional_headers，14+ 移除 extra_headers）
        import inspect

        try:
            sig = inspect.signature(websockets.connect)
            params = sig.parameters
        except (ValueError, TypeError):
            return ""  # 无法内省时不阻断，运行时兼容回退兜底
        if "additional_headers" not in params and "extra_headers" not in params:
            return (
                f"websockets 版本 {getattr(websockets, '__version__', '?')} 的 connect() "
                "不支持 headers 参数，请安装 websockets>=12,<15"
            )
        return ""

    async def run_forever(self, *, on_reconnect: Callable[[int], None] | None = None) -> None:
        """带指数退避 + 抖动的 WS 长连接。依赖错误在启动前已校验，这里只处理连接异常。"""
        err = self.check_ws_dependency()
        if err:
            raise RuntimeError(err)
        attempt = 0
        while True:
            disconnected = False
            try:
                await self._ws_once()
                disconnected = True  # 正常返回 = 服务端干净关闭，也要退避
            except asyncio.CancelledError:
                raise
            except Exception:
                disconnected = True
            if disconnected:
                attempt += 1
                if on_reconnect:
                    on_reconnect(attempt)
                delay = min(
                    self.config.reconnect_base_sec * (2 ** min(attempt, 6)),
                    self.config.reconnect_max_sec,
                )
                delay *= 1 + random.uniform(-0.2, 0.2)
                await asyncio.sleep(max(0.5, delay))

    async def _ws_once(self) -> None:
        import inspect

        import websockets  # type: ignore[import-not-found]

        headers = {"Authorization": f"Bearer {self.config.access_token}"} if self.config.access_token else None
        # 版本兼容：13+ 用 additional_headers，旧版用 extra_headers
        try:
            params = inspect.signature(websockets.connect).parameters
        except (ValueError, TypeError):
            params = {}
        if "additional_headers" in params:
            ws_ctx = websockets.connect(self.config.ws_url, additional_headers=headers)
        elif "extra_headers" in params:
            ws_ctx = websockets.connect(self.config.ws_url, extra_headers=headers)
        else:
            ws_ctx = websockets.connect(self.config.ws_url)
        async with ws_ctx as ws:
            async for raw in ws:
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                try:
                    await self.on_event(evt)
                except Exception as exc:
                    # 单个事件处理失败不中断连接；上报给宿主记录，不静默吞
                    if self.on_event_error:
                        try:
                            self.on_event_error(exc, evt)
                        except Exception:
                            pass
