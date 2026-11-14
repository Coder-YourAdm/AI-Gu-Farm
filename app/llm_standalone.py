"""模型客户端：`complete(messages) -> (text, tokens_in, tokens_out)`。

**这个文件的归属**

分工方案 v7.3 把 `app/llm.py` 划给 B（`complete()` 是代循环的组成部分）。
但 B 到 21:40 为止一行未交，而 C 侧 `--backend real` 只差这一个函数。

所以这里放一份**按冻结签名写好的标准实现**，目的只有一个：让「缺函数」不再阻塞
联调。B 交文件时整份替换即可，调用方（`app/demo.py`）不需要改一行——签名就是契约。

**依赖只用标准库**（`urllib`）。不引 requests / openai SDK：
赛场上少一个依赖就少一个装不上的可能，而这里是纯粹的 HTTP POST。

**配置**：`config.json` 的 `model` / `base_url`，密钥读环境变量（默认 `LLM_API_KEY`）。
密钥不进仓库，`config.json` 里只存变量名 `api_key_env`。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

# 占位符前缀。config.json 里凡是 REPLACE_WITH_ 开头的都还没填。
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
    if not CONFIG_PATH.exists():
        raise LLMError(f"找不到 {CONFIG_PATH}，无法确定 model / base_url")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


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
        problems.append("config.json 的 model 还是占位符 —— 填成实际要跑的模型 id")
    if not base_url or base_url.startswith(PLACEHOLDER):
        problems.append("config.json 的 base_url 还是占位符 —— 填成 .../v1 这样的根地址")
    if not key:
        problems.append(f"环境变量 {key_env} 为空 —— 先 export {key_env}=<你的 key>")

    if problems:
        raise LLMError(
            "模型客户端未配置：\n  - " + "\n  - ".join(problems)
            + "\n（密钥只从环境变量读，不要写进 config.json——它会被提交）")

    return {
        "model": model,
        "base_url": base_url,
        "api_key_env": key_env,
        "timeout_s": float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S),
        "retries": int(cfg.get("network_retries", DEFAULT_RETRIES)),
    }


def budget(role: str, default: int) -> int:
    """读 `config.json` 的 `max_tokens_<role>`。

    为什么要有这个函数：原先 `max_tokens_solver: 1200` 写在配置里，**却没有任何
    代码读它** —— `demo.py` 直接用模块常量 1200。于是改配置毫无效果，人会以为
    自己调过了。配置项不被读取比配置项不存在更危险，因为它长得像已经生效。
    """
    try:
        value = _load_config().get(f"max_tokens_{role}")
    except LLMError:
        return default
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


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

    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = "" if content is None else str(content)

    usage = data.get("usage") or {}

    # 被 max_tokens 截断且一个字答案都没落地 —— 必须报错，不能返回空串。
    # 原因：deepseek-flash 是推理模型，先烧 reasoning_tokens 再吐 content。
    # 实测 5 阶行列式一道题要 3700+ 推理 token，而预算是 1200 时全部被推理吃光，
    # content 为空。空串往下传会被判成 "unanswered"，病历上写的是"模型不会做"，
    # 真病因却是预算不够 —— 这种误诊会让人去换模型，白折腾一轮。
    finish = choice.get("finish_reason")
    if finish == "length" and not text.strip():
        spent = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        raise LLMError(
            f"输出被 max_tokens 截断：推理已用掉 {spent or '?'} token，答案没来得及输出。\n"
            "  config.json 的 max_tokens_solver 要按「推理 + 答案」之和给，\n"
            "  5 阶行列式这类题建议 ≥4000。")
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
