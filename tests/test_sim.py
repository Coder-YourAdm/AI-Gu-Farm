"""模拟学生回归：错答必须是合法字面量，且不能出现 unanswered。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import executor  # noqa: E402
from app.evalsim.sim_student import LEVEL_RATES, SimStudent  # noqa: E402


def task(raw=None):
    return executor.normalize_task(
        raw or {"template": "poly",
                "params": {"coeffs": [3, -2, 1], "lower": -1, "upper": 2}},
        task_id="T", source="test", by=None, gen=None)


def verdict(t, literal):
    return executor.judge(t, None if literal is None
                          else json.dumps({"value": literal}))


def test_three_levels_and_hint_bonus():
    for level, rate in LEVEL_RATES.items():
        s = SimStudent(level)
        assert s.correct_probability(0) == rate
        assert s.correct_probability(4) == pytest.approx(min(0.95, rate + 0.4))
    assert SimStudent("strong").correct_probability(4) == 0.95  # 上限


def test_all_answers_are_legal_literals_never_unanswered():
    """这是成本指标不失真的前提。"""
    t = task()
    pattern = re.compile(r"^[+-]?[0-9]+(?:/[0-9]+)?$")
    for level in LEVEL_RATES:
        s = SimStudent(level, seed=7)
        for hint in range(5):
            for _ in range(40):
                v = s.answer(t, hint)
                assert v is not None and pattern.fullmatch(v), v
                assert verdict(t, v) in ("right", "wrong"), v


def test_wrong_answers_use_only_two_error_shapes():
    """符号翻转 / 分子 ±1；`_literal(9)` 的候选中不该出现别的东西。"""
    t = task()
    s = SimStudent("weak", seed=1)
    wrong = set(s._wrong_literals(t))
    assert wrong == {"-9", "10", "8"}
    assert all(verdict(t, w) == "wrong" for w in wrong)


def test_zero_reference_does_not_produce_a_fake_wrong_answer():
    """ref=0 时 -ref==0 会被判 right，必须从候选里剔掉。"""
    zero = task({"template": "det",
                 "params": {"matrix": [[1, 2], [2, 4]]}})
    s = SimStudent("weak", seed=2)
    wrong = s._wrong_literals(zero)
    assert "0" not in wrong
    assert all(verdict(zero, w) == "wrong" for w in wrong)


def test_same_seed_is_reproducible():
    t = task()
    a = [SimStudent("mid", seed=42).answer(t, 0) for _ in range(3)]
    b = [SimStudent("mid", seed=42).answer(t, 0) for _ in range(3)]
    assert a == b
    s1, s2 = SimStudent("mid", seed=42), SimStudent("mid", seed=42)
    assert [s1.answer(t, 0) for _ in range(20)] == \
           [s2.answer(t, 0) for _ in range(20)]


def test_giveup_rate_returns_none():
    s = SimStudent("mid", seed=42, giveup_rate=1.0)
    assert all(s.answer(task(), 0) is None for _ in range(5))


def test_bad_inputs():
    with pytest.raises(ValueError, match="档位"):
        SimStudent("genius")
    with pytest.raises(ValueError, match="seed"):
        SimStudent("mid", seed="42")
    with pytest.raises(ValueError, match="giveup_rate"):
        SimStudent("mid", giveup_rate=1.5)
    with pytest.raises(ValueError, match="hint_level"):
        SimStudent("mid").correct_probability(5)
