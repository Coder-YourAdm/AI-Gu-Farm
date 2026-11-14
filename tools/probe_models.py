"""确认 endpoint 实际服务的 model id。

背景：请求写 deepseek-chat，但响应体 model 字段回的是 deepseek-flash。
若换一个不存在的 model 也返回 200，说明中间有别名/代理，"固定 model id" 就不能只写在配置里。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PROBE_BASE_URL", "https://api.deepseek.com/v1")
KEY = os.environ.get("LLM_API_KEY", "")


def call(model: str) -> None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复 ok"}],
        "temperature": 0.7,
        "max_tokens": 8,
        "stream": False,
    }
    req = urllib.request.Request(
        BASE.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode())
        dt = (time.perf_counter() - t0) * 1000
        print(f"requested={model:<28} HTTP 200 {dt:6.0f} ms  served={data.get('model')!r} "
              f"fingerprint={data.get('system_fingerprint')!r}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        print(f"requested={model:<28} HTTP {e.code}  {detail}")
    except Exception as e:
        print(f"requested={model:<28} FAIL {type(e).__name__}: {e}")


if __name__ == "__main__":
    if not KEY:
        sys.exit("LLM_API_KEY 未设置")
    for m in ("deepseek-chat", "deepseek-flash", "definitely-not-a-real-model-xyz"):
        call(m)
