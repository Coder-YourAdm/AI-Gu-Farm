"""五级提示阶梯：纯模板，不含答案。

契约（`docs/demo/给B_B7最小集.md` §一）：
    LADDER                五级，0..4
    next_level(current, correct)
    render(level, task, kc)

为什么不在这里写死某道题的解法：同一级的文案要能套到任意题面上，
否则提示梯度会跟着题库漂。这里只按「知识点 + 题型」给方法级话术，
具体数字由学生自己算——也符合「不给答案」的红线。
"""

from __future__ import annotations

from ..student.kc import KC_NAMES, kc_of

MAX_LEVEL = 4

# 每级一句话的定位，给 `why`/前端展示用
LADDER: tuple[dict, ...] = (
    {"level": 0, "label": "重述题意", "goal": "确认学生在求什么"},
    {"level": 1, "label": "点方法", "goal": "指出该用哪类公式或定理"},
    {"level": 2, "label": "第一步", "goal": "给出可以动手的第一步"},
    {"level": 3, "label": "中间步", "goal": "给出关键的中间变换"},
    {"level": 4, "label": "完整解法", "goal": "把解题过程完整走一遍（仍不报最终数值）"},
)

# KC -> 方法 / 第一步 / 中间步 / 完整解法
_GUIDE: dict[str, dict[str, str]] = {
    "integral_definite": {
        "method": "先求原函数，再代上下限；多项式就逐项积分，别急着合并。",
        "first": "把被积函数按幂次拆开，每一项单独积分。",
        "middle": "把上下限代进去之后再做减法，注意负号跟着整项走。",
        "full": "逐项积分得到原函数，代上限减代下限，最后约成最简分数。",
    },
    "integral_improper": {
        "method": "认一下是不是 ∫₀^∞ xⁿe^{-rx} 型；是的话用 n!/rⁿ⁺¹ 的闭式。",
        "first": "把幂次 n、衰减率 r、系数 c 分别对出来。",
        "middle": "代入 c·n!/rⁿ⁺¹ 之前，先确认 n 和 r 没看错。",
        "full": "对出 c、n、r，套 c·n!/rⁿ⁺¹，再约分。",
    },
    "det": {
        "method": "用消元把矩阵化成三角形，对角线相乘；换行记得改符号。",
        "first": "选一行或一列，挑零最多的那个位置开始消。",
        "middle": "每消一次就记一次符号变化，最后统一乘。",
        "full": "按主元逐列消元，记录换行符号，对角线相乘得到整数。",
    },
}


def next_level(current: int, correct: bool) -> int:
    """答错升一级（封顶 4）；答对回到 0。"""
    if type(current) is not int or not 0 <= current <= MAX_LEVEL:
        raise ValueError(f"current 必须是 0..{MAX_LEVEL} 的整数，收到 {current!r}")
    if type(correct) is not bool:
        raise ValueError(f"correct 必须是 bool，收到 {correct!r}")
    if correct:
        return 0
    return min(MAX_LEVEL, current + 1)


def render(level: int, task: dict, kc: str | None = None) -> str:
    """第 `level` 级提示的文案。level=0 是重述题意，4 是完整解法。"""
    if type(level) is not int or not 0 <= level <= MAX_LEVEL:
        raise ValueError(f"level 必须是 0..{MAX_LEVEL} 的整数，收到 {level!r}")
    key = kc or kc_of(task)
    if key not in _GUIDE:
        raise ValueError(f"未知知识点 {key!r}")
    g = _GUIDE[key]
    name = KC_NAMES.get(key, key)
    if level == 0:
        return f"这题考的是「{name}」。先看清楚要求什么，再动笔。"
    if level == 1:
        return g["method"]
    if level == 2:
        return g["first"]
    if level == 3:
        return g["middle"]
    return g["full"]
