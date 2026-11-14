"""B4：orchestrator mock 一代的六项核对 + 验收断言。

分工方案 v7.3 §B4 的六件事，这里逐条对应：
1. 槽位数 6/30/24/18=78，挑战活跃代 84
2. M != 6 → 整代重做，只重跑缺题的人
3. 重做上限 3 轮 × 2 次 = 每人最多 6 次出题调用
4. 3 轮用尽 → aborted，不创建任何解题槽位
5. unanswered 占分母，n_t 不缩
6. gene_history 只在 completed 后追加

验收另加：降噪窗口、同分抽签、跨数据集隔离、F 零算子、快照与 RNG 往返。
"""

from __future__ import annotations

import json
import random
from fractions import Fraction
from pathlib import Path

import pytest

from app.config import Config, per_gen_calls
from app.genotype import GeneHistory, Genes, Individual, Meta, gene_key
from app.orchestrator import (
    Challenge,
    Slot,
    ancestor_metrics,
    build_setter_messages,
    challenge_for_generation,
    rank_population,
    run_generation,
    scores,
    update_challenge,
)
from app.store import SnapshotStore, rng_from_dict, rng_state_to_dict

from doubles import (
    ANS_BAD_JSON,
    ANS_OK,
    ANS_TRANSPORT,
    ANS_WRONG,
    ANS_WRONG_TYPE,
    SET_BAD_JSON,
    SET_BAD_SCHEMA,
    SET_OK,
    SET_TRANSPORT,
    FakeDataset,
    FakeEngine,
    FakeOperators,
    ScriptedChat,
    build_state,
    seed_dedup,
    statement_of,
    validate_task,
)

DEV = Path("config.dev.json")
ALL_RIGHT = lambda slot_id, statement: ANS_OK
ALL_WRONG = lambda slot_id, statement: ANS_WRONG


def _cfg(**over) -> Config:
    raw = json.loads(DEV.read_text(encoding="utf-8"))
    raw.update(over)
    return Config.from_dict(raw, source_path="in-memory")


def _setup(**over):
    cfg = _cfg(**over)
    engine = FakeEngine()
    dataset = FakeDataset(engine, n_gens=cfg.n_gen)
    state = build_state(cfg)
    seed_dedup(state, dataset)
    return cfg, engine, dataset, state


async def _run(gen, state, engine, dataset, chat, operators=None):
    return await run_generation(gen=gen, state=state, engine=engine,
                                dataset=dataset, chat=chat, operators=operators)


# --------------------------------------------------------------------------
# 1 槽位数
# --------------------------------------------------------------------------

async def test_generation_has_78_slots_before_first_challenge():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "completed"
    assert rec["M"] == 6
    assert rec["slots"] == {"peer": 30, "common": 24, "ancestor": 18, "challenge": 0}
    # 分工表里的「78」是调用数：6 次出题 + 72 个解题槽位
    assert rec["slot_total"] == 72
    assert rec["setter_calls"] == 6
    assert rec["call_total"] == 78
    assert rec["call_total"] == per_gen_calls(cfg)      # 与 config 的公式对齐
    assert chat.count("setter") == 6
    assert chat.count("solver") == 72


async def test_challenge_generation_has_84_calls():
    # 挑战 born=1 时本代就要注入：72 + 6 槽位，加 6 次出题 = 84 次调用
    cfg, engine, dataset, state = _setup(condition="F", challenge_gens=[1, 8])
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["slots"]["challenge"] == 6
    assert rec["slot_total"] == 78
    assert rec["call_total"] == 84
    assert rec["ancestor"]["integral_failure"] == 0.0
    # 六人全对 → 达到 3 人阈值，当轮退役
    ch = state.challenges["C01"]
    assert ch.state == "retired"
    assert ch.retired_reason == "threshold_reached"
    assert ch.observations == [{"gen": 1, "attempt": 1, "correct": 6, "p_t": 1.0}]


