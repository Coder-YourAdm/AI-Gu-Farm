#!/usr/bin/env python3
"""预算链路：`max_tokens_solver` 必须是真旋钮，不能是装饰品。

2026-11-13 实测的两个坑
-----------------------
1. `config.json` 里有 `max_tokens_solver: 1200`，但**没有任何代码读它** ——
   `demo.py` 直接用 `llm_standalone.py` 的模块常量。改配置零效果，人却以为
   自己调过了。配置项不被读取比不存在更危险，因为它长得像已经生效。
2. `deepseek-flash` 是推理模型，先烧 reasoning_tokens 再吐 content。预算 1200
   时推理没收敛就被砍断，`content` 为空，往下传被判成 `unanswered`。
   病历上写「模型不会做」，真病因是预算不够 —— 这种误诊会让人白换一轮模型。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm_standalone as L  # noqa: E402


def test_budget_reads_config(monkeypatch, tmp_path):
    """budget() 真的去读 config，而不是返回常量。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"max_tokens_solver": 4000}), encoding="utf-8")
    monkeypatch.setattr(L, "CONFIG_PATH", cfg)
    assert L.budget("solver", 1200) == 4000


def test_budget_falls_back_when_missing(monkeypatch, tmp_path):
    """配置没这一项 / 文件不存在时退回默认值，不能崩。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(L, "CONFIG_PATH", cfg)
    assert L.budget("solver", 1200) == 1200
    monkeypatch.setattr(L, "CONFIG_PATH", tmp_path / "nope.json")
    assert L.budget("solver", 1200) == 1200


def test_truncated_reply_raises_instead_of_empty_string():
    """被截断且无答案 → 必须抛错。返回空串会被误判成「模型不会做」。"""
    data = {
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "",
                        "reasoning_content": "We need compute determinant..."},
        }],
        "usage": {"prompt_tokens": 136, "completion_tokens": 1200,
                  "completion_tokens_details": {"reasoning_tokens": 1200}},
    }
    with pytest.raises(L.LLMError) as ei:
        L._parse(data)
    msg = str(ei.value)
    assert "截断" in msg
    assert "4000" in msg, "报错要直接给出该调到多少，不能只说自己错了"


def test_normal_reply_unaffected():
    """正常响应照旧，别把闸门关死。"""
    text, tin, tout = L._parse({
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "66060"}}],
        "usage": {"prompt_tokens": 136, "completion_tokens": 3723},
    })
    assert text == "66060" and tin == 136 and tout == 3723


def test_partial_content_is_kept():
    """有内容但被截断（答案写了一部分）不该抛错 —— 那是模型的问题，交给判分。"""
    text, _, _ = L._parse({
        "choices": [{"finish_reason": "length",
                     "message": {"role": "assistant", "content": "6606"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1200},
    })
    assert text == "6606"


def test_solver_budget_is_wired_into_pair_run():
    """双跑必须把配置里的预算传给 complete()。

    这条防的是回退：有人把 `_ask` 换回裸 `_complete(...)`，配置就又变装饰品了。
    """
    src = (ROOT / "app" / "demo.py").read_text(encoding="utf-8")
    assert "_budget_fn(\"solver\"" in src, "预算没从 config 读"
    assert "max_tokens=solver_max_tokens" in src, "预算没传给 complete()"
    assert "_complete([{\"role\"" not in src, "还有裸 _complete 调用绕过预算"
