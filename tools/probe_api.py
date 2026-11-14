"""第 0 步连通性探针（纯标准库，不依赖 httpx）。

用途：在装完依赖、跑正式链路之前，先用 1 次真实调用确认四件事：
  1. 网络能到 api.deepseek.com
  2. key 有效（不是 401）
  3. model id 存在（不是 404）
  4. 出题 prompt 能否拿回一个可解析的 JSON 对象

不写任何文件、不落盘 key、不参与实验数据。成功后由 tools/probe_cloud.py 走真实 LLMClient 复测。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("PROBE_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.environ.get("PROBE_MODEL", "deepseek-chat")
KEY_ENV = "LLM_API_KEY"

ROOT = Path(__file__).resolve().parents[1]


def render_setter_system() -> str:
    text = (ROOT / "prompts" / "setter.md").read_text(encoding="utf-8")
    # 用种子 1 的 set_prompt 作策略偏好；str.replace 而不是 format（文案里有花括号）
    return text.replace("{set_prompt}", "优先 poly，用高次项和非对称区间提高区分度，系数尽量都不为零。")


def main() -> int:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"[FAIL] 环境变量 {KEY_ENV} 未设置")
        return 2

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": render_setter_system()},
            {"role": "user", "content": "请出第 1 道题。"},
        ],
        "temperature": 0.7,
        "max_tokens": 400,
        "stream": False,
    }
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # 网络/DNS/超时
        print(f"[FAIL] 连不上 {BASE_URL}: {type(e).__name__}: {e}")
        return 3
    dt = time.perf_counter() - t0

    print(f"HTTP {status}  {dt*1000:.0f} ms  model={MODEL}  base_url={BASE_URL}")
    if status != 200:
        print(f"[FAIL] 响应: {raw[:800]}")
        return 4

    data = json.loads(raw)
    text = data["choices"][0]["message"].get("content")
    usage = data.get("usage") or {}
    print(f"usage: {json.dumps(usage, ensure_ascii=False)}")
    print(f"content: {text!r}")

    try:
        obj = json.loads(text)
    except Exception as e:
        print(f"[WARN] 内容不是可解析 JSON: {e}")
        return 5
    print(f"[OK] JSON 可解析: {json.dumps(obj, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
