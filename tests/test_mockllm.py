# -*- coding: utf-8 -*-
"""B1 本地假模型服务的验收测试。

分两层，缺一层都不够：

1. **协议层**：直接打 HTTP，确认它确实能按场景返回 200/401/500/坏 JSON。
   这层证明「靶子本身是对的」。

2. **契约层**：用真的 `app.llm_standalone.complete()` 去打它。
   这层才是关键 —— mock 的字段名如果和真客户端读取的路径不一致，
   协议层测试全绿也照样联调不了。方法名对不上是最常见的假绿。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from app import llm_standalone as L
from app import mockllm as M


@pytest.fixture
def server(tmp_path):
    """起一个真端口。port=0 让系统分配，避免和 8000/8001 撞。"""
    M.reset()
    srv = M.make_server("127.0.0.1", 0, scenario="ok", content="42",
                        flaky_fail=2, slow_s=0.2, quiet=True, record=None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    yield base
    srv.shutdown()
    srv.server_close()


def _post(base: str, payload: dict, *, scenario: str | None = None,
          raw: bytes | None = None):
    """返回 (status, body_text)。scenario 走请求头，用来测优先级。"""
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if scenario:
        headers["X-Mock-Scenario"] = scenario
    req = urllib.request.Request(base + "/chat/completions", data=data,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _ok_payload(model: str = "deepseek-flash", content: str = "hi") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": 100, "temperature": 0}


# ────────────────────────── 一、协议层 ──────────────────────────
def test_models_endpoint_is_alive(server):
    with urllib.request.urlopen(server + "/models", timeout=5) as r:
        body = json.loads(r.read())
    assert body["object"] == "list"
    assert body["data"][0]["id"]


def test_ok_returns_openai_shaped_payload(server):
    code, body = _post(server, _ok_payload())
    assert code == 200
    d = json.loads(body)
    choice = d["choices"][0]
    assert choice["message"]["content"] == "42"
    assert choice["finish_reason"] == "stop"
    assert d["usage"]["prompt_tokens"] > 0
    assert d["usage"]["total_tokens"] == (d["usage"]["prompt_tokens"]
                                          + d["usage"]["completion_tokens"])


def test_scenario_via_model_name(server):
    """改 config.model 一个字段就能切场景，不用动 URL。"""
    code, body = _post(server, _ok_payload(model="mock-badkey"))
    assert code == 401
    assert "401" in json.loads(body)["error"]["message"] or "key" in body


def test_header_overrides_model_name(server):
    """请求头优先级高于 model 名——否则没法在同一个 model 下测多场景。"""
    code, _ = _post(server, _ok_payload(model="mock-badkey"), scenario="ok")
    assert code == 200


def test_unknown_model_falls_back_to_default(server):
    """B 用真模型名（deepseek-flash）打 mock 也该能用，默认走 ok。"""
    code, _ = _post(server, _ok_payload(model="deepseek-flash"))
    assert code == 200


def test_malformed_returns_200_but_broken_json(server):
    code, body = _post(server, _ok_payload(model="mock-malformed"))
    assert code == 200                  # HTTP 是成的
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)                # 但解析不了 —— 这才是要测的分支


def test_notjson_returns_plain_text(server):
    code, body = _post(server, _ok_payload(model="mock-notjson"))
    assert code == 200
    assert body.strip() == "OK"


def test_flaky_fails_twice_then_succeeds(server):
    """前 2 次 500，第 3 次成功，且计数清零（下次仍是失败 2 次）。"""
    assert _post(server, _ok_payload(model="mock-flaky"))[0] == 500
    assert _post(server, _ok_payload(model="mock-flaky"))[0] == 500
    assert _post(server, _ok_payload(model="mock-flaky"))[0] == 200
    # 清零验证：再来又是 500
    assert _post(server, _ok_payload(model="mock-flaky"))[0] == 500


def test_ratelimit_is_429_then_success(server):
    assert _post(server, _ok_payload(model="mock-ratelimit"))[0] == 429
    assert _post(server, _ok_payload(model="mock-ratelimit"))[0] == 429
    assert _post(server, _ok_payload(model="mock-ratelimit"))[0] == 200


def test_servererror_never_recovers(server):
    for _ in range(4):
        assert _post(server, _ok_payload(model="mock-servererror"))[0] == 500


def test_badkey_returns_401_with_openai_error_shape(server):
    code, body = _post(server, _ok_payload(model="mock-badkey"))
    assert code == 401
    assert isinstance(json.loads(body)["error"]["message"], str)


def test_truncated_has_length_finish_and_empty_content(server):
    """复刻 C 侧真实故障：推理烧光预算，content 是空串。"""
    code, body = _post(server, _ok_payload(model="mock-truncated"))
    assert code == 200
    d = json.loads(body)
    assert d["choices"][0]["finish_reason"] == "length"
    assert d["choices"][0]["message"]["content"] == ""
    assert d["usage"]["completion_tokens_details"]["reasoning_tokens"] > 0


def test_slow_actually_delays(server):
    import time
    t0 = time.monotonic()
    assert _post(server, _ok_payload(model="mock-slow"))[0] == 200
    assert time.monotonic() - t0 >= 0.2


# ────────────────────── 二、参数校验对齐真接口 ──────────────────────
def test_empty_messages_rejected(server):
    code, _ = _post(server, {"model": "x", "messages": [], "max_tokens": 10})
    assert code == 400


def test_message_without_role_rejected(server):
    code, _ = _post(server, {"model": "x", "messages": [{"content": "hi"}]})
    assert code == 400


def test_zero_max_tokens_rejected(server):
    code, _ = _post(server, {"model": "x",
                             "messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 0})
    assert code == 400


def test_missing_model_rejected(server):
    code, _ = _post(server, {"messages": [{"role": "user", "content": "hi"}],
                             "max_tokens": 10})
    assert code == 400


def test_broken_request_body_rejected(server):
    """桩如果收下坏请求，B 的 payload 拼错了也发现不了，到真跑才报错。"""
    code, _ = _post(server, {}, raw=b"{not json")
    assert code == 400


# ────────────── 三、契约层：用真客户端 complete() 打通 ──────────────
@pytest.fixture
def pointed_at_mock(server, tmp_path, monkeypatch):
    """把 llm_standalone 的配置指到 mock 上，走完整真链路。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "model": "deepseek-flash",          # 真名，靠请求头/model 名切场景
        "base_url": server,
        "api_key_env": "MOCK_KEY",
        "timeout_s": 10,
        "network_retries": 2,
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(L, "CONFIG_PATH", cfg)
    monkeypatch.setenv("MOCK_KEY", "sk-not-a-real-key")
    return server