async def test_three_consecutive_generations_do_not_crash():
    """B4 验收原句：连跑 3 代不崩，每代调用 78（前 7 代无挑战）。"""
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    for gen in (1, 2, 3):
        rec = await _run(gen, state, engine, dataset, chat)
        assert rec["status"] == "completed", gen
        assert rec["slots"]["challenge"] == 0
        assert rec["call_total"] == per_gen_calls(cfg) == 78
        assert rec["M"] == 6
    assert state.last_gen == 3
    assert chat.count("setter") == 18
    assert chat.count("solver") == 216


# --------------------------------------------------------------------------
# 2 / 3 整代重做与重做上限
# --------------------------------------------------------------------------

async def test_refetch_reruns_only_the_missing_author():
    cfg, engine, dataset, state = _setup(condition="F")
    # G00_S2 连续两次坏 JSON（都在首轮内用掉），第 3 次（第 2 轮）成功
    chat = ScriptedChat(cfg, engine,
                        setter_plan={"G00_S2": [SET_BAD_JSON, SET_BAD_JSON, SET_OK]},
                        answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "completed"
    assert rec["refetch_round"] == 1
    assert rec["M"] == 6
    assert chat.count("setter", "G00_S2") == 3
    for other in ("G00_S1", "G00_S3", "G00_S4", "G00_S5", "G00_S6"):
        assert chat.count("setter", other) == 1, f"{other} 被无谓重跑"
    # 8 次出题调用 = 5 人各 1 次 + 1 人 3 次
    assert chat.count("setter") == 8
    assert sum(1 for a in rec["attempts"]
               if str(a["reason"] or "").startswith("format:")) == 2


async def test_bad_json_triggers_refetch_within_same_round():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine,
                        setter_plan={"G00_S4": [SET_BAD_JSON, SET_OK]},
                        answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["refetch_round"] == 0          # 同一轮内第二次成功
    assert chat.count("setter", "G00_S4") == 2
    assert any(a["individual_id"] == "G00_S4" and not a["accepted"]
               and str(a["reason"]).startswith("format:") for a in rec["attempts"])


async def test_duplicate_is_rejected_then_retried_with_occupied_block():
    """撞签名不是盲试：第二次尝试必须带上已占用参数摘要。"""
    cfg, engine, dataset, state = _setup(condition="F")
    fixed = {"template": "gamma", "params": {"n": 7, "rate": 6, "c": 4}}
    chat = ScriptedChat(cfg, engine,
                        setter_plan={"G00_S1": [fixed], "G00_S2": [fixed, SET_OK]},
                        answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "completed"
    assert rec["refetch_round"] == 0
    assert chat.count("setter", "G00_S2") == 2
    dup = next(a for a in rec["attempts"]
               if a["individual_id"] == "G00_S2" and a["reason"] == "duplicate")
    assert dup["accepted"] is False
    # 第一次尝试没有追加块，第二次有
    s2_calls = [c for c in chat.calls
                if c["role"] == "setter" and c["individual_id"] == "G00_S2"]
    assert s2_calls[0]["occupied_block"] is False
    assert s2_calls[1]["occupied_block"] is True
    # 六道题的签名仍然两两不同
    assert len({t["signature"] for t in rec["tasks"]}) == 6


# --------------------------------------------------------------------------
# 4 重做用尽 → aborted，且不创建解题槽位
# --------------------------------------------------------------------------

async def test_refetch_exhausted_aborts_without_any_solver_call():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine,
                        setter_plan={"G00_S2": [SET_BAD_SCHEMA] * 6},
                        answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "aborted"
    assert rec["reason"] == "invalid_generation"
    assert rec["slots"] == {}
    assert rec["slot_total"] == 0
    # 3 轮 × 2 次内容尝试 = 6 次，一次不多
    assert chat.count("setter", "G00_S2") == 6
    assert chat.count("setter") == 5 + 6
    # 关键：没有进入作答阶段，所以一次解题调用都没浪费
    assert chat.count("solver") == 0
    # 失败代保留全部尝试记录
    assert len(rec["attempts"]) == 11
    assert rec["M"] == 5
    # 状态未被污染，可以原样重跑
    assert [i.id for i in state.next_population] == [f"G00_S{i}" for i in range(1, 7)]
    assert state.gene_history.to_dict() == {}


async def test_setter_transport_error_aborts_immediately():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine,
                        setter_plan={"G00_S3": [SET_TRANSPORT]},
                        answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "aborted"
    assert rec["reason"] == "transport_error"
    assert rec["slots"] == {}
    assert chat.count("solver") == 0


async def test_solver_transport_error_aborts_generation():
    cfg, engine, dataset, state = _setup(condition="F")

    def policy(slot_id, statement):
        if slot_id.endswith("_G00_S5") and "_peer_" in slot_id:
            return ANS_TRANSPORT
        return ANS_OK

    chat = ScriptedChat(cfg, engine, answer_policy=policy)
    rec = await _run(1, state, engine, dataset, chat)

    assert rec["status"] == "aborted"
    assert rec["reason"] == "transport_error"
    assert state.gene_history.to_dict() == {}
    assert rec["slots"] == {}


# --------------------------------------------------------------------------
# 5 unanswered 占分母
# --------------------------------------------------------------------------

async def test_unanswered_keeps_denominator_at_five():
    cfg, engine, dataset, state = _setup(condition="F")

    def policy(slot_id, statement):
        # T01_01 是 G00_S1 的题；让 G00_S3 对这道题格式错
        if slot_id == "g01_peer_T01_01_G00_S3":
            return ANS_BAD_JSON
        if slot_id == "g01_peer_T01_01_G00_S4":
            return ANS_WRONG_TYPE      # {"value": 3}：value 不是字符串
        return ANS_OK

    chat = ScriptedChat(cfg, engine, answer_policy=policy)
    rec = await _run(1, state, engine, dataset, chat)

    row = rec["per_task"]["T01_01"]
    assert row["assigned"] == 5
    assert row["unanswered"] == 2
    assert row["received"] == 3        # 解析失败的那两次没有收到可用答案
    assert row["right"] == 3
    assert row["p_t"] == pytest.approx(0.6)

    # 作者 G00_S1：3 人答对 → 出题分 1.0；格式错没有被从分母里剔除
    s1 = next(i for i in rec["individuals"] if i["id"] == "G00_S1")
    assert s1["set"] == 1.0


async def test_peer_slot_count_is_five_per_author():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)
    assert all(row["assigned"] == 5 for row in rec["per_task"].values())
    assert set(rec["per_task"]) == {f"T01_{i:02d}" for i in range(1, 7)}


# --------------------------------------------------------------------------
# 6 gene_history 只在 completed 后追加
# --------------------------------------------------------------------------

async def test_gene_history_appends_only_after_completed():
    cfg, engine, dataset, state = _setup(condition="F")
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    hist = state.gene_history.to_dict()
    assert len(hist) == 6
    for ind in state.next_population:
        assert hist[gene_key(ind.genes)] == [rec_individual_total(rec, ind.id)]


def rec_individual_total(rec: dict, ind_id: str) -> float:
    return next(i["total"] for i in rec["individuals"] if i["id"] == ind_id)


# --------------------------------------------------------------------------
# 验收：降噪窗口
# --------------------------------------------------------------------------

async def test_fitness_window_two_averages_two_generations():
    cfg, engine, dataset, state = _setup(condition="F", fitness_window=2)
    chat1 = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    r1 = await _run(1, state, engine, dataset, chat1)
    chat2 = ScriptedChat(cfg, engine, answer_policy=ALL_WRONG)
    r2 = await _run(2, state, engine, dataset, chat2)

    for row1 in r1["individuals"]:
        row2 = next(i for i in r2["individuals"] if i["id"] == row1["id"])
        expected = (row1["total"] + row2["total"]) / 2
        assert row2["est"] == pytest.approx(expected), row1["id"]
        assert row2["est"] != row2["total"], f"{row1['id']} 的 est 等于当代分，窗口没生效"
        assert row2["n_samples"] == 2


async def test_fitness_window_one_equals_current_total():
    cfg, engine, dataset, state = _setup(condition="F", fitness_window=1)
    chat1 = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    await _run(1, state, engine, dataset, chat1)
    chat2 = ScriptedChat(cfg, engine, answer_policy=ALL_WRONG)
    r2 = await _run(2, state, engine, dataset, chat2)
    for row in r2["individuals"]:
        assert row["est"] == row["total"]


async def test_child_never_inherits_parent_samples():
    parent = Genes("父策略", "父偏好", True)
    child = Genes("子策略", "子偏好", True)
    gh = GeneHistory()
    gh.append(parent, 7.0)
    assert gh.n_samples(child) == 0
    assert gh.estimate_including(child, 2, 3.0) == 3.0
    assert gh.estimate_including(parent, 2, 3.0) == pytest.approx(5.0)


# --------------------------------------------------------------------------
# 验收：同分抽签
# --------------------------------------------------------------------------

def test_tie_break_is_deterministic_and_shuffles_after_id_sort():
    people = [Individual(Genes(f"策略{i}", f"偏好{i}", True),
                         Meta(f"G00_S{i}", (), "seed", 0)) for i in range(1, 7)]
    for p in people:
        p.est, p.current_total = 4.0, 4.0

    a = [i.id for i in rank_population(people, random.Random(7))]
    b = [i.id for i in rank_population(people, random.Random(7))]
    assert a == b

    expected = sorted(p.id for p in people)
    random.Random(7).shuffle(expected)
    assert a == expected


def test_ranking_orders_by_est_then_current_total():
    people = []
    for i, (est, total) in enumerate([(4.0, 4.0), (5.5, 3.0), (5.5, 6.0), (3.0, 7.0)]):
        p = Individual(Genes(f"策略{i}", f"偏好{i}", True),
                       Meta(f"G{i:02d}", (), "seed", 0))
        p.est, p.current_total = est, total
        people.append(p)
    order = [p.id for p in rank_population(people, random.Random(1))]
    assert order[0] == "G02"     # est 5.5 且当代分更高
    assert order[1] == "G01"     # est 5.5，当代分低
    assert order[-1] == "G03"    # est 3.0


# --------------------------------------------------------------------------
# 验收：跨数据集隔离（作者题难度不影响解题分）
# --------------------------------------------------------------------------

def test_solve_score_is_isolated_from_peer_results():
    cfg = _cfg()

    def common(states):
        return [Slot(f"c{i}", "common", f"D01_0{i}", "G00_S1", st)
                for i, st in enumerate(states, start=1)]

    common_slots = common(["right", "right", "wrong", "unanswered"])
    peer_two = [Slot(f"p{i}", "peer", "T01_01", "G00_S1", st)
                for i, st in enumerate(["right", "right", "wrong", "wrong", "wrong"])]
    peer_one = [Slot(f"p{i}", "peer", "T01_01", "G00_S1", st)
                for i, st in enumerate(["right", "wrong", "wrong", "wrong", "wrong"])]

    s_two = scores(common_slots, peer_two, None, cfg)
    s_one = scores(common_slots, peer_one, None, cfg)

    assert s_two[0] == s_one[0] == 2          # solve 只由共同卷决定
    assert s_two[1] == 1.0                    # 2 人答对 → 满分
    assert s_one[1] == 0.5                    # 1 人答对 → 中档


def test_scores_reject_wrong_slot_counts():
    cfg = _cfg()
    with pytest.raises(AssertionError):
        scores([Slot("c1", "common", "D01_01", "x", "right")], [], None, cfg)


# --------------------------------------------------------------------------
# 验收：条件分派
# --------------------------------------------------------------------------

async def test_condition_f_clones_and_never_calls_operators():
    cfg, engine, dataset, state = _setup(condition="F")
    before = [gene_key(i.genes) for i in state.next_population]
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat)

    assert chat.count("operator") == 0
    assert rec["breed"] == {"condition": "F", "operator_calls": 0, "cloned": 6}
    after = [gene_key(i.genes) for i in state.next_population]
    assert after == before
    assert [i.id for i in state.next_population] == [f"G00_S{i}" for i in range(1, 7)]


