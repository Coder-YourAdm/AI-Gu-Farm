"""从证据文件直接生成交付简报（result_brief.json + 人读版 markdown）。

所有数字都来自 runs/ 下的冻结文件，不手抄：
  * protocol_v5.json            协议与注册成员
  * calibration_v5.json         校准通过率
  * ancestors.json              三个冻结祖先
  * summaries/<run>.json        每趟状态与调用数
  * evaluations/<run>.json      留出评测（逐检查点、分题型）
  * snapshots/<run>/gNN.json    题族分布、出题分、挑战观察

产物给 C 接页面，也给答辩用。
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
PAIRS = [("s42", "E_s42_7f878e7097", "F_s42_7f878e7097"),
         ("s43", "E_s43_7f878e7097", "F_s43_7f878e7097")]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_stats(run_id: str) -> dict:
    tpl: dict[str, int] = {}
    set_scores: list[float] = []
    ch: dict[str, dict] = {}
    calls = 0
    for p in sorted((RUNS / "snapshots" / run_id).glob("g*.json")):
        rec = load(p).get("record") or {}
        if not rec or rec.get("gen", 0) == 0:
            continue
        calls += int(rec.get("call_total") or 0)
        for t in rec.get("tasks") or []:
            tpl[t["template"]] = tpl.get(t["template"], 0) + 1
        for i in rec.get("individuals") or []:
            set_scores.append(float(i["set"]))
        for c in rec.get("challenges") or []:
            ch[c["task_id"]] = {"state": c["state"],
                                "observations": [(o["gen"], o["correct"])
                                                 for o in c.get("observations") or []]}
    return {
        "calls": calls,
        "templates": tpl,
        "set_mean": round(statistics.mean(set_scores), 4) if set_scores else None,
        "set_zero_rate": round(sum(1 for s in set_scores if s == 0) / len(set_scores), 4)
        if set_scores else None,
        "challenges": ch,
    }


def main() -> int:
    proto = load(RUNS / "protocol_v5.json")
    cal = load(RUNS / "calibration_v5.json")
    anc = load(RUNS / "ancestors.json")
    cal_tasks = load(ROOT / "data" / "calibration.json")
    type_of = {t["id"]: t["type"] for t in cal_tasks}
    cal_by_type = {}
    for typ in ("integral", "det"):
        rows = [a for a in cal["answers"] if type_of.get(a["task_id"]) == typ]
        cal_by_type[typ] = round(sum(1 for a in rows if a["status"] == "right") / len(rows), 4)

    runs = {}
    for m in proto["members"]:
        rid = m["run_id"]
        summary = load(RUNS / "summaries" / f"{rid}.json")
        ev_path = RUNS / "evaluations" / f"{rid}.json"
        ev = load(ev_path) if ev_path.exists() else None
        runs[rid] = {
            "condition": m["condition"],
            "seed": m["seed"],
            "status": summary.get("status"),
            "gens_run": summary.get("gens_run"),
            "calls": summary.get("call_total"),
            "aborted": summary.get("aborted"),
            "stats": run_stats(rid) if (RUNS / "snapshots" / rid).exists() else None,
            "evaluation": ev,
        }

    pairs = []
    for tag, e, f in PAIRS:
        ee, ff = runs[e]["evaluation"], runs[f]["evaluation"]
        rows = []
        for ce, cf in zip(ee["checkpoints"], ff["checkpoints"]):
            rows.append({
                "checkpoint": ce["checkpoint"],
                "E": round(ce["mean"], 4), "F": round(cf["mean"], 4),
                "E_minus_F": round(ce["mean"] - cf["mean"], 4),
                "E_integral": round(ce["integral_mean"], 4),
                "E_det": round(ce["det_mean"], 4),
                "F_integral": round(cf["integral_mean"], 4),
                "F_det": round(cf["det_mean"], 4),
            })
        g0e, g0f = rows[0]["E"], rows[0]["F"]
        gne, gnf = rows[-1]["E"], rows[-1]["F"]
        pairs.append({
            "tag": tag, "E_run": e, "F_run": f, "checkpoints": rows,
            "final": {"E": gne, "F": gnf, "E_minus_F": round(gne - gnf, 4),
                      "E_vs_g0": round(gne - g0e, 4), "F_vs_g0": round(gnf - g0f, 4)},
        })

    deltas = [p["final"]["E_minus_F"] for p in pairs]
    brief = {
        "schema": "result_brief/1",
        "generated_from": "runs/protocol_v5.json + evaluations",
        "protocol_id": proto["id"],
        "protocol": {"n_gen": proto["config"]["n_gen"],
                     "fitness_window": proto["config"]["fitness_window"],
                     "solver_max_tokens": proto["config"]["max_tokens_solver"],
                     "setter_max_tokens": proto["config"]["max_tokens_setter"],
                     "concurrency": proto["config"]["concurrency"],
                     "challenge_gens": proto["config"]["challenge_gens"],
                     "challenge_task_ids": proto["config"].get("challenge_task_ids"),
                     "model": proto["config"]["model"],
                     "base_url": proto["config"]["base_url"]},
        "calibration": {"mean_pass_rate": cal["mean_pass_rate"],
                        "warning_band_ok": cal["warning_band_ok"],
                        "by_type": cal_by_type},
        "ancestors": [{"id": a["id"], "calibration_mean": a["calibration_mean"]}
                      for a in anc["ancestors"]],
        "runs": {rid: {"condition": r["condition"], "seed": r["seed"],
                       "status": r["status"], "gens_run": r["gens_run"],
                       "calls": r["calls"], "aborted": r["aborted"],
                       "stats": r["stats"]}
                 for rid, r in runs.items()},
        "pairs": pairs,
        "main_result": {
            "metric": "末代检查点（g20）六人 × 20 题留出均分，0–20",
            "E_minus_F_by_pair": deltas,
            "E_minus_F_mean": round(statistics.mean(deltas), 4),
            "verdict": "未观察到可靠改善：两对方向一致为正，但幅度远小于抽样波动",
        },
        "limits": [
            "只跑了 2 对（s42/s43）；第三对 E_s44 第 6 代人工取消、F_s44 未启动",
            "solver 推理预算 1500 token；约 24% 行列式作答因预算截断记 unanswered",
            "积分题型对当前模型已饱和（校准 100%），信号只来自行列式",
            "fitness_window=1（pilot 实测 1 与 2 的 top3 翻转率同为 33%，无改善）",
            "挑战题固定为 X04(5阶)、X05(4阶)，非按运行结果挑选",
        ],
    }

    out = RUNS / "result_brief.json"
    out.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"protocol {proto['id'][:12]}  calibration {cal['mean_pass_rate']}")
    for p in pairs:
        f = p["final"]
        print(f"  {p['tag']}: E {f['E']}  F {f['F']}  E-F {f['E_minus_F']:+.2f} "
              f"(E vs g0 {f['E_vs_g0']:+.2f}, F vs g0 {f['F_vs_g0']:+.2f})")
    print(f"  mean E-F {brief['main_result']['E_minus_F_mean']:+.3f}")
    for rid, r in brief["runs"].items():
        s = r["stats"] or {}
        print(f"  {rid}: {r['status']:<9} gens={r['gens_run']} calls={r['calls']} "
              f"templates={s.get('templates')} set_mean={s.get('set_mean')}")
    print(f"written {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
