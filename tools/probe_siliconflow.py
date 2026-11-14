"""SiliconFlow 连通性 + 模型行为对比（第 0 步换源后重做）。

对比 DeepSeek-V4-Flash 与 V4.1-Flash：是否思考、延迟、token 拆分、是否返回合法 JSON。
不做实验，不发大量请求。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.siliconflow.cn/v1"
KEY = os.environ.get("LLM_API_KEY", "")
MODELS = ("deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V4.1-Flash")

SETTER = (
    '你是出题人，只输出一个 JSON 对象。只能输出 template 与 params 两个字段。\n'
    'gamma: params={n,rate,c}，n=1..9，rate=1..8，c 为 ±1..±5。\n'
    '输出示例：{"template":"gamma","params":{"n":4,"rate":2,"c":1}}'
)


def call(model: str, system: str, user: str, cap: int) -> dict:
    body = {"model": model, "max_tokens": cap, "temperature": 0.7, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"model": model, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"model": model, "error": f"{type(e).__name__}: {e}"}
    dt = (time.perf_counter() - t0) * 1000
    ch = data["choices"][0]
    msg = ch.get("message") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    u = data.get("usage") or {}
    ok_json = False
    try:
        json.loads(content)
        ok_json = True
    except Exception:
        pass
    return {"model": model, "served": data.get("model"), "ms": round(dt),
            "content": len(content), "reasoning": len(reasoning),
            "finish": ch.get("finish_reason"), "json": ok_json,
            "in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
            "reason_tok": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "text": content[:120]}


def main() -> int:
    if not KEY:
        sys.exit("LLM_API_KEY 未设置")
    for m in MODELS:
        r = call(m, SETTER, "请出第 1 道题。", 800)
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
