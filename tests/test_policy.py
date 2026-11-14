"""三套教法 preset 的回归：它们是 3×3 实验里唯一的自变量。"""
from __future__ import annotations

import pytest

from app.tutor.policy import (
    POLICIES, Policy, get_policy, hint_weight, list_policies,
)


def test_three_presets_are_frozen():
    assert list_policies() == ("loose", "verbose", "answer_only")
    assert set(POLICIES) == {"loose", "verbose", "answer_only"}


@pytest.mark.parametrize("name,levels", [
    ("loose", (0, 1)),
    ("verbose", (0, 1, 2, 3)),
    ("answer_only", (4,)),
])
def test_hint_levels(name, levels):
    assert get_policy(name).hint_levels() == levels


def test_answer_giving_costs_more_than_a_nudge():
    """给完整答案（level 4）一次，必须贵过任何单级低提示。"""
    assert hint_weight(4) > sum(hint_weight(l)
                                for l in get_policy("loose").hint_levels())
    assert hint_weight(4) > max(hint_weight(l)
                                for l in get_policy("verbose").hint_levels())


def test_hint_weight_is_level_plus_one_and_bounded():
    assert [hint_weight(i) for i in range(5)] == [1, 2, 3, 4, 5]
    for bad in (-1, 5, 1.0, True, "2"):
        with pytest.raises(ValueError):
            hint_weight(bad)


def test_get_policy_rejects_unknown():
    with pytest.raises(ValueError, match="未知教法"):
        get_policy("magic")


def test_policy_validation():
    with pytest.raises(ValueError, match="非负整数"):
        Policy("x", "x", -1, 1, 2, "x")
    with pytest.raises(ValueError, match="max_hints"):
        Policy("x", "x", 0, 1, 0, "x")
    # 级别靠 min(4, …) 封顶，step 再大也不会越界
    assert Policy("x", "x", 4, 4, 3, "x").hint_levels() == (4, 4, 4)


def test_policy_is_immutable():
    with pytest.raises(Exception):
        get_policy("loose").max_hints = 9
