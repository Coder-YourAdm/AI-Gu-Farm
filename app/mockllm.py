#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地假模型服务（B1）：OpenAI 兼容，只依赖标准库，能按需失败。

归属
----
方案/B_比对报告.md:183  「B1 本地服务 | 0（C 做，B 验收）」
方案/B_比对报告.md:51   「B2/B3 用本地服务替代假 chat()，验证真实 HTTP 路径、
                        JSON 解析、重试逻辑。这比纯 mock 更接近真实，且不消耗预算。」

它是什么，不是什么
------------------
不是「一个会答题的假模型」——语义正确性它一概不管。
是「一个协议完全正确、但可以按需失败的对端」。

区别很重要：如果桩永远返回 200 + 合法 JSON，那么 B 的 retry、refetch、
截断处理全是死代码 —— 写了但一次都没执行过。这些分支要到正式跑才第一次
执行，而那时没有重跑窗口。所以这个服务的正文不是 ok 场景，是后面那八个。

用法
----
    python -m app.mockllm                       # 127.0.0.1:8001，默认 ok
    python -m app.mockllm --port 8123
    python -m app.mockllm --content-file data/hard_det.json

B 侧只改两行配置：
    base_url = http://127.0.0.1:8001/v1
    model    = deepseek-flash        # 原名即可，不用为了联调改名

场景怎么切（优先级从高到低）
--------------------------
1. 请求头  `X-Mock-Scenario: flaky`
2. 模型名  `mock-flaky`             （只改 config.model 一个字段，不用动 URL）
3. 启动参数 `--scenario`             （默认 ok）

    ok          200，content 由 --content / --content-file 决定
    malformed   200 但响应体是截断的 JSON   → 测 JSONDecodeError 分支
    notjson     200 但响应体是纯文本        → 同上，更彻底
    flaky       前 N 次 500，之后成功并清零 → 测重试真的生效
    ratelimit   前 N 次 429，之后成功并清零 → 测 429 属于可重试
    servererror 永久 500                   → 测重试耗尽后抛得清不清楚
    badkey      401                        → 测「4xx 不重试」
    truncated   200 + finish_reason=length + content="" + reasoning_tokens
    slow        先 sleep 再返回            → 测 timeout

为什么 truncated 是重点场景
--------------------------
deepseek-flash 是推理模型：先烧 reasoning_tokens，再吐 content。
max_tokens 给 1200 时，5 阶行列式一道题要 3700+ 推理 token，content 是空串。
空串往下传会被判成「模型不会做」，真病因却是预算不够 —— 这种误诊会让团队
去换模型，白折腾一轮。真接口在这种情况下返回 finish_reason=length，
工具链必须看这个字段，不能只看 content 非空。C 侧昨晚就是踩这个坑。

约定
----
· 不给 key 也能用（联调阶段不该被 key 卡住）；要测 401 用 badkey 场景。
· 参数校验模拟真接口：messages 非空、max_tokens > 0，否则 400。
  桩如果比真接口宽松，B 的 payload 拼错了也发现不了，到真跑才报错。
· 只服务本机，默认绑 127.0.0.1。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8001
DEFAULT_CONTENT = "42"
DEFAULT_FLAKY_FAIL = 2
DEFAULT_SLOW_S = 3.0

SCENARIOS = (
    "ok", "malformed", "notjson", "flaky", "ratelimit",
    "servererror", "badkey", "truncated", "slow",
)

_CHAT_PATHS = ("/v1/chat/completions", "/chat/completions")
_MODEL_PATHS = ("/v1/models", "/models")

# 运行期配置。make_server() 会覆盖，测试也靠它注入。
_CFG: dict = {
    "scenario": "ok",
    "content": DEFAULT_CONTENT,
    "flaky_fail": DEFAULT_FLAKY_FAIL,
    "slow_s": DEFAULT_SLOW_S,
    "quiet": False,
    "record": None,
}

_STATE: dict = {"counts": {}, "total": 0, "by_scenario": {}}
_LOCK = threading.Lock()


# ────────────────────────────── 场景与响应 ──────────────────────────────
def _bump(key: str) -> int:
    with _LOCK:
        _STATE["counts"][key] = _STATE["counts"].get(key, 0) + 1
        return _STATE["counts"][key]


def _reset(key: str) -> None:
    with _LOCK:
        _STATE["counts"][key] = 0


def reset() -> None:
    """清空计数。测试之间互不影响，靠这个。"""
    with _LOCK:
        _STATE["counts"].clear()
        _STATE["total"] = 0
        _STATE["by_scenario"].clear()