def test_complete_works_against_mock(pointed_at_mock):
    """mock 的字段名必须和 _parse 的读取路径一致，否则这里就红。"""
    text, tin, tout = L.complete([{"role": "user", "content": "一加一"}], max_tokens=100)
    assert text == "42"
    assert tin > 0 and tout > 0


def test_complete_retries_flaky_and_succeeds(pointed_at_mock):
    """前 2 次 500、第 3 次成功 —— 正好等于 complete() 的 2 次重试。
    这条不绿，说明重试逻辑是死代码。"""
    text, _, _ = L.complete([{"role": "user", "content": "hi"}], max_tokens=100)
    assert text == "42"


def test_complete_raises_on_persistent_500(pointed_at_mock):
    M._CFG["scenario"] = "servererror"
    try:
        with pytest.raises(L.LLMError) as ei:
            L.complete([{"role": "user", "content": "hi"}], max_tokens=100)
        assert "500" in str(ei.value)
    finally:
        M._CFG["scenario"] = "ok"


def test_complete_does_not_retry_401(pointed_at_mock):
    """4xx 重试只是把同一个错误问 3 遍。badkey 必须在第一次就抛。"""
    import time
    t0 = time.monotonic()
    with pytest.raises(L.LLMError) as ei:
        _call_with_scenario("badkey")
    assert "401" in str(ei.value)
    assert time.monotonic() - t0 < 1.0        # 没有 0.6+1.2 秒的退避


def _call_with_scenario(scenario: str):
    """临时把默认场景切过去，避免每个用例都改 config。"""
    old = M._CFG["scenario"]
    M._CFG["scenario"] = scenario
    try:
        return L.complete([{"role": "user", "content": "hi"}], max_tokens=100)
    finally:
        M._CFG["scenario"] = old


def test_complete_reports_truncation_not_empty_answer(pointed_at_mock):
    """最要紧的一条：截断不能被当成「模型不会做」。

    _parse 看到 finish_reason=length 且 content 为空，必须抛 LLMError
    并说明「推理已用掉 N token」。如果它静默返回空串，下游会把
    infra 故障记成能力缺陷 —— 这正是昨晚 20/20→19/20 被误判的那次。
    """
    with pytest.raises(L.LLMError) as ei:
        _call_with_scenario("truncated")
    msg = str(ei.value)
    assert "截断" in msg
    assert "3700" in msg or "token" in msg


def test_complete_raises_on_broken_json(pointed_at_mock):
    with pytest.raises(L.LLMError) as ei:
        _call_with_scenario("malformed")
    assert "JSON" in str(ei.value) or "json" in str(ei.value)


# ────────────────────────── 四、记录能力 ──────────────────────────
def test_record_writes_what_client_sent(tmp_path):
    """联调排错靠它：事后能核对「我到底发出去什么」。"""
    rec = tmp_path / "req.jsonl"
    M.reset()
    srv = M.make_server("127.0.0.1", 0, scenario="ok", content="42",
                        flaky_fail=2, slow_s=0.1, quiet=True, record=str(rec))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        _post(base, _ok_payload())
        line = json.loads(rec.read_text(encoding="utf-8").strip())
        assert line["request"]["messages"][0]["content"] == "hi"
        assert line["scenario"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_content_file_overrides_default(tmp_path):
    cf = tmp_path / "answer.txt"
    cf.write_text("7", encoding="utf-8")
    M.reset()
    srv = M.make_server("127.0.0.1", 0, scenario="ok",
                        content=cf.read_text(encoding="utf-8"),
                        flaky_fail=2, slow_s=0.1, quiet=True, record=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        _, body = _post(base, _ok_payload())
        assert json.loads(body)["choices"][0]["message"]["content"] == "7"
    finally:
        srv.shutdown()
        srv.server_close()
