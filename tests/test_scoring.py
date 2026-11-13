# -*- coding: utf-8 -*-
"""scoring 的验收：计分边界 + 规格 §6.1 的三条公平性回归。"""

import random

import pytest

import app.scoring as S

CFG = {"set_weight": 2, "bounty": 2.0, "fitness_window": 2, "seed": 42}
CFG_W1 = {"set_weight": 2, "bounty": 2.0, "fitness_window": 1, "seed": 42}


def slot(status, scenario="common", solver="G00_S1", task_id="T1"):
    return {
        "slot_id": "S_%s_%s" % (scenario, status),
        "task_id": task_id,
        "solver": solver,
        "scenario": scenario,
        "status": status,
        "raw_answer_id": "calls/000001",
    }


def peer_slots_with(c):
    """构造 5 个 peer 槽位，其中 c 个 right。"""
    return [slot("right", "peer") for _ in range(c)] + [
        slot("wrong", "peer") for _ in range(S.N_PEER - c)
    ]


def ind(i, total=0.0, born=0, genes=None):
    return {
        "id": "G00_S%d" % i,
        "genes": genes or {
            "solve_prompt": "solve-%d" % i,
            "set_prompt": "set-%d" % i,
            "self_check": bool(i % 2),
        },
        "current_total": total,
        "born_gen": born,
        "status": "completed",
    }


# --------------------------------------------------------------------------
# 计分
# --------------------------------------------------------------------------

@pytest.mark.parametrize("c,expect", [(0, 0.0), (1, 0.5), (2, 1.0), (3, 1.0), (4, 0.5), (5, 0.0)])
def test_set_score_six_levels(c, expect):
    common = [slot("wrong")] * 4
    solve, set_score, bounty, total = S.scores(common, peer_slots_with(c), None, CFG)
    assert set_score == expect
    assert solve == 0
    assert bounty == 0.0
    assert total == 2 * expect


def test_total_formula():
    common = [slot("right"), slot("right"), slot("right"), slot("wrong")]
    solve, set_score, bounty, total = S.scores(common, peer_slots_with(2), None, CFG)
    assert (solve, set_score, bounty, total) == (3, 1.0, 0.0, 5.0)


def test_bounty_only_when_challenge_right():
    common = [slot("right")] * 4
    _, _, bounty, total = S.scores(common, peer_slots_with(0), slot("right"), CFG)
    assert bounty == 2.0 and total == 4 + 0 + 2.0

    _, _, bounty2, total2 = S.scores(common, peer_slots_with(0), slot("wrong"), CFG)
    assert bounty2 == 0.0 and total2 == 4.0


def test_unanswered_counts_as_zero_but_takes_a_seat():
    common = [slot("right"), slot("unanswered"), slot("wrong"), slot("unanswered")]
    solve, _, _, _ = S.scores(common, peer_slots_with(0), None, CFG)
    assert solve == 1                      # 分母仍是 4


@pytest.mark.parametrize("bad_common,bad_peer", [([], []), ([slot("right")] * 3, []), ([slot("right")] * 4, [slot("right")] * 4)])
def test_slot_count_is_enforced(bad_common, bad_peer):
    with pytest.raises(ValueError):
        S.scores(bad_common, bad_peer, None, CFG)


# --------------------------------------------------------------------------
# 回归 1：跨数据集隔离
# --------------------------------------------------------------------------

def test_common_slots_are_isolated_from_peer_scoring():
    """同一批 common 给六人 -> 六个 solve 完全相同；差异只能来自 set_score。"""
    common = [slot("right"), slot("right"), slot("wrong"), slot("unanswered")]

    results = []
    for c in range(S.N_PEER + 1):
        solve, set_score, bounty, total = S.scores(common, peer_slots_with(c), None, CFG)
        results.append((solve, set_score, bounty, total))

    solves = {r[0] for r in results}
    assert solves == {2}, "common 相同的六人，solve 必须完全相同"

    # total 的差异必须完全由 set_score 解释
    for solve, set_score, bounty, total in results:
        assert total == solve + CFG["set_weight"] * set_score + bounty


# --------------------------------------------------------------------------
# 回归 2：降噪不越界（子代不沾父代样本）
# --------------------------------------------------------------------------

def test_child_does_not_inherit_parent_history():
    hist = {}
    parent = ind(1, total=6.0)
    for _ in range(3):
        S.append_gene_history(parent, hist, total=6.0)
    assert S.est_score(parent, CFG, hist) == 6.0

    child_genes = {
        "solve_prompt": "child prompt",
        "set_prompt": "child setter",
        "self_check": False,
    }
    child = ind(2, total=1.0, genes=child_genes)

    # 子代还没 append 时，不能借用父代样本
    with pytest.raises(ValueError):
        S.est_score(child, CFG, hist)

    S.append_gene_history(child, hist, total=1.0)
    assert S.est_score(child, CFG, hist) == 1.0
    assert S.est_score(child, CFG, hist) != S.est_score(parent, CFG, hist)


