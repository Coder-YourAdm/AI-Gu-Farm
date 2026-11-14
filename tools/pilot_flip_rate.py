"""pilot 必产数之二：fitness_window = 1 与 = 2 的排名翻转率。

定义（大纲 §13 / 技术规格 §7.3）：
    翻转率 = 相邻两代 top-3 集合的变化比例
    top-3 取当代 ranking 前三

口径要点（这一条决定数字有没有意义）：
    种群每代换 ID（子代是 G01X1 这种新 ID），按 ID 比 top-3 会永远 100% 翻转。
    必须按「基因指纹」比 —— solve_prompt + self_check 完全相同才算同一个体。
    这正是 v5.1 引入基因级 key 的原因：基因不变而 ID 变，是精英克隆，不是翻转。

用法：python flip_rate.py <run_dir...>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def gene_fp(genes: dict) -> str:
    """基因指纹：三字段规范化 JSON 的 sha1 前 12 位。"""
    blob = json.dumps(genes, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def load_gens(run_dir: Path) -> list[dict]:
    rows = []
    path = run_dir / "history.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def top3_fingerprints(row: dict) -> list[str]:
    """当代 ranking 前三对应的基因指纹。"""
    fp_of = {}
    for ind in row.get("evaluated_population") or []:
        meta = ind.get("meta") or {}
        gid = meta.get("id")
        if gid:
            fp_of[gid] = gene_fp(ind.get("genes") or {})
    out = []
    for gid in (row.get("ranking") or [])[:3]:
        # 排名里出现却没被评测的个体，用 ID 兜底并记下来（正常不该发生）
        out.append(fp_of.get(gid, "MISSING:" + str(gid)))
    return out


def flip_rate(rows: list[dict]) -> dict:
    tops = [(r.get("gen"), top3_fingerprints(r)) for r in rows]
    per_step = []
    for (g_prev, a), (g_cur, b) in zip(tops, tops[1:]):
        changed = len(set(a) ^ set(b))
        # 集合对称差 / 3，按「位置无关」口径；同时记录严格同集合
        per_step.append(
            {
                "gen_from": g_prev,
                "gen_to": g_cur,
                "top3_from": a,
                "top3_to": b,
                "n_changed_members": changed,
                "rate": round(changed / 3.0, 4),
            }
        )
    n = len(per_step)
    mean_rate = round(sum(p["rate"] for p in per_step) / n, 4) if n else None
    return {"n_steps": n, "mean_rate": mean_rate, "per_step": per_step}


def summarize(run_dir: Path) -> dict:
    rows = load_gens(run_dir)
    man = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    res = flip_rate(rows)
    # 结构描述：每代 top-3 里有没有重复指纹（同一基因占多席 = 精英克隆）
    tops = [(r.get("gen"), top3_fingerprints(r)) for r in rows]
    return {
        "run_id": man.get("run_id"),
        "fitness_window": man.get("fitness_window"),
        "backend": man.get("backend"),
        "is_mock": man.get("is_mock"),
        "model_id": man.get("model_id"),
        "n_gen_rows": len(rows),
        "n_steps": res["n_steps"],
        "mean_flip_rate": res["mean_rate"],
        "per_step": res["per_step"],
        "top3_by_gen": [{"gen": g, "top3": t} for g, t in tops],
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = {}
    for d in argv:
        p = Path(d)
        if not (p / "history.jsonl").exists():
            print(f"[skip] {p} 没有 history.jsonl")
            continue
        s = summarize(p)
        out[p.name] = s
        print(f"=== {p.name} ===")
        print(f"  fitness_window = {s['fitness_window']}  model = {s['model_id']}  is_mock = {s['is_mock']}")
        print(f"  代行数 = {s['n_gen_rows']}  相邻步数 = {s['n_steps']}  平均翻转率 = {s['mean_flip_rate']}")
        for st in s["per_step"]:
            print(f"    g{st['gen_from']}→g{st['gen_to']}  变化 {st['n_changed_members']}/3  rate={st['rate']}")
    print("\n" + json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "per_step"} for k, v in out.items()},
                           ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
