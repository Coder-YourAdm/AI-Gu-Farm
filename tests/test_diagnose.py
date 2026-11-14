"""错因诊断回归：六条判据必须按序命中，且不许退化成第二套判分。

最要紧的两条：
- 判据 4 若写成「差值是整数」，会把判据 5 的 det/misread 全吃掉；
- 判对错只能由 judge 决定，classify 只负责分类。
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import executor  # noqa: E402
from app.student import kc  # noqa: E402
from app.student.diagnose import (  # noqa: E402
    ERR_KINDS, ERR_LABELS, classify, next_task, why,
)
from app.student.difficulty import DifficultyBook  # noqa: E402


def task(raw: dict) -> dict:
    return executor.normalize_task(
        raw, task_id="T", source="test", by=None, gen=None)


def ans(value: str) -> str:
    return json.dumps({"value": value}, ensure_ascii=False)


POLY_9 = task({"template": "poly",
               "params": {"coeffs": [3, -2, 1], "lower": -1, "upper": 2}})
FRAC_35_3 = task({"template": "poly",
                  "params": {"coeffs": [0, 0, 5], "lower": 1, "upper": 2}})
GAMMA_3_4 = task({"template": "gamma",
                  "params": {"c": 1, "n": 4, "rate": 2}})
DET_5 = task({"template": "det", "params": {"matrix": [[2, 1], [1, 3]]}})


def test_err_kinds_are_frozen():
    assert ERR_KINDS == ("sign", "calc", "concept", "misread", "giveup", "none")
    assert set(ERR_LABELS) == set(ERR_KINDS)


@pytest.mark.parametrize("t,a,expected", [
    (POLY_9, ans("9"), "none"),                 # 答对
    (POLY_9, None, "giveup"),                   # 没答
    (POLY_9, "", "giveup"),
    (POLY_9, "sqrt(4)", "giveup"),               # 非法 JSON
    (POLY_9, ans("9^2"), "giveup"),              # 表达式不是字面量
    (POLY_9, json.dumps({"value": 9}), "giveup"),  # value 不是字符串
    (POLY_9, ans("-9"), "sign"),                 # 2 符号
    (FRAC_35_3, ans("34/3"), "calc"),           # 3 同分母分子差 1
    (GAMMA_3_4, ans("3/2"), "calc"),            # 4 差 2 倍
    (DET_5, ans("7"), "misread"),                # 5 det 差偶数
    (DET_5, ans("8"), "concept"),                # det 差奇数 -> 不是 misread
    (POLY_9, ans("12345"), "concept"),           # 6 其余
])
def test_six_rules_in_order(t, a, expected):
    assert classify(t, a) == expected


def test_sign_rule_needs_nonzero_reference():
    """ref=0 时 -ref == ref，不能误判成符号错。"""
    zero = task({"template": "det",
                 "params": {"matrix": [[1, 2], [2, 4]]}})
    assert executor._ref_fraction(zero["reference"]) == Fraction(0)
    assert classify(zero, ans("3")) == "concept"


def test_decimal_answer_is_classified_exactly():
    """十进制也必须走 Fraction 比较，不能因为浮点误差落到 concept。"""
    assert classify(GAMMA_3_4, ans("1.5")) == "calc"    # 3/2，差 2 倍
    assert classify(GAMMA_3_4, ans("0.75")) == "none"  # 判对


def test_broken_reference_raises_value_error():
    """参考值损坏属于数据故障，不能伪装成一次答错。"""
    broken = dict(POLY_9)
    broken["reference"] = {"kind": "integer"}
    with pytest.raises(ValueError, match="缺 value"):
        classify(broken, ans("8"))


def test_classify_never_contradicts_judge():
    """classify 返回 none 当且仅当 judge 判 right。"""
    for t, a in [(POLY_9, ans("9")), (POLY_9, ans("8")),
                 (GAMMA_3_4, ans("0.75")), (DET_5, ans("7"))]:
        right = executor.judge(t, a) == "right"
        assert (classify(t, a) == "none") == right


# ── next_task ────────────────────────────────────────────────────────


def _pool():
    return [POLY_9, FRAC_35_3, GAMMA_3_4, DET_5]


def test_next_task_picks_weakest_kc():
    book = DifficultyBook()
    mastery = {"integral_definite": 0.31, "integral_improper": 0.64,
               "det": 0.78}
    chosen = next_task(_pool(), mastery, book)
    assert kc.kc_of(chosen) == "integral_definite"


def test_next_task_falls_back_when_weakest_has_no_task():
    """最弱 KC 没题可出时必须退回全池，不能卡住。"""
    book = DifficultyBook()
    mastery = {"integral_definite": 0.2, "det": 0.9}
    chosen = next_task([DET_5], mastery, book)
    assert chosen is DET_5


def test_next_task_global_when_not_weakest_first():
    book = DifficultyBook()
    mastery = {"integral_definite": 0.2, "det": 0.9}
    chosen = next_task(_pool(), mastery, book, weakest_first=False)
    assert chosen in _pool()


def test_next_task_rejects_bad_inputs():
    book = DifficultyBook()
    with pytest.raises(ValueError, match="非空"):
        next_task([], {"det": 0.5}, book)
    with pytest.raises(ValueError, match="mastery"):
        next_task(_pool(), {}, book)
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        next_task(_pool(), {"det": 1.5}, book)
    with pytest.raises(ValueError, match="bool"):
        next_task(_pool(), {"det": 0.5}, book, weakest_first=1)


# ── why ──────────────────────────────────────────────────────────────


def test_why_three_numbers_are_real():
    book = DifficultyBook()
    mastery = {"integral_definite": 0.31, "integral_improper": 0.64,
               "det": 0.78}
    out = why(POLY_9, mastery, book, "calc", ["evt_0031", "evt_0044"])
    assert out["mastery"] == 0.31
    assert out["difficulty"] == pytest.approx(book.get(POLY_9), abs=1e-4)
    assert out["gap"] == pytest.approx(out["difficulty"] - out["mastery"],
                                       abs=1e-4)
    assert out["last_error"] == ERR_LABELS["calc"]
    assert out["memory"] == ["evt_0031", "evt_0044"]


def test_why_accepts_plain_text_and_none():
    book = DifficultyBook()
    mastery = {"integral_definite": 0.4, "integral_improper": 0.6, "det": 0.7}
    assert why(POLY_9, mastery, book, None, [])["last_error"] == "本知识点暂无错题记录"
    assert why(POLY_9, mastery, book, "换元后忘记换上下限", [])["last_error"] \
        == "换元后忘记换上下限"


def test_why_rejects_missing_kc():
    book = DifficultyBook()
    with pytest.raises(ValueError, match="缺知识点"):
        why(POLY_9, {"det": 0.8}, book, None, [])
