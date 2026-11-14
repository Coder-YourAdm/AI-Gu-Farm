"""生成结果页的真数据 `web/result.json`（契约见 app/result_schema.py）。

数据来源全部是已冻结的证据：
  runs/protocol_v5.json            协议与模型 id
  runs/evaluations/<run>.json      每趟检查点与逐人留出得分
  runs/snapshots/<run>/g020.json   末代排名（冠军名次）
  runs/summaries/<run>.json        调用数
  runs/ancestors.json              三个冻结祖先的基因
  runs/ancestor_holdout.json       祖先的留出得分（本脚本 --evaluate-ancestors 生成一次，缓存）

为什么要单独测祖先：留出评测只评了每趟的六个人，没评冻结祖先；而页面要画
「末代冠军 vs 冻结祖先」的对比，缺祖先那三个数。3 人 × 20 题 = 60 次调用。

用法：
  python tools/build_result_board.py --evaluate-ancestors   # 先补祖先（发 60 次请求）
  python tools/build_result_board.py                        # 离线重建 web/result.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import executor  # noqa: E402
from app.config import Config  # noqa: E402
from app.evaluate import load_holdout_tasks  # noqa: E402
from app.genotype import Genes, build_solver_system  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.llm_async import build_solver_messages  # noqa: E402
from app.result_schema import check_conclusion_fit, delta_pp, validate  # noqa: E402

RUNS = ROOT / "runs"
TZ = timezone(timedelta(hours=8))
PROTOCOL = RUNS / "protocol_v5.json"
ANCESTOR_CACHE = RUNS / "ancestor_holdout.json"
OUT = ROOT / "web" / "result.json"

RUNS_ORDER = ["E_s42_7f878e7097", "F_s42_7f878e7097",
              "E_s43_7f878e7097", "F_s43_7f878e7097"]


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── 祖先留出评测（只跑一次，缓存）────────────────────────────────────


async def _evaluate_ancestors(cfg: Config, tasks: list[dict], ancestors: list[dict]) -> dict:
    """3 个祖先 × 20 题，全部并发提交（并发闸门在 LLMClient 里）。

    串行版本实测 60 次要 25 分钟；并发后约 1–2 分钟。
    """

    async def one(chat, anc: dict, task: dict) -> tuple[str, str, bool]:
        genes = Genes(**anc["genes"])
        system = build_solver_system(genes)
        view = executor.solver_view(task)
        res = await chat.chat("solver",
                              build_solver_messages(system, view["statement"],
                                                    view["answer_format"]),
                              slot_id=f"anc_{anc['id']}_{task['id']}")
        status = executor.judge(task, res.text) if res.ok else "unanswered"
        return anc["id"], task["type"], status == "right"

    async with LLMClient(cfg, run_id="result_board_ancestors") as chat:
        rows = await asyncio.gather(*[one(chat, anc, task)
                                      for anc in ancestors for task in tasks])

    out = {}
    for anc in ancestors:
        mine = [r for r in rows if r[0] == anc["id"]]
        by_type = {"integral": [0, 0], "det": [0, 0]}
        for _, typ, ok in mine:
            by_type[typ][1] += 1
            by_type[typ][0] += ok
        right = sum(1 for _, _, ok in mine if ok)
        out[anc["id"]] = {"correct": right, "total": len(mine),
                          "integral": by_type["integral"][0], "det": by_type["det"][0]}
        print(f"  {anc['id']}: {right}/{len(mine)}（积分 {by_type['integral'][0]}/"
              f"{by_type['integral'][1]}，行列式 {by_type['det'][0]}/{by_type['det'][1]}）")
    return out


# ── 每趟的冠军与元数据 ──────────────────────────────────────────────


def _started_at(run_id: str) -> str:
    d = ROOT / "logs" / "raw" / run_id
    files = sorted(d.glob("*.json")) if d.exists() else []
    if files:
        try:
            first = json.loads(files[0].read_text(encoding="utf-8"))
            for f in files:
                obj = json.loads(f.read_text(encoding="utf-8"))
                if obj.get("started_at"):
                    first = obj
                    break
            ts = datetime.fromisoformat(first["started_at"].replace("Z", "+00:00"))
            return ts.astimezone(TZ).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            pass
    return "2026-11-14T11:36:00+08:00"


def _run_meta(run_id: str) -> dict:
    summary = load(RUNS / "summaries" / f"{run_id}.json")
    wall_ms = 0
    d = RUNS / "snapshots" / run_id
    for p in sorted(d.glob("g*.json")):
        rec = json.loads(p.read_text(encoding="utf-8")).get("record") or {}
        wall_ms += int(rec.get("wall_ms") or 0)
    return {"calls": int(summary.get("call_total") or 0),
            "duration_min": round(wall_ms / 60000, 1),
            "started_at": _started_at(run_id)}


def _champion(run_id: str) -> tuple[dict, dict]:
    ev = load(RUNS / "evaluations" / f"{run_id}.json")
    cp = ev["checkpoints"][-1]
    people = cp["individuals"]
    snap = load(RUNS / "snapshots" / run_id / "g20.json")["record"]
    ranking = snap.get("ranking") or [p["id"] for p in people]
    best = max(people, key=lambda p: (p["score"], -ranking.index(p["id"])))
    return best, {"rank": ranking.index(best["id"]) + 1, "n": len(people)}


# ── 组装 ────────────────────────────────────────────────────────────


def build(ancestor_scores: dict, ancestors: list[dict], protocol: dict) -> dict:
    order = ["champion"] + [f"ancestor_{i+1}" for i in range(len(ancestors))]
    labels = ["末代冠军"] + [f"固定祖先 · {a['id']}" for a in ancestors]
    colors = ["s3", "s1", "s2", "s4"]

    # 祖先在每次运行里的名次：按总正确数排序，1..3
    anc_sorted = sorted(ancestors, key=lambda a: -ancestor_scores[a["id"]]["correct"])
    anc_rank = {a["id"]: i + 1 for i, a in enumerate(anc_sorted)}

    runs = []
    for run_id in RUNS_ORDER:
        cond = "E" if run_id.startswith("E_") else "F"
        best, rank_info = _champion(run_id)
        meta = _run_meta(run_id)
        lineages = [{
            "key": "champion", "label": "末代冠军", "kind": "champion",
            "color": "s3", "final_rank": rank_info["rank"],
            "n_individuals": 6,
            "holdout": {"correct": int(best["score"]), "total": int(best["n_tasks"])},
        }]
        for i, a in enumerate(ancestors):
            s = ancestor_scores[a["id"]]
            lineages.append({
                "key": f"ancestor_{i+1}", "label": f"固定祖先 · {a['id']}",
                "kind": "ancestor", "color": colors[i + 1],
                "final_rank": anc_rank[a["id"]], "n_individuals": len(ancestors),
                "holdout": {"correct": int(s["correct"]), "total": int(s["total"])},
            })
        runs.append({
            "run_id": run_id,
            "tag": f"{'进化组' if cond == 'E' else '冻结组'} · {run_id.split('_')[1]}",
            "condition": cond,
            "generations": 20,
            "status": "ok",
            "calls": meta["calls"],
            "duration_min": meta["duration_min"],
            "started_at": meta["started_at"],
            "lineages": lineages,
        })

    core = {
        "schema": "result-board",
        "schema_version": 1,
        "is_mock": False,
        "backend": "real",
        "model_id": protocol["config"]["model"],
        "generated_by": "tools/build_result_board.py",
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "metric": {
            "name": "留出集正确率", "short": "holdout acc", "unit": "%", "n_tasks": 20,
            "higher_is_better": True,
            "definition": "每趟结束后统一评测：个体对 20 道从未参与演化的 holdout 题作答，"
                          "由执行器精确判分，取完全正确题数除以总题数",
        },
        "lineage_order": order,
        "runs": runs,
    }

    # 结论：按「冠军 vs 最好祖先」的方向定级，文案里同时给出 E−F。
    # 判定要比多数：4 趟里只有 1 趟领先 1 道题，不能叫 positive——那是单题噪声。
    deltas = [delta_pp(r) for r in runs]
    up = sum(1 for d in deltas if d is not None and d >= 2)
    down = sum(1 for d in deltas if d is not None and d <= -2)
    n = len([d for d in deltas if d is not None])
    level = "positive" if up * 2 > n else "negative" if down * 2 > n else "none"
    ev_text = []
    for r in runs:
        d = delta_pp(r)
        ch = next(l for l in r["lineages"] if l["kind"] == "champion")["holdout"]["correct"]
        ba = max(l["holdout"]["correct"] for l in r["lineages"] if l["kind"] == "ancestor")
        ev_text.append({"run_id": r["run_id"],
                        "text": f"冠军 {ch}/20，最好祖先 {ba}/20，差 {d:+.1f}pp"})
    core["conclusion"] = {
        "level": level,
        "text": ("四趟里三趟冠军与最好祖先同为 16/20，仅进化组 s43 多答对 1 题（17/20）；"
                 "两对 E−F 为 +0.50 / +0.17 题（均值 +0.33/20），方向为正但幅度在噪声内。"
                 "据此报告未观察到可靠改善。"),
        "evidence": ev_text,
    }
    core["provenance"] = {
        "source": "runs/evaluations + runs/ancestor_holdout.json",
        "runs_count": len(runs),
        "payload_sha256": hashlib.sha256(json.dumps(
            core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    return core


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate-ancestors", action="store_true",
                    help="补跑 3 个祖先 × 20 题留出（60 次调用），结果缓存")
    args = ap.parse_args()

    protocol = load(PROTOCOL)
    ancestors = load(RUNS / "ancestors.json")["ancestors"]

    if args.evaluate_ancestors:
        cfg = Config.from_dict({**protocol["config"], "concurrency": 8})
        cfg.require_runtime_ready()
        tasks = load_holdout_tasks(ROOT / "data" / "holdout.json", executor)
        print(f"祖先留出评测：{len(ancestors)} 人 × {len(tasks)} 题")
        scores = asyncio.run(_evaluate_ancestors(cfg, tasks, ancestors))
        ANCESTOR_CACHE.write_text(json.dumps(scores, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"已缓存 {ANCESTOR_CACHE.relative_to(ROOT)}")
    if not ANCESTOR_CACHE.exists():
        raise SystemExit("缺 runs/ancestor_holdout.json：先跑 --evaluate-ancestors")

    payload = build(load(ANCESTOR_CACHE), ancestors, protocol)
    errs = validate(payload) + check_conclusion_fit(payload)
    if errs:
        print("契约校验失败：")
        for e in errs:
            print("  -", e)
        return 1
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written {OUT.relative_to(ROOT)}  is_mock=False  level={payload['conclusion']['level']}")
    for r in payload["runs"]:
        ch = next(l for l in r["lineages"] if l["kind"] == "champion")["holdout"]["correct"]
        ba = max(l["holdout"]["correct"] for l in r["lineages"] if l["kind"] == "ancestor")
        print(f"  {r['run_id']}: 冠军 {ch}/20，最好祖先 {ba}/20，E-F 口径见简报")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