def _estimate_tokens(messages: list) -> int:
    chars = 0
    for m in messages:
        if isinstance(m, dict):
            chars += len(str(m.get("content") or ""))
    return max(1, chars // 2)


def _payload(model: str, content: str, *, finish: str = "stop",
             tin: int = 0, tout: int = 0, reasoning: int | None = None) -> dict:
    """OpenAI chat.completion 结构。字段名必须与 app/llm_standalone._parse 对齐。"""
    usage: dict = {
        "prompt_tokens": tin,
        "completion_tokens": tout,
        "total_tokens": tin + tout,
    }
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish,
        }],
        "usage": usage,
    }


def _error_body(message: str, *, kind: str = "invalid_request_error",
                code: str | None = None) -> dict:
    """OpenAI 风格错误体。llm_standalone._read_error_body 读 error.message。"""
    return {"error": {"message": message, "type": kind, "code": code}}


def choose_scenario(model: str, header_value: str | None) -> str:
    """优先级：请求头 > model 名里的 mock-<场景> > 启动参数。"""
    if header_value and header_value in SCENARIOS:
        return header_value
    if model.startswith("mock-"):
        cand = model[len("mock-"):]
        if cand in SCENARIOS:
            return cand
    return str(_CFG["scenario"])


# ────────────────────────────── HTTP ──────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/1.0"
    protocol_version = "HTTP/1.1"

    # 默认日志会往 stderr 打一条 access log，这里换成人能读的摘要
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if not _CFG["quiet"]:
            sys.stderr.write(f"  {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass       # 客户端超时先断了，不是服务端的问题

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # ── GET：探活 ──
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in _MODEL_PATHS:
            self._json(200, {
                "object": "list",
                "data": [{"id": "mock-ok", "object": "model", "owned_by": "mockllm"}],
            })
            return
        if path in ("/", "/index.html"):
            body = _usage_text().encode("utf-8")
            self._send(200, body, "text/plain; charset=utf-8")
            return
        self._json(404, _error_body(f"没有这个路径：{path}", code="not_found"))

    # ── POST：chat/completions ──
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in _CHAT_PATHS:
            self._json(404, _error_body(f"没有这个路径：{path}", code="not_found"))
            return

        raw = self._read_body()
        if raw is None:
            return

        try:
            req = json.loads(raw.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("顶层不是对象")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            # 请求体本身坏了。真接口这里也返回 400，且不该被重试。
            self._json(400, _error_body(f"请求体不是合法 JSON：{e}"))
            return

        messages = req.get("messages")
        model = str(req.get("model") or "unknown")
        scenario = choose_scenario(model, self.headers.get("X-Mock-Scenario"))

        with _LOCK:
            _STATE["total"] += 1
            _STATE["by_scenario"][scenario] = _STATE["by_scenario"].get(scenario, 0) + 1
            seq = _STATE["total"]

        if _CFG["record"]:
            _record(seq, scenario, req)

        if not _CFG["quiet"]:
            n_msg = len(messages) if isinstance(messages, list) else 0
            sys.stderr.write(
                f"  [{seq:>3}] {scenario:<11} model={model[:22]:<22} "
                f"msgs={n_msg} max_tokens={req.get('max_tokens')}\n")

        # 参数校验放在场景分发之前：桩比真接口宽松，B 的 payload 拼错了也发现不了。
        problem = _validate(req, messages)
        if problem:
            self._json(400, _error_body(problem))
            return

        self._dispatch(scenario, model, req, messages)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, _error_body("Content-Length 不是整数"))
            return None
        if length <= 0:
            self._json(400, _error_body("请求体为空"))
            return None
        try:
            return self.rfile.read(length)
        except (ConnectionResetError, TimeoutError):
            return None

    def _dispatch(self, scenario: str, model: str, req: dict, messages: list) -> None:
        tin = _estimate_tokens(messages)
        content = str(_CFG["content"])
        tout = max(1, len(content) // 2)

        if scenario == "ok":
            self._json(200, _payload(model, content, tin=tin, tout=tout))

        elif scenario == "malformed":
            # 200 + 截断的 JSON。客户端 json.loads 会抛，但不是 HTTP 错误。
            broken = '{"id":"chatcmpl-mock","object":"chat.completion","choices":[{"index":0,'
            self._send(200, broken.encode("utf-8"))

        elif scenario == "notjson":
            self._send(200, b"OK", "text/plain; charset=utf-8")

        elif scenario in ("flaky", "ratelimit"):
            code = 500 if scenario == "flaky" else 429
            n = _bump(scenario)
            if n <= int(_CFG["flaky_fail"]):
                self._json(code, _error_body(
                    f"模拟第 {n} 次失败（共 {_CFG['flaky_fail']} 次后恢复）",
                    kind="server_error" if code == 500 else "rate_limit_error",
                    code="mock_flaky"))
                return
            _reset(scenario)          # 恢复后清零，下次调用重新走「失败 N 次」
            self._json(200, _payload(model, content, tin=tin, tout=tout))

        elif scenario == "servererror":
            self._json(500, _error_body("模拟持续 500（重试也不会好）",
                                        kind="server_error", code="mock_500"))

        elif scenario == "badkey":
            self._json(401, _error_body(
                "模拟 401：key 无效（这个错误重试一万次也没用）",
                kind="invalid_request_error", code="invalid_api_key"))

        elif scenario == "truncated":
            # 复刻 C 侧昨晚的真实故障：推理烧光预算，content 为空。
            self._json(200, _payload(
                model, "", finish="length", tin=tin, tout=4000, reasoning=3700))

        elif scenario == "slow":
            time.sleep(float(_CFG["slow_s"]))
            self._json(200, _payload(model, content, tin=tin, tout=tout))

        else:                          # 不该发生，兜底别静默
            self._json(500, _error_body(f"未知场景 {scenario}", code="mock_unknown"))


def _validate(req: dict, messages) -> str | None:
    """返回问题描述；没问题返回 None。对齐真接口的 400 情形。"""
    if not isinstance(messages, list) or not messages:
        return "messages 必须是非空数组"
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or "role" not in m:
            return f"messages[{i}] 缺 role 字段"
    mt = req.get("max_tokens")
    if mt is not None:
        try:
            if int(mt) <= 0:
                return f"max_tokens 必须为正整数，收到 {mt}"
        except (TypeError, ValueError):
            return f"max_tokens 不是整数：{mt!r}"
    if not req.get("model"):
        return "model 不能为空"
    return None


def _record(seq: int, scenario: str, req: dict) -> None:
    """把请求原文落到 jsonl，供事后核对「我到底发出去什么」。"""
    path = Path(_CFG["record"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"seq": seq, "scenario": scenario, "request": req},
                               ensure_ascii=False) + "\n")
    except OSError as e:
        sys.stderr.write(f"  ⚠ 记录失败 {path}：{e}\n")


def _usage_text() -> str:
    return f"""MockLLM —— 本地假模型服务（B1）

  POST http://127.0.0.1:{_CFG.get('port', DEFAULT_PORT)}/v1/chat/completions
  GET  http://127.0.0.1:{_CFG.get('port', DEFAULT_PORT)}/v1/models

场景切换（优先级从高到低）：
  1. 请求头 X-Mock-Scenario: <场景>
  2. model 名写 mock-<场景>
  3. 启动参数 --scenario

可选场景：
  {chr(10).join('  ' + s for s in SCENARIOS)}

示例：
  curl -s -X POST http://127.0.0.1:{_CFG.get('port', DEFAULT_PORT)}/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{{"model":"mock-badkey","messages":[{{"role":"user","content":"hi"}}]}}' -i
"""


# ────────────────────────────── 启动 ──────────────────────────────
def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT, **cfg) -> ThreadingHTTPServer:
    """建服务但不启动。测试用 port=0 让系统分配，再读 server_address[1]。"""
    _CFG.update(cfg)
    _CFG["port"] = port
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.daemon_threads = True
    _CFG["port"] = srv.server_address[1]
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地假模型服务（B1，OpenAI 兼容）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只绑本机")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--scenario", default="ok", choices=SCENARIOS,
                    help="没有 model 前缀和请求头时用的默认场景")
    ap.add_argument("--content", default=DEFAULT_CONTENT, help="ok 场景返回的正文")
    ap.add_argument("--content-file", default=None,
                    help="从文件读正文（测出题流程时给一道真题）")
    ap.add_argument("--flaky-fail", type=int, default=DEFAULT_FLAKY_FAIL,
                    help="flaky/ratelimit 先失败几次再成功")
    ap.add_argument("--slow", type=float, default=DEFAULT_SLOW_S,
                    help="slow 场景睡几秒")
    ap.add_argument("--record", default=None, help="把每个请求落到这个 jsonl")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    content = a.content
    if a.content_file:
        p = Path(a.content_file)
        if not p.exists():
            print(f"✗ 找不到 {p}", file=sys.stderr)
            return 2
        content = p.read_text(encoding="utf-8")

    try:
        srv = make_server(a.host, a.port, scenario=a.scenario, content=content,
                          flaky_fail=a.flaky_fail, slow_s=a.slow, quiet=a.quiet,
                          record=a.record)
    except OSError as e:
        print(f"✗ 起不来（端口 {a.port} 可能被占）：{e}\n"
              f"  换一个：python -m app.mockllm --port 8123", file=sys.stderr)
        return 2

    port = srv.server_address[1]
    print(f"→ MockLLM 已启动 http://127.0.0.1:{port}/v1"
          f"　默认场景 {a.scenario}　可切 {len(SCENARIOS)} 种")
    print(f"  探活：curl http://127.0.0.1:{port}/v1/models")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        if _STATE["total"]:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(_STATE["by_scenario"].items()))
            print(f"\n共 {_STATE['total']} 次请求：{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
