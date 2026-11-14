"""题面必须进入发给模型的消息 —— 回归测试。

背景（2026-11-13 22:40）
------------------------
`solver.md` 是 **system** prompt（技术规格 §3.3），全文只有：
    求解给定数学题。只输出 {"value":"..."}，不要解释或代码块。
    ...
    在内部执行下列解题策略：{solve_prompt}
    {check_instruction}

它**没有** `{statement}` 这个占位符。而 `app/demo.py` 原来用
`solver_tpl.replace("{statement}", 题面)` 往里塞题面 —— `str.replace`
找不到目标时静默返回原串，于是模型收到的消息里**根本没有题目**。

后果非常隐蔽：模型照样返回合法 JSON，判分照样返回 wrong，
跑完只是通过率安静地变成 0/10，没有任何一处报错。
所以这条不变量必须由代码断言，不能靠人看代码看出来。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo import _assert_statement_visible   # noqa: E402

SOLVER_MD = ROOT / "prompts" / "solver.md"
STATEMENT = "计算定积分 ∫_{-1}^{2} (x² - 2x + 3) dx。"


def test_solver_md_has_no_statement_placeholder():
    """solvers.md 是 system prompt，不该出现 {statement}。

    如果哪天有人往 solver.md 里加了 {statement}，这条会提醒他：
    题面走的是 user 消息，不是模板替换。
    """
    text = SOLVER_MD.read_text(encoding="utf-8")
    assert "{statement}" not in text
    assert "{solve_prompt}" in text and "{check_instruction}" in text


def test_guard_passes_when_statement_in_user_message():
    _assert_statement_visible(system_prompt="求解给定数学题。",
                              user_prompt=STATEMENT,
                              statement=STATEMENT)


def test_guard_catches_the_original_bug():
    """复现原 bug：题面只做了一次 replace，而模板里没有占位符。"""
    tpl = SOLVER_MD.read_text(encoding="utf-8")
    rendered = tpl.replace("{statement}", STATEMENT)   # 空操作，题面丢了
    assert STATEMENT not in rendered

    with pytest.raises(SystemExit) as ei:
        _assert_statement_visible(system_prompt=rendered,
                                  user_prompt="答案格式：integer only",
                                  statement=STATEMENT)
    assert "题面没有进入" in str(ei.value)


def test_guard_rejects_empty_statement():
    with pytest.raises(SystemExit):
        _assert_statement_visible("", "", "")


def test_guard_accepts_statement_in_system_prompt_too():
    """题面写在 system 里也算送到了 —— 拦的是「两边都没有」，不是形式。"""
    _assert_statement_visible(system_prompt=STATEMENT, user_prompt="x",
                              statement=STATEMENT)
