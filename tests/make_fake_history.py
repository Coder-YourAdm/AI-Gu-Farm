#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造一份 20 代的假 history.jsonl，专门用来验证 export.py 的白名单。

为什么需要它：
    B 的 orchestrator 还没跑出真实数据，但大屏不能等。假数据的字段名严格照
    技术规格 §8 L460–484 抄，所以 export.py 和大屏可以先做完，等真数据到了
    只换文件、不改代码。

故意投毒：
    每条代记录里塞了 "reference"、"raw_response"、"holdout_statement" 三个
    敏感字段。export.py 如果整体透传（dict(rec) / {**rec}），这些就会进
    web/history.json —— 自检会当场失败。这是白名单实现的活体测试。

用法：
    python tests/make_fake_history.py
    python app/export.py --run runs/fake_s42 --out web/history.json
"""
from __future__ import annotations

import json
import random
from pathlib import Path

N_GEN = 20
N_POP = 6
SEED = 42
OUT = Path("runs/fake_s42/history.jsonl")

ANCESTOR_KEYS = ("integral_failure", "det_failure", "balanced_failure", "mixed_failure")

GENE_TEXT = {
    "solve_prompt": "先按结构选方法，再检查分数计算。",
    "set_prompt": "优先 gamma，调整 n 和 rate。",
}


def _meta(mid: str, parents: list[str], origin: str, born_gen: int) -> dict:
    return {"genes": dict(GENE_TEXT, self_check=born_gen % 2 == 1),
            "meta": {"id": mid, "parents": parents, "origin": origin,
                     "born_gen": born_gen}}


def main() -> None:
    rng = random.Random(SEED)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    # 初代六个体
    pop = [_meta(f"G{i:02d}", [], "seed", 0) for i in range(1, N_POP + 1)]
    history: list[dict] = []

    for gen in range(N_GEN + 1):
        ids = [p["meta"]["id"] for p in pop]
        # 分数：随代次上升 + 噪声；三个祖先指标随代次下降
        progress = gen / N_GEN
        inds = []
        for k, mid in enumerate(ids):
            solve = round(min(5.0, 1.2 + 2.6 * progress + rng.uniform(-0.6, 0.6)), 2)
            set_score = round(min(2.0, 0.3 + 1.2 * progress + rng.uniform(-0.3, 0.3)), 2)
            bounty = 2.0 if (gen in (8, 15) and k == gen % N_POP) else 0
            total = round(solve + set_score + bounty, 2)
            n_samples = 1 if gen < 2 else 2
            inds.append({"id": mid, "solve": solve, "set": set_score,
                         "bounty": bounty, "total": total,
                         # est 是最近 n_samples 次的均值（§8 L486）
                         "est": round(total - rng.uniform(0, 0.35), 3),
                         "n_samples": n_samples})

        ranking = [i["id"] for i in sorted(inds, key=lambda x: -x["est"])]

        ancestor = {
            "integral_failure": round(0.72 - 0.34 * progress + rng.uniform(-0.03, 0.03), 6),
            "det_failure": round(0.66 - 0.30 * progress + rng.uniform(-0.03, 0.03), 6),
            "balanced_failure": round(0.69 - 0.32 * progress + rng.uniform(-0.03, 0.03), 6),
            "mixed_failure": round(0.63 - 0.27 * progress + rng.uniform(-0.03, 0.03), 6),
        }

        challenges = []
        for tid, born in (("C01", 8), ("C02", 15)):
            if gen < born:
                continue
            # §6.3 L399：每题观察窗口最多 6 次尝试，超出即退役，不能一直追加
            real = gen - born                      # 真实经过的代次，决定状态
            elapsed = min(real, 5)                 # 观察最多 6 次（attempt 1..6）
            if real >= 6:
                state, reason = "retired", "window_closed"
            else:
                state, reason = "active", None
            challenges.append({
                "task_id": tid, "born_gen": born, "window": 6,
                "state": state,
                "observations": [{"gen": born + t, "attempt": t + 1,
                                  "correct": 1 if t >= 2 else 0,
                                  "p_t": round(1 / 6, 10)} for t in range(elapsed + 1)],
                "first_success_attempt": 3 if elapsed >= 2 else None,
                "threshold_attempt": None, "retired_reason": reason,
            })

        # 下一代：3 精英 + 2 交叉 + 1 变异
        survivors = [p for p in pop if p["meta"]["id"] in ranking[:3]]
        next_pop = list(survivors)
        for j in range(2):
            pa, pb = rng.sample(survivors, 2)
            next_pop.append(_meta(f"G{gen:02d}X{j}", [pa["meta"]["id"], pb["meta"]["id"]],
                                  "crossover", gen + 1))
        mut_src = rng.choice(survivors)
        next_pop.append(_meta(f"G{gen:02d}M1", [mut_src["meta"]["id"]], "mutation", gen + 1))

        rec = {
            "schema_version": "5.1",
            "run_id": "E_fake_20261113",
            "condition": "E",
            "gen": gen,
            "status": "completed",
            "M": N_POP,
            "refetch_round": 0,
            "task_counts": {"integral": 4, "det": 2},
            "slots": {"peer": 30, "common": 24, "ancestor": 18,
                      "challenge": 6 if gen in (8, 15, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20) else 0},
            "ancestor": ancestor,
            "individuals": inds,
            "ranking": ranking,
            "evaluated_population": pop,
            "next_population": next_pop if gen < N_GEN else None,
            "challenges": challenges,
            "timing": {"wall_ms": 90000 + gen * 400,
                       "role_ms": {"solve": 51000, "set": 21000,
                                   "common": 12000, "ancestor": 6000}},
            "usage": {"call_count": 78, "input_tokens": 42000 + gen * 120,
                      "output_tokens": 6100 + gen * 30,
                      "reasoning_tokens": 0, "cost": round(0.42 + gen * 0.01, 4)},
            "warnings": [],
            # ↓↓↓ 以下三个是投毒字段，必须被 export.py 剥掉 ↓↓↓
            "reference": {"C01": "3/4", "C02": "17/60"},
            "raw_response": "{\"answer\": \"3/4\", \"thinking\": \"...\"}",
            "holdout_statement": "计算 ∫_0^1 x^3(1-x)^2 dx 的精确值。",
        }
        history.append(rec)
        pop = next_pop

    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        for rec in history:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    poisoned = {"reference", "raw_response", "holdout_statement"}
    print(f"✓ 已写入 {OUT}（{len(history)} 代 / 每代投毒字段 {sorted(poisoned)}）")


if __name__ == "__main__":
    main()
