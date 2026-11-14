"""`app/llm_standalone.py` 的离线测试：不打真接口，全用假响应。

为什么值得写：这个文件在赛场上唯一一次被真正调用时，错误必须一眼看懂。
401 重试三次、超时不重试、把 token 用量算错——这些在演示现场都来不及查。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm_standalone as llm  # noqa: E402


class FakeResp:
    """最小可用的 urlopen 返回值。"""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok_payload(text="42", tin=11, tout=3) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout},
    }


@pytest.fixture
def cfg(monkeypatch):
    """让 preflight 认一份可用的配置，密钥走环境变量。"""
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    conf = {
        "model": "test-model-id",
        "base_url": "https://api.example.com/v1",
        "api_key_env": "TEST_LLM_KEY",
        "timeout_s": 5,
        "network_retries": 2,
    }
    monkeypatch.setattr(llm, "_load_config", lambda: conf)
    return conf


@pytest.fixture
def capture(monkeypatch):
    """录下最后一次请求，方便断言 URL / 头 / 载荷。"""
    box: dict = {"calls": 0, "url": None, "payload": None, "headers": None}

    def fake_urlopen(req, timeout=None):
        box["calls"] += 1
        box["url"] = req.full_url
        box["payload"] = json.loads(req.data.decode("utf-8"))
        box["headers"] = dict(req.header_items())
        return FakeResp(ok_payload())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return box


def test_preflight_rejects_placeholders(monkeypatch):
    """仓库里的 config.json 现在就是占位符状态，报错必须说清改哪里。"""
    monkeypatch.setattr(llm, "_load_config", lambda: {
        "model": "REPLACE_WITH_PINNED_MODEL_ID",
        "base_url": "REPLACE_WITH_ENDPOINT",
        "api_key_env": "LLM_API_KEY",
    })
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(llm.LLMError) as ei:
        llm.preflight()
    msg = str(ei.value)
    assert "model" in msg and "base_url" in msg and "LLM_API_KEY" in msg


def test_preflight_reports_missing_key_only(cfg, monkeypatch):
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    with pytest.raises(llm.LLMError) as ei:
        llm.preflight()
    assert "TEST_LLM_KEY" in str(ei.value)
    assert "base_url" not in str(ei.value)      # 已填的不该再报


def test_request_shape(cfg, capture):
    text, tin, tout = llm.complete([{"role": "user", "content": "1+1=?"}])
    assert (text, tin, tout) == ("42", 11, 3)
    assert capture["url"] == "https://api.example.com/v1/chat/completions"
    p = capture["payload"]
    assert p["model"] == "test-model-id"
    assert p["messages"] == [{"role": "user", "content": "1+1=?"}]
    assert p["temperature"] == llm.DEFAULT_TEMPERATURE
    assert p["max_tokens"] == llm.DEFAULT_MAX_TOKENS
    assert capture["headers"].get("Authorization") == "Bearer sk-test"


def test_kwargs_override(cfg, capture):
    llm.complete([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=400)
    assert capture["payload"]["temperature"] == 0.0
    assert capture["payload"]["max_tokens"] == 400


def test_endpoint_tolerates_three_base_url_forms(cfg, capture):
    for base, want in [
        ("https://a/v1", "https://a/v1/chat/completions"),
        ("https://a/v1/", "https://a/v1/chat/completions"),
        ("https://a/v1/chat/completions", "https://a/v1/chat/completions"),
    ]:
        cfg["base_url"] = base
        llm.complete([{"role": "user", "content": "x"}])
        assert capture["url"] == want, base


def test_retries_on_5xx_then_succeeds(cfg, capture, monkeypatch):
    seq = [urllib.error.HTTPError("u", 502, "bad gateway", {}, io.BytesIO(b"{}")),
           urllib.error.HTTPError("u", 503, "unavailable", {}, io.BytesIO(b"{}"))]

    def flaky(req, timeout=None):
        if seq:
            raise seq.pop(0)
        return FakeResp(ok_payload())

    monkeypatch.setattr(llm.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    text, _, _ = llm.complete([{"role": "user", "content": "x"}])
    assert text == "42"


def test_gives_up_after_retries(cfg, monkeypatch):
    calls = {"n": 0}

    def always_timeout(req, timeout=None):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm.urllib.request, "urlopen", always_timeout)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(llm.LLMError) as ei:
        llm.complete([{"role": "user", "content": "x"}])
    assert calls["n"] == 3                       # 首发 + 重试 2 次
    assert "连不上" in str(ei.value) and "curl" in str(ei.value)


def test_401_does_not_retry(cfg, monkeypatch):
    """重试 401 只是把同一个错误问三遍，白烧配额。"""
    calls = {"n": 0}

    def unauthorized(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            "u", 401, "unauthorized", {},
            io.BytesIO(b'{"error":{"message":"invalid api key"}}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", unauthorized)
    with pytest.raises(llm.LLMError) as ei:
        llm.complete([{"role": "user", "content": "x"}])
    assert calls["n"] == 1
    assert "401" in str(ei.value) and "invalid api key" in str(ei.value)


def test_parse_segmented_content_and_missing_usage():
    data = {
        "choices": [{"message": {"content": [{"text": "1"}, {"text": "2"}]}}],
    }
    assert llm._parse(data) == ("12", 0, 0)


def test_parse_rejects_empty_choices():
    with pytest.raises(llm.LLMError) as ei:
        llm._parse({"error": "no choices"})
    assert "choices" in str(ei.value)


def test_signature_is_tuple_of_str_int_int(cfg, capture):
    """契约：调用方按三元组解包。返回 list 或 dict 都会让 demo.py 崩在解包上。"""
    out = llm.complete([{"role": "user", "content": "x"}])
    assert isinstance(out, tuple) and len(out) == 3
    assert isinstance(out[0], str) and isinstance(out[1], int) and isinstance(out[2], int)
