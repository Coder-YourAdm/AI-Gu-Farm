"""B6 前置：用 A 的真 `app.executor` 替换 `FakeEngine`，跑通完整一代。

这一份测试的存在理由：B4 的替身是「按契约写的参照实现」，替身全过不代表
A 的真模块能接上。集成时暴露过两处口径差异，都在这里被钉住：

1. `judge` 的入参。契约 §6 写的是「模型的原始返回字符串（整段文本）」，
   B4 的替身接的是裸值 `"3/4"`。真 executor 会先 `json.loads`，于是裸值
   一路判成 unanswered —— 全对的一代看起来像全员零分。
2. `judge` 在 reference 损坏时抛 `ValueError`。B 必须捕获并中止该代，
   不能吞成 wrong（契约 §6 末段、规格 §15）。

替身只保留「假模型」的部分（ScriptedChat 的作答行为、FakeDataset 的取题
逻辑），六个契约函数全部走 A 的真实实现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.executor as executor
from app.config import Config, per_gen_calls
from app.orchestrator import run_generation, task_from_raw

from doubles import ANS_BAD_JSON, ANS_OK, ANS_WRONG, FakeDataset, ScriptedChat, build_state, seed_dedup

DEV = Path("config.dev.json")


# --------------------------------------------------------------------------
# 真 executor 的薄包装
# --------------------------------------------------------------------------

class RealEngine:
    """`app.executor` 的直通包装，只多维护一张 statement -> 答案 表。

    那张表属于「假模型的知识」，不是 Engine 契约的一部分：真模型自己算答案。
    六个函数的签名与返回值一字不改地转发。
    """

    def __init__(self) -> None:
        self.reference_by_statement: dict[str, str] = {}

    # ---- §2.3 六个函数：直通 ----

    def validate_task(self, raw: dict) -> tuple[bool, str]:
        return executor.validate_task(raw)

    def normalize_task(self, raw: dict, *, task_id: str, source: str,
                       by: str | None, gen: int | None) -> dict:
        task = executor.normalize_task(raw, task_id=task_id, source=source,
                                       by=by, gen=gen)
        ref = task["reference"]
        answer = (ref["value"] if ref["kind"] == "integer"
                  else "%s/%s" % (ref["numerator"], ref["denominator"]))
        self.reference_by_statement[task["statement"]] = answer
        return task

    def solve_exact(self, task: dict) -> dict:
        return executor.solve_exact(task)

    def solver_view(self, task: dict) -> dict:
        return executor.solver_view(task)

    def signature(self, task: dict) -> str:
        return executor.signature(task)

    def judge(self, task: dict, raw_answer: str | None) -> str:
        return executor.judge(task, raw_answer)


def _cfg(**over) -> Config:
    raw = json.loads(DEV.read_text(encoding="utf-8"))
    raw.update(over)
    return Config.from_dict(raw, source_path="in-memory")


def _setup(**over):
    cfg = _cfg(**over)
    engine = RealEngine()
    dataset = FakeDataset(engine, n_gens=cfg.n_gen)
    state = build_state(cfg)
    seed_dedup(state, dataset)
    return cfg, engine, dataset, state


ALL_RIGHT = lambda slot_id, statement: ANS_OK
ALL_WRONG = lambda slot_id, statement: ANS_WRONG
ALL_MALFORMED = lambda slot_id, statement: ANS_BAD_JSON


async def _run(gen, state, engine, dataset, chat):
    return await run_generation(gen=gen, state=state, engine=engine,
                                dataset=dataset, chat=chat)


# --------------------------------------------------------------------------
# 1 一代真跑通
# --------------------------------------------------------------------------

async def test_generation_end_to_end_with_real_executor():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "completed"
    assert rec["M"] == 6
    assert rec["slots"] == {"peer": 30, "common": 24, "ancestor": 18,
                            "challenge": 0}
    assert rec["slot_total"] == 72
    assert rec["setter_calls"] == 6
    assert rec["call_total"] == 78 == per_gen_calls(cfg)
    assert chat.count("setter") == 6 and chat.count("solver") == 72

    # 全对：6 道题各 5 个互答槽全对（这是「judge 入参」修好后才成立的断言）。
    # 若把裸值 "3/4" 直接喂给真 judge，这里会全是 unanswered。
    assert len(rec["per_task"]) == 6
    for task_id, row in rec["per_task"].items():
        assert row["assigned"] == 5, task_id
        assert row["received"] == 5, task_id
        assert row["right"] == 5, task_id
        assert row["p_t"] == 1.0, task_id

    for row in rec["individuals"]:
        assert row["solve"] == 4          # 4 道共同卷全对
        assert row["set"] == 0.0          # 5 人全对 -> min(5,0)/2


async def test_challenge_generation_84_calls_with_real_executor():
    cfg, engine, dataset, state = _setup(condition="F", challenge_gens=[1, 8])
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["slots"]["challenge"] == 6
    assert rec["slot_total"] == 78
    assert rec["call_total"] == 84
    ch = state.challenges["C01"]
    assert ch.state == "retired"
    assert ch.observations == [{"gen": 1, "attempt": 1, "correct": 6,
                                "p_t": 1.0}]


async def test_wrong_and_malformed_both_occupy_denominator():
    """三态都要占分母：全错与全格式错都不许让 n_t 缩水，也不许中止。"""
    for policy, expect_received, expect_status in (
            (ALL_WRONG, 5, "wrong"), (ALL_MALFORMED, 0, "unanswered")):
        cfg, engine, dataset, state = _setup(condition="F")
        chat = ScriptedChat(cfg, engine, answer_policy=policy)
        rec = await _run(1, state, engine, dataset, chat)

        assert rec["status"] == "completed"
        assert rec["slot_total"] == 72                    # 分母不缩
        assert all(row["solve"] == 0 for row in rec["individuals"])
        for task_id, row in rec["per_task"].items():
            assert row["assigned"] == 5, task_id
            assert row["received"] == expect_received, task_id
            assert row["right"] == 0, task_id
            assert row[expect_status] == 5, task_id
            assert (row["right"] + row["wrong"] + row["unanswered"]) == 5


# --------------------------------------------------------------------------
# 2 A 的契约逐条复核（接口契约 §2/§5/§7/§8）
# --------------------------------------------------------------------------

def test_contract_validate_rejections():
    assert executor.validate_task({"template": "gamma",
                                   "params": {"n": 3, "rate": 0, "c": 1}}) \
        == (False, "rate 越界：0 不在 [1, 8]")
    ok, reason = executor.validate_task(
        {"template": "poly", "params": {"coeffs": [1, 1], "lower": 0, "upper": 1},
         "level": 3})
    assert ok is False and "只能有 template 与 params" in reason
    # true / 2.0 / "2" 全部拒
    for bad in (True, 2.0, "2"):
        ok, _ = executor.validate_task(
            {"template": "gamma", "params": {"n": bad, "rate": 2, "c": 1}})
        assert ok is False, bad
    assert executor.validate_task({"template": "det",
                                   "params": {"matrix": [[2, 1], [1, 3]]}})[0]


def test_contract_signature_formats():
    cases = [
        ({"template": "gamma", "params": {"n": 4, "rate": 2, "c": 1}},
         "gamma|c=1|n=4|rate=2"),
        ({"template": "poly", "params": {"coeffs": [1, 1], "lower": 0, "upper": 1}},
         "poly|coeffs=1,1|lower=0|upper=1"),
        ({"template": "gaussian_odd", "params": {"k": 2, "rate": 3, "c": -2}},
         "gaussian_odd|c=-2|k=2|rate=3"),
        ({"template": "det", "params": {"matrix": [[2, 1], [1, 3]]}},
         "det|matrix=2,1;1,3"),
    ]
    for raw, sig in cases:
        assert executor.signature(raw) == sig


def test_contract_reference_encoding():
    task = executor.normalize_task({"template": "gamma",
                                    "params": {"n": 1, "rate": 1, "c": 1}},
                                   task_id="T", source="evolved", by="G01_S1", gen=1)
    assert task["reference"] == {"kind": "integer", "value": "1"}   # 9/1 -> 9

    task = executor.normalize_task({"template": "gamma",
                                    "params": {"n": 4, "rate": 2, "c": 1}},
                                   task_id="T", source="evolved", by="G01_S1", gen=1)
    assert task["reference"] == {"kind": "rational", "numerator": "3",
                                 "denominator": "4"}


def test_contract_solver_view_key_set():
    task = executor.normalize_task({"template": "det",
                                    "params": {"matrix": [[2, 1], [1, 3]]}},
                                   task_id="T", source="seed", by=None, gen=0)
    view = executor.solver_view(task)
    assert set(view) == {"type", "statement", "answer_format"}
    assert view["answer_format"] == "integer only"
    assert "reference" not in json.dumps(view)


def test_contract_judge_table():
    integral = {"type": "integral",
                "reference": {"kind": "rational", "numerator": "3",
                              "denominator": "4"}}
    assert executor.judge(integral, '{"value":"3/4"}') == "right"
    assert executor.judge(integral, '{"value":"0.75"}') == "right"
    assert executor.judge(integral, '{"value":"0.8"}') == "wrong"
    assert executor.judge(integral, "sqrt(4)") == "unanswered"
    assert executor.judge(integral, None) == "unanswered"
    assert executor.judge(integral, '{"value":"3","extra":1}') == "unanswered"
    assert executor.judge(integral, '{"value":7}') == "unanswered"
    assert executor.judge(integral, "x" * 9000) == "unanswered"

    det = {"type": "det", "reference": {"kind": "integer", "value": "5"}}
    assert executor.judge(det, '{"value":"5"}') == "right"
    assert executor.judge(det, '{"value":"5/1"}') == "unanswered"
    assert executor.judge(det, '{"value":"5.0"}') == "unanswered"


def test_contract_judge_raises_on_broken_reference():
    """契约 §6：reference 结构损坏要抛异常，B 才能中止该代。

    实测差异（已反馈 A）：`kind` 不认识 / 整个不是 dict -> ValueError，符合文档；
    但**缺字段**（`{"kind":"integer"}` 没有 `value`）-> KeyError，与实现说明
    「缺字段抛 ValueError」不符。两者都是数据故障，B 侧一并接住。
    """
    with pytest.raises(ValueError):
        executor.judge({"type": "integral", "reference": {"kind": "weird"}},
                       '{"value":"1"}')
    with pytest.raises(ValueError):
        executor.judge({"type": "integral", "reference": None}, '{"value":"1"}')
    with pytest.raises(KeyError):
        executor.judge({"type": "integral", "reference": {"kind": "integer"}},
                       '{"value":"1"}')
    with pytest.raises(KeyError):
        executor.judge({"type": "integral",
                        "reference": {"kind": "rational", "numerator": "1"}},
                       '{"value":"1"}')


# --------------------------------------------------------------------------
# 3 reference 损坏 -> 中止该代，不记成 wrong（契约 §6 末段）
# --------------------------------------------------------------------------

class _BrokenEngine(RealEngine):
    def judge(self, task, raw_answer):
        raise ValueError("reference 结构损坏（测试注入）")


async def test_broken_reference_aborts_generation():
    """reference 损坏 -> 该代 aborted，且不排名、不写历史；不记成 wrong。"""
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    broken = _BrokenEngine()
    broken.reference_by_statement = engine.reference_by_statement

    before = len(state.gene_history.history) if hasattr(state.gene_history, "history") else None
    rec = await _run(1, state, broken, dataset, chat)

    assert rec["status"] == "aborted"
    assert rec["reason"] == "invalid_generation"
    assert rec["ranking"] == []
    assert any("参考值" in w for w in rec["warnings"])
    assert state.phase == "aborted"
    if before is not None:
        assert len(state.gene_history.history) == before


# --------------------------------------------------------------------------
# 4 task_from_raw 在真 executor 下的四类拒绝码
# --------------------------------------------------------------------------

def test_task_from_raw_reason_codes():
    engine = RealEngine()
    ok_task, reason = task_from_raw(
        {"template": "gamma", "params": {"n": 4, "rate": 2, "c": 1}},
        individual_id="G01_S1", gen=1, engine=engine)
    assert ok_task is not None and reason is None
    assert ok_task["signature"] == "gamma|c=1|n=4|rate=2"
    assert ok_task["reference"]["kind"] == "rational"

    _, reason = task_from_raw({"template": "gamma",
                               "params": {"n": 0, "rate": 2, "c": 1}},
                              individual_id="G01_S1", gen=1, engine=engine)
    assert reason == "schema"

    _, reason = task_from_raw({"nope": 1}, individual_id="G01_S1", gen=1,
                              engine=engine)
    assert reason == "schema"
