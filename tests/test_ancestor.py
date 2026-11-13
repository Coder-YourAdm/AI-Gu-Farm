# -*- coding: utf-8 -*-
"""祖先指标的验收：单题失败率、分型、null 规则、均线断点、ancestors.json 校验。"""

import json
import random

import pytest

import app.ancestor as A


def slot(task_id, status, solver="G00_S1", scenario="ancestor"):
    return {
        "slot_id": "S_%s_%s_%s" % (task_id, solver, status),
        "task_id": task_id,
        "solver": solver,
        "scenario": scenario,
        "status": status,
        "raw_answer_id": "calls/000001",
    }


def three(task_id, rights, status_wrong="wrong"):
    """一道题上 3 个祖先：rights 个 right，其余 wrong。"""
    out = [slot(task_id, "right", "G00_S%d" % (i + 1)) for i in range(rights)]
    out += [slot(task_id, status_wrong, "G00_S%d" % (i + rights + 1)) for i in range(3 - rights)]
    return out


def generation(gen, plan):
    """plan: [(task_id, type, rights), ...]"""
    slots, types = [], {}
    for task_id, task_type, rights in plan:
        types[task_id] = task_type
        slots.extend(three(task_id, rights))
    return {"gen": gen, "slots": slots, "task_types": types}


# --------------------------------------------------------------------------
# 单题失败率
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rights,expect", [(0, 1.0), (1, 2 / 3), (2, 1 / 3), (3, 0.0)])
def test_per_task_failure(rights, expect):
    assert A.per_task_failure(three("T1", rights)) == pytest.approx(expect)


def test_per_task_failure_rejects_wrong_slot_count():
    with pytest.raises(ValueError):
        A.per_task_failure(three("T1", 2)[:2])


def test_per_task_failure_rejects_transport_error():
    slots = three("T1", 1)
    slots[2]["status"] = "transport_error"
    with pytest.raises(A.InvalidGeneration):
        A.per_task_failure(slots)


# --------------------------------------------------------------------------
# 单代四指标
# --------------------------------------------------------------------------

def test_balanced_and_mixed():
    g = generation(0, [
        ("I1", "integral", 3),   # 0
        ("I2", "integral", 2),   # 1/3
        ("I3", "integral", 1),   # 2/3
        ("D1", "det", 2),        # 1/3
        ("D2", "det", 1),        # 2/3
        ("D3", "det", 0),        # 1
    ])
    m = A.generation_failures(g["slots"], g["task_types"])

    integral = (0 + 1 / 3 + 2 / 3) / 3
    det = (1 / 3 + 2 / 3 + 1) / 3
    mixed = (0 + 1 / 3 + 2 / 3 + 1 / 3 + 2 / 3 + 1) / 6

    assert m["integral_failure"] == pytest.approx(integral)
    assert m["det_failure"] == pytest.approx(det)
    assert m["balanced_failure"] == pytest.approx(0.5 * integral + 0.5 * det)
    assert m["mixed_failure"] == pytest.approx(mixed)
    assert m["task_mix"] == {"integral": 3, "det": 3}


def test_missing_type_gives_null_and_blocks_balanced():
    g = generation(0, [("I1", "integral", 2), ("I2", "integral", 1)])
    m = A.generation_failures(g["slots"], g["task_types"])

    assert m["integral_failure"] == pytest.approx((1 / 3 + 2 / 3) / 2)
    assert m["det_failure"] is None            # 本代没有行列式题
    assert m["balanced_failure"] is None       # 两型不全 -> 不计算
    assert m["mixed_failure"] == pytest.approx((1 / 3 + 2 / 3) / 2)
    assert m["task_mix"] == {"integral": 2, "det": 0}


def test_null_is_not_zero():
    """缺型时是 None，绝不能变成 0（0 意味着「全都答对了」）。"""
    g = generation(0, [("D1", "det", 3)])
    m = A.generation_failures(g["slots"], g["task_types"])
    assert m["integral_failure"] is None
    assert m["det_failure"] == 0.0
    assert m["balanced_failure"] is None


def test_unanswered_counts_as_wrong_but_reported():
    slots = [slot("I1", "right"), slot("I1", "unanswered"), slot("I1", "unanswered")]
    m = A.generation_failures(slots, {"I1": "integral"})
    assert m["integral_failure"] == pytest.approx(2 / 3)   # 只对 1 个
    assert m["unanswered_count"] == 2


def test_unknown_task_type_raises():
    g = generation(0, [("I1", "integral", 1)])
    with pytest.raises(ValueError):
        A.generation_failures(g["slots"], {})


