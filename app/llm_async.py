"""模型调用层：限流、角色预算、原始记录、网络重试、JSON 提取。

对应分工方案 v7.3 B2 第 2–7 条。三个不可退让的点：

1. **温度按角色显式传参**。不传就会吃 provider 默认值（常见 1.0），
   实验既不可复现，解题与出题的温度也会不一致。
2. **重试只对网络类错误**。4xx（401/403/404）立即抛 `ProtocolError`，
   由 orchestrator 中止本趟；重试它只是在掩盖配置错误。
3. **JSON 提取失败不重发请求**。格式错是 `unanswered`，占槽位、占分母，
   重发会把「模型答不出」洗成「多试几次就答出」。

不做输出缓存：F 组与留出评测都必须重新调用（技术规格 §4 末段）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
import uuid
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

from .config import Config
from .budget import RequestBudget, BudgetExceeded

# 可重试的 HTTP 状态：超时类、限流、明确的服务端故障。
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

VALID_ROLES = ("solver", "setter", "operator")

_ROLE_TEMP_ATTR = {
    "solver": "solver_temperature",
    "setter": "setter_temperature",
    "operator": "operator_temperature",
}
_ROLE_TOKENS_ATTR = {
    "solver": "max_tokens_solver",
    "setter": "max_tokens_setter",
    "operator": "max_tokens_operator",
}


class ProtocolError(RuntimeError):
    """不可重试的调用失败：鉴权、端点不存在、请求被拒。必须中止本趟。"""


class TransportFailure(ProtocolError):
    """Infrastructure failure, never eligible for content fallback."""


class JSONExtractError(ValueError):
    """模型输出不是「唯一一个完整 JSON 对象」。计为 unanswered，不重发。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------
# JSON 提取
# --------------------------------------------------------------------------

class _DuplicateKey(Exception):
    pass


class _BadConstant(Exception):
    pass


def _no_dup_pairs(pairs: list[tuple[str, Any]]) -> dict:
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise _DuplicateKey(k)
        seen.add(k)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    # json 默认接受 NaN / Infinity / -Infinity，它们不是合法 JSON
    raise _BadConstant(name)


def _strict_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_dup_pairs,
                      parse_constant=_reject_constant)


