#!/usr/bin/env python3
"""判定口径：别让页面说出它自己也不确定的话。

2026-11-13 真实事故
-------------------
pass1 20/20，pass2 19/20 → 页面判定 `regressed -5.0pp`。
唯一差的那道题是调用被 max_tokens 截断（infra），不是模型能力下降。
一次基建抖动被渲染成"系统变差了"，这在大屏上是要命的说谎。

本文件锁三条：
  1. infra 失败不进分母，也不进判定
  2. 容差不得小于单题分辨率（20 题 = 5pp），否则单题抖动就能翻转结论
  3. 分母为 0 时不许判 improved —— 不能靠"没数据"赢
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evolve_schema import compare_passes, verdict_of  # noqa: E402


def _pass(*flags: tuple[bool, bool]) -> dict:
    """flags = (correct, infra) 序列。"""
    return {"per_task": [
        {"id": f"K{i:02d}", "family": "det", "correct": c, "infra": f}
        for i, (c, f) in enumerate(flags, 1)
    ]}


def test_infra_failure_is_not_a_regression():
    """pass2 少对一道，但那是 infra 故障 → 不许判 regressed。"""
    p1 = _pass(*[(True, False)] * 20)
    p2 = _pass(*([(True, False)] * 19 + [(False, True)]))
    c = compare_passes(p1, p2)
    assert c["delta_pp"] == 0.0, f"infra 竟然影响 delta：{c}"
    assert c["verdict"] == "no_change"
    assert c["infra_p2"] == 1


def test_single_task_flip_is_noise_not_change():
    """20 题库里翻一道题 = 5pp，落在分辨率内，只能说"测不出来"。"""
    p1 = _pass(*([(True, False)] * 20))
    p2 = _pass(*([(True, False)] * 19 + [(False, False)]))
    c = compare_passes(p1, p2)
    assert c["resolution_pp"] == 5.0
    assert c["verdict"] == "no_change", "单题抖动被判成了变化"


def test_two_task_improvement_is_real():
    """两题以上（≥2×分辨率）才配叫 improved。"""
    p1 = _pass(*([(True, False)] * 18 + [(False, False)] * 2))
    p2 = _pass(*([(True, False)] * 20))
    c = compare_passes(p1, p2)
    assert c["delta_pp"] == 10.0
    assert c["verdict"] == "improved"


def test_two_task_drop_is_real_regression():
    p1 = _pass(*([(True, False)] * 20))
    p2 = _pass(*([(True, False)] * 18 + [(False, False)] * 2))
    assert compare_passes(p1, p2)["verdict"] == "regressed"


def test_all_infra_cannot_win():
    """分母被故障清空时不许判 improved。"""
    p1 = _pass(*([(True, False)] * 20))
    p2 = _pass(*([(False, True)] * 20))
    c = compare_passes(p1, p2)
    assert c["n2"] == 0
    assert c["verdict"] == "no_change", "靠没有数据赢了"


def test_verdict_tolerance_default_still_works():
    """基础函数保持原语义，别把闸门焊死。"""
    assert verdict_of(5.0) == "improved"
    assert verdict_of(-5.0, tol=5.0) == "no_change"
    assert verdict_of(0.0) == "no_change"
