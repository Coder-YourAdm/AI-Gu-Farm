"""留出评测器（`app/evaluate.py`）的契约测试。

这三类错都出现过一次，值得各钉一条：
  1. 判分方向反了 —— 正解判 wrong，人会以为是模型不行。
  2. 结论比数据大胆 —— 差值落在单题分辨率内却写"改善"。
  3. 题面泄漏 —— 把 statement / reference 顺手写进 result.json，
     等于把留出集公开，`export.py` 的白名单就白做了。

这些测试全部离线，不打真实接口。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import executor as E          # noqa: E402
from app.evaluate_holdout import (             # noqa: E402
    LINEAGE_META, LINEAGE_ORDER, build_conclusion, load_holdout, pick_lineages,
)

HISTORY = ROOT / "runs" / "E_real_20261114" / "history.jsonl"
RESULT = ROOT / "web" / "result.json"


# ─────────────────────────── 题面与参考值 ───────────────────────────

def test_holdout_has_20_tasks_with_exact_references():
    """holdout 只有 template/params，参考值必须能现算出来，且判分认这个值。"""
    rows = load_holdout()
    assert len(rows) == 20
    for row in rows:
        task = row["task"]
        assert task["statement"].strip(), f"{task['id']} 题面为空"
        ref = task["reference"]
        assert ref, f"{task['id']} 参考值为空"
        # 用参考值反喂判分：必须判 right，否则整趟评测的数字都是废的
        if "numerator" in ref:
            good = f"{ref['numerator']}/{ref['denominator']}"
        else:
            good = str(ref.get("value"))
        assert E.judge(task, json.dumps({"value": good})) == "right", \
            f"{task['id']} 正解被判错，判分链路断了"
        assert E.judge(task, json.dumps({"value": "999999"})) == "wrong", \
            f"{task['id']} 错解被判对，判分放水"


def test_holdout_answer_format_is_declared():
    for row in load_holdout():
        assert row["answer_format"] in ("integer", "fraction or finite decimal")


# ─────────────────────────── 血脉选取 ───────────────────────────

@pytest.mark.skipif(not HISTORY.exists(), reason="缺 20 代运行记录")
def test_champion_from_last_gen_ancestors_from_gen0():
    lin = pick_lineages(HISTORY)
    recs = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lin["champion"]["gen"] == recs[-1]["gen"]
    assert lin["champion"]["id"] == recs[-1]["ranking"][0]
    # 祖先必须是起点，不能是"历史上最好那一代"，否则对照变成自欺
    assert lin["ancestor_seed_1"]["gen"] == recs[0]["gen"]
    assert lin["ancestor_seed_2"]["gen"] == recs[0]["gen"]
    for key in LINEAGE_ORDER:
        assert lin[key]["genes"]["solve_prompt"].strip()


# ─────────────────────────── 结论口径 ───────────────────────────

def _fake_runs(champ: list[int], anc: list[int], total: int = 20) -> list[dict]:
    """把每趟的冠军/祖先正确题数拼成 schema 形状，只测结论逻辑。"""
    runs = []
    for i, (c, a) in enumerate(zip(champ, anc), 1):
        runs.append({
            "run_id": f"r{i}",
            "lineages": [
                {"key": "champion", "holdout": {"correct": c, "total": total}},
                {"key": "ancestor_seed_1", "holdout": {"correct": a, "total": total}},
                {"key": "ancestor_seed_2", "holdout": {"correct": a - 1, "total": total}},
            ],
        })
    return runs


def test_conclusion_none_when_delta_within_one_task():
    """20 题里差 1 道 = 5pp，正好等于分辨率 —— 不能叫改善，也不能叫变差。"""
    conc, deltas = build_conclusion(_fake_runs([14, 13, 15], [13, 13, 14]), 20, 3)
    assert conc["level"] == "none"
    assert deltas == [5.0, 0.0, 5.0]
    assert "未观察到稳定改善" in conc["text"]


def test_conclusion_none_when_runs_disagree():
    conc, _ = build_conclusion(_fake_runs([18, 12, 16], [12, 16, 13]), 20, 3)
    assert conc["level"] == "none", "方向不一致时不许给 positive"


def test_conclusion_positive_only_if_every_run_wins():
    conc, deltas = build_conclusion(_fake_runs([18, 17, 19], [12, 12, 12]), 20, 3)
    assert conc["level"] == "positive"
    assert min(deltas) > 5.0


def test_conclusion_negative_is_allowed():
    """规格 §10：负结果是完整交付。判定必须能说出"输"，不能只会说"没变化"。"""
    conc, _ = build_conclusion(_fake_runs([10, 11, 10], [18, 18, 19]), 20, 3)
    assert conc["level"] == "negative"


def test_conclusion_has_caveat_with_resolution():
    conc, _ = build_conclusion(_fake_runs([14, 13, 15], [13, 13, 14]), 20, 3)
    assert "5.0" in conc["caveat"] and "分辨率" in conc["caveat"]


# ─────────────────────────── 结果页数据 ───────────────────────────

@pytest.mark.skipif(not RESULT.exists(), reason="尚未产出 result.json")
def test_result_json_is_not_mock_and_matches_schema():
    from app.result_schema import validate
    d = json.loads(RESULT.read_text(encoding="utf-8"))
    assert d["is_mock"] is False, "假数据必须挂红标；真数据必须写 False"
    assert d["generated_by"].startswith("app/"), "必须能追溯到生产者"
    assert validate(d) == []
    assert [l["key"] for l in d["runs"][0]["lineages"]] == d["lineage_order"]


@pytest.mark.skipif(not RESULT.exists(), reason="尚未产出 result.json")
def test_result_json_leaks_no_task_text():
    """白名单：题面、参考值、原始回答一律不许进静态目录。"""
    text = RESULT.read_text(encoding="utf-8")
    assert "statement" not in text
    assert '"reference"' not in text
    assert "raw_response" not in text
    assert "api_key" not in text


@pytest.mark.skipif(not RESULT.exists(), reason="尚未产出 result.json")
def test_result_lineage_meta_matches_page_contract():
    d = json.loads(RESULT.read_text(encoding="utf-8"))
    for run in d["runs"]:
        for l in run["lineages"]:
            meta = LINEAGE_META[l["key"]]
            # 跨趟必须同名同色，result_schema 校验指纹，这里钉住本文件自身的一致性
            assert (l["label"], l["color"], l["kind"]) == \
                   (meta["label"], meta["color"], meta["kind"])
            assert l["holdout"]["total"] == d["metric"]["n_tasks"]
