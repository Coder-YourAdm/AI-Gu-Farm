# -*- coding: utf-8 -*-
"""附录 A 的 24 个核心用例：逐条独立已知答案。

所有期望值都来自规格附录 A（手算或独立推导），**不是**先跑 solve_exact()
再把输出贴回来——那样只能证明代码等于自己。

覆盖：I1–I8（积分）、D1–D8（行列式）、S1/S2/S3/S5/S6（系统契约）。
未覆盖：S4（需要 dedup_set）、S7（需要出题重做机制）、S8 的 RNG 部分
（需要快照），这三条等 B 的 orchestrator 接上后在 test_contracts.py 补。
"""

import pytest

from app.executor import (
    judge,
    normalize_task,
    parse_fraction,
    signature,
    solver_view,
    solve_exact,
    validate_task,
)


def _task(template, params, *, task_id="T1", source="evolved", by=None, gen=0):
    return normalize_task(
        {"template": template, "params": params},
        task_id=task_id, source=source, by=by, gen=gen,
    )


# --------------------------------------------------------------------------
# A.1 积分 8 条
# --------------------------------------------------------------------------

def test_I1_poly_rational():
    t = _task("poly", {"coeffs": [1, 1], "lower": 0, "upper": 1})
    assert t["reference"] == {"kind": "rational", "numerator": "3", "denominator": "2"}
    assert t["statement"] == "计算定积分 ∫_{0}^{1} (x + 1) dx。"


def test_I2_poly_integer_encoding():
    # F(x)=3x-x^2+x^3/3; F(2)-F(-1) = 14/3 + 13/3 = 9
    t = _task("poly", {"coeffs": [3, -2, 1], "lower": -1, "upper": 2})
    assert t["reference"] == {"kind": "integer", "value": "9"}
    assert t["statement"] == "计算定积分 ∫_{-1}^{2} (x² - 2x + 3) dx。"


def test_I3_poly_irreducible():
    t = _task("poly", {"coeffs": [0, 0, 5], "lower": 1, "upper": 2})
    assert t["reference"] == {"kind": "rational", "numerator": "35", "denominator": "3"}
    assert t["statement"] == "计算定积分 ∫_{1}^{2} (5x²) dx。"


def test_I4_gamma_basic():
    t = _task("gamma", {"n": 1, "rate": 2, "c": 1})
    assert t["reference"] == {"kind": "rational", "numerator": "1", "denominator": "4"}
    assert t["statement"] == "计算定积分 ∫₀^∞ x e^{-2x} dx。"


def test_I5_gamma_denominator_upper_bound():
    # 5*9! = 1814400, 8^10 = 1073741824 -> 约分 14175/8388608
    t = _task("gamma", {"n": 9, "rate": 8, "c": 5})
    assert t["reference"] == {
        "kind": "rational", "numerator": "14175", "denominator": "8388608"}
    assert t["statement"] == "计算定积分 ∫₀^∞ 5x⁹ e^{-8x} dx。"


def test_I6_gaussian_k0():
    t = _task("gaussian_odd", {"k": 0, "rate": 2, "c": 3})
    assert t["reference"] == {"kind": "rational", "numerator": "3", "denominator": "4"}
    assert t["statement"] == "计算定积分 ∫₀^∞ 3x e^{-2x²} dx。"


def test_I7_gaussian_negative_coefficient():
    # -2*120/(2*1) = -120，负号不能丢
    t = _task("gaussian_odd", {"k": 5, "rate": 1, "c": -2})
    assert t["reference"] == {"kind": "integer", "value": "-120"}
    assert t["statement"] == "计算定积分 ∫₀^∞ -2x¹¹ e^{-1x²} dx。"


@pytest.mark.parametrize("raw", [
    {"template": "gamma", "params": {"n": 3, "rate": 0, "c": 1}},
    {"template": "gamma", "params": {"n": 0, "rate": 2, "c": 1}},
    {"template": "gamma", "params": {"n": 3, "rate": 2, "c": 0}},
    {"template": "eigen", "params": {}},
    {"template": "poly", "params": {"coeffs": [1, 0], "lower": 0, "upper": 1}},
    {"template": "poly", "params": {"coeffs": [1, 1], "lower": 0, "upper": 0}},
])
def test_I8_rejects(raw):
    ok, reason = validate_task(raw)
    assert not ok, "应拒绝：%r" % (raw,)
    assert reason


# --------------------------------------------------------------------------
# A.2 行列式 8 条
# --------------------------------------------------------------------------

def _det(m):
    return solve_exact({"template": "det", "params": {"matrix": m}})


def test_D1():
    assert _det([[2, 1], [1, 3]]) == {"kind": "integer", "value": "5"}


def test_D2():
    assert _det([[1, 2, 3], [4, 5, 6], [7, 8, 10]]) == {"kind": "integer", "value": "-3"}


def test_D3_singular():
    assert _det([[1, 2], [2, 4]]) == {"kind": "integer", "value": "0"}


def test_D4():
    assert _det([[2, 1, 0], [1, 3, 1], [0, 1, 2]]) == {"kind": "integer", "value": "8"}


