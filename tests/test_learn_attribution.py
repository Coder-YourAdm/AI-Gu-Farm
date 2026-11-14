#!/usr/bin/env python3
"""学习归因的边界：机器不能从自己的故障里学东西。

2026-11-13 真实事故
-------------------
`deepseek-flash` 是推理模型，K11（5 阶行列式）推理烧掉 4000 token 后答案没出来，
`complete()` 抛错，双跑把它记成「调用异常」。原来的 `learn_from()` 不看原因，
只看到 det 族 1/10 失败，就自动写出一条规则：

    「Bareiss 消元每步检查主元；主元为 0 时交换行、结果乘 -1」

这条规则对截断毫无用处，却被注入下一趟的 prompt —— 学习器在自己造的故障上
认认真真地学错了方向。这类污染比学不到东西更糟：它看起来像在学习。

所以本文件锁三条：
  1. 标了 infra 的失败不进规则库、不进记忆库
  2. 但它们必须留在账上（infra_excluded），不能被悄悄抹掉
  3. 真实能力失败照旧能触发学习 —— 别把闸门关死
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo import learn_from, seed_state  # noqa: E402


def _task(tid: str, family: str, correct: bool, infra: bool = False,
          reason: str = "答案与期望值不符") -> dict:
    return {"id": tid, "family": family, "correct": correct, "infra": infra,
            "reason": reason, "answer_chars": 4, "tokens_in": 0,
            "tokens_out": 0, "latency_s": 0.0}


def _pass(tasks: list[dict]) -> dict:
    return {"pass": 1, "per_task": tasks,
            "correct": sum(1 for t in tasks if t["correct"]), "total": len(tasks)}


def test_infra_failure_does_not_create_rule():
    """截断/超时不能换来一条解题规则。"""
    st = learn_from(_pass([_task("K11", "det", False, infra=True,
                                reason="调用异常：输出被 max_tokens 截断")]),
                    seed_state())
    assert st["rules"] == [], f"基础设施故障竟然生成了规则：{st['rules']}"
    assert st["memories"] == [], "基础设施故障竟然生成了记忆"


def test_infra_failure_is_recorded_not_swallowed():
    """排除 ≠ 抹掉。谁被排除必须留在账上，否则会被读成「全对了」。"""
    st = learn_from(_pass([_task("K11", "det", False, infra=True)]), seed_state())
    assert st.get("infra_excluded") == ["K11"]


def test_real_failure_still_learns():
    """真正的能力失败必须照旧触发学习，别把闸门关死。"""
    st = learn_from(_pass([_task("K11", "det", False), _task("K12", "det", True)]),
                    seed_state())
    assert len(st["rules"]) == 1
    assert st["rules"][0]["scope"] == "det"
    assert st["rules"][0]["evidence"].startswith("K11")


def test_mixed_pass_only_learns_from_capability():
    """同族里既有故障又有真失败时，规则只依据真失败计数。"""
    st = learn_from(_pass([
        _task("K11", "det", False, infra=True),
        _task("K12", "det", False),
        _task("K13", "det", True),
        _task("K14", "det", True),
    ]), seed_state())
    assert len(st["rules"]) == 1
    # 分母是 4（全族），分子只能是 1（真失败），不能把故障也算进去
    assert st["rules"][0]["evidence"] == "K12 共 1/4 题失败", st["rules"][0]["evidence"]
    assert st.get("infra_excluded") == ["K11"]
