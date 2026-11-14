"""kc.py 的回归：映射必须确定性、覆盖 125 题、缺型要大声报错。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.student import kc


def test_kc_ids_and_names_match():
    assert len(kc.KC_IDS) == 3
    assert set(kc.KC_NAMES) == set(kc.KC_IDS)
    assert all(kc.KC_NAMES[i] for i in kc.KC_IDS)


def test_kc_of_four_templates():
    assert kc.kc_of({"template": "poly"}) == "integral_definite"
    assert kc.kc_of({"template": "gamma"}) == "integral_improper"
    assert kc.kc_of({"template": "gaussian_odd"}) == "integral_improper"
    assert kc.kc_of({"template": "det"}) == "det"


@pytest.mark.parametrize("task", [
    {"template": "limit_rat"},      # 尚未实现的模板，不能静默归类
    {"template": ""},
    {"params": {"n": 1}},           # 缺 template
    {"template": 42},               # 类型不对
    None,
    "poly",
])
def test_kc_of_rejects_bad_task(task):
    with pytest.raises(ValueError):
        kc.kc_of(task)


def test_kc_of_accepts_full_normalized_task():
    from app.executor import normalize_task
    task = normalize_task(
        {"template": "gamma", "params": {"c": 1, "n": 2, "rate": 3}},
        task_id="T", source="dataset", by=None, gen=None,
    )
    assert kc.kc_of(task) == "integral_improper"


def test_audit_covers_all_125_tasks():
    counts = kc.audit()
    assert sum(counts.values()) == 125
    assert counts == {
        "integral_definite": 21,
        "integral_improper": 42,
        "det": 62,
    }


def test_audit_raises_when_a_kc_has_no_tasks(tmp_path):
    # 只放 det，另外两个 KC 为空
    for name in kc.PARTITIONS:
        (tmp_path / f"{name}.json").write_text(
            json.dumps([{"template": "det", "params": {"matrix": [[1, 0], [0, 1]]}}]),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="一道题都没有"):
        kc.audit(tmp_path)


def test_audit_raises_on_unknown_template(tmp_path):
    for name in kc.PARTITIONS:
        (tmp_path / f"{name}.json").write_text(
            json.dumps([
                {"template": "poly", "params": {}},
                {"template": "gamma", "params": {}},
                {"template": "gaussian_odd", "params": {}},
                {"template": "det", "params": {}},
                {"template": "unknown", "params": {}},
            ]), encoding="utf-8",
        )
    with pytest.raises(ValueError, match="未知模板"):
        kc.audit(tmp_path)


def test_audit_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        kc.audit(tmp_path)


def test_audit_accepts_tasks_wrapper(tmp_path):
    # 题库历史上出现过 {"tasks": [...]} 形式
    rows = [
        {"template": "poly", "params": {}},
        {"template": "gamma", "params": {}},
        {"template": "gaussian_odd", "params": {}},
        {"template": "det", "params": {}},
    ]
    for name in kc.PARTITIONS:
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"tasks": rows}), encoding="utf-8",
        )
    assert sum(kc.audit(tmp_path).values()) == 16


def test_name_of():
    assert kc.name_of("det") == "行列式"
    with pytest.raises(ValueError):
        kc.name_of("nope")
