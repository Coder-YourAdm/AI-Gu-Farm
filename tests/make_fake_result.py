"""造多趟独立运行的留出评测假数据，供 web/result.html 联调。

**这是假数据，一个数字都不许进比赛结论。**
真数据由留出评测（规格 §9「留出表能算出来」）产出，字段契约见本文件顶部注释与
app/result_schema.py；页面只认 result.json，不认别的。

    用法： python tests/make_fake_result.py
    产出： web/result.json

设计上刻意让第 3 趟是负结果（冠军没跑赢最好祖先）。技术规格 §10 明确
「出现负结果仍可形成完整交付」，所以结果页必须能扛住负结果，而不是只在
赢了的时候好看。
"""

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "web" / "result.json"

GENERATED_AT = "2026-11-13T19:40:00+08:00"

# 留出集规模（规格 §2.2：holdout 20 道）。metric.n_tasks 与每条 lineage 的
# holdout.total 都从这里取，避免两处各写一个 20 然后对不上。
HOLDOUT_TOTAL = 20

# ── 三条比较对象 ────────────────────────────────────────────────
# 一条冠军 + 两条固定祖先。规格 §6.2：祖先由 calibration 均分前三选出，
# 冻结组复用同一套祖先，所以跨 run 的祖先必须同名同色，否则评委看不出对照。
LINEAGE_ORDER = ["champion", "ancestor_integral", "ancestor_det"]
LINEAGE_META = {
    "champion": {"label": "末代冠军", "kind": "champion", "color": "s3"},
    "ancestor_integral": {"label": "固定祖先 · 积分", "kind": "ancestor", "color": "s1"},
    "ancestor_det": {"label": "固定祖先 · 行列式", "kind": "ancestor", "color": "s2"},
}

# ── 三趟独立运行（末代 6 个体，排名 1..6） ───────────────────────
# holdout 每趟每人 20 题（规格 §2.2）。calls 按 §10「20 代满窗口 E=2289」取整。
RUNS_RAW = [
    {
        "run_id": "r1113E01",
        "tag": "第 1 趟",
        "condition": "E",
        "condition_note": "完整代事务",
        "generations": 20,
        "status": "ok",
        "calls": 2289,
        "duration_min": 74,
        "started_at": "2026-11-13T10:12:00+08:00",
        "final_rank": {"champion": 1, "ancestor_integral": 4, "ancestor_det": 5},
        "holdout": {"champion": 15, "ancestor_integral": 13, "ancestor_det": 12},
    },
    {
        "run_id": "r1113E02",
        "tag": "第 2 趟",
        "condition": "E",
        "condition_note": "完整代事务",
        "generations": 20,
        "status": "ok",
        "calls": 2289,
        "duration_min": 79,
        "started_at": "2026-11-13T13:05:00+08:00",
        "final_rank": {"champion": 1, "ancestor_integral": 3, "ancestor_det": 6},
        "holdout": {"champion": 14, "ancestor_integral": 14, "ancestor_det": 11},
    },
    {
        "run_id": "r1113F01",
        "tag": "第 3 趟",
        "condition": "F",
        "condition_note": "冻结组对照",
        "generations": 20,
        "status": "ok",
        "calls": 2232,
        "duration_min": 68,
        "started_at": "2026-11-13T15:48:00+08:00",
        "final_rank": {"champion": 2, "ancestor_integral": 1, "ancestor_det": 4},
        "holdout": {"champion": 12, "ancestor_integral": 13, "ancestor_det": 12},
    },
]

# ── 结论：一句话 + 可追溯的证据 ──────────────────────────────────
# level 只允许 positive / none / negative。[none] 就是「未观察到改善」，
# 规格 §10 明确这算完整交付。页面按 level 上色，不自己造句。
CONCLUSION = {
    "level": "none",
    "text": "三趟独立运行中，末代冠军的留出正确率相对最好祖先为 +10.0 / 0.0 / −5.0 个百分点，"
            "方向不一致；未观察到稳定改善。",
    "evidence": [
        {"run_id": "r1113E01", "text": "冠军 15/20 对最好祖先 13/20，末代排名第 1"},
        {"run_id": "r1113E02", "text": "冠军 14/20 与积分祖先 14/20 持平，末代排名第 1"},
        {"run_id": "r1113F01", "text": "冠军 12/20 低于积分祖先 13/20，末代排名第 2（该趟为冻结组对照）"},
    ],
    "caveat": "留出集 20 题，单个个体的正确率分辨率为 5 个百分点；"
              "本结论不外推到 20 题以外的难度分布。",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build() -> dict:
    runs = []
    for raw in RUNS_RAW:
        lineages = []
        for key in LINEAGE_ORDER:
            meta = LINEAGE_META[key]
            correct = raw["holdout"][key]
            lineages.append({
                "key": key,
                "label": meta["label"],
                "kind": meta["kind"],
                "color": meta["color"],
                # 分母来自数据，页面不许写死 20 / 6
                "final_rank": raw["final_rank"][key],
                "n_individuals": 6,
                "holdout": {
                    "correct": correct,
                    "total": HOLDOUT_TOTAL,
                },
            })
        runs.append({
            "run_id": raw["run_id"],
            "tag": raw["tag"],
            "condition": raw["condition"],
            "condition_note": raw["condition_note"],
            "generations": raw["generations"],
            "status": raw["status"],
            "calls": raw["calls"],
            "duration_min": raw["duration_min"],
            "started_at": raw["started_at"],
            "lineages": lineages,
        })

    body = {
        "schema": "result-board",
        "schema_version": 1,
        "generated_by": "tests/make_fake_result.py",
        "generated_at": GENERATED_AT,
        # 页面靠这个字段决定是否挂「mock 未接真模型」红标
        "is_mock": True,
        "backend": "mock",
        "model_id": "mock/offline",
        "note": "本文件由假数据生成，仅用于结果页联调；现场的正式数字必须来自留出评测产出的同名文件。",
        "metric": {
            "name": "留出集正确率",
            "short": "holdout acc",
            "unit": "%",
            "n_tasks": HOLDOUT_TOTAL,
            "higher_is_better": True,
            "definition": "每趟结束后统一评测：个体对 20 道从未参与演化的 holdout 题作答，"
                          "由执行器精确判分，取完全正确题数除以总题数",
        },
        "lineage_order": LINEAGE_ORDER,
        "runs": runs,
        "conclusion": CONCLUSION,
    }
    body["provenance"] = {
        "source": "holdout evaluation",
        "runs_count": len(runs),
        "payload_sha256": _sha(json.dumps(body, ensure_ascii=False, sort_keys=True)),
    }
    return body


def main() -> int:
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写出 {OUT.relative_to(ROOT)} · {len(data['runs'])} 趟 · {OUT.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
