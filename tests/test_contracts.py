# -*- coding: utf-8 -*-
"""附录 A 的 S4 / S7 / S8：系统契约里需要 orchestrator 才能测的三条。

前两条在 `orchestrator` 层验证（出题去重、整代重做用尽），
S8 只测「精确值 + RNG 状态往返」，不依赖运行态。
"""

import json
import random
from fractions import Fraction

import pytest

from app import executor
from app.config import Config
from app.dataset_adapter import FileDataset
from app.llm import CallResult
from app.orchestrator import collect_tasks, run_generation
from app.runtime import new_run
from app.store import rng_from_dict, rng_state_to_dict

from pathlib import Path


def _config(**changes):
    raw = json.loads(Path("config.dev.json").read_text(encoding="utf-8"))
    raw.update(model="test-model", base_url="https://api.example.com/v1",
               n_gen=1, challenge_gens=[], budget_amount=None, wall_limit_s=30)
    raw.update(changes)
    return Config.from_dict(raw)


def _result(*, parsed=None, text=None, parse_error=None, call_id="c1"):
    return CallResult(
        call_id=call_id, role="setter", model="m", base_url="u",
        temperature=0.7, max_tokens=400, status="ok",
        text=text if text is not None else json.dumps(parsed, ensure_ascii=False),
        parsed=parsed, parse_error=parse_error,
    )


class ScriptedChat:
    """按脚本返回 setter 响应；记录每次调用，用来断言「一次 solver 都没发」。"""

    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    async def chat(self, role, messages, slot_id=None, attempt=None):
        self.calls.append((role, slot_id, attempt))
        return self.factory(role, slot_id, attempt)


def _state(**changes):
    cfg = _config(**changes)
    dataset = FileDataset()
    state = new_run(cfg=cfg, dataset=dataset, run_id="contracts")
    return cfg, dataset, state


# --------------------------------------------------------------------------
# S4：同一签名提交两次 -> duplicate，题不入池
# --------------------------------------------------------------------------

async def test_S4_duplicate_signature_rejected():
    cfg, dataset, state = _state()
    task = {"template": "gamma", "params": {"c": 1, "n": 1, "rate": 2}}
    chat = ScriptedChat(lambda role, slot, attempt: _result(parsed=dict(task)))

    valid, refetch, attempts, reason = await collect_tasks(
        gen=1, evaluated=state.next_population, cfg=cfg,
        engine=executor, chat=chat, occupied_sigs=set(),
    )

    assert valid is None
    assert reason == "invalid_generation"
    accepted = [a for a in attempts if a.accepted]
    duplicates = [a for a in attempts if a.reason == "duplicate"]
    assert len(accepted) == 1, "同一签名只能有一个人被接受"
    assert len(duplicates) == len(attempts) - 1
    assert refetch == cfg.refetch_rounds


# --------------------------------------------------------------------------
# S7：三个轮次用尽 -> aborted，slots_created == 0，solver_calls == 0
# --------------------------------------------------------------------------

async def test_S7_invalid_generation_creates_no_slots():
    cfg, dataset, state = _state()
    chat = ScriptedChat(
        lambda role, slot, attempt: _result(text="这不是 JSON", parse_error="no_object")
    )

    record = await run_generation(
        gen=1, state=state, engine=executor, dataset=dataset, chat=chat,
    )

    assert record["status"] == "aborted"
    assert record["reason"] == "invalid_generation"
    assert record["slot_total"] == 0
    assert record["slots"] == {}
    assert record["M"] == 0
    assert record["refetch_round"] == cfg.refetch_rounds

    # 六个人 × 每人 task_attempts × (1 + refetch_rounds) 轮
    assert len(chat.calls) == cfg.n_pop * cfg.task_attempts * (cfg.refetch_rounds + 1)
    assert all(call[0] == "setter" for call in chat.calls), "出题失败时不允许发解题请求"


async def test_S7_transport_error_aborts_without_slots():
    """网络故障要中止该代，不能退化成「全错」。"""
    from app.llm import CallResult

    def broken(role, slot, attempt):
        return CallResult(
            call_id="c1", role="setter", model="m", base_url="u",
            temperature=0.7, max_tokens=400, status="transport_error",
        )

    cfg, dataset, state = _state()
    chat = ScriptedChat(broken)
    record = await run_generation(gen=1, state=state, engine=executor,
                                  dataset=dataset, chat=chat)

    assert record["status"] == "aborted"
    assert record["reason"] == "transport_error"
    assert record["slot_total"] == 0


# --------------------------------------------------------------------------
# S8：精确值与 RNG 状态往返
# --------------------------------------------------------------------------

def test_S8_fraction_survives_roundtrip():
    value = Fraction(35, 3)
    restored = Fraction(int(str(value.numerator)), int(str(value.denominator)))
    assert restored == value
    assert restored.denominator == 3


def test_S8_rng_state_survives_roundtrip():
    rng = random.Random(42)
    blob = json.dumps(rng_state_to_dict(rng), ensure_ascii=False)
    restored = rng_from_dict(json.loads(blob))

    assert [rng.random() for _ in range(100)] == [restored.random() for _ in range(100)]
