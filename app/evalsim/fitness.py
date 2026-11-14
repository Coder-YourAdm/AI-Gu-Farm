"""对照实验的适应度：学生增益减去交互成本（契约 §6.6）。

    gain    = (post − pre) / (1 − pre)          pre>=1 时记 0
    cost    = 0.3·(items/20) + 0.7·(hints/20)
    fitness = gain − 0.25·cost

**为什么必须减成本项**：只加增益，演化一定选出「无限提示、把答案嚼碎」
的策略——学生当次答对了，增益高，但没真学会。减掉提示与题量成本，
才能选出「话少而准」的教法。

`hints` 是**加权提示次数**（第 L 级提示折算 `hint_weight(L)` 次，见
`app.tutor.policy`），不是原始次数；公式本身按契约冻结，一个字都不改。
`tools/check_web_contract.py` 会逐行复算这里的结果，容差 0.002。
"""
from __future__ import annotations

BASE_ITEMS: float = 20.0
BASE_HINTS: float = 20.0
W_ITEM: float = 0.3
W_HINT: float = 0.7
WEIGHT: float = 0.25


def gain(pre: float, post: float) -> float:
    """标准化增益：从 `pre` 提升到 `post`，除以还剩多少提升空间。"""
    for name, value in (("pre", pre), ("post", post)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} 必须是数字，收到 {value!r}")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} 必须在 [0,1]，收到 {value!r}")
    if pre >= 1.0:
        return 0.0
    return (post - pre) / (1.0 - pre)


def cost(items: float, hints: float,
         base_items: float = BASE_ITEMS,
         base_hints: float = BASE_HINTS) -> float:
    """交互成本：题量与提示量的加权和，两个基数按契约固定为 20。"""
    for name, value in (("items", items), ("hints", hints)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} 必须是数字，收到 {value!r}")
        if value < 0:
            raise ValueError(f"{name} 不能为负，收到 {value!r}")
    if base_items <= 0 or base_hints <= 0:
        raise ValueError("base_items / base_hints 必须为正数")
    return W_ITEM * (items / base_items) + W_HINT * (hints / base_hints)


def score(g: float, c: float, weight: float = WEIGHT) -> float:
    """fitness = gain − weight·cost。"""
    for name, value in (("gain", g), ("cost", c), ("weight", weight)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} 必须是数字，收到 {value!r}")
    if weight < 0:
        raise ValueError("weight 不能为负")
    return g - weight * c
