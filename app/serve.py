#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地静态服务（标准库，只服务 web/）。

依据
----
技术规格 §1  L27  `run.py / serve.py   CLI、标准库本地 HTTP`
技术规格 §10 L596 「断网本地结果与录屏可播」
赛道要求     「怎么退回第一次之前的版本，评委要看时当场演示」
            → `--allow-rollback` 打开 POST /api/rollback，页面按钮一点就真回退

为什么必须有它：
    页面现在有两条数据通路：同目录的 `data.js`（`<script src>` 同步加载，file:// 双击
    也能开）和 `fetch("*.json")`（本地服务下的兜底）。这个脚本提供后者，并且是
    唯一能让"点按钮真回退"生效的地方 —— file:// 下没有服务端，回退只能手动敲命令。

用法
----
    python app/serve.py                          # http://127.0.0.1:8000/
    python app/serve.py --port 8080
    python app/serve.py --allow-rollback         # 演示用：允许页面触发回退
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# 只有在 --allow-rollback 时才被打开；默认关闭，避免静态服务带上写操作
ALLOW_ROLLBACK = False


class Handler(http.server.SimpleHTTPRequestHandler):
    """默认只允许 GET/HEAD；不列目录；不缓存（大屏改版后必须立即生效）。"""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/api/rollback":
            self.send_error(404, "没有这个接口")
            return
        if not ALLOW_ROLLBACK:
            self._json(501, {"error": "服务未开启回退（启动时加 --allow-rollback）"})
            return
        try:
            evolve = WEB / "evolve.json"
            if not evolve.exists():
                self._json(400, {"error": "web/evolve.json 不存在，没有可回退的 pair"})
                return
            pair = json.loads(evolve.read_text(encoding="utf-8")).get("pair_id")
            if not pair:
                self._json(400, {"error": "evolve.json 里没有 pair_id"})
                return
            sys.path.insert(0, str(ROOT))
            from app.demo import mark_rollback_verified, rollback  # 延迟导入，静态服务不依赖它
            res = rollback(pair, "v0_seed")
            mark_rollback_verified(res)     # 留下"真的回退过"的证据
            self._json(200, res)
        except SystemExit as e:          # demo.rollback 用 SystemExit 报错
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def list_directory(self, path):  # noqa: D401
        self.send_error(403, "不提供目录列表")
        return None

    def log_message(self, fmt: str, *a) -> None:
        sys.stderr.write("  %s\n" % (fmt % a))


def main(argv: list[str] | None = None) -> int:
    global ALLOW_ROLLBACK
    ap = argparse.ArgumentParser(description="web/ 静态服务（标准库）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只绑本机")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--allow-rollback", action="store_true",
                    help="演示用：开启 POST /api/rollback，页面按钮可直接回退状态")
    args = ap.parse_args(argv)
    ALLOW_ROLLBACK = args.allow_rollback

    if not (WEB / "index.html").exists():
        print(f"✗ 找不到 {WEB / 'index.html'}", file=sys.stderr)
        return 1

    os.chdir(WEB)
    handler = functools.partial(Handler, directory=str(WEB))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), handler) as httpd:
        print(f"✓ 演化大屏: http://{args.host}:{args.port}/")
        print(f"✓ 双跑对照: http://{args.host}:{args.port}/evolve.html")
        print(f"  服务目录: {WEB}")
        print(f"  回退接口: {'已开启 POST /api/rollback' if ALLOW_ROLLBACK else '关闭（演示时加 --allow-rollback）'}")
        print("  Ctrl-C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

