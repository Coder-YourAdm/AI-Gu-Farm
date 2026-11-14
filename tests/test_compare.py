"""3×3 对照实验回归：单变量、公式自洽、winner 来自聚合而非单点。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import check_web_contract as cwc  # noqa: E402
import compare  # noqa: E402


def test_shape_and_all_required_fields():
    d = compare.build(seed=42)
    for key in cwc.COMPARE_TOP:
        assert key in d, key
    assert len(d["rows"]) == 9
    assert d["students"] == ["weak", "mid", "strong"]
    assert d["policies"] == ["loose", "verbose", "answer_only"]
    pairs = {(r["student"], r["policy"]) for r in d["rows"]}
    assert len(pairs) == 9


def test_every_row_is_formula_consistent():
    """页面逐行自检的同口径：差 > 0.002 就该标红。"""
    for row in compare.build()["rows"]:
        exp_c = cwc._expected_cost(row["items"], row["hints"])
        exp_f = cwc._expected_fitness(row["gain"], exp_c)
        assert abs(row["cost"] - exp_c) <= 0.002, row
        assert abs(row["fitness"] - exp_f) <= 0.002, row


def test_real_compare_json_passes_contract():
    assert cwc.check_compare(ROOT / "web" / "compare.json") == []


def test_single_variable_gain_is_identical_across_policies():
    """三套教法在同一档学生下，第一次（无提示）答题必须逐位相同。"""
    d = compare.build(seed=42)
    for level in d["students"]:
        gains = {r["gain"] for r in d["rows"] if r["student"] == level}
        assert len(gains) == 1, (level, gains)


def test_winner_is_best_policy_by_mean_not_best_row():
    d = compare.build(seed=42)
    means = {
        name: sum(r["fitness"] for r in d["rows"] if r["policy"] == name) / 3
        for name in d["policies"]
    }
    assert d["winner"]["policy"] == max(means, key=lambda n: means[n])
    assert d["winner"]["student"] in d["students"]
    for name, value in means.items():
        assert d["policy_means"][name] == round(value, 3)


def test_answer_only_is_penalised_by_cost():
    """减成本项真的在起作用：直接给答案不该靠摊答案赢。"""
    d = compare.build(seed=42)
    assert d["policy_means"]["loose"] > d["policy_means"]["answer_only"]


def test_build_is_deterministic():
    a = compare.build(seed=42)
    b = compare.build(seed=42)
    assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
           json.dumps(b, sort_keys=True, ensure_ascii=False)


def test_seeds_are_stable_and_item_scoped():
    assert compare.derive_item_seed(42, "weak", 0) == \
           compare.derive_item_seed(42, "weak", 0)
    assert compare.derive_item_seed(42, "weak", 0) != \
           compare.derive_item_seed(42, "weak", 1)
    assert compare.derive_item_seed(42, "weak", 0) != \
           compare.derive_item_seed(42, "mid", 0)


def test_run_one_rejects_unknown_level():
    with pytest.raises(ValueError, match="学生档位"):
        compare.run_one("genius", "loose")


def test_cli_writes_and_self_checks(tmp_path):
    out = tmp_path / "compare.json"
    assert compare.main(["--seed", "42", "--out", str(out)]) == 0
    assert cwc.check_compare(out) == []
    first = out.read_bytes()
    assert compare.main(["--seed", "42", "--out", str(out)]) == 0
    assert out.read_bytes() == first