async def test_condition_e_uses_operators_and_produces_six():
    cfg, engine, dataset, state = _setup(condition="E")
    ops = FakeOperators(cfg)
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    rec = await _run(1, state, engine, dataset, chat, operators=ops)

    assert ops.calls == [1]
    assert len(state.next_population) == 6
    assert [i.meta.origin for i in state.next_population[:3]] == ["seed"] * 3
    assert rec["breed"]["children"] == 3


async def test_last_generation_has_no_next_population():
    cfg, engine, dataset, state = _setup(condition="E", n_gen=6,
                                         challenge_gens=[1])
    ops = FakeOperators(cfg)
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    state.last_gen = 5
    rec = await _run(6, state, engine, dataset, chat, operators=ops)

    assert rec["next_population"] is None
    assert ops.calls == []


async def test_condition_a_zeroes_setter_weight():
    cfg, engine, dataset, state = _setup(condition="A", set_weight=0.0)
    ops = FakeOperators(cfg)
    # 只有 S1/S3/S5 答对：作者 S2 的五个同伴里 3 人答对 → 出题分 1.0
    mixed = lambda slot_id, statement: ANS_OK if slot_id[-1] in "135" else ANS_WRONG
    chat = ScriptedChat(cfg, engine, answer_policy=mixed)
    rec = await _run(1, state, engine, dataset, chat, operators=ops)

    assert any(row["set"] > 0 for row in rec["individuals"])
    for row in rec["individuals"]:
        assert row["total"] == row["solve"] + row["bounty"]
    # 有出题分的人，若权重仍是 2，总分会更高；这里必须等于 0 权重
    row = next(r for r in rec["individuals"] if r["set"] > 0)
    assert row["total"] != row["solve"] + 2 * row["set"] + row["bounty"]


