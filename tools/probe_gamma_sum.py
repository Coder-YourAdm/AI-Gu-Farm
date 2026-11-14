"""附录 B 可行性预演：不改任何生产代码，先量出 gamma_sum 能不能把积分难度拉开。

做法：手工写 10 道「有限项线性组合 × 指数核」的题，用它自己的精确 Fraction 判分，
6 份种子各答一遍（60 次调用）。它不进入题库、不产生实验数据，只回答一个问题：
   换成组合积分后，通过率会不会从 100% 降到有区分度的区间？

对照：现模板 gamma 在 calibration 上是 120/120 全对。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.executor import judge  # noqa: E402
from app.genotype import build_solver_system, load_seed_genotypes  # noqa: E402
from app.llm_async import LLMClient, build_solver_messages  # noqa: E402

SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
# (rate, [(c, n), ...])
SPECS = [
    (2, [(3, 4), (-2, 2)]),
    (3, [(2, 3), (1, 1)]),
    (2, [(-1, 5), (4, 2)]),
    (1, [(5, 2), (-3, 1)]),
    (8, [(1, 9), (-2, 4)]),
    (4, [(3, 6), (2, 3), (-1, 1)]),
    (3, [(-4, 7), (2, 5)]),
    (2, [(2, 8), (-5, 2), (1, 1)]),
    (6, [(4, 3), (-1, 2)]),
    (7, [(-2, 9), (3, 5)]),
]


def sup(n: int) -> str:
    return str(n).translate(SUP) if n else ""


def term(c: int, n: int, first: bool) -> str:
    ax = "x" + sup(n) if n else ""
    mag = abs(c)
    coef = "" if mag == 1 and n else str(mag)
    body = f"{coef}{ax}"
    if first:
        return f"-{body}" if c < 0 else body
    return f" - {body}" if c < 0 else f" + {body}"


def statement(rate: int, terms: list[tuple[int, int]]) -> str:
    terms = sorted(terms, key=lambda t: -t[1])
    poly = "".join(term(c, n, i == 0) for i, (c, n) in enumerate(terms))
    return f"计算定积分 ∫₀^∞ ({poly}) e^{{-{rate}x}} dx。"


def reference(rate: int, terms: list[tuple[int, int]]) -> dict:
    val = sum(Fraction(c * math.factorial(n), rate ** (n + 1)) for c, n in terms)
    if val.denominator == 1:
        return {"kind": "integer", "value": str(val.numerator)}
    return {"kind": "rational", "numerator": str(val.numerator),
            "denominator": str(val.denominator)}


async def main() -> int:
    cfg = Config.load(ROOT / "config.local.json")
    people = load_seed_genotypes(ROOT / "prompts" / "seed_genotypes.json")
    tasks = []
    for i, (rate, terms) in enumerate(SPECS, 1):
        tasks.append({"id": f"B{i:02d}", "type": "integral",
                      "statement": statement(rate, terms),
                      "reference": reference(rate, terms)})

    print(f"model={cfg.model} cap={cfg.max_tokens_solver} calls={len(people)*len(tasks)}")
    for t in tasks[:3]:
        print("  ", t["statement"], "->", t["reference"])

    async def one(p, t):
        res = await client.chat(
            "solver",
            build_solver_messages(build_solver_system(p.genes), t["statement"],
                                  "integer, fraction or finite decimal"),
            slot_id=f"gsum_{p.id}_{t['id']}")
        return p.id, t["id"], judge(t, res.text) if res.ok else "unanswered", res.latency_ms

    async with LLMClient(cfg, run_id="probe_gamma_sum") as client:
        rows = await asyncio.gather(*[one(p, t) for p in people for t in tasks])

    right = sum(1 for r in rows if r[2] == "right")
    unans = sum(1 for r in rows if r[2] == "unanswered")
    lat = sorted(r[3] for r in rows)
    print(f"通过 {right}/{len(rows)} = {right/len(rows):.1%}，"
          f"unanswered {unans}，p50 {lat[len(lat)//2]} ms")
    per_task = {}
    for _, tid, status, _ in rows:
        per_task.setdefault(tid, []).append(status == "right")
    for tid, vals in per_task.items():
        print(f"  {tid}: {sum(vals)}/{len(vals)}")
    (ROOT / "logs" / "probe_gamma_sum.json").write_text(
        json.dumps([{"person": a, "task": b, "status": c, "latency_ms": d} for a, b, c, d in rows],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