def _strip_one_fence(text: str) -> str:
    """去掉一层完整代码围栏。只去一层，不做正则清洗。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    head, sep, rest = t.partition("\n")
    if not sep:
        return t
    if not rest.rstrip().endswith("```"):
        return t
    inner = rest.rstrip()[:-3]
    return inner.strip()


def _balanced_regions(text: str) -> list[str]:
    """扫描出所有顶层花括号区域，字符串与转义由状态机处理。

    不用「数大括号」硬切：字符串里的 { } 会把计数带偏。
    """
    regions: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    regions.append(text[start:i + 1])
                    start = -1
    return regions


def extract_json_object(text: str | None, *, max_chars: int) -> dict:
    """严格提取唯一一个完整 JSON 对象。失败抛 `JSONExtractError`。

    顺序：限长 → 去一层围栏 → 整体严格解码 → 否则找唯一完整对象。
    多候选、重复键、NaN/Infinity、非对象顶层，一律失败。
    """
    if text is None:
        raise JSONExtractError("empty", "响应没有文本内容")
    if not isinstance(text, str):
        raise JSONExtractError("not_text", f"内容类型 {type(text).__name__}")
    if len(text) > max_chars:
        raise JSONExtractError("too_long", f"{len(text)} > {max_chars}")

    body = _strip_one_fence(text)
    if not body:
        raise JSONExtractError("empty", "去围栏后为空")

    # 快路径：整体就是一个对象
    try:
        whole = _strict_loads(body)
    except _DuplicateKey as e:
        raise JSONExtractError("duplicate_key", str(e)) from e
    except _BadConstant as e:
        raise JSONExtractError("bad_constant", str(e)) from e
    except json.JSONDecodeError:
        whole = None
    else:
        if type(whole) is not dict:
            raise JSONExtractError("not_object", f"顶层是 {type(whole).__name__}")
        return whole

    # 慢路径：外围有文字时，接受唯一一个能解析的完整对象
    candidates = []
    for region in _balanced_regions(body):
        try:
            obj = _strict_loads(region)
        except (_DuplicateKey, _BadConstant, json.JSONDecodeError):
            continue
        if type(obj) is dict:
            candidates.append(obj)

    if not candidates:
        raise JSONExtractError("no_object", "找不到可解析的 JSON 对象")
    if len(candidates) > 1:
        raise JSONExtractError("ambiguous", f"找到 {len(candidates)} 个候选对象")
    return candidates[0]


# --------------------------------------------------------------------------
# 调用结果
# --------------------------------------------------------------------------

@dataclass
class CallResult:
    call_id: str
    role: str
    model: str
    base_url: str
    temperature: float
    max_tokens: int
    status: str                      # ok | transport_error | protocol_error
    text: str | None = None
    parsed: dict | None = None
    parse_error: str | None = None   # too_long/no_object/ambiguous/... 计为 unanswered
    usage: dict = field(default_factory=dict)
    attempt: int = 1                 # 本次返回来自第几次尝试
    attempts: int = 1                # 一共发了几次请求
    latency_ms: int = 0
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    slot_id: str | None = None
    http_response_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_transport_error(self) -> bool:
        return self.status == "transport_error"

    @property
    def content_ok(self) -> bool:
        """HTTP 通了且 JSON 提取成功。用作「模型是否给出可用对象」。"""
        return self.status == "ok" and self.parse_error is None


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_usage(raw: Any) -> dict:
    """统一 usage；缺失就是 unknown，不当作 0 也不当作免费。"""
    if not isinstance(raw, Mapping):
        return {"input_tokens": None, "output_tokens": None,
                "total_tokens": None, "reasoning_tokens": None}
    details = raw.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens") if isinstance(details, Mapping) else None
    return {
        "input_tokens": raw.get("prompt_tokens"),
        "output_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "reasoning_tokens": reasoning,
    }


def _content_of(data: Mapping) -> str | None:
    """从 OpenAI 兼容响应里取文本。数组形式的 content 拼成一段。"""
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError):
        return None
    content = msg.get("content") if isinstance(msg, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts) if parts else None
    return None


class LLMClient:
    """一个全局限流的模型客户端。所有角色共用一个 Semaphore。"""

    def __init__(
        self,
        cfg: Config,
        *,
        run_id: str,
        log_root: str | Path = "logs",
        token_param: str = "max_tokens",
        backoff: Sequence[float] = (1.0, 2.0),
        _transport: httpx.AsyncBaseTransport | None = None,
        budget: RequestBudget | None = None,
    ):
        if cfg.condition not in ("E", "F", "A"):
            raise ProtocolError(f"未知 condition: {cfg.condition}")
        self.cfg = cfg
        self.run_id = run_id
        self.log_root = Path(log_root)
        self.token_param = token_param
        self.backoff = tuple(backoff)
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._seq = itertools.count(1)
        self._session_id = uuid.uuid4().hex[:12]
        self.budget = budget or RequestBudget(cfg.wall_limit_s, cfg.max_calls)
        self.records = []
        self._transport = _transport
        self._client: httpx.AsyncClient | None = None
        self._write_lock = asyncio.Lock()

        self.raw_dir = self.log_root / "raw" / run_id
        self.calls_path = self.log_root / "calls.jsonl"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 基础设施 ----------------

    def _api_key(self) -> str:
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key.strip():
            raise ProtocolError(
                f"环境变量 {self.cfg.api_key_env} 未设置或为空；"
                "鉴权只从环境变量读，不写进配置文件与日志"
            )
        return key

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.timeout_s),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---------------- 日志 ----------------

    async def _write_call(self, res: CallResult, request: dict) -> None:
        """原始响应落盘 + calls.jsonl 追加索引行。密钥不进任何一份。"""
        async with self._write_lock:
            raw = {
                "run_id": self.run_id,
                "call_id": res.call_id,
                "slot_id": res.slot_id,
                "role": res.role,
                "attempt": res.attempt,
                "model": res.model,
                "base_url": res.base_url,
                "temperature": res.temperature,
                "max_tokens": res.max_tokens,
                "request": request,          # 已剔除 Authorization
                "response_text": res.text,
                "http_response_text": res.http_response_text,
                "usage": res.usage,
                "status": res.status,
                "error": res.error,
                "parse_error": res.parse_error,
                "latency_ms": res.latency_ms,
                "started_at": res.started_at,
                "ended_at": res.ended_at,
            }
            path = self.raw_dir / f"{res.call_id}_http{res.attempts}.json"
            tmp = path.with_suffix(".json.tmp")
            encoded = json.dumps(raw, ensure_ascii=False, indent=2)
            secret = os.environ.get(self.cfg.api_key_env, '')
            if secret:
                encoded = encoded.replace(secret, '[REDACTED]')
            tmp.write_text(encoded, encoding="utf-8")
            os.replace(tmp, path)

            index_line = {
                "run_id": self.run_id,
                "raw_path": str(path),
                "call_id": res.call_id,
                "slot_id": res.slot_id,
                "role": res.role,
                "attempt": res.attempt,
                "attempts": res.attempts,
                "model": res.model,
                "base_url": res.base_url,
                "temperature": res.temperature,
                "max_tokens": res.max_tokens,
                "usage": res.usage,
                "status": res.status,
                "parse_error": res.parse_error,
                "started_at": res.started_at,
                "ended_at": res.ended_at,
            }
            with self.calls_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(index_line, ensure_ascii=False) + "\n")
            if res.status != 'retrying':
                self.records.append(copy.deepcopy(res))

    # ---------------- 主入口 ----------------

    async def chat(
        self,
        role: str,
        messages: Sequence[Mapping[str, str]],
        *,
        slot_id: str | None = None,
        attempt: int = 1,
    ) -> CallResult:
        """发一次对话请求（内部含网络重试），返回 CallResult。

        网络类错误重试 `network_retries` 次后返回 status=transport_error；
        4xx / 鉴权问题抛 `ProtocolError`；内容格式问题只写 parse_error。
        """
        if role not in VALID_ROLES:
            raise ProtocolError(f"未知角色 {role!r}，允许 {VALID_ROLES}")
        if attempt < 1:
            raise ProtocolError(f"attempt 从 1 开始，当前 {attempt}")

        temperature = float(getattr(self.cfg, _ROLE_TEMP_ATTR[role]))
        max_tokens = int(getattr(self.cfg, _ROLE_TOKENS_ATTR[role]))

        seq = next(self._seq)
        tag = slot_id or role
        call_id = f"{tag}_a{attempt}_{self._session_id}_{seq:06d}"

        body = {
            "model": self.cfg.model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,          # 必须显式，按角色取
            self.token_param: max_tokens,
            "stream": False,
        }
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

        res = CallResult(
            call_id=call_id,
            role=role,
            model=self.cfg.model,
            base_url=self.cfg.base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            status="ok",
            slot_id=slot_id,
            attempt=attempt,
        )

        total_attempts = self.cfg.network_retries + 1
        last_error: str | None = None

        for i in range(1, total_attempts + 1):
            res.attempts = i
            res.text = res.http_response_text = None
            res.usage = {}
            res.started_at = _now_iso()
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(self._sem.acquire(), self.budget.remaining())
            except TimeoutError as exc:
                raise BudgetExceeded('wall_limit_s exhausted waiting for concurrency') from exc
            try:
                self.budget.reserve()
                resp = await asyncio.wait_for(
                    self._get_client().post(url, json=body, headers=headers),
                    self.budget.remaining())
            except TimeoutError as exc:
                res.status, res.error = 'transport_error', 'budget_limit; usage unknown'
                await self._write_call(res, body)
                raise BudgetExceeded(res.error) from exc
            except asyncio.CancelledError:
                res.status, res.error = 'transport_error', 'cancelled; usage unknown'
                res.ended_at = _now_iso()
                res.latency_ms = int((time.perf_counter() - t0) * 1000)
                await self._write_call(res, body)
                raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                res.http_response_text = resp.text
                if resp.status_code >= 400:
                    if resp.status_code in RETRYABLE_STATUS:
                        last_error = f"HTTP {resp.status_code}"
                    else:
                        # 不可重试：鉴权、端点、参数问题，重试只会拖时间
                        res.status = "protocol_error"
                        res.error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                        res.ended_at = _now_iso()
                        res.latency_ms = int((time.perf_counter() - t0) * 1000)
                        await self._write_call(res, body)
                        error = ProtocolError(
                            f"{call_id} 不可重试的 HTTP {resp.status_code}: {resp.text[:300]}"
                        )
                        error.result = res
                        raise error
                else:
                    try:
                        data = resp.json()
                    except ValueError as e:
                        last_error = f"响应不是 JSON: {e}"
                    else:
                        res.latency_ms = int((time.perf_counter() - t0) * 1000)
                        res.ended_at = _now_iso()
                        if not isinstance(data, Mapping):
                            res.status, res.error = 'protocol_error', 'HTTP response must be a JSON object'
                            await self._write_call(res, body)
                            error = ProtocolError(res.error)
                            error.result = res
                            raise error
                        res.usage = _normalize_usage(data.get("usage"))
                        res.text = _content_of(data)
                        res.status = "ok"
                        res.error = None
                        try:
                            res.parsed = extract_json_object(
                                res.text, max_chars=self.cfg.max_response_chars
                            )
                            res.parse_error = None
                        except JSONExtractError as e:
                            # 格式失败不重发请求，记 unanswered 的依据
                            res.parsed = None
                            res.parse_error = e.code
                        await self._write_call(res, body)
                        return res
            finally:
                self._sem.release()

            # 到这里说明这一次可重试地失败了
            res.ended_at = _now_iso()
            res.latency_ms = int((time.perf_counter() - t0) * 1000)
            if i < total_attempts:
                res.status, res.error = 'retrying', last_error
                await self._write_call(res, body)
                try:
                    await asyncio.wait_for(asyncio.sleep(
                        self.backoff[min(i - 1, len(self.backoff) - 1)]), self.budget.remaining())
                except TimeoutError as exc:
                    raise BudgetExceeded('wall_limit_s exhausted during backoff') from exc

        res.status = "transport_error"
        res.error = last_error
        await self._write_call(res, body)
        return res


# --------------------------------------------------------------------------
# 辅助：初始消息拼装（B3 会用它接 solve_prompt / check_instruction）
# --------------------------------------------------------------------------

CHECK_INSTRUCTION = "在内部检查符号、数量级和算术，再给最终答案。"


def build_solver_messages(system_prompt: str, statement: str,
                          answer_format: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"{statement}\n答案格式：{answer_format}"},
    ]