# --------------------------------------------------------------------------
# 验收：挑战状态机
# --------------------------------------------------------------------------

def test_challenge_window_closes_after_six_attempts():
    ch = Challenge(task_id="C01", born_gen=8, window=6, state="active")
    cfg = _cfg()
    slots = [Slot(f"c{i}", "challenge", "C01", f"G00_S{i}", "wrong") for i in range(6)]
    for attempt, gen in enumerate(range(8, 14), start=1):
        update_challenge(ch, gen=gen, challenge_slots=slots, cfg=cfg)
        assert len(ch.observations) == attempt
    assert ch.state == "retired"
    assert ch.retired_reason == "window_closed"
    assert ch.first_success_attempt is None


def test_challenge_retires_on_threshold_and_keeps_bounty_gen():
    ch = Challenge(task_id="C01", born_gen=8, window=6, state="active")
    cfg = _cfg()
    slots = [Slot(f"c{i}", "challenge", "C01", f"G00_S{i}",
                  "right" if i < 3 else "wrong") for i in range(6)]
    update_challenge(ch, gen=8, challenge_slots=slots, cfg=cfg)
    assert ch.threshold_attempt == 1
    assert ch.retired_reason == "threshold_reached"
    # 已退役的题下一轮不再注入
    assert challenge_for_generation({"C01": ch}, 9) is None


