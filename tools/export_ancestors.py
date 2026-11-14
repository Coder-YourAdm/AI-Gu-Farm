"""B5 收尾：把 calibration 报告转成祖先清单 + 三项分析。

产出 runs/ancestors.json，一份文件同时满足两边的读取约定：
  * A4 的 `app.ancestor.load_ancestors()`：顶层有 ancestors，恰好 3 个，
    每个含 id 与 genes 三字段；
  * 分工方案 v7.3 §B5：含三次均分、排序依据、RNG 记录、model 与 protocol。

同时打印并写入：分题型通过率、同策略三次重复波动、六份策略之间的差异。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ancestor import load_ancestors  # noqa: E402
from app.config import Config  # noqa: E402
from app.protocol import solver_fingerprint  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibration", default="runs/calibration.json")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--config", default="config.local.json")
    ap.add_argument("--out", default="runs/ancestors.json")
    args = ap.parse_args()

    cfg = Config.load(ROOT / args.config)
    report = json.loads((ROOT / args.calibration).read_text(encoding="utf-8"))
    tasks = json.loads((ROOT / args.data_dir / "calibration.json").read_text(encoding="utf-8"))
    type_of = {t["id"]: t["type"] for t in tasks}
    rows = report["answers"]

    def pct(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    # ---- 每个人的分项统计 ----
    per_person: dict[str, dict] = {}
    for person in sorted({r["individual_id"] for r in rows}):
        mine = [r for r in rows if r["individual_id"] == person]
        reps = [sum(1 for r in mine if r["checkpoint"] == k and r["status"] == "right")
                for k in sorted({r["checkpoint"] for r in mine})]
        by_type = {}
        for typ in ("integral", "det"):
            sub = [r for r in mine if type_of.get(r["task_id"]) == typ]
            by_type[typ] = {
                "right": sum(1 for r in sub if r["status"] == "right"),
                "total": len(sub),
                "mean": pct(sum(1 for r in sub if r["status"] == "right"), len(sub)),
            }
        per_person[person] = {
            "id": person,
            "mean": pct(sum(1 for r in mine if r["status"] == "right"), len(mine)),
            "reps": reps,
            "rep_std": round(statistics.pstdev(reps), 3) if len(reps) > 1 else 0.0,
            "rep_range": max(reps) - min(reps) if reps else 0,
            "by_type": by_type,
            "unanswered": sum(1 for r in mine if r["status"] == "unanswered"),
            "wrong": sum(1 for r in mine if r["status"] == "wrong"),
            "transport": sum(1 for r in mine if r["status"] == "transport_error"),
        }

    scores = report["scores"]
    totals = {p: sum(v) for p, v in scores.items()}
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    boundary = len(ranked) >= cfg.n_ancestor
    third = ranked[cfg.n_ancestor - 1][1] if boundary else None
    ties_at_boundary = sum(1 for _, t in ranked if t == third) > 1 if boundary else False

    ancestors = []
    for item in report["ancestors"]:
        pid = item["meta"]["id"]
        row = per_person[pid]
        ancestors.append({
            "id": pid,
            "genes": item["genes"],
            "calibration_mean": round(totals[pid] / 60, 4),
            "calibration_runs": scores[pid],
            "calibration_total": totals[pid],
            "by_type": {k: v["mean"] for k, v in row["by_type"].items()},
        })

    out = {
        "schema_version": "5.1",
        "model": cfg.model,
        "base_url": cfg.base_url,
        "calibration_run": args.calibration,
        "calibration_seed": cfg.seed,
        "calibration_digest": report.get("calibration_digest"),
        "solver_fingerprint": solver_fingerprint(cfg),
        "selection_rule": "三次作答总分（满分 60）降序；同分先用本地 RNG(seed) 打散再排序（大纲 §8）",
        "tie_break_rng": {
            "seed": cfg.seed,
            "shuffle_before_sort": True,
            "tied_at_top3_boundary": ties_at_boundary,
            "ranked_totals": {p: t for p, t in ranked},
        },
        "protocol": {
            "temperature": cfg.solver_temperature,
            "max_tokens": cfg.max_tokens_solver,
            "max_response_chars": cfg.max_response_chars,
            "parse": "strict_json",
            "repetitions": 3,
            "tasks_per_rep": len(tasks),
        },
        "scores": [{k: v for k, v in per_person[p].items()} for p in per_person],
        "team": {
            "mean_pass_rate": report.get("mean_pass_rate"),
            "warning_band_ok": report.get("warning_band_ok"),
            "band": "20%-80%",
            "unanswered_rate": pct(sum(1 for r in rows if r["status"] == "unanswered"), len(rows)),
            "wrong_rate": pct(sum(1 for r in rows if r["status"] == "wrong"), len(rows)),
            "transport_error": sum(1 for r in rows if r["status"] == "transport_error"),
            "by_type": {
                typ: pct(sum(1 for r in rows if type_of.get(r["task_id"]) == typ
                             and r["status"] == "right"),
                         sum(1 for r in rows if type_of.get(r["task_id"]) == typ))
                for typ in ("integral", "det")
            },
            "stability": {
                "max_rep_std": max(v["rep_std"] for v in per_person.values()),
                "mean_rep_std": round(statistics.mean(v["rep_std"] for v in per_person.values()), 3),
                "spread_between_strategies": round(
                    max(v["mean"] for v in per_person.values())
                    - min(v["mean"] for v in per_person.values()), 4),
            },
        },
        "ancestors": ancestors,
    }

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 用 A 的加载器当场验一遍，避免「格式看起来对、A 读不了」
    back = load_ancestors(str(out_path))

    print(f"mean_pass_rate={out['team']['mean_pass_rate']}  in_band={out['team']['warning_band_ok']}")
    print(f"by_type={out['team']['by_type']}  unanswered={out['team']['unanswered_rate']}")
    print(f"策略间差距={out['team']['stability']['spread_between_strategies']:.3f}  "
          f"重复波动(平均 std)={out['team']['stability']['mean_rep_std']}")
    for row in out["scores"]:
        print(f"  {row['id']:<8} mean={row['mean']:.3f} reps={row['reps']} "
              f"std={row['rep_std']} int={row['by_type']['integral']['mean']:.2f} "
              f"det={row['by_type']['det']['mean']:.2f} unans={row['unanswered']}")
    print("祖先:", " > ".join(a["id"] for a in ancestors),
          f"（同分边界={ties_at_boundary}）")
    print(f"written {args.out}；A 的 load_ancestors() 读回 {len(back)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