def test_same_gene_accumulates_measurements():
    """同一个基因（精英）多代受测 -> 估计取最近 window 次均值。"""
    hist = {}
    elite = ind(1, total=4.0)
    for total in (2.0, 4.0, 6.0):
        S.append_gene_history(elite, hist, total=total)
    assert S.est_score(elite, CFG, hist) == 5.0          # (4+6)/2


# --------------------------------------------------------------------------
# 回归 3：窗口边界
# --------------------------------------------------------------------------

def test_window_one_equals_current_total():
    hist = {}
    e = ind(1, total=5.0)
    S.append_gene_history(e, hist, total=1.0)
    S.append_gene_history(e, hist, total=3.0)
    S.append_gene_history(e, hist, total=5.0)
    assert S.est_score(e, CFG_W1, hist) == 5.0
    assert S.est_score(e, CFG_W1, hist) == e["current_total"]


# --------------------------------------------------------------------------
# gene_key
# --------------------------------------------------------------------------

def test_gene_key_is_stable_and_content_sensitive():
    a = S.gene_key({"solve_prompt": "x", "set_prompt": "y", "self_check": True})
    b = S.gene_key({"self_check": True, "set_prompt": "y", "solve_prompt": "x"})
    c = S.gene_key({"solve_prompt": "x", "set_prompt": "y", "self_check": False})
    assert a == b            # 键序不影响
    assert a != c            # 开关翻转就换指纹
    assert len(a) == 64


def test_gene_key_ignores_metadata():
    genes = {"solve_prompt": "x", "set_prompt": "y", "self_check": True}
    with_meta = dict(genes, id="G00_S1", parents=["G00_S2"], born_gen=3)
    assert S.gene_key(genes) == S.gene_key(with_meta)


# --------------------------------------------------------------------------
# 排序与同分打散
# --------------------------------------------------------------------------

def test_ranking_prefers_higher_est_then_higher_total():
    hist = {}
    weak = ind(1, total=2.0)
    strong = ind(2, total=5.0)
    for x in (weak, strong):
        S.append_gene_history(x, hist, total=x["current_total"])

    order = S.rank_population([weak, strong], CFG, random.Random(1), hist)
    assert [x["id"] for x in order] == [strong["id"], weak["id"]]


def test_tie_break_is_independent_of_input_order():
    """同分时必须按 ID 排好再 shuffle：换输入顺序，结果要一样。"""
    hist = {}
    pop = [ind(i, total=3.0) for i in range(1, 7)]
    for x in pop:
        S.append_gene_history(x, hist, total=3.0)

    forward = S.rank_population(pop, CFG, random.Random(20261113), hist)
    backward = S.rank_population(list(reversed(pop)), CFG, random.Random(20261113), hist)

    assert [x["id"] for x in forward] == [x["id"] for x in backward]


def test_ranked_population_is_a_permutation():
    hist = {}
    pop = [ind(i, total=float(i % 4)) for i in range(1, 7)]
    for x in pop:
        S.append_gene_history(x, hist, total=x["current_total"])

    order = S.rank_population(pop, CFG, random.Random(7), hist)
    assert sorted(x["id"] for x in order) == sorted(x["id"] for x in pop)


def test_survivors_returns_three():
    hist = {}
    pop = [ind(i, total=float(i)) for i in range(1, 7)]
    for x in pop:
        S.append_gene_history(x, hist, total=x["current_total"])

    top = S.survivors(pop, CFG, random.Random(3), hist)
    assert len(top) == S.N_SURVIVE
    assert [x["id"] for x in top] == ["G00_S6", "G00_S5", "G00_S4"]


# --------------------------------------------------------------------------
# gene_history 快照往返
# --------------------------------------------------------------------------

def test_history_export_import_roundtrip():
    hist = {}
    e = ind(1, total=4.0)
    S.append_gene_history(e, hist, total=2.0)
    S.append_gene_history(e, hist, total=4.0)

    dumped = S.export_gene_history(hist)
    restored = {}
    S.import_gene_history(dumped, restored)

    assert restored == hist
    assert S.est_score(e, CFG, restored) == S.est_score(e, CFG, hist)


def test_est_score_requires_appended_sample():
    with pytest.raises(ValueError):
        S.est_score(ind(9, total=1.0), CFG, {})