def test_challenge_injected_only_at_born_gen():
    ch = Challenge(task_id="C01", born_gen=8, window=6)
    assert challenge_for_generation({"C01": ch}, 7) is None
    assert challenge_for_generation({"C01": ch}, 8) is ch
    assert ch.state == "active"
    assert challenge_for_generation({"C01": ch}, 9) is ch
    assert challenge_for_generation({"C01": ch}, 14) is None


# --------------------------------------------------------------------------
# 验收：祖先指标
# --------------------------------------------------------------------------

def test_ancestor_metrics_split_by_type():
    tasks = {
        "T01": {"type": "integral"}, "T02": {"type": "integral"},
        "T03": {"type": "det"}, "T04": {"type": "det"},
    }
    slots = []
    # T01 三祖先全错；T02 全对；T03 一人对；T04 两人对
    pattern = {"T01": [0, 0, 0], "T02": [1, 1, 1],
               "T03": [1, 0, 0], "T04": [1, 1, 0]}
    for tid, marks in pattern.items():
        for i, m in enumerate(marks):
            slots.append(Slot(f"{tid}_{i}", "ancestor", tid, f"A{i}",
                              "right" if m else "wrong"))
    m = ancestor_metrics(slots, tasks)
    assert m["integral_failure"] == pytest.approx((1.0 + 0.0) / 2)
    assert m["det_failure"] == pytest.approx((2 / 3 + 1 / 3) / 2)
    assert m["balanced_failure"] == pytest.approx(0.5 * 0.5 + 0.5 * 0.5)
    assert m["mixed_failure"] == pytest.approx((1 + 0 + 2 / 3 + 1 / 3) / 4)
    assert m["task_counts"] == {"integral": 2, "det": 2}


