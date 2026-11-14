"""留出卷 JSONL：必须能被 B 的 `load_holdout_tasks()` 直接吃下。

B7 的约定（`app/evaluate.py`）：
  · 一题一行，只认 `id` / `template` / `params`
  · `reference` 由他重算，不采信文件
  · 题型配额 integral=10 / det=10，不符直接抛

我的 `data/holdout.json` 是 JSON 数组，格式不对会让 B 报「留出卷不存在」，
而这个报错会被误读成「C 没交题」——所以必须有这条测试盯着。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.executor import validate_task  # noqa: E402

JSONL = ROOT / "data" / "holdout.jsonl"
SOURCE = ROOT / "data" / "holdout.json"

EXPECT_MIX = {"integral": 10, "det": 10}


@pytest.fixture(scope="module")
def lines() -> list[dict]:
    assert JSONL.is_file(), "缺 data/holdout.jsonl，先跑 python tests/make_holdout_jsonl.py"
    text = JSONL.read_text(encoding="utf-8")
    rows = [json.loads(x) for x in text.splitlines() if x.strip()]
    assert rows, "留出卷为空"
    return rows


def test_one_object_per_line(lines):
    """JSONL 的硬要求：每行一个独立对象，不许是一个数组。"""
    raw = JSONL.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == len(lines) == 20
    assert not raw[0].lstrip().startswith("[")


def test_only_three_keys(lines):
    """只给 id/template/params。多写 reference 就是造第二个真值来源。"""
    for r in lines:
        assert set(r) == {"id", "template", "params"}, f"{r.get('id')} 键集合不对：{set(r)}"


def test_ids_unique_and_prefixed(lines):
    ids = [r["id"] for r in lines]
    assert len(set(ids)) == 20
    assert all(i.startswith("Y") for i in ids)


def test_mix_matches_b_quota(lines):
    """B 的 HOLDOUT_MIX 是硬校验，不符他直接报错。"""
    types = {"integral": 0, "det": 0}
    src = {t["id"]: t["type"] for t in json.loads(SOURCE.read_text(encoding="utf-8"))}
    for r in lines:
        types[src[r["id"]]] += 1
    assert types == EXPECT_MIX


def test_every_task_passes_validate(lines):
    """B 会逐行调 validate_task，一道不过就整份留出卷装载失败。"""
    for r in lines:
        ok, reason = validate_task({"template": r["template"], "params": r["params"]})
        assert ok, f"{r['id']} 校验不过：{reason}"


def test_in_sync_with_holdout_json(lines):
    """JSONL 必须与 holdout.json 同源，防止改了 JSON 忘了重生成 JSONL。"""
    src = {t["id"]: (t["template"], t["params"])
           for t in json.loads(SOURCE.read_text(encoding="utf-8"))}
    assert set(src) == {r["id"] for r in lines}
    for r in lines:
        assert src[r["id"]] == (r["template"], r["params"]), f"{r['id']} 与源文件不一致"
