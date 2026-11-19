"""Web 状态面板 — 读取告警与登录状态，红标显示。

纯标准库 http.server，无第三方依赖。只读展示，不暴露控制接口（避免鉴权问题）。

端点：
    GET /api/status    -> 登录守护状态（含 is_tripped / can_attempt）
    GET /api/alerts    -> 最近的告警列表（从 jsonl 读）
    GET /              -> 简单状态页

用法：
    python -m panel.server --state <guard.json> --alerts <alerts.jsonl> --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_jsonl(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """从文件尾部读取最后 limit 条，不把整个文件读进内存。"""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            pos = size
            pending = b""
            while pos > 0 and len(out) < limit:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size) + pending
                lines = chunk.split(b"\n")
                pending = lines.pop(0)  # 半行留到下一轮
                for line in reversed(lines):
                    if not line.strip():
                        continue
                    try:
                        out.append(json.loads(line.decode("utf-8")))
                    except Exception:
                        continue
                    if len(out) >= limit:
                        break
        return out[:limit]
    except Exception:
        return []


LEVEL_COLOR = {"info": "#185FA5", "warning": "#BA7517", "critical": "#A32D2D"}
PHASE_LABEL = {
    "idle": "空闲",
    "quick_login": "快速登录中",
    "backoff": "退避等待",
    "wait_manual": "已熔断·等待人工确认",
    "online": "在线",
}


def _status_html(state: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    import html

    phase = state.get("phase", "idle")
    tripped = state.get("is_tripped", False)
    color = LEVEL_COLOR["critical"] if tripped else LEVEL_COLOR["info"]
    # 标签走白名单映射，未知值统一显示"未知"，不把原始 phase 输出（防注入）
    label = PHASE_LABEL.get(phase, "未知")
    # 所有动态内容转义，防存储型 XSS（告警正文可能来自异常文本/外部事件）
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(a.get("level", "")) if a.get("level") in LEVEL_COLOR else "未知"),
            html.escape(str(a.get("title", ""))),
            html.escape(str(a.get("body", ""))),
        )
        for a in alerts
    )
    fail_count = html.escape(str(state.get("fail_count", 0)))
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>接入层状态</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#2C2C2A}}
.badge{{display:inline-block;padding:.5rem 1rem;border-radius:8px;color:#fff;background:{color}}}
table{{border-collapse:collapse;width:100%;margin-top:1.5rem}}
td,th{{border:1px solid #D3D1C7;padding:.5rem;text-align:left;font-size:13px}}
</style>
<h1>接入层状态</h1>
<p class="badge">{label}</p>
<p>连续失败：<b>{fail_count}</b>　可自动重试：{'否' if tripped else '是'}</p>
<h2>最近告警</h2>
<table><tr><th>级别</th><th>标题</th><th>内容</th></tr>{rows}</table>
</html>"""


class Handler(BaseHTTPRequestHandler):
    state_path: Path = Path("state/guard.json")
    alerts_path: Path = Path("logs/alerts.jsonl")

    def _send(self, code: int, body: str, ctype: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]  # 精确匹配，忽略 query
        if route == "/api/status":
            st = _read_json(self.state_path)
            st["is_tripped"] = st.get("phase") == "wait_manual"
            st["can_attempt"] = st.get("phase") != "wait_manual"
            self._send(200, json.dumps(st, ensure_ascii=False), "application/json")
        elif route == "/api/alerts":
            self._send(200, json.dumps(_read_jsonl(self.alerts_path), ensure_ascii=False), "application/json")
        elif route == "/":
            st = _read_json(self.state_path)
            st["is_tripped"] = st.get("phase") == "wait_manual"
            self._send(200, _status_html(st, _read_jsonl(self.alerts_path)), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *args: Any) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/guard.json")
    ap.add_argument("--alerts", default="logs/alerts.jsonl")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ns = ap.parse_args()
    if ns.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"警告：面板绑定到 {ns.host}（非本机回环），且无任何鉴权。"
            "任何人都能读取状态与告警内容。请放在受控内网或反向代理之后。",
            file=sys.stderr,
        )
    Handler.state_path = Path(ns.state)
    Handler.alerts_path = Path(ns.alerts)
    srv = ThreadingHTTPServer((ns.host, ns.port), Handler)
    print(f"panel on http://{ns.host}:{ns.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