def test_ancestor_metrics_null_when_a_type_missing():
    tasks = {"T01": {"type": "integral"}}
    slots = [Slot(f"T01_{i}", "ancestor", "T01", f"A{i}", "right") for i in range(3)]
    m = ancestor_metrics(slots, tasks)
    assert m["integral_failure"] == 0.0
    assert m["det_failure"] is None
    assert m["balanced_failure"] is None


# --------------------------------------------------------------------------
# 出题重出块
# --------------------------------------------------------------------------

def test_second_attempt_appends_only_error_and_occupied():
    genes = Genes("先判断题型。", "优先 gamma。", True)
    first = build_setter_messages(genes)
    assert len(first) == 2 and "\n{" not in first[1]["content"]

    second = build_setter_messages(
        genes, error="duplicate",
        occupied=[{"template": "gamma", "params": {"n": 4, "rate": 2, "c": 1},
                   "reference": {"kind": "rational", "numerator": "3",
                                 "denominator": "4"}}])
    block = json.loads(second[1]["content"].splitlines()[-1])
    assert block["error"] == "duplicate"
    assert block["occupied"] == [{"template": "gamma",
                                 "params": {"n": 4, "rate": 2, "c": 1}}]
    assert "3/4" not in second[1]["content"]      # 答案绝不外泄
    assert first[0] == second[0]                  # system prompt 不变


def test_occupied_block_capped_at_five():
    genes = Genes("先判断题型。", "优先 gamma。", True)
    many = [{"template": "gamma", "params": {"n": i + 1, "rate": 2, "c": 1}}
            for i in range(8)]
    block = json.loads(build_setter_messages(genes, error="duplicate",
                                             occupied=many)[1]["content"]
                       .splitlines()[-1])
    assert len(block["occupied"]) == 5


def test_unknown_error_code_rejected():
    with pytest.raises(ValueError):
        build_setter_messages(Genes("a", "b", True), error="answer_leak")


# --------------------------------------------------------------------------
# 诚实性：执行器 bug 必须炸出来，不能被当成「模型出不了题」
# --------------------------------------------------------------------------

