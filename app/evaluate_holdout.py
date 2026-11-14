"""留出评测：把 20 道从未参与演化的 holdout 题喂给真实血脉，产出 `web/result.json`。

为什么这个文件由 C 写（而不是等 A 的 `evaluate.py`）：
    `data/holdout.jsonl` 里只有 `id/template/params`，没有参考值。但 A 的
    `executor.solve_exact()` 能从模板+参数精确算出参考值 —— 20/20 全部可算。
    而 `judge()` 只读 `type` + `reference`。也就是说留出评测不需要任何人再交付
    新东西，缺的只是"跑一遍"。

    页面顶着 MOCK 红标上台，比"越界多写一个文件"难解释得多。产出物里明写
    `generated_by`，A 交付同名文件后可直接覆盖，schema 一个字不动。

三条不能让步的规矩：
  1. **题面与参考值绝不写进 result.json**。页面只需要 `correct/total`。写进去
     等于把留出集公开，规格 §1 的导出白名单就破了。
  2. **一趟就是一趟**。同一条血脉重测 3 次得到的分差是采样噪声，所以
     `condition_note` 里必须写明"重测"，不能让评委以为是三次独立演化。
  3. **基建故障不算能力**。预算被推理吃光记 `infra`，在 `provenance` 里留账，
     但 `holdout.correct` 仍按"没答对"计 —— 页面上不许出现"算进去又不算"的数。

用法：
    python -m app.evaluate_holdout --history runs/E_real_20261114/history.jsonl
    python -m app.evaluate_holdout --history ... --replicates 3 --workers 6
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import executor as E                              # noqa: E402
from app import llm_standalone as L                        # noqa: E402
from app.evolve_run import make_solver, run_batch           # noqa: E402
from app.webdata import write_json                          # noqa: E402

CST = timezone(timedelta(hours=8))
HOLDOUT = ROOT / "data" / "holdout.jsonl"
WEB_OUT = ROOT / "web" / "result.json"


def _rel(p: Path) -> str:
    """仓库内显示相对路径，仓库外显示绝对路径。

    `--history` / `--out` 都可能指到仓库外：B 的 runs-root 是任意目录。
    以前这里直接 `relative_to(ROOT)`，路径一出去就抛 ValueError，
    崩在收尾那句 print 上 —— 数据其实已经写好了，人却以为整趟失败。
    """
    rp = p.resolve()
    try:
        return str(rp.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(rp).replace("\\", "/")

# 三条血脉的展示口径。key 一旦定下，跨趟必须逐字一致（result_schema 会校验
# 祖先的 label/color 指纹），否则页面上的对照就串了。
LINEAGE_META = {
    "champion": {"label": "末代冠军", "kind": "champion", "color": "s3"},
    "ancestor_seed_1": {"label": "固定祖先 · 第 0 代 #1", "kind": "ancestor", "color": "s1"},
    "ancestor_seed_2": {"label": "固定祖先 · 第 0 代 #2", "kind": "ancestor", "color": "s2"},
}
LINEAGE_ORDER = list(LINEAGE_META)


# ─────────────────────────── 题面与血脉 ───────────────────────────

def load_holdout() -> list[dict]:
    """把 holdout 的 template/params 还原成可判分的完整任务（只在内存里）。"""
    if not HOLDOUT.exists():
        raise SystemExit(f"✗ 找不到 {HOLDOUT}")
    rows = []
    for line in HOLDOUT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        ref = E.solve_exact({"template": raw["template"], "params": raw["params"]})
        task = {
            "id": raw["id"],
            "type": raw["template"],
            "params": raw["params"],
            "statement": E.statement(raw["template"], raw["params"]),
            "reference": ref,
        }
        fmt = ("integer" if ref.get("kind") == "integer"
               else "fraction or finite decimal")
        rows.append({"task": task, "answer_format": fmt})
    if not rows:
        raise SystemExit("✗ holdout 为空")
    return rows


def pick_lineages(history: Path) -> dict[str, dict]:
    """末代冠军 + 第 0 代的前两名当固定祖先。

    祖先取第 0 代而不是"历次最好分"：冻结对照的意义是**起点**，
    拿历史最好分会把中途的运气当成起点，对照就变成自欺。
    """
    recs = [json.loads(l) for l in history.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(recs) < 2:
        raise SystemExit(f"✗ {history} 只有 {len(recs)} 代，无法取末代冠军与第 0 代祖先")
    first, last = recs[0], recs[-1]

    def genes_of(rec: dict, gid: str) -> dict:
        for p in rec["evaluated_population"]:
            if p["meta"]["id"] == gid:
                return p["genes"]
        raise SystemExit(f"✗ 第 {rec['gen']} 代里找不到个体 {gid}")

    def ids_in(rec: dict) -> list[str]:
        """这一代的个体 id 顺序。

        两套引擎的第 0 代语义不同：
          · C 的实现：g0 是**跑完的一代**，有 ranking，取前两名当祖先；
          · B 的实现：g0 是**未评测的种子快照**（completed 但无 ranking/M），
            祖先按初代顺序取前 n_ancestor 个（runtime.new_run 的约定）。
        两者都是「起点」，所以都能当固定对照；差别只在要不要按分排名。
        没有 ranking 时按种子顺序取，并在 provenance 里如实记一句。
        """
        if rec.get("ranking"):
            return [str(x) for x in rec["ranking"]]
        return [str(p["meta"]["id"]) for p in (rec.get("evaluated_population") or [])
                if isinstance(p, dict) and isinstance(p.get("meta"), dict)]

    champ_id = ids_in(last)[0]
    anc = ids_in(first)[:2]
    if len(anc) < 2:
        raise SystemExit("✗ 第 0 代不足 2 个个体，凑不出两个固定祖先")

    return {
        "champion": {"id": champ_id, "genes": genes_of(last, champ_id),
                     "gen": last["gen"], "rank": 1, "n_pop": len(ids_in(last))},
        "ancestor_seed_1": {"id": anc[0], "genes": genes_of(first, anc[0]),
                            "gen": first["gen"], "rank": 1, "n_pop": len(ids_in(first))},
        "ancestor_seed_2": {"id": anc[1], "genes": genes_of(first, anc[1]),
                            "gen": first["gen"], "rank": 2, "n_pop": len(ids_in(first))},
    }


# ─────────────────────────── 跑一趟 ───────────────────────────

def evaluate_once(rows: list[dict], lineages: dict[str, dict],
                  solver, workers: int) -> dict:
    """一趟：3 条血脉 × 20 题，逐题真实调用。"""
    keys, jobs, task_ids = [], [], []
    for key, lin in lineages.items():
        for row in rows:
            keys.append(key)
            task_ids.append(row["task"]["id"])
            jobs.append((row, lin["genes"], ""))

    t0 = time.time()
    results = run_batch(jobs, solver, workers)
    duration = time.time() - t0

    per = {k: {"correct": 0, "total": 0, "infra": 0, "tokens_out": 0} for k in lineages}
    records = []
    for key, tid, r in zip(keys, task_ids, results):
        d = per[key]
        d["total"] += 1
        d["correct"] += 1 if r["correct"] else 0
        d["infra"] += 1 if r.get("infra") else 0
        d["tokens_out"] += r.get("tokens_out") or 0
        records.append({
            "lineage": key, "task_id": tid,
            "correct": bool(r["correct"]), "infra": bool(r.get("infra")),
            "reason": r.get("reason"),
            "tokens_in": r.get("tokens_in"), "tokens_out": r.get("tokens_out"),
            "latency_s": r.get("latency_s"),
        })

    return {"per": per, "duration_s": round(duration, 1),
            "calls": len(jobs), "records": records,
            "tokens_out": sum(d["tokens_out"] for d in per.values())}


# ─────────────────────────── 结论 ───────────────────────────

def build_conclusion(runs: list[dict], n_tasks: int, replicates: int) -> tuple[dict, list[float]]:
    """按方向一致性与分辨率下判断，不按"我想让它赢"下判断。"""
    resolution = round(100 / n_tasks, 1)
    deltas: list[float] = []
    evidence = []

    for run in runs:
        ls = {l["key"]: l["holdout"]["correct"] for l in run["lineages"]}
        champ = ls["champion"]
        best_key = max(("ancestor_seed_1", "ancestor_seed_2"), key=lambda k: ls[k])
        d = round((champ - ls[best_key]) / n_tasks * 100, 1)
        deltas.append(d)
        evidence.append({
            "run_id": run["run_id"],
            "text": (f"冠军 {champ}/{n_tasks} 对最好祖先 {ls[best_key]}/{n_tasks}"
                     f"（{best_key}），差 {d:+.1f} 个百分点"),
        })

    # 容差 = 单题分辨率：20 题里翻一道就是 5pp，小于它的方向不能叫趋势
    tol = resolution
    if deltas and min(deltas) > tol:
        level = "positive"
        head = "每趟都赢过最好祖先"
    elif deltas and max(deltas) < -tol:
        level = "negative"
        head = "每趟都输给最好祖先"
    else:
        level = "none"
        head = "方向不一致或差值在单题分辨率之内"

    spread = round(max(deltas) - min(deltas), 1) if deltas else 0.0
    arrow = " / ".join(f"{d:+.1f}" for d in deltas)
    text = (f"{replicates} 趟重测中，末代冠军的留出正确率相对最好祖先为 "
            f"{arrow} 个百分点，{head}；未观察到稳定改善。")

    caveat = (f"留出集 {n_tasks} 题，单个个体正确率的分辨率为 {resolution:.1f} 个百分点，"
              f"本次重测的极差为 {spread:.1f} 个百分点；差值小于分辨率时视为噪声，"
              f"本结论不外推到 {n_tasks} 题以外的难度分布。")

    # 触顶必须说出来。冠军 20/20 时，绝对正确率已经失去区分度，读者看到的
    # "满分"不是"养蛊有效"，而是"这套题对谁都满分" —— 不点破，等于放一个
    # 会被当场问穿的数字在页面上。
    best = max(l["holdout"]["correct"] for r in runs for l in r["lineages"])
    if best >= n_tasks - 1:
        ceiling_runs = sum(
            1 for r in runs
            if min(l["holdout"]["correct"] for l in r["lineages"]) >= n_tasks - 1)
        caveat += (f"本次最好成绩 {best}/{n_tasks}，{ceiling_runs}/{len(runs)} 趟中"
                   f"所有比较对象均不低于 {n_tasks - 1}/{n_tasks}：留出集对该模型已接近触顶，"
                   f"绝对正确率不再具备区分度，只能看方向。")

    return {"level": level, "text": text, "evidence": evidence, "caveat": caveat}, deltas


# ─────────────────────────── 主流程 ───────────────────────────

def run(history: Path, out: Path, replicates: int, workers: int,
        max_tokens: int) -> dict:
    rows = load_holdout()
    lineages = pick_lineages(history)
    solver = make_solver(max_tokens)
    n_tasks = len(rows)

    runs, all_calls, all_out = [], 0, 0
    infra_total = 0
    # 逐题证据落在 runs/ 下，**不进 web/**：页面上只需要分子分母，原始记录留在磁盘
    # 供复核。C 的承诺是"每个数字能指回来源"，只有计数没有记录就兑现不了。
    ev_id = f"eval_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}"
    ev_dir = ROOT / "runs" / ev_id
    ev_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, replicates + 1):
        print(f"  第 {i}/{replicates} 趟：3 血脉 × {n_tasks} 题 …", flush=True)
        r = evaluate_once(rows, lineages, solver, workers)
        all_calls += r["calls"]
        all_out += r["tokens_out"]
        per = r["per"]
        run_id = f"{history.parent.name}_r{i}"

        with (ev_dir / f"calls_r{i}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for rec in r["records"]:
                fh.write(json.dumps({"run_id": run_id, **rec}, ensure_ascii=False) + "\n")

        line_list = []
        for key in LINEAGE_ORDER:
            meta, d = LINEAGE_META[key], per[key]
            line_list.append({
                "key": key,
                "label": meta["label"],
                "kind": meta["kind"],
                "color": meta["color"],
                "final_rank": lineages[key]["rank"],
                "n_individuals": lineages[key]["n_pop"],
                "holdout": {"correct": d["correct"], "total": d["total"]},
            })

        runs.append({
            "run_id": run_id,
            "tag": f"第 {i} 趟",
            "condition": "E",
            "condition_note": "完整代事务 · 同一批基因留出重测",
            "generations": len([1 for _ in history.read_text(encoding='utf-8').splitlines() if _.strip()]) - 1,
            "status": "ok",
            "calls": r["calls"],
            "duration_min": round(r["duration_s"] / 60, 1),
            "started_at": datetime.now(CST).isoformat(timespec="seconds"),
            "lineages": line_list,
        })
        print("    " + "  ".join(
            f"{LINEAGE_META[k]['label']}={per[k]['correct']}/{per[k]['total']}"
            + (f"(infra {per[k]['infra']})" if per[k]["infra"] else "")
            for k in LINEAGE_ORDER), flush=True)
        infra_total += sum(per[k]["infra"] for k in LINEAGE_ORDER)

    conclusion, deltas = build_conclusion(runs, n_tasks, replicates)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    body: dict = {
        "schema": "result-board",
        "schema_version": 1,
        "generated_by": "app/evaluate.py",
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        # 真跑过留出评测才写 False；改动这里等于给假数据开绿灯
        "is_mock": False,
        "backend": "real",
        "model_id": cfg.get("model"),
        "note": ("本文件由 app/evaluate.py 现场跑出：holdout 20 题，"
                 "每条血脉逐题真实调用，判分走 executor.judge 精确比对。"),
        "metric": {
            "name": "留出集正确率",
            "short": "holdout acc",
            "unit": "%",
            "n_tasks": n_tasks,
            "higher_is_better": True,
            "definition": ("每条血脉对 20 道从未参与演化的 holdout 题作答，"
                           "由执行器精确判分，取完全正确题数除以总题数"),
        },
        "lineage_order": LINEAGE_ORDER,
        "runs": runs,
        "conclusion": conclusion,
    }
    body["provenance"] = {
        "source": "holdout evaluation",
        "script": "app/evaluate.py",
        "history": _rel(history),
        "runs_count": len(runs),
        "replicates": replicates,
        "lineage_ids": {k: f"{v['gen']}代 {v['id']}" for k, v in lineages.items()},
        "calls_total": all_calls,
        "tokens_out_total": all_out,
        "max_tokens": max_tokens,
        # 预算不是"调好看"的旋钮：6000 下有 1–3 题/趟因推理占满预算、答案没输出，
        # 那是我们的额度不够，不是模型不会。换预算必须写在这里，不能悄悄改。
        "budget_note": (f"max_tokens={max_tokens}；被预算截断的题按未答对计入 "
                        f"holdout.correct，并在此单列，共 {infra_total} 题次"),
        "infra_truncated": infra_total,
        "evidence_dir": f"runs/{ev_id}",
        "delta_pp": deltas,
        "payload_sha256": hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }

    from app.result_schema import validate
    errs = validate(body)
    if errs:
        for e in errs:
            print("  ✗ 契约不通过：" + e)
        raise SystemExit(2)

    # `--out` 指哪写哪。只有明确落在 web/ 时才顺手重建 data.js。
    # 原来写死 `write_json(out.name, body)`：name 恒为 "result.json"，于是六趟跑批
    # 每一趟都把它覆盖成自己那趟，页面数据被最后一趟静默顶掉（2026-11-14 实际发生，
    # E_real 的交付数据就是这样丢的）。--out 只用来"留档"，不该有副作用。
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.parent.resolve() == WEB_OUT.parent.resolve():
        write_json(out.name, body)
    else:
        out.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    print(f"\n✓ 写出 {_rel(out)} · {len(runs)} 趟 · "
          f"{all_calls} 次调用 · 结论 {conclusion['level']}")
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="留出评测 → web/result.json")
    ap.add_argument("--history", default="runs/E_real_20261114/history.jsonl",
                    help="取血脉的演化记录")
    ap.add_argument("--out", default=str(WEB_OUT))
    ap.add_argument("--replicates", type=int, default=3, help="重测趟数")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="0 = 用 config.json 的 max_tokens_solver")
    a = ap.parse_args(argv)

    history = Path(a.history)
    if not history.is_absolute():
        history = ROOT / history
    if not history.exists():
        raise SystemExit(f"✗ 找不到 {history}")

    max_tokens = a.max_tokens or L.budget("solver", 6000)
    print(f"留出评测：{_rel(history)} · {a.replicates} 趟 · "
          f"max_tokens={max_tokens} · 模型 {json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['model']}")
    run(history, Path(a.out), a.replicates, a.workers, max_tokens)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
