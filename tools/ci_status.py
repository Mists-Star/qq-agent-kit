"""查询本仓库 GitHub Actions 最近一次 CI 状态。

两种通道，任选其一：
    1. GitHub Token（通用）：设置 GITHUB_TOKEN，直接读 api.github.com
    2. 加速转发（可选）：设置 DEVHUB_API + DEVHUB_KEY，走自定义转发端点
       ——适合本机无法直连 GitHub 的环境，接口需兼容 gh_proxy_fetch 约定

两者都不设时，走匿名 GitHub API（有频率限制，通常够查一次）。

用法：
    python tools/ci_status.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

REPO = os.environ.get("CI_REPO", "Mists-Star/qq-agent-kit")
WORKFLOW = os.environ.get("CI_WORKFLOW", "tests.yml")
API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=1"


def _fetch(url: str, headers: dict[str, str] | None = None, payload: dict | None = None) -> dict:
    # 本地/内网请求绕过代理，外网请求用系统默认
    if url.startswith("http://127.") or url.startswith("http://localhost"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with opener.open(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    devhub_api = os.environ.get("DEVHUB_API", "")
    devhub_key = os.environ.get("DEVHUB_KEY", "")

    if devhub_api and devhub_key:
        # 走加速转发端点：key 放 POST body，不进 URL query（防 access log 泄漏）
        data = _fetch(
            devhub_api.rstrip("/"),
            headers={"Content-Type": "application/json"},
            payload={"action": "gh_proxy_fetch", "key": devhub_key, "url": API},
        )
    else:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = _fetch(API, headers)

    if "workflow_runs" not in data:
        print("查询失败：", data.get("error") or data.get("message") or data, file=sys.stderr)
        return 2
    runs = data["workflow_runs"]
    if not runs:
        print("没有找到 CI 运行记录")
        return 1
    run = runs[0]
    print(f"状态: {run.get('status')} | 结论: {run.get('conclusion')}")
    print(f"分支: {run.get('head_branch')} | 工作流: {run.get('name')}")
    print(f"详情: {run.get('html_url')}")
    return 0 if run.get("conclusion") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