async def test_executor_bug_is_archived_as_execution_error():
    class BrokenEngine(FakeEngine):
        def solve_exact(self, task):
            if task.get("source") == "evolved":
                raise NameError("A 的 executor 里有个拼错的变量名")
            return super().solve_exact(task)

    cfg = _cfg(condition="F")
    state = build_state(cfg)
    dataset = FakeDataset(BrokenEngine(), n_gens=cfg.n_gen)
    seed_dedup(state, dataset)
    chat = ScriptedChat(cfg, BrokenEngine(), answer_policy=ALL_RIGHT)

    rec = await run_generation(gen=1, state=state, engine=BrokenEngine(),
                               dataset=dataset, chat=chat)
    assert rec['status'] == 'aborted' and rec['reason'] == 'execution_error'
    assert 'NameError' in rec['warnings'][0]
    assert state.last_gen == 0 and state.gene_history.to_dict() == {}


async def test_executor_arithmetic_error_is_treated_as_bad_params():
    class ZeroDivisionEngine(FakeEngine):
        def solve_exact(self, task):
            # 只对演化题失败；数据集里的题照常算，否则连题目集都建不起来
            if task.get("source") == "evolved":
                raise ZeroDivisionError("rate=0")
            return super().solve_exact(task)

    cfg = _cfg(condition="F")
    engine = ZeroDivisionEngine()
    state = build_state(cfg)
    dataset = FakeDataset(engine, n_gens=cfg.n_gen)
    seed_dedup(state, dataset)
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)

    rec = await run_generation(gen=1, state=state, engine=engine,
                               dataset=dataset, chat=chat)
    # 每次都算不出来 → 3 轮 × 2 次后按 invalid_generation 中止
    assert rec["status"] == "aborted"
    assert rec["reason"] == "invalid_generation"
    assert all(a["reason"] == "range" for a in rec["attempts"])


# --------------------------------------------------------------------------
# 快照与 RNG（规格 §8）
# --------------------------------------------------------------------------

def test_rng_state_roundtrip_matches_100_draws():
    rng = random.Random(9)
    for _ in range(17):
        rng.random()
    obj = rng_state_to_dict(rng)
    assert obj["schema"] == "rng_state/1" and len(obj["words"]) == 625

    clone = rng_from_dict(json.loads(json.dumps(obj)))
    assert [clone.random() for _ in range(100)] == [rng.random() for _ in range(100)]


def test_rng_state_rejects_tampered_payload():
    rng = random.Random(1)
    obj = rng_state_to_dict(rng)
    bad = dict(obj, words=obj["words"][:10])
    with pytest.raises(Exception):
        rng_from_dict(bad)
    with pytest.raises(Exception):
        rng_from_dict(dict(obj, version=2))


def test_snapshot_written_atomically_and_readable(tmp_path):
    cfg, engine, dataset, state = _setup(condition="F")
    store = SnapshotStore(root=tmp_path, run_id=state.run_id)
    path = store.save_generation(
        {"gen": 0, "status": "completed", "run_id": state.run_id,
         "schema_version": "5.1"},
        rng=state.rng, state_payload={"gen": 0})
    assert path.exists()
    assert not (path.parent / (path.name + ".tmp")).exists()

    store.append_history({"gen": 0, "status": "completed"})
    data = store.load_generation(0)
    assert data["phase"] == "completed"
    rng_from_dict(data["rng"])       # 结构可还原


# --------------------------------------------------------------------------
# 替身自身的可信度：它必须真的按规格拒绝/接受
# --------------------------------------------------------------------------

def test_double_engine_rejects_bool_and_float_params():
    ok, reason = validate_task({"template": "gamma",
                                "params": {"n": True, "rate": 2, "c": 1}})
    assert not ok and "schema" in reason
    ok, reason = validate_task({"template": "gamma",
                                "params": {"n": 4.0, "rate": 2, "c": 1}})
    assert not ok and "schema" in reason


