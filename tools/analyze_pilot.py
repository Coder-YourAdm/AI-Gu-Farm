"""pilot 读数：每代题族构成、出题分、解题分、排名稳定性、耗时与 token。

不做判断，只把 snapshot 里已有的字段整理成一张表。要点是回答两个问题：
  1. 出题策略会不会向唯一有区分度的题型（行列式）迁移？
  2. 排名在两代之间翻动多少（fitness_window 的翻转率，两趟对照时用）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_gens(run_id: str) -> list[dict]:
    d = ROOT / "runs" / "snapshots" / run_id
    out = []
    for p in sorted(d.glob("g*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        rec = obj.get("record")
        if rec and rec.get("gen", 0) >= 1 and rec.get("M") is not None:
            out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    args = ap.parse_args()

    gens = load_gens(args.run)
    if not gens:
        print("没有可读的 completed 代", file=sys.stderr)
        return 2

    prev_top3: list[str] | None = None
    rows = []
    for rec in gens:
        tasks = rec.get("tasks") or []
        per_task = rec.get("per_task") or {}
        templates = {}
        for t in tasks:
            templates[t["template"]] = templates.get(t["template"], 0) + 1
        cs = [per_task[t["id"]]["right"] if t["id"] in per_task else None for t in tasks]
        set_scores = [min(c, 5 - c) / 2 for c in cs if c is not None]
        inds = rec.get("individuals") or []
        ranking = rec.get("ranking") or []
        top3 = ranking[:3]
        moved = None
        if prev_top3 is not None:
            moved = 1 - len(set(top3) & set(prev_top3)) / 3
        prev_top3 = top3
        usage = rec.get("usage") or {}
        rows.append({
            "gen": rec["gen"],
            "status": rec["status"],
            "M": rec.get("M"),
            "refetch_round": rec.get("refetch_round"),
            "templates": templates,
            "type_counts": rec.get("task_counts"),
            "peer_c": cs,
            "set_scores": set_scores,
            "mean_set": round(statistics.mean(set_scores), 3) if set_scores else None,
            "zero_set_questions": sum(1 for s in set_scores if s == 0),
            "solve": {i["id"]: i["solve"] for i in inds},
            "totals": {i["id"]: i["total"] for i in inds},
            "calibration_spread": round(max(i["total"] for i in inds)
                                        - min(i["total"] for i in inds), 3) if inds else None,
            "ranking": ranking,
            "top3_changed": moved,
            "call_total": rec.get("call_total"),
            "wall_s": round((rec.get("wall_ms") or 0) / 1000, 1),
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "operator_calls": rec.get("operator_calls"),
        })

    print(f"run={args.run}  代数={len(rows)}")
    print(f"{'gen':>3} {'M':>2} {'refetch':>7} {'templates':<28} {'mean_set':>8} "
          f"{'zero_set':>8} {'spread':>6} {'c':<14} {'wall_s':>6} {'top3_changed':>12}")
    for r in rows:
        print(f"{r['gen']:>3} {r['M']:>2} {str(r['refetch_round']):>7} "
              f"{str(r['templates']):<28} {str(r['mean_set']):>8} "
              f"{r['zero_set_questions']:>8} {str(r['calibration_spread']):>6} "
              f"{str(r['peer_c']):<14} {r['wall_s']:>6} {str(r['top3_changed']):>12}")

    det_share = [round(r["templates"].get("det", 0) / sum(r["templates"].values()), 2)
                 if r["templates"] else None for r in rows]
    changes = [r["top3_changed"] for r in rows if r["top3_changed"] is not None]
    summary = {
        "run": args.run,
        "gens": len(rows),
        "det_share_by_gen": det_share,
        "mean_set_by_gen": [r["mean_set"] for r in rows],
        "top3_flip_rate": round(statistics.mean(changes), 3) if changes else None,
        "total_calls": sum(r["call_total"] or 0 for r in rows),
        "total_wall_s": round(sum(r["wall_s"] for r in rows), 1),
        "tokens_in": sum(r["tokens_in"] or 0 for r in rows),
        "tokens_out": sum(r["tokens_out"] or 0 for r in rows),
        "rows": rows,
    }
    out = ROOT / "runs" / f"{args.run}_analysis.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ndet 占比逐代: {det_share}")
    print(f"top3 翻转率: {summary['top3_flip_rate']}  总调用 {summary['total_calls']}  "
          f"总墙钟 {summary['total_wall_s']}s")
    print(f"written {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