def test_bad_task_type_value_raises():
    g = generation(0, [("I1", "integral", 1)])
    with pytest.raises(ValueError):
        A.generation_failures(g["slots"], {"I1": "matrix"})


def test_per_task_slot_count_must_be_three():
    slots = three("I1", 1)[:2]
    with pytest.raises(ValueError):
        A.generation_failures(slots, {"I1": "integral"})


# --------------------------------------------------------------------------
# 三代均线
# --------------------------------------------------------------------------

def test_rolling_mean_full_window():
    assert A.rolling_mean([1.0, 2.0, 3.0], 3) == pytest.approx([1.0, 1.5, 2.0])


def test_rolling_mean_short_run_uses_available():
    assert A.rolling_mean([1.0, 3.0], 3) == pytest.approx([1.0, 2.0])


def test_rolling_mean_breaks_at_none():
    got = A.rolling_mean([1.0, 2.0, None, 4.0], 3)
    assert got[0] == pytest.approx(1.0)
    assert got[1] == pytest.approx(1.5)
    assert got[2] is None          # 当前值是 null -> 均线也是 null
    assert got[3] == pytest.approx(4.0)   # 断开后重新起算，不吃前面两个


def test_rolling_mean_window_one():
    assert A.rolling_mean([1.0, None, 3.0], 1) == [1.0, None, 3.0]


# --------------------------------------------------------------------------
# 端到端报告
# --------------------------------------------------------------------------

def test_ancestor_report_shape_and_means():
    gens = [
        generation(0, [("I1", "integral", 3), ("D1", "det", 3)]),   # 全对 -> 0
        generation(1, [("I1", "integral", 2), ("D1", "det", 2)]),   # 1/3
        generation(2, [("I1", "integral", 1), ("D1", "det", 1)]),   # 2/3
    ]
    rows = A.ancestor_report(gens)

    assert [r["gen"] for r in rows] == [0, 1, 2]
    assert rows[0]["balanced_failure"] == pytest.approx(0.0)
    assert rows[2]["balanced_failure"] == pytest.approx(2 / 3)
    assert rows[2]["balanced_failure_mean"] == pytest.approx((0 + 1 / 3 + 2 / 3) / 3)


def test_report_keeps_gap_in_line():
    gens = [
        generation(0, [("I1", "integral", 3), ("D1", "det", 3)]),
        generation(1, [("I1", "integral", 1)]),                      # 缺 det
        generation(2, [("I1", "integral", 0), ("D1", "det", 0)]),
    ]
    rows = A.ancestor_report(gens)

    assert rows[1]["balanced_failure"] is None
    assert rows[1]["balanced_failure_mean"] is None
    assert rows[2]["balanced_failure"] == pytest.approx(1.0)
    # 缺口之后重新起算，不吃 gen0 的 0
    assert rows[2]["balanced_failure_mean"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# ancestors.json
# --------------------------------------------------------------------------

def _write(tmp_path, payload):
    p = tmp_path / "ancestors.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _ok_payload():
    return {"ancestors": [
        {"id": "G00_S%d" % i,
         "genes": {"solve_prompt": "s%d" % i, "set_prompt": "t%d" % i, "self_check": bool(i % 2)},
         "calibration_mean": 0.6 - i * 0.05}
        for i in range(1, 4)
    ]}


def test_load_ancestors_ok(tmp_path):
    got = A.load_ancestors(_write(tmp_path, _ok_payload()))
    assert len(got) == 3
    assert got[0]["id"] == "G00_S1"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(ancestors=p["ancestors"][:2]),                     # 只有两个
    lambda p: p["ancestors"][1].update(id=p["ancestors"][0]["id"]),       # id 重复
    lambda p: p["ancestors"][0].pop("genes"),                             # 缺 genes
    lambda p: p["ancestors"][0]["genes"].pop("self_check"),               # 缺字段
    lambda p: p.pop("ancestors"),                                        # 顶层不对
])
def test_load_ancestors_rejects(tmp_path, mutate):
    payload = _ok_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        A.load_ancestors(_write(tmp_path, payload))


def test_pick_ancestors_takes_top_three():
    means = {"G00_S1": 0.9, "G00_S2": 0.5, "G00_S3": 0.7, "G00_S4": 0.1, "G00_S5": 0.8}
    got = A.pick_ancestors(means, random.Random(1))
    assert got == ["G00_S1", "G00_S5", "G00_S3"]


def test_pick_ancestors_breaks_tie_with_rng():
    means = {"A": 0.5, "B": 0.5, "C": 0.5}
    got = A.pick_ancestors(means, random.Random(7))
    assert sorted(got) == ["A", "B", "C"]
    assert A.pick_ancestors(means, random.Random(7)) == got     # 同种子可复现
