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


@dataclass
class OneBotConfig:
    ws_url: str = "ws://127.0.0.1:15701"
    http_url: str = "http://127.0.0.1:15700"
    access_token: str = ""
    expect_uin: int = 0              # 期望登录的 QQ 号，0=不校验
    reconnect_base_sec: float = 1.0
    reconnect_max_sec: float = 30.0


class OneBotClient:
    def __init__(self, config: OneBotConfig, on_event: Callable[[dict[str, Any]], Awaitable[None]]):
        self.config = config
        self.on_event = on_event
        self._echo = 0
        self._pending: dict[str, asyncio.Future] = {}
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
            return json.loads(resp.read().decode("utf-8"))

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
            return False, f"登录号 {uin} != 期望 {self.config.expect_uin}"
        return True, f"登录号 {uin}"

    # ---------- WS 事件循环 ----------
    async def run_forever(self, *, on_reconnect: Callable[[int], None] | None = None) -> None:
        """带指数退避 + 抖动的 WS 长连接。需要 websockets 库（可选依赖）。"""
        attempt = 0
        while True:
            try:
                await self._ws_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
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
        import websockets  # type: ignore[import-not-found]

        extra = {"Authorization": f"Bearer {self.config.access_token}"} if self.config.access_token else None
        async with websockets.connect(self.config.ws_url, extra_headers=extra) as ws:  # type: ignore[call-arg]
            async for raw in ws:
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                if "echo" in evt and evt.get("echo") in self._pending:
                    fut = self._pending.pop(evt["echo"])
                    if not fut.done():
                        fut.set_result(evt)
                    continue
                asyncio.create_task(self.on_event(evt))