def test_D5_upper_triangular():
    assert _det([[3, 1, 2], [0, -2, 5], [0, 0, 7]]) == {"kind": "integer", "value": "-42"}


def test_D6_row_swap():
    assert _det([[0, 1], [1, 0]]) == {"kind": "integer", "value": "-1"}


def test_D7_zero_pivots():
    assert _det([[0, 2, 1], [3, 0, 1], [4, 5, 0]]) == {"kind": "integer", "value": "23"}


@pytest.mark.parametrize("raw", [
    {"template": "det", "params": {"matrix": [[1, 2, 3], [4, 5, 6]]}},
    {"template": "det", "params": {"matrix": [[2.0, 1], [1, 3]]}},
    {"template": "det", "params": {"matrix": [[True, 1], [1, 3]]}},
    {"template": "det", "params": {"matrix": [[5]]}},
])
def test_D8_rejects(raw):
    ok, reason = validate_task(raw)
    assert not ok, "应拒绝：%r" % (raw,)
    assert reason


# --------------------------------------------------------------------------
# A.3 系统契约（S4 / S7 / S8-RNG 待补）
# --------------------------------------------------------------------------

def _integral_task():
    return _task("poly", {"coeffs": [3, -2, 1], "lower": -1, "upper": 2})   # 9


def test_S1_oversized_response_rejected_before_json():
    # 9000 字符 > MAX_RESPONSE_CHARS：必须在 json.loads 之前就拒
    assert judge(_integral_task(), "x" * 9000) == "unanswered"


def test_S1_oversized_never_reaches_json_loads(monkeypatch):
    """规格要求用 mock 计数确认未调用 json.loads——只看返回值不够。"""
    import json as json_module

    calls = []
    real = json_module.loads

    def spy(text, *args, **kwargs):
        calls.append(len(text))
        return real(text, *args, **kwargs)

    monkeypatch.setattr(json_module, "loads", spy)
    assert judge(_integral_task(), "x" * 9000) == "unanswered"
    assert calls == [], "超长响应不应进入 json.loads，实际调用 %r" % (calls,)


@pytest.mark.parametrize("answer", ['{"value":"sqrt(4)"}',
                                    '{"value":"1; import os"}',
                                    '{"value":"1/0"}'])
def test_S2_illegal_forms_are_unanswered(answer):
    assert judge(_integral_task(), answer) == "unanswered"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e31", "9" * 65])
def test_S3_numeric_limits(value):
    assert judge(_integral_task(), '{"value":"%s"}' % value) == "unanswered"


def test_S5_interval_enters_signature():
    a = signature({"template": "poly", "params": {"coeffs": [1, 1], "lower": 0, "upper": 1}})
    b = signature({"template": "poly", "params": {"coeffs": [1, 1], "lower": 1, "upper": 2}})
    assert a != b


def test_S6_solver_view_whitelist():
    t = _integral_task()
    view = solver_view(t)
    assert set(view) == {"type", "statement", "answer_format"}
    for leaked in ("reference", "template", "params", "signature", "by", "gen", "id"):
        assert leaked not in view
    assert t["statement"] == view["statement"]


def test_parse_fraction_rejects_bare_integer():
    """规格的 fraction 分支是 `a/b`，纯整数必须走 integer 分支。"""
    with pytest.raises(ValueError):
        parse_fraction("5")


@pytest.mark.parametrize("reference", [
    {"kind": "weird"},                                          # kind 不认识
    {"kind": "integer"},                                        # 缺 value
    {"kind": "integer", "value": "3.5"},                        # value 不是整数
    {"kind": "integer", "value": True},                         # value 是 bool
    {"kind": "rational", "numerator": "1"},                     # 缺 denominator
    {"kind": "rational", "denominator": "2"},                   # 缺 numerator
    {"kind": "rational", "numerator": "1", "denominator": "0"}, # 分母为 0
    {"kind": "rational", "numerator": "1", "denominator": "x"}, # 分母非数字
    None,                                                       # 整体不是 dict
    "integer|1",                                                # 整体不是 dict
])
def test_broken_reference_raises_value_error(reference):
    """reference 的结构问题一律抛 ValueError。

    不能漏成 KeyError（缺字段）或 ZeroDivisionError（分母 0）——B 侧只按
    ValueError 兜底，漏出去会冒到主循环把整趟跑挂。
    """
    with pytest.raises(ValueError):
        judge({"type": "integral", "reference": reference}, '{"value":"1"}')


def test_judge_three_states():
    t = _integral_task()
    assert judge(t, '{"value":"9"}') == "right"
    assert judge(t, '{"value":"18/2"}') == "right"
    assert judge(t, '{"value":"9.0"}') == "right"
    assert judge(t, '{"value":"10"}') == "wrong"
    assert judge(t, None) == "unanswered"
    assert judge(t, "答案是 9") == "unanswered"


def test_det_accepts_integer_only():
    t = _task("det", {"matrix": [[2, 1], [1, 3]]})     # 5
    assert judge(t, '{"value":"5"}') == "right"
    assert judge(t, '{"value":"5/1"}') == "unanswered"
    assert judge(t, '{"value":"5.0"}') == "unanswered"
