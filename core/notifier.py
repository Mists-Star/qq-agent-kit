"""多渠道通知器 — 登录失败等关键时刻，优先把消息送到人眼前。

设计要点：
- 通知和业务逻辑解耦：任何模块都能 ``notify(title, body, level)``。
- 多通道并行投递，单通道失败不影响其它通道（best-effort）。
- 每个通道可独立开关；默认只启用本地文件 + 控制台，联网通道需显式配置。
- 去重：同一 fingerprint 在 cooldown 内不重复通知，防止告警风暴。

通道：
    FileChannel    -> logs/alerts.jsonl（Web 面板读取，红标告警）
    ConsoleChannel -> stderr（开发期可见）
    ToastChannel   -> Windows 系统托盘气泡（pythonw 无窗口也能弹）
    WebhookChannel -> 自定义 HTTP POST（企业微信/钉钉/Server酱等）

"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

LEVEL_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _atomic_write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """追加一行 JSON 到 jsonl，用 O_APPEND 保证多进程安全。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


class Channel:
    """通道基类。子类实现 send()。"""

    name = "base"
    enabled = True

    def send(self, title: str, body: str, level: str, extra: dict[str, Any] | None = None) -> bool:
        raise NotImplementedError


@dataclass
class FileChannel(Channel):
    """写告警到 jsonl 文件，Web 面板轮询此文件显示状态。"""

    path: Path
    name: str = "file"
    enabled: bool = True

    def send(self, title: str, body: str, level: str, extra: dict[str, Any] | None = None) -> bool:
        _atomic_write_jsonl(
            self.path,
            {
                "ts": time.time(),
                "level": level,
                "title": title,
                "body": body,
                "extra": extra or {},
            },
        )
        return True


@dataclass
class ConsoleChannel(Channel):
    name: str = "console"
    enabled: bool = True

    def send(self, title: str, body: str, level: str, extra: dict[str, Any] | None = None) -> bool:
        prefix = {"info": "INFO", "warning": "WARN", "critical": "CRIT"}.get(level, level.upper())
        print(f"[{prefix}] {title}: {body}", file=sys.stderr, flush=True)
        return True


@dataclass
class CallbackChannel(Channel):
    """自定义回调，便于测试或接入任意宿主（如 Qt 信号、DSH 事件总线）。"""

    callback: Callable[[str, str, str, dict[str, Any] | None], None]
    name: str = "callback"
    enabled: bool = True

    def send(self, title: str, body: str, level: str, extra: dict[str, Any] | None = None) -> bool:
        self.callback(title, body, level, extra)
        return True


@dataclass
class WebhookChannel(Channel):
    """HTTP POST JSON，接企业微信/钉钉/Server酱等。

    payload 用 json.dumps 整体构造，杜绝字符串拼接/format 造成的非法 JSON
    （此前模板花括号未转义，str.format 直接 KeyError）。
    """

    url: str
    name: str = "webhook"
    enabled: bool = True
    timeout: float = 5.0

    def build_payload(self, title: str, body: str, level: str) -> str:
        """生成企业微信机器人格式的 JSON。可被单测独立校验。"""
        text = f"[{level.upper()}] {title}\n{body}"
        return json.dumps(
            {"msgtype": "text", "text": {"content": text}},
            ensure_ascii=False,
        )

    def send(self, title: str, body: str, level: str, extra: dict[str, Any] | None = None) -> bool:
        raw = self.build_payload(title, body, level)
        req = urllib.request.Request(
            self.url,
            data=raw.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return 200 <= resp.status < 300


@dataclass
class ToastChannel(Channel):
    """Windows 系统托盘气泡。pythonw 无控制台也能弹。仅 Windows 生效。"""

    name: str = "toast"
    enabled: bool = True

    def send(self, title: str, body: str, level: str, extra: dict[str, Any] | None = None) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from notifypy import Notify  # 可选依赖，缺失则降级

            n = Notify()
            n.title = title
            n.message = body
            n.send(block=False)
            return True
        except Exception:
            return False


@dataclass
class Notifier:
    """聚合通知器。

    Args:
        channels: 通道列表。
        dedup_sec: 相同 fingerprint 的去重窗口（秒），0 表示不去重。
    """

    channels: list[Channel] = field(default_factory=list)
    dedup_sec: float = 60.0
    max_seen: int = 512  # 去重表上限，防长期运行内存泄漏
    _seen: dict[str, float] = field(default_factory=dict, repr=False)
    _seen_level: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def notify(
        self,
        title: str,
        body: str,
        level: str = "info",
        *,
        fingerprint: str | None = None,
        extra: dict[str, Any] | None = None,
        force: bool = False,
    ) -> int:
        """投递一条通知，返回成功送达的通道数。

        force=True 跳过去重（用于升级告警等必须送达的场景）。
        """
        if level not in LEVEL_ORDER:
            raise ValueError(f"unknown level: {level}")
        key = fingerprint or f"{level}|{title}|{body}"
        now = time.time()
        with self._lock:
            self._prune_seen(now)
            if not force and self.dedup_sec > 0:
                last = self._seen.get(key, 0.0)
                last_level = self._seen_level.get(key, level)
                # 级别升级（warning -> critical）强制穿透，不被低级别抑制
                if now - last < self.dedup_sec and LEVEL_ORDER[level] <= LEVEL_ORDER[last_level]:
                    return 0
            self._seen[key] = now
            self._seen_level[key] = level

        delivered = 0
        for ch in self.channels:
            if not ch.enabled:
                continue
            try:
                if ch.send(title, body, level, extra):
                    delivered += 1
            except Exception as exc:  # 单通道失败不影响其它
                print(f"[notifier] channel {ch.name} failed: {exc}", file=sys.stderr)
        return delivered

    def _prune_seen(self, now: float) -> None:
        """惰性清理：删过期项；仍超上限时按时间删最旧。调用方须持 _lock。"""
        expired = [k for k, t in self._seen.items() if now - t > max(self.dedup_sec, 1.0) * 2]
        for k in expired:
            self._seen.pop(k, None)
            self._seen_level.pop(k, None)
        while len(self._seen) >= self.max_seen:
            oldest = min(self._seen, key=self._seen.get)  # type: ignore[arg-type]
            self._seen.pop(oldest, None)
            self._seen_level.pop(oldest, None)

    def clear_dedup(self, fingerprint: str | None = None) -> None:
        """清除去重记录；fingerprint=None 清空全部。"""
        with self._lock:
            if fingerprint is None:
                self._seen.clear()
                self._seen_level.clear()
            else:
                self._seen.pop(fingerprint, None)
                self._seen_level.pop(fingerprint, None)


def default_notifier(base_dir: Path, *, console: bool = True) -> Notifier:
    """构造默认通知器：文件 + 控制台，联网/系统通道按需开启。"""
    channels: list[Channel] = [FileChannel(path=base_dir / "logs" / "alerts.jsonl")]
    if console:
        channels.append(ConsoleChannel())
    return Notifier(channels=channels)
