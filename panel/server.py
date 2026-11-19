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
import mimetypes
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
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out[-limit:][::-1]
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
    phase = state.get("phase", "idle")
    tripped = state.get("is_tripped", False)
    color = LEVEL_COLOR["critical"] if tripped else LEVEL_COLOR["info"]
    label = PHASE_LABEL.get(phase, phase)
    rows = "".join(
        f'<tr><td>{a.get("level","")}</td><td>{a.get("title","")}</td>'
        f'<td>{a.get("body","")}</td></tr>'
        for a in alerts
    )
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>接入层状态</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#2C2C2A}}
.badge{{display:inline-block;padding:.5rem 1rem;border-radius:8px;color:#fff;background:{color}}}
table{{border-collapse:collapse;width:100%;margin-top:1.5rem}}
td,th{{border:1px solid #D3D1C7;padding:.5rem;text-align:left;font-size:13px}}
</style>
<h1>接入层状态</h1>
<p class="badge">{label}</p>
<p>连续失败：<b>{state.get("fail_count",0)}</b>　可自动重试：{'否' if tripped else '是'}</p>
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
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/status"):
            st = _read_json(self.state_path)
            st["is_tripped"] = st.get("phase") == "wait_manual"
            st["can_attempt"] = st.get("phase") != "wait_manual"
            self._send(200, json.dumps(st, ensure_ascii=False), "application/json")
        elif self.path.startswith("/api/alerts"):
            self._send(200, json.dumps(_read_jsonl(self.alerts_path), ensure_ascii=False), "application/json")
        elif self.path == "/":
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
    Handler.state_path = Path(ns.state)
    Handler.alerts_path = Path(ns.alerts)
    srv = ThreadingHTTPServer((ns.host, ns.port), Handler)
    print(f"panel on http://{ns.host}:{ns.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
