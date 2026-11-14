"""第 0 步：把「模型到底怎么回」钉死。

发现两个必须查清的现象：
  1. 请求 deepseek-chat 时服务端回 deepseek-flash（别名）；
  2. 显式请求 deepseek-flash 时，max_tokens=400/500 全部被 reasoning 吃掉，
     content 为空、finish_reason=length。

本脚本按 (model, max_tokens, role) 组合各打一次，打印：
  finish_reason / content 长度 / reasoning 长度 / 用量拆分。
只用于第 0 步定型，不产生实验数据。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("PROBE_BASE_URL", "https://api.deepseek.com/v1")
KEY = os.environ.get("LLM_API_KEY", "")

SETTER_SYS = (ROOT / "prompts" / "setter.md").read_text(encoding="utf-8").replace(
    "{set_prompt}", "优先 poly，用高次项和非对称区间提高区分度，系数尽量都不为零。"
)
SOLVER_SYS = (ROOT / "prompts" / "solver.md").read_text(encoding="utf-8").replace(
    "{solve_prompt}", "先判断题型，再计算，最后核对符号。"
).replace("{check_instruction}", "")

CASES = [
    ("deepseek-chat", 400, "setter"),
    ("deepseek-chat", 400, "setter"),
    ("deepseek-flash", 2000, "setter"),
    ("deepseek-flash", 2000, "solver"),
    ("deepseek-chat", 400, "solver"),
    ("deepseek-v4-pro", 2000, "setter"),
]


def messages_for(role: str) -> list[dict]:
    if role == "setter":
        return [{"role": "system", "content": SETTER_SYS},
                {"role": "user", "content": "请出第 1 道题。"}]
    return [{"role": "system", "content": SOLVER_SYS},
            {"role": "user", "content": "计算定积分 ∫₀¹ (x + 1) dx。\n答案格式：integer, fraction or finite decimal"}]


def main() -> int:
    for model, cap, role in CASES:
        body = {"model": model, "messages": messages_for(role),
                "temperature": 0.7, "max_tokens": cap, "stream": False}
        req = urllib.request.Request(
            BASE.rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            method="POST")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"{model:<16} cap={cap:<5} {role:<7} HTTP {e.code} {e.read().decode()[:120]}")
            continue
        dt = (time.perf_counter() - t0) * 1000
        ch = data["choices"][0]
        msg = ch.get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        u = data.get("usage") or {}
        served = data.get("model")
        ok_json = False
        if content.strip():
            try:
                json.loads(content)
                ok_json = True
            except Exception:
                pass
        print(f"{model:<16} cap={cap:<5} {role:<7} served={served:<16} {dt:6.0f} ms "
              f"finish={ch.get('finish_reason'):<8} content={len(content):>4} chars "
              f"reasoning={len(reasoning):>5} chars json={ok_json} "
              f"tok in/out/reason={u.get('prompt_tokens')}/{u.get('completion_tokens')}/"
              f"{(u.get('completion_tokens_details') or {}).get('reasoning_tokens')}")
        if content:
            print(f"    content: {content[:160]}")
    return 0


if __name__ == "__main__":
    if not KEY:
        sys.exit("LLM_API_KEY 未设置")
    raise SystemExit(main())
