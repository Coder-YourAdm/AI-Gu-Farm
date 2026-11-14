"""20 代运行器的契约测试。

两件事必须锁死：
1. 它产出的 history.jsonl 能过 export.py 的白名单（否则大屏拿不到数据）
2. 基因算子不制造"语法破碎的 prompt"（那等于关掉能力，不是变异）

用 `runs/smoke_g0` 这份真跑产物做样本：单代、34 次调用，成本可以忽略。
文件不存在时跳过，不让 CI 依赖一次网络调用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SMOKE = ROOT / "runs" / "smoke_g0"


def test_crossover_keeps_ids_and_origin():
    from app.evolve_run import crossover
    # 算子吃的是**个体**（含 genes/meta），不是 genes —— 传错会在第 0 代崩
    a = {"genes": {"solve_prompt": "先判断题型。再选方法。", "self_check": False}, "_id": "G01"}
    b = {"genes": {"solve_prompt": "先写提纲。最后验算。", "self_check": True}, "_id": "G02"}
    child = crossover(a, b, "G00X1", 0)
    assert child["meta"]["parents"] == ["G01", "G02"]
    assert child["meta"]["origin"] == "crossover"
    assert child["meta"]["born_gen"] == 1
    # 自检开关取「或」：有能力的一方不该被交叉掉
    assert child["genes"]["self_check"] is True
    assert len(child["genes"]["solve_prompt"]) <= 200


def test_mutate_injects_rule_and_truncates():
    from app.evolve_run import mutate

    p = {"genes": {"solve_prompt": "逐步求解，最后只输出答案。", "self_check": False},
         "_id": "G01"}
    child = mutate(p, "G00M1", 0, "行列式题先确认主元不为零，再消元")
    assert "主元" in child["genes"]["solve_prompt"]
    assert len(child["genes"]["solve_prompt"]) <= 200, "基因超长会把 prompt 撑爆"
    # 没有规则可注入时，退化为翻转自检开关（仍然是一次真实变异）
    flip = mutate(p, "G00M2", 0, None)
    assert flip["genes"]["self_check"] is True


def test_mutate_does_not_mutate_parent_in_place():
    from app.evolve_run import mutate

    p = {"genes": {"solve_prompt": "逐步求解。", "self_check": False}, "_id": "G01"}
    mutate(p, "G00M1", 0, "先验算")
    assert p["genes"]["solve_prompt"] == "逐步求解。", "变异不能改到父代基因"


@pytest.mark.skipif(not (SMOKE / "history.jsonl").exists(), reason="没有 smoke 真跑产物")
def test_smoke_history_passes_export_whitelist(tmp_path):
    from app.export import export_run

    out = tmp_path / "history.json"
    payload = export_run(SMOKE, out)
    assert payload["n_gen"] == 1
    assert payload["run_id"] == "smoke_g0"
    # manifest.backend=real 是解除红标的唯一依据
    assert payload["is_mock"] is False, "真跑必须挂 REAL 标"

    gen = payload["generations"][0]
    for key in ("gen", "M", "ancestor", "individuals", "ranking", "usage", "timing"):
        assert key in gen, f"代记录缺 {key}"
    # 白名单外的键一个都不许漏
    from app.export import TOP_ALLOWED
    assert set(gen) <= set(TOP_ALLOWED)
    for ind in gen["individuals"]:
        assert {"total", "est", "n_samples"} <= set(ind)
    # 祖先题的键必须与页面读的一致
    assert set(gen["ancestor"]) <= {"integral_failure", "det_failure",
                                    "balanced_failure", "mixed_failure"}


@pytest.mark.skipif(not (SMOKE / "manifest.json").exists(), reason="没有 smoke 真跑产物")
def test_smoke_manifest_declares_real():
    man = json.loads((SMOKE / "manifest.json").read_text(encoding="utf-8"))
    assert man["backend"] == "real"
    assert man["is_mock"] is False
    assert man["model_id"], "manifest 必须记下模型 id，否则事后无法解释结果"
