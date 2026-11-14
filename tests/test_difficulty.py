"""difficulty.py 的回归：重点是「刻度没退化」这件事。

如果有人照抄通用 Elo 的 /400 公式，E 会恒等于 0.5，所有测试看起来
还能过一半，但难度校准其实死了。下面第一条就是专门钉住这个坑。
"""
from __future__ import annotations

import random

import pytest

from app.student.difficulty import (
    D_MAX,
    D_MIN,
    DifficultyBook,
    PRIOR_HI,
    PRIOR_LO,
    expected,
    update_rating,
)


def _task(tpl="poly", **params):
    base = {
        "poly": {"coeffs": [1, 1], "lower": 0, "upper": 1},
        "gamma": {"c": 1, "n": 2, "rate": 2},
        "det": {"matrix": [[2, 1], [1, 3]]},
    }
    return {"template": tpl, "params": params or base[tpl]}


# ---------------------------------------------------------------------------
# 刻度
# ---------------------------------------------------------------------------

def test_expected_is_not_flat_half():
    """ hardest guard：难度变化必须真的改变 E，不能恒为 0.5。"""
    assert expected(0.5, 0.5) == pytest.approx(0.5)
    assert expected(0.5, 0.8) < 0.4
    assert expected(0.5, 0.2) > 0.6


def test_expected_monotone_in_difficulty_and_ability():
    assert expected(0.5, 0.3) > expected(0.5, 0.5) > expected(0.5, 0.7)
    assert expected(0.7, 0.5) > expected(0.5, 0.5) > expected(0.3, 0.5)


def test_expected_rejects_out_of_range():
    for bad in (-0.1, 1.1, "0.5", None, True):
        with pytest.raises(ValueError):
            expected(bad, 0.5)
        with pytest.raises(ValueError):
            expected(0.5, bad)


def test_update_rating_moves_in_right_direction():
    assert update_rating(0.5, 0.5, True) > 0.5
    assert update_rating(0.5, 0.5, False) < 0.5


def test_update_rating_step_is_small():
    """一次答题最多动 k/400 = 0.04，避免一步跳爆。"""
    for correct in (True, False):
        assert abs(update_rating(0.5, 0.5, correct) - 0.5) <= 0.04 + 1e-9


def test_update_rating_stays_in_bounds():
    r = 0.5
    for _ in range(50):
        r = update_rating(r, 0.5, True)
        assert D_MIN <= r <= D_MAX
    assert r <= D_MAX


def test_update_rating_rejects_bad_input():
    with pytest.raises(ValueError, match="correct"):
        update_rating(0.5, 0.5, 1)
    with pytest.raises(ValueError, match="k"):
        update_rating(0.5, 0.5, True, k=0)


# ---------------------------------------------------------------------------
# DifficultyBook
# ---------------------------------------------------------------------------

def test_prior_is_stable_and_in_range():
    a, b = DifficultyBook(), DifficultyBook()
    t = _task()
    assert PRIOR_LO <= a.prior(t) <= PRIOR_HI
    assert a.prior(t) == b.prior(t)          # 跨实例一致，不依赖 RNG


def test_prior_differs_across_tasks():
    vals = [DifficultyBook().prior(_task(t)) for t in ("poly", "gamma", "det")]
    assert len(set(vals)) == 3


def test_get_equals_prior_before_any_update():
    book = DifficultyBook()
    t = _task()
    assert book.get(t) == book.prior(t)


def test_update_lowers_difficulty_on_correct_raises_on_wrong():
    t = _task()
    d0 = DifficultyBook().get(t)

    b1 = DifficultyBook()
    assert b1.update(t, 0.5, True) < d0      # 答对了 → 这题没那么难

    b2 = DifficultyBook()
    assert b2.update(t, 0.5, False) > d0     # 答错了 → 这题更难


def test_book_persists_to_disk(tmp_path):
    path = tmp_path / "difficulty.json"
    t = _task()
    b = DifficultyBook(path)
    new = b.update(t, 0.5, True)
    assert path.exists()
    assert DifficultyBook(path).get(t) == pytest.approx(new)


def test_book_rejects_corrupt_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        DifficultyBook(path)


def test_pick_chooses_nearest_to_theta_plus_offset():
    book = DifficultyBook()
    tasks = [_task(t) for t in ("poly", "gamma", "det")]
    theta = 0.5
    chosen = book.pick(tasks, theta, offset=0.1)
    target = theta + 0.1
    assert abs(book.get(chosen) - target) == min(
        abs(book.get(t) - target) for t in tasks
    )


def test_pick_is_reproducible_without_rng():
    tasks = [_task(t) for t in ("poly", "gamma", "det")]
    assert (DifficultyBook().pick(tasks, 0.5)["template"]
            == DifficultyBook().pick(tasks, 0.5)["template"])


def test_pick_rng_changes_choice_only_among_ties():
    # 用同一道题复制成多个等价候选，签名相同 → 结果必须一样（没有假随机）
    tasks = [_task() for _ in range(4)]
    a = DifficultyBook().pick(tasks, 0.5)
    b = DifficultyBook().pick(tasks, 0.5, rng=random.Random(42))
    assert a == b


def test_pick_empty_candidates_raises():
    with pytest.raises(ValueError, match="为空"):
        DifficultyBook().pick([], 0.5)


def test_sig_requires_identifiable_task():
    with pytest.raises(ValueError, match="signature"):
        DifficultyBook().get({"foo": 1})
