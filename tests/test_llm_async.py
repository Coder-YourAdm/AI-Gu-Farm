"""llm 的契约测试：温度必须显式、重试边界、JSON 提取、日志不泄密。

用 httpx.MockTransport 打桩，不联网、不花钱。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Config
from app.llm import (
    JSONExtractError,
    LLMClient,
    ProtocolError,
    extract_json_object,
)

DEV = Path("config.dev.json")


def _cfg(**over) -> Config:
    raw = json.loads(DEV.read_text(encoding="utf-8"))
    # 占位符不是合法 URL，打桩测试需要一个真实形状的端点
    raw["base_url"] = "https://api.example.com/v1"
    raw["model"] = "test-model-pinned"
    raw.update(over)
    return Config.from_dict(raw, source_path="in-memory")


def _ok_body(content: str, usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-value")


# ---------------------------------------------------------------- JSON 提取

def test_plain_object():
    assert extract_json_object('{"a": 1}', max_chars=100) == {"a": 1}


def test_strip_one_fence():
    text = '```json\n{"value": "3/4"}\n```'
    assert extract_json_object(text, max_chars=100) == {"value": "3/4"}


def test_fence_without_language_tag():
    assert extract_json_object('```\n{"a": 1}\n```', max_chars=100) == {"a": 1}


def test_unclosed_fence_falls_back_to_scanning():
    text = '```json\n{"a": 1}'
    # 围栏没闭合就不当围栏，退化为「外围是文字、唯一对象」
    assert extract_json_object(text, max_chars=100) == {"a": 1}


def test_two_objects_inside_fence_is_ambiguous():
    text = '```json\n{"a":1}\n{"b":2}\n```'
    with pytest.raises(JSONExtractError) as e:
        extract_json_object(text, max_chars=100)
    assert e.value.code == "ambiguous"


def test_prose_around_single_object():
    text = '按你的要求，结果是 {"value": "5"} ，以上。'
    assert extract_json_object(text, max_chars=100) == {"value": "5"}


def test_braces_inside_strings_do_not_break_scanning():
    text = '说明：{"value": "7", "note": "含 { 和 } 的字符串"} 完毕'
    obj = extract_json_object(text, max_chars=200)
    assert obj["note"] == "含 { 和 } 的字符串"


def test_two_objects_is_ambiguous():
    text = '{"a":1}{"b":2}'
    with pytest.raises(JSONExtractError) as e:
        extract_json_object(text, max_chars=100)
    assert e.value.code == "ambiguous"


def test_duplicate_key_rejected():
    with pytest.raises(JSONExtractError) as e:
        extract_json_object('{"a":1,"a":2}', max_chars=100)
    assert e.value.code == "duplicate_key"


@pytest.mark.parametrize("lit", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_rejected(lit):
    with pytest.raises(JSONExtractError) as e:
        extract_json_object('{"value": %s}' % lit, max_chars=100)
    assert e.value.code in ("bad_constant", "no_object")


def test_top_level_array_rejected():
    with pytest.raises(JSONExtractError) as e:
        extract_json_object("[1,2,3]", max_chars=100)
    assert e.value.code == "not_object"


def test_too_long_rejected():
    with pytest.raises(JSONExtractError) as e:
        extract_json_object('{"a":"' + "x" * 50 + '"}', max_chars=20)
    assert e.value.code == "too_long"


def test_empty_rejected():
    with pytest.raises(JSONExtractError):
        extract_json_object("   ", max_chars=100)
    with pytest.raises(JSONExtractError):
        extract_json_object(None, max_chars=100)


# ---------------------------------------------------------------- 请求体

async def test_temperature_and_tokens_come_from_role(tmp_path):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-test-secret-value"
        return httpx.Response(200, json=_ok_body('{"value":"1"}'))

    cfg = _cfg(solver_temperature=0.7, setter_temperature=0.2,
               operator_temperature=0.9,
               max_tokens_solver=1200, max_tokens_setter=400,
               max_tokens_operator=500)
    client = LLMClient(cfg, run_id="t1", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    msgs = [{"role": "user", "content": "做题"}]

    await client.chat("solver", msgs)
    await client.chat("setter", msgs)
    await client.chat("operator", msgs)
    await client.aclose()

    assert seen[0]["temperature"] == 0.7 and seen[0]["max_tokens"] == 1200
    assert seen[1]["temperature"] == 0.2 and seen[1]["max_tokens"] == 400
    assert seen[2]["temperature"] == 0.9 and seen[2]["max_tokens"] == 500
    assert seen[0]["model"] == cfg.model
    assert seen[0]["stream"] is False


async def test_unknown_role_rejected(tmp_path):
    cfg = _cfg()
    client = LLMClient(cfg, run_id="t2", log_root=tmp_path, _transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=_ok_body("{}"))))
    with pytest.raises(ProtocolError):
        await client.chat("judge", [{"role": "user", "content": "x"}])
    await client.aclose()


# ---------------------------------------------------------------- 重试与失败

async def test_retryable_5xx_then_success(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=_ok_body('{"value":"2"}', {"prompt_tokens": 5,
                                                                 "completion_tokens": 2,
                                                                 "total_tokens": 7}))

    cfg = _cfg(network_retries=2)
    client = LLMClient(cfg, run_id="t3", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    res = await client.chat("solver", [{"role": "user", "content": "x"}], slot_id="g01_peer_T01_G00_S1")
    await client.aclose()

    assert res.status == "ok"
    assert res.attempts == 2
    assert res.parsed == {"value": "2"}
    assert res.usage["input_tokens"] == 5
    assert res.slot_id == "g01_peer_T01_G00_S1"


async def test_429_is_retryable(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_ok_body("{}"))

    client = LLMClient(_cfg(network_retries=2), run_id="t4", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    res = await client.chat("setter", [{"role": "user", "content": "x"}])
    await client.aclose()
    assert res.status == "ok" and res.attempts == 3


async def test_transport_error_after_retries_exhausted(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = LLMClient(_cfg(network_retries=2), run_id="t5", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    res = await client.chat("solver", [{"role": "user", "content": "x"}])
    await client.aclose()

    # 1 次首发 + 2 次重试，不静默吞掉
    assert calls["n"] == 3
    assert res.status == "transport_error"
    assert res.attempts == 3
    assert "ConnectError" in (res.error or "")


@pytest.mark.parametrize("status", [401, 403, 404, 422])
async def test_client_errors_are_not_retried(tmp_path, status):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="nope")

    client = LLMClient(_cfg(network_retries=2), run_id="t6", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    with pytest.raises(ProtocolError):
        await client.chat("solver", [{"role": "user", "content": "x"}])
    await client.aclose()
    assert calls["n"] == 1        # 鉴权/端点错误重试只是拖时间

    # 失败也要留下记录
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["status"] == "protocol_error"


async def test_missing_api_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = LLMClient(_cfg(), run_id="t7", log_root=tmp_path,
                       _transport=httpx.MockTransport(
                           lambda r: httpx.Response(200, json=_ok_body("{}"))))
    with pytest.raises(ProtocolError) as e:
        await client.chat("solver", [{"role": "user", "content": "x"}])
    assert "LLM_API_KEY" in str(e.value)
    await client.aclose()


async def test_bad_json_is_parse_error_not_retry(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_ok_body("抱歉，我算不出来。"))

    client = LLMClient(_cfg(network_retries=2), run_id="t8", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    res = await client.chat("solver", [{"role": "user", "content": "x"}])
    await client.aclose()

    assert calls["n"] == 1                 # 格式失败不重发
    assert res.status == "ok"
    assert res.parsed is None
    assert res.parse_error == "no_object"
    assert res.content_ok is False


# ---------------------------------------------------------------- 日志

async def test_call_index_and_raw_log(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body('{"value":"3/4"}'))

    cfg = _cfg()
    client = LLMClient(cfg, run_id="E_s42_test", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    res = await client.chat("solver", [{"role": "user", "content": "题面"}],
                            slot_id="g01_common_T01_G00_S1")
    await client.aclose()

    index_path = tmp_path / "calls.jsonl"
    line = json.loads(index_path.read_text(encoding="utf-8").strip().splitlines()[0])
    for key in ("call_id", "slot_id", "role", "attempt", "model", "base_url",
                "temperature", "max_tokens", "usage", "status",
                "started_at", "ended_at"):
        assert key in line, key
    assert line["temperature"] == 0.7
    assert line["slot_id"] == "g01_common_T01_G00_S1"

    raw_path = Path(line['raw_path'])
    assert raw_path.exists()
    raw_text = raw_path.read_text(encoding="utf-8")
    assert "sk-test-secret-value" not in raw_text        # 密钥绝不落盘
    assert "Authorization" not in raw_text
    assert json.loads(raw_text)["response_text"] == '{"value":"3/4"}'


async def test_call_ids_are_unique(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("{}"))

    client = LLMClient(_cfg(concurrency=4), run_id="t9", log_root=tmp_path,
                       backoff=(0, 0), _transport=httpx.MockTransport(handler))
    import asyncio as _asyncio
    results = await _asyncio.gather(*[
        client.chat("solver", [{"role": "user", "content": str(i)}]) for i in range(8)
    ])
    await client.aclose()
    ids = [r.call_id for r in results]
    assert len(set(ids)) == 8
