#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造 gamma_sum 变体题库 —— 技术规格附录 B 的官方降难方向。

为什么不用"换个小模型"：实测 Qwen 9B/4B 在硅基上 100%/50%，但要 35–180 秒
一题，20 代跑不完。flash 快（毫米级）但题太容易（95–100%），曲线是平的。
附录 B 的原话是：pilot 全对时**不要**靠加代数硬扛，改用线性组合变体。

    ∫₀^∞ (3x⁴ − 2x²) e^{−2x} dx = Σ cᵢ·nᵢ! / rate^(nᵢ+1)     （精确 Fraction）

A 的 executor 只认 poly / gamma / gaussian_odd / det，所以题面与参考值在这里
自己算。这不影响判分：`judge()` 只读 `task["type"]` 与 `task["reference"]`，
不碰模板名，也不调 validate_task —— 这一点是查过代码确认的，不是猜的。

难度阶梯（每档变的是项数、幂次上限、rate，不是"看起来复杂"）：
    L1  2 项  n≤4  rate=2     偏易，用于验证链路
    L2  3 项  n≤5  rate=2
    L3  3 项  n≤6  rate=3
    L4  4 项  n≤7  rate=3     偏难，用于压通过率

用法：
    python tests/make_gamma_bank.py --out data/hard_bank.json --n 120
    python tests/probe_model.py --model deepseek-flash --data data/hard_bank.json --n 12
"""
from __future__ import annotations

import argparse
import json
import random
from fractions import Fraction
from math import factorial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 档位 → (项数, n 上限, rate)
LADDER = {
    "L1": (2, 4, 2),
    "L2": (3, 5, 2),
    "L3": (3, 6, 3),
    "L4": (4, 7, 3),
}
SUP = {0: "⁰", 1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹"}


def _sup(k: int) -> str:
    return "".join(SUP[int(c)] for c in str(k))


def _poly_text(terms: list[dict]) -> str:
    """把 [{c,n}] 渲染成 `3x⁴ − 2x² + 5x`。系数 1 不写，幂次 1 写 x。"""
    parts = []
    for i, t in enumerate(terms):
        c, n = t["c"], t["n"]
        mag = abs(c)
        coef = "" if mag == 1 else str(mag)
        var = "" if n == 0 else ("x" if n == 1 else "x" + _sup(n))
        body = f"{coef}{var}" or str(mag)
        if i == 0:
            parts.append(("-" if c < 0 else "") + body)
        else:
            parts.append((" − " if c < 0 else " + ") + body)
    return "".join(parts)


def make_task(tid: str, level: str, rng: random.Random) -> dict:
    n_terms, n_max, rate = LADDER[level]
    terms = []
    used_n = set()
    while len(terms) < n_terms:
        n = rng.randint(2, n_max)
        if n in used_n:
            continue
        used_n.add(n)
        c = rng.choice([-1, 1]) * rng.randint(2, 9)
        terms.append({"c": c, "n": n})
    terms.sort(key=lambda t: -t["n"])

    # 精确参考值：Σ c·n! / rate^(n+1)
    val = sum((Fraction(t["c"] * factorial(t["n"]), rate ** (t["n"] + 1))
               for t in terms), Fraction(0))

    statement = (f"计算积分 ∫₀^∞ ({_poly_text(terms)}) e^(−{rate}x) dx 的精确值。")
    ref = ({"kind": "integer", "value": str(val.numerator)} if val.denominator == 1 else
           {"kind": "rational", "numerator": str(val.numerator),
            "denominator": str(val.denominator)})
    return {
        "id": tid, "source": "hard_bank", "by": None, "gen": None,
        "type": "integral", "template": "gamma_sum",
        "params": {"terms": terms, "rate": rate},
        "signature": "gamma_sum|" + "|".join(f"c{c['c']}n{c['n']}" for c in terms)
                     + f"|rate={rate}",
        "statement": statement, "reference": ref, "level": level,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/hard_bank.json")
    ap.add_argument("--n", type=int, default=120, help="总题数（四档均分）")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--with-det", action="store_true",
                    help="额外并入 calibration 的五阶行列式（保证 det 族有题）")
    a = ap.parse_args(argv)

    rng = random.Random(a.seed)
    levels = list(LADDER)
    per = max(1, a.n // len(levels))
    tasks = []
    k = 0
    for lv in levels:
        for _ in range(per):
            k += 1
            tasks.append(make_task(f"H{k:03d}", lv, rng))

    if a.with_det:
        calib = json.loads((ROOT / "data" / "calibration.json").read_text(encoding="utf-8"))
        dets = [t for t in calib if t["type"] == "det"]
        for t in dets:
            t = dict(t)
            t["id"] = "D" + t["id"].lstrip("K")
            t["source"] = "hard_bank"
            tasks.append(t)

    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    by_lv = {}
    for t in tasks:
        by_lv[t.get("level", "det")] = by_lv.get(t.get("level", "det"), 0) + 1
    print(f"✓ {out.relative_to(ROOT)}  {len(tasks)} 题  分布 {by_lv}")
    for lv in levels:
        s = next((t for t in tasks if t.get("level") == lv), None)
        if s:
            print(f"  {lv}: {s['statement']}  → {s['reference']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
