"""QQ 空间桥接客户端 — 把外部 MIT 桥接服务的能力封装成语义化接口。

本模块不内置任何桥接服务，只定义调用约定：
    桥接服务把 QZone Web API 暴露成 OneBot v11 风格的 action，
    本客户端通过 HTTP POST 调用这些 action。

安全设计：
    - action 白名单：只放行已知的空间 action，不接受任意 action 名，防止被误用。
    - 写操作与读操作分开标记，上层模块可据此做分级授权。
    - 调用失败抛异常，由上层决定是否告警，不做静默重试。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionSpec:
    action: str
    write: bool  # 是否写操作（发/删/改），写操作需更高授权


# 已知空间 action 白名单（来自桥接服务约定）
ACTION_SPECS: dict[str, ActionSpec] = {
    spec.action: spec
    for spec in [
        ActionSpec("send_msg", True),
        ActionSpec("send_private_msg", True),
        ActionSpec("delete_msg", True),
        ActionSpec("send_comment", True),
        ActionSpec("delete_comment", True),
        ActionSpec("send_like", True),
        ActionSpec("unlike", True),
        ActionSpec("forward_msg", True),
        ActionSpec("create_album", True),
        ActionSpec("delete_album", True),
        ActionSpec("delete_photo", True),
        ActionSpec("upload_image", True),
        ActionSpec("set_emotion_privacy", True),
        ActionSpec("login_cookie", True),
        ActionSpec("update_cookie", True),
        ActionSpec("login_qr", True),
        ActionSpec("logout", True),
        ActionSpec("get_emotion_list", False),
        ActionSpec("get_friend_feeds", False),
        ActionSpec("get_comment_list", False),
        ActionSpec("get_like_list", False),
        ActionSpec("get_msg", False),
        ActionSpec("get_album_list", False),
        ActionSpec("get_photo_list", False),
        ActionSpec("get_feed_images", False),
        ActionSpec("fetch_image", False),
        ActionSpec("get_visitor_list", False),
        ActionSpec("get_traffic_data", False),
        ActionSpec("get_friend_list", False),
        ActionSpec("get_portrait", False),
        ActionSpec("get_stranger_info", False),
        ActionSpec("get_login_info", False),
        ActionSpec("get_status", False),
        ActionSpec("check_cookie", False),
        ActionSpec("probe_capabilities", False),
    ]
}


@dataclass
class QZoneConfig:
    base_url: str = "http://127.0.0.1:5700"
    access_token: str = ""
    timeout: float = 15.0


class QZoneError(RuntimeError):
    pass


class QZoneBridge:
    """空间桥接客户端。action 白名单 + 写读分级。"""

    def __init__(self, config: QZoneConfig | None = None):
        self.config = config or QZoneConfig()

    def is_write_action(self, action: str) -> bool:
        spec = ACTION_SPECS.get(action)
        if spec is None:
            raise QZoneError(f"action not allowed: {action}")
        return spec.write

    def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用一个空间 action。非白名单 action 直接拒绝。"""
        if action not in ACTION_SPECS:
            raise QZoneError(f"action not allowed: {action}")
        payload = {"action": action, "params": params or {}, "echo": action}
        headers = {"Content-Type": "application/json"}
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        req = urllib.request.Request(
            self.config.base_url.rstrip("/") + "/",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("retcode") not in (0, None):
            raise QZoneError(f"{action} retcode={data.get('retcode')}: {data.get('data')}")
        return data

    # ---------- 读操作 ----------
    def my_emotions(self, num: int = 10) -> dict[str, Any]:
        return self.call("get_emotion_list", {"num": num})

    def friend_feeds(self, num: int = 20) -> dict[str, Any]:
        return self.call("get_friend_feeds", {"num": num})

    def comments(self, tid: str, num: int = 20) -> dict[str, Any]:
        return self.call("get_comment_list", {"tid": tid, "num": num})

    def health(self) -> dict[str, Any]:
        return self.call("get_status")