def test_double_engine_range_checks():
    cases = [
        ({"template": "gamma", "params": {"n": 0, "rate": 2, "c": 1}}, "range"),
        ({"template": "gamma", "params": {"n": 4, "rate": 0, "c": 1}}, "range"),
        ({"template": "gamma", "params": {"n": 4, "rate": 2, "c": 0}}, "range"),
        ({"template": "poly", "params": {"coeffs": [1, 0], "lower": -1, "upper": 2}},
         "range"),
        ({"template": "det", "params": {"matrix": [[1, 2, 3], [4, 5, 6]]}}, "schema"),
    ]
    for raw, code in cases:
        ok, reason = validate_task(raw)
        assert not ok and code in reason, raw


def test_double_statement_matches_spec_examples():
    assert (statement_of("poly", {"coeffs": [3, -2, 1], "lower": -1, "upper": 2})
            == "计算定积分 ∫_{-1}^{2} (x² - 2x + 3) dx。")
    assert (statement_of("gamma", {"n": 4, "rate": 2, "c": 3})
            == "计算定积分 ∫₀^∞ 3x⁴ e^{-2x} dx。")
    assert (statement_of("gaussian_odd", {"k": 2, "rate": 3, "c": -2})
            == "计算定积分 ∫₀^∞ -2x⁵ e^{-3x²} dx。")
    assert (statement_of("det", {"matrix": [[2, 1], [1, 3]]})
            == "计算下列 2 阶矩阵的行列式：[[2, 1], [1, 3]]。")


def test_double_engine_closed_forms():
    """对照大纲 §4.1 的三条闭式，用 Fraction 精确比较（不是浮点近似）。"""
    engine = FakeEngine()
    cases = [
        # gamma: c·n!/rate^(n+1) = 3·24/2⁵ = 72/32 = 9/4
        ({"template": "gamma", "params": {"n": 4, "rate": 2, "c": 3}},
         Fraction(3 * 24, 2 ** 5)),
        # gaussian_odd: c·k!/(2·rate^(k+1)) = -2·2/(2·27) = -2/27
        ({"template": "gaussian_odd", "params": {"k": 2, "rate": 3, "c": -2}},
         Fraction(-2 * 2, 2 * 3 ** 3)),
        # ∫_{-1}^{2} (x²-2x+3) dx = 9
        ({"template": "poly", "params": {"coeffs": [3, -2, 1],
                                         "lower": -1, "upper": 2}},
         Fraction(9)),
    ]
    for raw, expected in cases:
        task = engine.normalize_task(raw, task_id="X", source="evolved",
                                     by="a", gen=1)
        ref = engine.solve_exact(task)
        assert Fraction(int(ref["numerator"]), int(ref["denominator"])) == expected

    det = engine.normalize_task({"template": "det",
                                 "params": {"matrix": [[2, 1], [1, 3]]}},
                                task_id="X", source="evolved", by="a", gen=1)
    assert engine.solve_exact(det) == {"kind": "integer", "value": "5"}


def test_double_judge_separates_unanswered_from_wrong():
    engine = FakeEngine()
    task = engine.normalize_task({"template": "det", "params": {"matrix": [[2, 1], [1, 3]]}},
                                 task_id="X", source="evolved", by="a", gen=1)
    engine.solve_exact(task)
    # 入参是整段返回文本（契约 §6），不是裸值
    assert engine.judge(task, '{"value":"5"}') == "right"
    assert engine.judge(task, '{"value":" 5 "}') == "right"
    assert engine.judge(task, '{"value":"6"}') == "wrong"
    assert engine.judge(task, '{"value":"5/1"}') == "unanswered"   # det 只接整数
    assert engine.judge(task, '{"value":"5.0"}') == "unanswered"
    assert engine.judge(task, None) == "unanswered"
    assert engine.judge(task, "sqrt(25)") == "unanswered"
    assert engine.judge(task, '{"value":5}') == "unanswered"       # value 必须是字符串
    assert engine.judge(task, '{"value":"5","x":1}') == "unanswered"
