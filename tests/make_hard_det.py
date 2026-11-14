#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造高阶行列式题（6–7 阶），把 flash 从天花板拉回可测区间。

为什么从行列式下手：积分族有闭式公式（c·n!/rate^(n+1)），flash 连 gamma_sum
的四项七次幂都是 100%，加难度没用。五阶行列式它也要 20 秒 / 4.5k token —— 说明
这一族的成本是"逐步整数消元"，多一阶就多一层手算，正是规则能帮上忙的地方。

题面格式与 A 的 executor 一致（"计算下列 N 阶矩阵的行列式：[[..], ..]。"），
参考值用 Bareiss 精确整数，与 executor 同源，不引入第二套算法。

注意：executor 的 validate_task 只允许 2–5 阶，所以这批题**不能**走
validate_task；`judge()` 不调它（只读 type 与 reference），已查代码确认。

用法：
    python tests/make_hard_det.py --out data/hard_det.json --n 24
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.executor import bareiss  # noqa: E402


def make(tid: str, order: int, rng: random.Random, vmax: int = 9) -> dict:
    while True:
        m = [[rng.randint(-vmax, vmax) for _ in range(order)] for _ in range(order)]
        if abs(bareiss(m)) > 1000:       # 太小的值可能靠猜；要的是算不出来
            break
    rows = ", ".join("[" + ", ".join(str(v) for v in r) + "]" for r in m)
    return {
        "id": tid, "source": "hard_det", "by": None, "gen": None,
        "type": "det", "template": "det", "params": {"matrix": m},
        "signature": f"det|order={order}|" + ",".join("_".join(map(str, r)) for r in m),
        "statement": f"计算下列 {order} 阶矩阵的行列式：[{rows}]。",
        "reference": {"kind": "integer", "value": str(bareiss(m))},
        "level": f"det{order}",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/hard_det.json")
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--orders", default="6,7", help="阶数序列，如 5 或 6,7")
    ap.add_argument("--value-max", type=int, default=9, help="元素绝对值上限")
    ap.add_argument("--seed", type=int, default=77)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    orders = [int(x) for x in a.orders.split(",") if x.strip()]
    tasks = []
    for i in range(a.n):
        tasks.append(make(f"X{i + 1:03d}", orders[i % len(orders)], rng, a.value_max))
    out = ROOT / a.out
    out.write_text(json.dumps(tasks, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ {out.relative_to(ROOT)}  {len(tasks)} 题（6 阶 {sum(1 for t in tasks if t['level'] == 'det6')}"
          f" / 7 阶 {sum(1 for t in tasks if t['level'] == 'det7')}）")
    print("  样例:", tasks[0]["statement"][:110])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
