"""make_tier_dataset 的回归：生成的两档题集必须合法、计数正确、签名互斥。

这一档题集直接决定校准结论可不可信，所以不能只"看起来对"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_tier_dataset as mtd  # noqa: E402
from app import executor  # noqa: E402


def _load(tmp_path, tier):
    assert mtd.main(["--tier", tier, "--out", str(tmp_path / f"out_{tier}")]) == 0
    rows = json.loads((tmp_path / f"out_{tier}" / "calibration.json").read_text(encoding="utf-8"))
    return rows


def _check(rows):
    assert len(rows) == 20
    assert sum(r["template"] == "det" for r in rows) == 10
    assert sum(r["template"] != "det" for r in rows) == 10
    assert len({r["signature"] for r in rows}) == 20
    # 每条都能被 executor 复算出一致的 statement 与 reference（不手写答案）
    for r in rows:
        raw = {"template": r["template"], "params": r["params"]}
        ok, reason = executor.validate_task(raw)
        assert ok, reason
        assert executor.statement(raw["template"], raw["params"]) == r["statement"]
        ref = executor.solve_exact(raw)
        assert ref == r["reference"]


def test_t1_is_valid_and_easy(tmp_path):
    rows = _load(tmp_path, "t1")
    _check(rows)
    # t1 的 det 全是 2–3 阶
    assert all(len(r["params"]["matrix"]) <= 3 for r in rows if r["template"] == "det")


def test_mix_keeps_integrals_and_downgrades_det(tmp_path):
    base = json.loads((ROOT / "data" / "calibration.json").read_text(encoding="utf-8"))
    rows = _load(tmp_path, "mix")
    _check(rows)
    # mix 的积分与底卷一致，det 全换成 2–3 阶
    base_int = {r["signature"] for r in base if r["template"] != "det"}
    mix_int = {r["signature"] for r in rows if r["template"] != "det"}
    assert base_int == mix_int, "mix 的积分必须与底卷一致，只改 det"
    assert all(len(r["params"]["matrix"]) <= 3 for r in rows if r["template"] == "det")
