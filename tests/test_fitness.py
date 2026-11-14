"""适应度公式回归：分工表 A8 的验收值必须逐字对上。"""
from __future__ import annotations

import pytest

from app.evalsim.fitness import cost, gain, score


def test_a8_acceptance_value():
    """`python -c "...print(round(score(0.4286,0.79),4))"` -> 0.2311"""
    assert round(score(0.4286, 0.79), 4) == 0.2311


def test_gain_formula_and_boundaries():
    assert round(gain(0.3, 0.6), 4) == 0.4286
    assert gain(0.0, 1.0) == 1.0
    assert gain(1.0, 1.0) == 0.0          # 已经满分，没有提升空间
    assert gain(0.8, 0.8) == 0.0
    assert gain(0.2, 0.1) < 0.0           # 退步也算（不截断）


def test_cost_formula():
    """徐潇订正的这组数：20 题 14 次提示 -> 0.790。"""
    assert round(cost(20, 14), 4) == 0.790
    assert cost(0, 0) == 0.0
    assert round(cost(10, 5), 4) == 0.325


def test_score_is_gain_minus_quarter_cost():
    g, c = 0.412, 0.790
    assert score(g, c) == pytest.approx(g - 0.25 * c)
    assert score(g, c, weight=0.5) == pytest.approx(g - 0.5 * c)


def test_bad_inputs_rejected():
    for bad in (None, "0.3", True):
        with pytest.raises(ValueError):
            gain(bad, 0.5)
        with pytest.raises(ValueError):
            score(0.3, bad)
    for bad in (1.5, -0.1):
        with pytest.raises(ValueError):
            gain(bad, 0.5)
    with pytest.raises(ValueError, match="不能为负"):
        cost(-1, 2)
    with pytest.raises(ValueError, match="正"):
        cost(1, 1, base_items=0)
    with pytest.raises(ValueError, match="weight"):
        score(0.3, 0.5, weight=-1)
