"""附录 A 16 条手算复核的可持续断言。

规格 §附录 A L602 的硬性要求：期望值必须由**手算或独立推导**得出，
禁止先跑 `solve()` 再回填。这个文件的职责是让"复核"这件事不会随对话结束而消失。

它验证的对象**不是 A 的代码**，而是**规格表格里那 14 个数字本身对不对**。
这是最容易被跳过、也最贵的一步：规格抄错一个数，A 照着实现就一起错，
而且错得很安静——跑到第 20 代才在末代排名里看出不对。

三条独立路径（见 `tests/recheck_hand_calc.py`）：
  闭式 Fraction 精确 / Laplace 递归 / Simpson 数值积分
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.recheck_hand_calc import (  # noqa: E402
    DET_CASES,
    GAMMA_CASES,
    GAUSS_CASES,
    INTEGRAL_CASES,
    SPEC_EXPECT,
    det_laplace,
    int_gamma,
    int_gaussian_odd,
    int_poly,
    num_gamma,
    num_gaussian_odd,
    num_poly,
)

REL_TOL = 1e-6


def _fmt(fr: Fraction) -> str:
    return str(fr.numerator) if fr.denominator == 1 else f"{fr.numerator}/{fr.denominator}"


# ── 积分 7 条 ──────────────────────────────────────────────────


@pytest.mark.parametrize("cid,coeffs,lo,hi", INTEGRAL_CASES)
def test_poly_matches_spec_and_numeric(cid, coeffs, lo, hi):
    exact = int_poly(coeffs, lo, hi)
    assert _fmt(exact) == SPEC_EXPECT[cid], f"{cid} 闭式与规格期望不符"
    assert num_poly(coeffs, lo, hi) == pytest.approx(float(exact), rel=REL_TOL), f"{cid} 数值路径偏离"


@pytest.mark.parametrize("cid,n,rate,c", GAMMA_CASES)
def test_gamma_matches_spec_and_numeric(cid, n, rate, c):
    exact = int_gamma(n, rate, c)
    assert _fmt(exact) == SPEC_EXPECT[cid], f"{cid} 闭式与规格期望不符"
    assert num_gamma(n, rate, c) == pytest.approx(float(exact), rel=REL_TOL), f"{cid} 数值路径偏离"


@pytest.mark.parametrize("cid,k,rate,c", GAUSS_CASES)
def test_gaussian_odd_matches_spec_and_numeric(cid, k, rate, c):
    exact = int_gaussian_odd(k, rate, c)
    assert _fmt(exact) == SPEC_EXPECT[cid], f"{cid} 闭式与规格期望不符"
    assert num_gaussian_odd(k, rate, c) == pytest.approx(float(exact), rel=REL_TOL), f"{cid} 数值路径偏离"


# ── 行列式 7 条 ────────────────────────────────────────────────


@pytest.mark.parametrize("cid,matrix", DET_CASES)
def test_det_matches_spec(cid, matrix):
    assert _fmt(det_laplace(matrix)) == SPEC_EXPECT[cid], f"{cid} Laplace 展开与规格期望不符"


# ── 三条路径之外的结构性确认 ────────────────────────────────────


def test_i5_denominator_reduction():
    """I5 是分母上限用例：1814400/1073741824 必须约到 14175/8388608。

    5·9! = 1814400 = 2⁷·14175，8¹⁰ = 2³⁰，约掉 2⁷ 后分母是 2²³ = 8388608。
    如果实现里忘了约分，数字仍然是同一个值，但分母会超出 64 位上限的检查口径。
    """
    raw = Fraction(5 * 362880, 8 ** 10)
    assert raw == Fraction(14175, 8388608)
    assert raw.denominator == 8388608
    assert 8388608 == 2 ** 23


def test_i2_passes_through_zero():
    """I2 的积分区间跨 0（先负后正），用来抓符号处理。

    F(x)=3x−x²+x³/3：F(2)=14/3，F(−1)=−13/3。
    区间积分 = F(2)−F(−1) = 14/3+13/3 = 9。

    注意区分：**F(−1)=−13/3 是原函数在 −1 处的值，不是 ∫_{−1}^{0}**。
    后者是 F(0)−F(−1)=+13/3。第一版这里写成 −13/3，被本测试抓出来了——
    这正是复核存在的意义，写复核的人自己也会错。
    """
    assert int_poly([3, -2, 1], -1, 2) == 9
    assert int_poly([3, -2, 1], 0, 2) == Fraction(14, 3)
    assert int_poly([3, -2, 1], -1, 0) == Fraction(13, 3)


def test_i7_keeps_negative_sign():
    """I7 负系数 −2、k=5：−2·5!/(2·1⁶) = −120。丢符号会得 +120。"""
    assert int_gaussian_odd(5, 1, -2) == -120


def test_d3_is_singular():
    """D3 奇异矩阵行列式为 0 —— 这是 Bareiss 主元全零的分支，最容易死循环。"""
    assert det_laplace([[1, 2], [2, 4]]) == 0


def test_d6_d7_need_row_swap():
    """D6 / D7 首元素为 0，必须换行且符号翻一次。

    不换行会直接 ZeroDivisionError；符号忘翻会得到 +1 / −23。
    """
    assert det_laplace([[0, 1], [1, 0]]) == -1
    assert det_laplace([[0, 2, 1], [3, 0, 1], [4, 5, 0]]) == 23


def test_d5_is_triangular():
    """D5 上三角：行列式 = 对角积 = 3·(−2)·7 = −42。"""
    m = [[3, 1, 2], [0, -2, 5], [0, 0, 7]]
    assert det_laplace(m) == -42
    assert 3 * -2 * 7 == -42
