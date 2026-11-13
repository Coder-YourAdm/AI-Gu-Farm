"""开发期交叉验证：用 SymPy 独立算一遍，与 app.executor 的精确解对照。

重要：本脚本**不属于判分链路**。
正式运行、pilot、留出评测都不导入 SymPy（规格 v5.1 §1：首版无须 SymPy / mpmath / NumPy）。
它只在开发期回答一个问题：手写公式和第三方库算出来的，是不是同一个数。

用法：
    python tools/sympy_crosscheck.py        # 每族 100 个随机样本
    python tools/sympy_crosscheck.py 300    # 每族 300 个
"""

import os
import random
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sympy as sp

from app.executor import solve_exact

X = sp.Symbol("x")
SEED = 20261113


def to_fraction(expr) -> Fraction:
    val = sp.simplify(expr)
    if not val.is_rational:
        raise ValueError("SymPy 给出非有理结果：%r" % val)
    return Fraction(int(val.p), int(val.q))


def as_fraction(ref: dict) -> Fraction:
    if ref["kind"] == "integer":
        return Fraction(int(ref["value"]))
    return Fraction(int(ref["numerator"]), int(ref["denominator"]))


def gen_poly(rng):
    deg = rng.randint(1, 4)
    coeffs = [rng.randint(-5, 5) for _ in range(deg + 1)]
    if coeffs[-1] == 0:
        coeffs[-1] = rng.choice([v for v in range(-5, 6) if v])
    if all(c == 0 for c in coeffs[1:]):
        coeffs[rng.randint(1, deg)] = rng.choice([v for v in range(-5, 6) if v])
    lower = rng.randint(-3, 3)
    upper = rng.randint(lower + 1, lower + 6)
    return {"coeffs": coeffs, "lower": lower, "upper": upper}


NONZERO = [1, 2, 3, 4, 5, -1, -2, -3, -4, -5]


def gen_gamma(rng):
    return {"n": rng.randint(1, 9), "rate": rng.randint(1, 8), "c": rng.choice(NONZERO)}


def gen_gaussian(rng):
    return {"k": rng.randint(0, 5), "rate": rng.randint(1, 8), "c": rng.choice(NONZERO)}


def gen_det(rng):
    n = rng.randint(2, 5)
    return {"matrix": [[rng.randint(-9, 9) for _ in range(n)] for _ in range(n)]}


def sym_poly(p):
    return sp.integrate(
        sum(a * X ** i for i, a in enumerate(p["coeffs"])),
        (X, p["lower"], p["upper"]),
    )


def sym_gamma(p):
    return sp.integrate(p["c"] * X ** p["n"] * sp.exp(-p["rate"] * X), (X, 0, sp.oo))


def sym_gaussian(p):
    return sp.integrate(
        p["c"] * X ** (2 * p["k"] + 1) * sp.exp(-p["rate"] * X ** 2), (X, 0, sp.oo)
    )


def sym_det(p):
    return sp.Matrix(p["matrix"]).det()


def run(family, gen, sympy_fn, n, rng):
    bad = 0
    for _ in range(n):
        params = gen(rng)
        try:
            mine = as_fraction(solve_exact({"template": family, "params": params}))
            theirs = to_fraction(sympy_fn(params))
        except Exception as exc:
            print("  [ERR ] %s %r -> %s" % (family, params, exc))
            bad += 1
            continue
        if mine != theirs:
            print("  [DIFF] %s %r ours=%s sympy=%s" % (family, params, mine, theirs))
            bad += 1
    print("%-13s %d/%d 一致" % (family, n - bad, n))
    return bad


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    rng = random.Random(SEED)
    bad = 0
    bad += run("poly", gen_poly, sym_poly, n, rng)
    bad += run("gamma", gen_gamma, sym_gamma, n, rng)
    bad += run("gaussian_odd", gen_gaussian, sym_gaussian, n, rng)
    bad += run("det", gen_det, sym_det, n, rng)
    print("\n不一致总数：%d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
