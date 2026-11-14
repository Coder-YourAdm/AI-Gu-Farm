"""schedule.py 的回归：间隔公式、到期过滤、时区混合、按 KC 隔离。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.student.schedule import (
    MAX_EASE,
    MIN_EASE,
    due_items,
    item_after,
    plan_items,
)

T0 = "2026-11-10T09:00:00+08:00"


def _ev(t, kc, correct):
    return {"t": t, "kc": kc, "correct": correct}


# ---------------------------------------------------------------------------
# item_after
# ---------------------------------------------------------------------------

def test_correct_grows_interval_and_ease():
    interval, ease = item_after(True, 1.0, 2.5)
    assert interval == pytest.approx(4.25)
    assert ease == pytest.approx(2.55)


def test_correct_twice_grows_again():
    i1, e1 = item_after(True, 1.0, 2.5)
    i2, e2 = item_after(True, i1, e1)
    assert i2 > i1
    assert e2 > e1


def test_wrong_resets_interval_and_lowers_ease():
    interval, ease = item_after(False, 10.0, 2.5)
    assert interval == 1.0
    assert ease == pytest.approx(2.3)


def test_ease_bounds():
    ease = 2.5
    for _ in range(20):
        _, ease = item_after(True, 1.0, ease)
    assert ease <= MAX_EASE
    ease = 2.5
    for _ in range(20):
        _, ease = item_after(False, 1.0, ease)
    assert ease >= MIN_EASE


def test_interval_never_below_one():
    assert item_after(True, 0.0, 1.3)[0] == 1.0


def test_item_after_rejects_bad_input():
    for bad in (1, 0, None, "true"):
        with pytest.raises(ValueError, match="correct"):
            item_after(bad, 1.0, 2.5)
    with pytest.raises(ValueError, match="interval"):
        item_after(True, -1.0, 2.5)
    with pytest.raises(ValueError, match="ease"):
        item_after(True, 1.0, "2.5")


# ---------------------------------------------------------------------------
# due_items
# ---------------------------------------------------------------------------

def test_due_items_empty_events():
    assert due_items([], T0) == []


def test_wrong_answer_due_next_day():
    events = [_ev("2026-11-28T09:00:00+08:00", "poly", False)]
    out = due_items(events, "2026-11-29T09:00:00+08:00")
    assert len(out) == 1
    assert out[0]["kc"] == "poly"
    assert out[0]["interval"] == 1.0
    assert out[0]["reason"].startswith("上次答错")


def test_not_due_before_date():
    events = [_ev("2026-11-28T09:00:00+08:00", "poly", True)]
    # 答对后间隔 4.25 天，第二天还不该复习
    assert due_items(events, "2026-11-29T09:00:00+08:00") == []
    assert len(due_items(events, "2026-12-10T09:00:00+08:00")) == 1


def test_multiple_kcs_are_independent():
    # 同一天、不同判定：答错的立刻到期，答对的要到 4.25 天后
    events = [
        _ev("2026-11-28T09:00:00+08:00", "poly", False),
        _ev("2026-11-28T09:00:00+08:00", "det", True),
    ]
    out = due_items(events, "2026-11-29T09:00:00+08:00")
    assert [r["kc"] for r in out] == ["poly"]


def test_due_items_sorted_by_due():
    events = [
        _ev("2026-11-01T09:00:00+08:00", "det", False),
        _ev("2026-11-02T09:00:00+08:00", "poly", False),
    ]
    out = due_items(events, "2026-12-01T09:00:00+08:00")
    assert [r["kc"] for r in out] == ["det", "poly"]


def test_due_keeps_local_timezone():
    events = [_ev(T0, "det", False)]
    out = due_items(events, "2026-11-12T09:00:00+08:00")
    assert out[0]["due"].endswith("+08:00")
    assert out[0]["due"] == "2026-11-11T09:00:00+08:00"


def test_mixed_timezones_compare_correctly():
    # 同一时刻的两种写法：+08:00 与 Z
    events = [_ev("2026-11-10T01:00:00+00:00", "det", False)]
    out = due_items(events, datetime(2026, 11, 11, 1, 1, tzinfo=timezone.utc))
    assert len(out) == 1
    assert out[0]["due"].endswith("+00:00")


def test_streak_counts_only_trailing_correct():
    events = [
        _ev("2026-11-01T09:00:00+08:00", "det", False),
        _ev("2026-11-02T09:00:00+08:00", "det", True),
        _ev("2026-11-03T09:00:00+08:00", "det", True),
    ]
    out = due_items(events, "2026-12-20T09:00:00+08:00")
    assert out[0]["streak"] == 2


def test_due_items_accepts_datetime_now():
    events = [_ev(T0, "det", False)]
    now = datetime.fromisoformat("2026-11-12T09:00:00+08:00")
    assert len(due_items(events, now)) == 1


def test_due_items_rejects_bad_events():
    with pytest.raises(ValueError, match="列表"):
        due_items("not a list", T0)
    with pytest.raises(ValueError, match="kc"):
        due_items([{"t": T0, "correct": True}], T0)
    with pytest.raises(ValueError, match="correct"):
        due_items([{"t": T0, "kc": "det", "correct": 1}], T0)
    with pytest.raises(ValueError, match="ISO"):
        due_items([{"t": "昨天", "kc": "det", "correct": True}], T0)


# ---------------------------------------------------------------------------
# plan_items（未来复习计划，不过滤是否到期）
# ---------------------------------------------------------------------------

def test_plan_items_includes_future_due_that_due_items_filters_out():
    """契约 §4.3 的 review_plan 是「下次什么时候复习」，不是「今天到期」。"""
    events = [_ev("2026-11-28T09:00:00+08:00", "poly", True)]
    assert due_items(events, "2026-11-29T09:00:00+08:00") == []
    plan = plan_items(events)
    assert len(plan) == 1
    assert plan[0]["due"] == "2026-12-02T15:00:00+08:00"


def test_plan_items_covers_every_kc_and_is_sorted():
    events = [
        _ev("2026-11-01T09:00:00+08:00", "det", False),
        _ev("2026-11-02T09:00:00+08:00", "poly", False),
        _ev("2026-11-03T09:00:00+08:00", "gamma", True),
    ]
    plan = plan_items(events)
    assert len(plan) == 3
    assert plan == sorted(plan, key=lambda r: (r["due"], r["kc"]))
    assert plan_items([]) == []


def test_plan_items_shares_fold_logic_with_due_items():
    """同一个事件的 due/interval 在两个 API 里必须完全一致（只有一份公式）。"""
    events = [_ev("2026-11-01T09:00:00+08:00", "det", False)]
    far = "2026-12-20T09:00:00+08:00"
    a = due_items(events, far)[0]
    b = plan_items(events)[0]
    for key in ("due", "interval", "ease", "streak", "reason"):
        assert a[key] == b[key]
