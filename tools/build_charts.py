"""生成大屏用的三张图数据（CSV），并用 A 的 ancestor_report 复算祖先指标。

三张表：
  docs/charts/ancestor_curve.csv  祖先分层失败率 + 三代均线（每代一行，按 run）
  docs/charts/ef_pair.csv         E/F 配对曲线（0/5/10/15/20）
  docs/charts/challenge.csv       两次挑战的逐次观察

同时做一次真实数据上的接口核对：把每代 18 个祖先槽位喂给 A 的
`ancestor.generation_failures`，结果必须与快照里 orchestrator 当时算出的
`record["ancestor"]` 逐字段相等——这是 A4 与 B 唯一还没在真数据上对过的接口。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ancestor import ancestor_report  # noqa: E402

RUNS = ROOT / "runs"
OUT = ROOT / "docs" / "charts"
FORMAL = ["E_s42_7f878e7097", "F_s42_7f878e7097",
          "E_s43_7f878e7097", "F_s43_7f878e7097"]


def load_gens(run_id: str) -> list[dict]:
    out = []
    for p in sorted((RUNS / "snapshots" / run_id).glob("g*.json")):
        rec = json.loads(p.read_text(encoding="utf-8")).get("record") or {}
        if rec and rec.get("gen", 0) >= 1 and rec.get("status") == "completed":
            out.append(rec)
    return out


def ancestor_rows(run_id: str) -> list[dict]:
    gens = load_gens(run_id)
    payload = []
    for rec in gens:
        types = {tid: v["type"] for tid, v in (rec.get("per_task") or {}).items()}
        slots = [a for a in rec.get("answers") or [] if a.get("role") == "ancestor"]
        payload.append({"gen": rec["gen"], "slots": slots, "task_types": types})
    rows = ancestor_report(payload)

    # 与 orchestrator 当时写进快照的值逐字段核对
    for rec, row in zip(gens, rows):
        stored = rec.get("ancestor") or {}
        for key in ("integral_failure", "det_failure", "balanced_failure", "mixed_failure"):
            a, b = stored.get(key), row.get(key)
            if a is None and b is None:
                continue
            if a is None or b is None or abs(float(a) - float(b)) > 1e-9:
                raise SystemExit(f"{run_id} g{rec['gen']} {key}: 快照 {a} != 重算 {b}")
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1 祖先曲线
    with open(OUT / "ancestor_curve.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["run", "gen", "integral_failure", "det_failure", "balanced_failure",
                    "mixed_failure", "integral_failure_mean", "det_failure_mean",
                    "balanced_failure_mean", "mixed_failure_mean", "task_integral", "task_det"])
        for run in FORMAL:
            for r in ancestor_rows(run):
                w.writerow([run, r["gen"], r["integral_failure"], r["det_failure"],
                            r["balanced_failure"], r["mixed_failure"],
                            r["integral_failure_mean"], r["det_failure_mean"],
                            r["balanced_failure_mean"], r["mixed_failure_mean"],
                            r["task_mix"]["integral"], r["task_mix"]["det"]])

    # 2 E/F 配对曲线
    pairs = [("s42", "E_s42_7f878e7097", "F_s42_7f878e7097"),
             ("s43", "E_s43_7f878e7097", "F_s43_7f878e7097")]
    with open(OUT / "ef_pair.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair", "checkpoint", "E", "F", "E_minus_F",
                    "E_integral", "E_det", "F_integral", "F_det"])
        for tag, e, f_ in pairs:
            ee = json.loads((RUNS / "evaluations" / f"{e}.json").read_text(encoding="utf-8"))
            ff = json.loads((RUNS / "evaluations" / f"{f_}.json").read_text(encoding="utf-8"))
            for ce, cf in zip(ee["checkpoints"], ff["checkpoints"]):
                w.writerow([tag, ce["checkpoint"], ce["mean"], cf["mean"],
                            round(ce["mean"] - cf["mean"], 4),
                            ce["integral_mean"], ce["det_mean"],
                            cf["integral_mean"], cf["det_mean"]])

    # 3 挑战曲线（每代的 record 都带着完整观测历史，必须按 (run,task,gen,attempt) 去重）
    seen = {}
    for run in FORMAL:
        for rec in load_gens(run):
            for ch in rec.get("challenges") or []:
                for ob in ch.get("observations") or []:
                    key = (run, ch["task_id"], ob["gen"], ob["attempt"])
                    seen[key] = [run, ch["task_id"], ob["gen"], ob["attempt"],
                                 ob["correct"], ob["p_t"], ch["state"],
                                 ch.get("retired_reason") or ""]
    with open(OUT / "challenge.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["run", "task_id", "gen", "attempt", "correct", "p_t", "state", "retired_reason"])
        for key in sorted(seen):
            w.writerow(seen[key])

    # 控制台摘要
    for run in FORMAL:
        rows = ancestor_rows(run)
        det = [r["det_failure"] for r in rows if r["det_failure"] is not None]
        mixed = [r["mixed_failure"] for r in rows]
        print(f"{run}: gens={len(rows)} 积分题={sum(r['task_mix']['integral'] for r in rows)} "
              f"行列式题={sum(r['task_mix']['det'] for r in rows)} "
              f"mixed 首/末={mixed[0]:.3f}/{mixed[-1]:.3f} "
              f"det 有题的代={len(det)}")
    print(f"written {OUT.relative_to(ROOT)}/ (ancestor_curve.csv, ef_pair.csv, challenge.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
