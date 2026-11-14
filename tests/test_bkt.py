"""bkt.py 的回归：公式可手算复核、顺序有关、bool 坑要挡住。

三个对照数来自 `docs/AI可读_实施方案.md` §6.1（已实算核对），
不要现场重推，也不要因为改了阈值就顺手改这里——那等于把尺子改短。
"""
from __future__ import annotations

import pytest

from app.student.bkt import (
    BKTParams,
    DEFAULT,
    LEVEL_THRESHOLDS,
    level,
    mastery_of,
    update,
)


# ---------------------------------------------------------------------------
# 公式
# ---------------------------------------------------------------------------

def test_update_correct_from_init():
    assert update(0.20, True) == pytest.approx(0.5526, abs=1e-3)


def test_update_wrong_from_init():
    assert update(0.20, False) == pytest.approx(0.1774, abs=1e-3)


def test_update_twice_correct():
    assert update(update(0.20, True), True) == pytest.approx(0.8440, abs=1e-3)


def test_correct_raises_wrong_lowers():
    assert update(0.5, True) > 0.5
    assert update(0.5, False) < 0.5


def test_update_never_leaves_unit_interval():
    p = 0.2
    pattern = [True, False, False, True, True, False, True, True, False,
               True, True, True, False, False, True]
    for correct in pattern:
        p = update(p, correct)
        assert 0.0 <= p <= 1.0


def test_update_custom_params_are_used():
    custom = BKTParams(init=0.5, transit=0.0, slip=0.0, guess=0.0)
    # transit=0 且 slip=guess=0：答对后验为 1，答错后验为 0（纯逻辑推理）
    assert update(0.5, False, custom) == 0.0
    assert update(0.5, True, custom) == 1.0


def test_update_rejects_bad_input():
    for bad_p in (-0.1, 1.1, "0.5", None, True):
        with pytest.raises(ValueError):
            update(bad_p, True)
    for bad_c in (1, 0, None, "true"):
        with pytest.raises(ValueError, match="correct"):
            update(0.5, bad_c)


def test_bkt_params_validation():
    with pytest.raises(ValueError):
        BKTParams(init=1.5)
    with pytest.raises(ValueError):
        BKTParams(slip=0.6, guess=0.6)      # 越答错越像会，参数坏掉
    with pytest.raises(ValueError):
        BKTParams(init="0.2")
    assert BKTParams().init == 0.20


# ---------------------------------------------------------------------------
# 事件折叠
# ---------------------------------------------------------------------------

def test_mastery_of_empty_returns_init_not_zero():
    assert mastery_of("det", []) == 0.20


def test_mastery_of_filters_other_kc():
    events = [
        {"kc": "det", "correct": True},
        {"kc": "poly", "correct": False},
        {"kc": "det", "correct": True},
    ]
    assert mastery_of("det", events) == pytest.approx(0.8440, abs=1e-3)
    assert mastery_of("poly", events) == pytest.approx(0.1774, abs=1e-3)


def test_mastery_of_is_order_dependent():
    """BKT 与顺序有关，调用方不许 shuffle 事件流。"""
    a = [{"kc": "det", "correct": True}, {"kc": "det", "correct": False}]
    b = [{"kc": "det", "correct": False}, {"kc": "det", "correct": True}]
    assert mastery_of("det", a) != mastery_of("det", b)


def test_mastery_of_accepts_params_override():
    custom = BKTParams(init=0.9)
    assert mastery_of("det", [], custom) == 0.9
    assert mastery_of("det", [], {"det": custom}) == 0.9


def test_mastery_of_rejects_bad_events():
    with pytest.raises(ValueError, match="correct"):
        mastery_of("det", [{"kc": "det"}])
    with pytest.raises(ValueError, match="correct"):
        mastery_of("det", [{"kc": "det", "correct": 1}])
    with pytest.raises(ValueError):
        mastery_of("det", [{"kc": "det", "correct": True}], params="bad")


def test_default_has_all_kcs():
    assert set(DEFAULT) == {"integral_definite", "integral_improper", "det"}
    assert all(isinstance(v, BKTParams) for v in DEFAULT.values())


# ---------------------------------------------------------------------------
# 分级
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0.0, 0), (0.19, 0),
    (0.20, 1), (0.39, 1),
    (0.40, 2), (0.59, 2),
    (0.60, 3), (0.74, 3),
    (0.75, 4), (0.89, 4),
    (0.90, 5), (1.0, 5),
])
def test_level_six_bands(value, expected):
    assert level(value) == expected


def test_level_rejects_out_of_range():
    for bad in (-0.01, 1.01, None, "0.5", True):
        with pytest.raises(ValueError):
            level(bad)


def test_level_thresholds_are_exposed_and_sorted():
    assert LEVEL_THRESHOLDS == (0.20, 0.40, 0.60, 0.75, 0.90)
    assert list(LEVEL_THRESHOLDS) == sorted(LEVEL_THRESHOLDS)
