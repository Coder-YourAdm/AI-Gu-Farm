"""模型接口的兼容入口。

演示保留同步 complete(messages) -> (text, tokens_in, tokens_out)，使用 urllib。
正式演化导出 llm_async 的 LLMClient / CallResult，使用 httpx，并统一限流、
重试、审计和预算。依赖见 requirements.txt。两种调用方式都只从环境变量读取密钥。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The evolution engine uses structured async results; keep the synchronous
# demo interface for existing callers instead of replacing either contract.
from .llm_async import (
    CallResult, LLMClient, ProtocolError, JSONExtractError,
    extract_json_object, build_solver_messages,
)

ROOT = Path(__file__).resolve().parent.parent

# 占位符前缀。配置文件里凡是 REPLACE_WITH_ 开头的都还没填。
PLACEHOLDER = "REPLACE_WITH_"

DEFAULT_TIMEOUT_S = 60
DEFAULT_RETRIES = 2
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 1200

# 这些状态码重试有用（限流 / 网关抖动）；4xx 里除 429 之外重试只是浪费配额。
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """模型调用失败。消息要能直接告诉人该怎么办，不要只抛一句 HTTP 500。"""


def _load_config() -> dict[str, Any]:
    """读取配置。走统一的解析顺序（显式路径 / LLM_CONFIG / config.local.json / config.json），
    不再写死 config.json——那会让配好的 local 配置被占位模板顶掉。"""
    from .config import ConfigError, resolve_config_path

    try:
        path = resolve_config_path()
    except ConfigError as exc:
        raise LLMError(str(exc)) from exc
    return json.loads(path.read_text(encoding="utf-8"))


def preflight() -> dict[str, Any]:
    """开跑前检查一次配置。失败就带着可执行的修法抛出来。

    为什么单独一个函数：`complete()` 在并发里被调用 10 次，配置错了会得到 10 条
    一样的报错。把检查提到最前面，只出一条，且能一眼看出是缺 key 还是没填 model。
    """
    cfg = _load_config()
    model = str(cfg.get("model", ""))
    base_url = str(cfg.get("base_url", ""))
    key_env = str(cfg.get("api_key_env") or "LLM_API_KEY")
    key = os.environ.get(key_env, "")

    problems: list[str] = []
    if not model or model.startswith(PLACEHOLDER):
        problems.append("配置里的 model 还是占位符 —— 填成实际要跑的模型 id")
    if not base_url or base_url.startswith(PLACEHOLDER):
        problems.append("配置里的 base_url 还是占位符 —— 填成 .../v1 这样的根地址")
    if not key:
        problems.append(f"环境变量 {key_env} 为空 —— 先 export {key_env}=<你的 key>")

    if problems:
        raise LLMError(
            "模型客户端未配置：\n  - " + "\n  - ".join(problems)
            + "\n（密钥只从环境变量读，不要写进配置文件——config.json 会被提交）")

    return {
        "model": model,
        "base_url": base_url,
        "api_key_env": key_env,
        "timeout_s": float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S),
        "retries": int(cfg.get("network_retries", DEFAULT_RETRIES)),
    }


def _endpoint(base_url: str) -> str:
    """把 base_url 补成 chat/completions 的完整地址。

    容忍三种写法：`https://x/v1`、`https://x/v1/`、`https://x/v1/chat/completions`。
    赛场上没人愿意为了少一个斜杠去改配置。
    """
    u = base_url.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    return u + "/chat/completions"


def _post(url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def complete(
    messages: list[dict],
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, int, int]:
    """调用模型，返回 `(文本, 输入 tokens, 输出 tokens)`。

    签名是契约（规格 §2.4 / A_to_B_接口契约），**不要改**：
    `app/demo.py` 与 B 的 `orchestrator.py` 都按这个元组解包。

    失败策略：网络类错误与 5xx/429 重试 `config.network_retries` 次，
    其余 4xx 立刻抛——重试一个 401 只是把同一个错误问 3 遍。
    """
    cfg = preflight()
    url = _endpoint(cfg["base_url"])
    key = os.environ[cfg["api_key_env"]]  # preflight 已确认非空

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    attempt = 0
    while True:
        try:
            data = _post(url, payload, key, cfg["timeout_s"])
            break
        except urllib.error.HTTPError as e:
            detail = _read_error_body(e)
            if e.code in RETRYABLE_STATUS and attempt < cfg["retries"]:
                attempt += 1
                time.sleep(0.6 * attempt)
                continue
            raise LLMError(f"模型接口返回 {e.code}：{detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < cfg["retries"]:
                attempt += 1
                time.sleep(0.6 * attempt)
                continue
            raise LLMError(
                f"连不上 {url}（已重试 {attempt} 次）：{e}\n"
                "  校内网常见于代理或 DNS，先 curl 一下这个地址确认。") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"响应不是合法 JSON：{e}") from e

    return _parse(data)


def _read_error_body(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "<无法读取响应体>"
    try:                                    # OpenAI 风格：{"error":{"message":...}}
        obj = json.loads(raw)
        err = obj.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
    except json.JSONDecodeError:
        pass
    return raw.strip()[:300] or "<空响应体>"


def _parse(data: dict[str, Any]) -> tuple[str, int, int]:
    """取出内容与 token 用量。

    兼容两种 message 形态：`content` 是字符串（常规），或是分段列表
    （部分网关的多模态格式）。token 字段缺失就记 0 —— 统计不该让判分崩。
    """
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError) as e:
        raise LLMError(f"响应里没有 choices：{json.dumps(data, ensure_ascii=False)[:300]}") from e

    content = (choice.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = "" if content is None else str(content)

    usage = data.get("usage") or {}
    try:
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        tin = tout = 0
    return text, tin, tout


if __name__ == "__main__":  # 手测：python -m app.llm
    info = preflight()
    print("配置就绪：", info["model"], "@", info["base_url"])
    txt, tin, tout = complete([{"role": "user", "content": "只回一个数字：1+1=?"}])
    print(f"回复：{txt!r}  tokens {tin}/{tout}")
