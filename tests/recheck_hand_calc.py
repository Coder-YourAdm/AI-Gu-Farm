"""附录 A 的 16 条手算复核（C 独立版）。

为什么不能直接用 A 的 `solve_exact()`
--------------------------------------
用被测代码验证被测代码，只能证明「代码等于自己」。所以这里**不 import app/ 里任何东西**，
三条互相独立的路径各自算一遍：

  路径 1  手算推导        写在 `复核表_16条手算.md`，评委能看懂
  路径 2  闭式公式        Fraction 精确运算；行列式用 **Laplace 递归**（A 用 Bareiss，算法不同）
  路径 3  数值积分        Simpson 法则，**不用任何闭式**，纯浮点近似

三条路径全部一致，才在文档里记「确认」。任何一条对不上，说明有东西错了——包括
规格本身可能抄错，那才是最贵的错误。

    用法： python tests/recheck_hand_calc.py
"""

from __future__ import annotations

import math
import sys
from fractions import Fraction


# ── 路径 2a：积分闭式（Fraction 精确） ──────────────────────────

def int_poly(coeffs: list[int], lo: int, hi: int) -> Fraction:
    """∫_lo^hi Σ aᵢxⁱ dx = Σ aᵢ(u^{i+1} − l^{i+1})/(i+1)"""
    total = Fraction(0)
    for i, c in enumerate(coeffs):
        total += Fraction(c) * (Fraction(hi) ** (i + 1) - Fraction(lo) ** (i + 1)) / (i + 1)
    return total


def int_gamma(n: int, rate: int, c: int) -> Fraction:
    """∫₀^∞ c·xⁿ·e^{−rate·x} dx = c·n! / rate^{n+1}"""
    return Fraction(c) * math.factorial(n) / Fraction(rate) ** (n + 1)


def int_gaussian_odd(k: int, rate: int, c: int) -> Fraction:
    """∫₀^∞ c·x^{2k+1}·e^{−rate·x²} dx = c·k! / (2·rate^{k+1})"""
    return Fraction(c) * math.factorial(k) / (2 * Fraction(rate) ** (k + 1))


# ── 路径 2b：行列式，Laplace 递归（与 Bareiss 不同算法） ─────────

def det_laplace(m: list[list[int]]) -> Fraction:
    n = len(m)
    if n == 1:
        return Fraction(m[0][0])
    if n == 2:
        return Fraction(m[0][0] * m[1][1] - m[0][1] * m[1][0])
    total = Fraction(0)
    for j in range(n):
        minor = [row[:j] + row[j + 1:] for row in m[1:]]
        total += Fraction((-1) ** j * m[0][j]) * det_laplace(minor)
    return total


# ── 路径 3：数值积分（Simpson，不碰闭式） ───────────────────────

def _simpson(f, a: float, b: float, n: int = 400_000) -> float:
    if n % 2:
        n += 1
    h = (b - a) / n
    s = f(a) + f(b)
    for i in range(1, n):
        s += (4 if i % 2 else 2) * f(a + i * h)
    return s * h / 3


def num_gamma(n: int, rate: int, c: int, xmax: float = 60.0) -> float:
    f = lambda t: c * (t ** n) * math.exp(-rate * t)  # noqa: E731
    return _simpson(f, 0.0, xmax)


def num_gaussian_odd(k: int, rate: int, c: int, xmax: float = 12.0) -> float:
    f = lambda t: c * (t ** (2 * k + 1)) * math.exp(-rate * t * t)  # noqa: E731
    return _simpson(f, 0.0, xmax)


def num_poly(coeffs: list[int], lo: int, hi: int) -> float:
    f = lambda t: sum(c * t ** i for i, c in enumerate(coeffs))  # noqa: E731
    return _simpson(f, lo, hi, n=20_000)


# ── 16 条：规格附录 A 的输入（期望值从规格抄，用于比对，不用于推导） ──

