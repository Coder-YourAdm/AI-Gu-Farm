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
    浏览器不允许 file:// 页面 fetch 本地 JSON（CORS）。双击 index.html 会白屏，
    不是页面坏了。这个脚本把 web/ 目录挂到 127.0.0.1，断网也能演示。

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
# B7 的实时助教接口；同样默认关闭，启动时加 --allow-tutor
ALLOW_TUTOR = False
_TUTORS: dict[str, object] = {}
_TUTOR_POOL: list[dict] | None = None


def _tutor_pool() -> list[dict]:
    """助教题库：优先用 calibration（20 题覆盖 3 个知识点），退化为共同卷。

    路径一律用绝对 ROOT 拼——服务启动后会 os.chdir(WEB)，相对路径会指错地方。
    """
    global _TUTOR_POOL
    if _TUTOR_POOL is None:
        sys.path.insert(0, str(ROOT))
        from app.dataset_adapter import read_tasks
        path = ROOT / "data" / "calibration.json"
        _TUTOR_POOL = read_tasks(path) if path.exists() else []
        if not _TUTOR_POOL:
            raise RuntimeError("助教题库为空：data/calibration.json 不存在或没有题")
    return _TUTOR_POOL


def _get_tutor(sid: str):
    # 服务用 `python app/serve.py` 启动时，sys.path[0] 是 app/ 目录，
    # 直接 import app.xxx 会失败；先把仓库根放进 sys.path（幂等）。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.tutor.dialog import Tutor
    t = _TUTORS.get(sid)
    if t is None:
        t = Tutor(sid=sid, pool=_tutor_pool(), root=str(ROOT))
        _TUTORS[sid] = t
    return t


class Handler(http.server.SimpleHTTPRequestHandler):
    """默认只允许 GET/HEAD；不列目录；不缓存（大屏改版后必须立即生效）。"""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/api/ask":
            self._handle_ask()
            return
        if path != "/api/rollback":
            # 不要用 send_error(code, "中文")：那句话会进 HTTP 状态行，而状态行只允许
            # ASCII，http.server 用 latin-1 编码，中文会抛 UnicodeEncodeError，
            # 客户端看到的是连接被重置而不是 404。中文一律放进响应体。
            self._json(404, {"error": "没有这个接口", "path": self.path})
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
            from app.demo import rollback  # 延迟导入，静态服务不依赖它
            res = rollback(pair, "v0_seed")
            self._json(200, res)
        except SystemExit as e:          # demo.rollback 用 SystemExit 报错
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_ask(self) -> None:
        """B7 最小集：POST /api/ask → Tutor.handle()。任何异常都不许让进程死。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
                return
            if not isinstance(req, dict):
                self._json(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
                return
            if not ALLOW_TUTOR:
                self._json(501, {"ok": False,
                                 "error": "服务未开启实时（启动时加 --allow-tutor）"})
                return
            sid = str(req.get("sid") or "demo")
            action = str(req.get("action") or "")
            res = _get_tutor(sid).handle(action, req.get("answer"))
            self._json(200, res)
        except Exception as e:  # noqa: BLE001 - 任何异常都不许让进程死
            self._json(500, {"ok": False, "error": f"Tutor: {e}"})

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def list_directory(self, path):  # noqa: D401
        # 同上：send_error 的 message 会进状态行，中文必须走响应体
        self._json(403, {"error": "不提供目录列表"})
        return None

    def log_message(self, fmt: str, *a) -> None:
        sys.stderr.write("  %s\n" % (fmt % a))


def main(argv: list[str] | None = None) -> int:
    global ALLOW_ROLLBACK, ALLOW_TUTOR
    ap = argparse.ArgumentParser(description="web/ 静态服务（标准库）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只绑本机")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--allow-rollback", action="store_true",
                    help="演示用：开启 POST /api/rollback，页面按钮可直接回退状态")
    ap.add_argument("--allow-tutor", action="store_true",
                    help="开启 POST /api/ask 实时助教（tutor.html 的 LIVE 模式）")
    args = ap.parse_args(argv)
    ALLOW_ROLLBACK = args.allow_rollback
    ALLOW_TUTOR = args.allow_tutor

    # 同一进程里还会调用 app.demo.rollback()（它会 print "✓ 已回退…"）。
    # Windows 默认控制台是 GBK，遇到 ✓ 这类字符会 UnicodeEncodeError，被
    # do_POST 的 except 捕获后变成 500 —— 回退其实成功了，页面却报错。
    # 这里把两个流都降级为「不可编码字符替换」，任何 in-process print 都不再中断演示。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 老版本或非文本流
            pass

    if not (WEB / "index.html").exists():
        print(f"[X] 找不到 {WEB / 'index.html'}", file=sys.stderr)
        return 1

    os.chdir(WEB)
    handler = functools.partial(Handler, directory=str(WEB))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), handler) as httpd:
        # 标记只用 ASCII：Windows 默认控制台编码是 GBK，打印 ✓/✗ 会 UnicodeEncodeError 直接退出。
        print(f"[OK] 演化大屏: http://{args.host}:{args.port}/")
        print(f"[OK] 双跑对照: http://{args.host}:{args.port}/evolve.html")
        print(f"[OK] 助教三页: http://{args.host}:{args.port}/cover.html → tutor.html → compare.html")
        print(f"  服务目录: {WEB}")
        print(f"  回退接口: {'已开启 POST /api/rollback' if ALLOW_ROLLBACK else '关闭（演示时加 --allow-rollback）'}")
        print(f"  实时助教: {'已开启 POST /api/ask' if ALLOW_TUTOR else '关闭（演示时加 --allow-tutor）'}")
        print("  Ctrl-C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