INTEGRAL_CASES = [
    ("I1", [1, 1], 0, 1),
    ("I2", [3, -2, 1], -1, 2),
    ("I3", [0, 0, 5], 1, 2),
]
GAMMA_CASES = [
    ("I4", 1, 2, 1),
    ("I5", 9, 8, 5),
]
GAUSS_CASES = [
    ("I6", 0, 2, 3),
    ("I7", 5, 1, -2),
]
DET_CASES = [
    ("D1", [[2, 1], [1, 3]]),
    ("D2", [[1, 2, 3], [4, 5, 6], [7, 8, 10]]),
    ("D3", [[1, 2], [2, 4]]),
    ("D4", [[2, 1, 0], [1, 3, 1], [0, 1, 2]]),
    ("D5", [[3, 1, 2], [0, -2, 5], [0, 0, 7]]),
    ("D6", [[0, 1], [1, 0]]),
    ("D7", [[0, 2, 1], [3, 0, 1], [4, 5, 0]]),
]

# 规格附录 A「期望参考值」一列（抄自 技术规格_v5.md L614–L637）
SPEC_EXPECT = {
    "I1": "3/2", "I2": "9", "I3": "35/3",
    "I4": "1/4", "I5": "14175/8388608", "I6": "3/4", "I7": "-120",
    "D1": "5", "D2": "-3", "D3": "0", "D4": "8",
    "D5": "-42", "D6": "-1", "D7": "23",
}


def _fmt(fr: Fraction) -> str:
    return str(fr.numerator) if fr.denominator == 1 else f"{fr.numerator}/{fr.denominator}"


def run() -> int:
    rows = []
    bad = 0

    for cid, coeffs, lo, hi in INTEGRAL_CASES:
        exact = int_poly(coeffs, lo, hi)
        num = num_poly(coeffs, lo, hi)
        rows.append((cid, exact, num, SPEC_EXPECT[cid]))

    for cid, n, rate, c in GAMMA_CASES:
        exact = int_gamma(n, rate, c)
        num = num_gamma(n, rate, c)
        rows.append((cid, exact, num, SPEC_EXPECT[cid]))

    for cid, k, rate, c in GAUSS_CASES:
        exact = int_gaussian_odd(k, rate, c)
        num = num_gaussian_odd(k, rate, c)
        rows.append((cid, exact, num, SPEC_EXPECT[cid]))

    for cid, m in DET_CASES:
        exact = det_laplace(m)
        rows.append((cid, exact, None, SPEC_EXPECT[cid]))

    print(f"{'#':<4}{'闭式(Fraction)':<20}{'数值积分':<16}{'规格期望':<16}{'一致':<6}")
    print("-" * 66)
    for cid, exact, num, spec_ans in rows:
        ok = _fmt(exact) == spec_ans
        num_ok = "" if num is None else f"{num:.10g}"
        # 数值路径容差：相对误差 1e-6（Simpson 截断 + 浮点）
        if num is not None:
            ref = float(exact)
            rel = abs(num - ref) / abs(ref) if ref else abs(num)
            if rel > 1e-6:
                ok = False
                num_ok += f" (Δ{rel:.1e})"
        if not ok:
            bad += 1
        print(f"{cid:<4}{_fmt(exact):<20}{num_ok:<16}{spec_ans:<16}{'OK' if ok else 'MISMATCH':<6}")

    print("-" * 66)
    print(f"共 {len(rows)} 条数值用例，分歧 {bad} 条")

    # I8 / D8 拒绝组：这里只做输入构造的确认，真正的拒绝行为由 A 的 validate 负责
    print("\n拒绝组（输入合法性，交 A 的 validate_task / validate_matrix 执行）：")
    print("  I8 · rate=0 / n=0 / c=0 / template='eigen' / coeffs 末项为 0 / upper==lower")
    print("  D8 · 非方阵 / 元素 2.0(float) / 元素 true(bool) / 1 阶 [[5]]")

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
