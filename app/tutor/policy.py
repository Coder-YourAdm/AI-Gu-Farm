"""三套教法 preset：对照实验里唯一被改变的自变量。

为什么要有这三套：完整方案里说过，**fitness 只加增益会演化出「无限提示、
把答案嚼碎」的策略**。所以这里刻意让 `answer_only`（直接给答案）的
提示权重更高——`hint_weight(level)` 按级别递增，第 4 级（给答案）算 3 次。

三套的差别**只在提示策略**：给几次、从第几级开始、升得多快。
题目、学生、判分、掌握度全部相同，这样 9 组数据的差异才能归因到教法。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    name: str
    label: str
    hint_start: int      # 第一次答错给第几级提示（0 基）
    hint_step: int        # 每次再错升几级
    max_hints: int        # 每题最多给几次提示
    desc: str

    def __post_init__(self) -> None:
        for field in ("hint_start", "hint_step", "max_hints"):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ValueError(f"Policy.{field} 必须是非负整数，收到 {value!r}")
        if self.max_hints < 1:
            raise ValueError("max_hints 至少为 1，否则教法和「不给提示」没区别")
        if any(lvl > 4 for lvl in self.hint_levels()):
            raise ValueError("提示级别封顶 4（契约 §6.5），不能超过")

    def hint_levels(self) -> tuple[int, ...]:
        """这道题依次会给出的提示级别。"""
        return tuple(min(4, self.hint_start + i * self.hint_step)
                     for i in range(self.max_hints))


POLICIES: dict[str, Policy] = {
    "loose": Policy(
        name="loose", label="克制提示",
        hint_start=0, hint_step=1, max_hints=2,
        desc="先让学生自己想；错了只重述题意、再给一步定理，最多两级就换题。",
    ),
    "verbose": Policy(
        name="verbose", label="详尽讲解",
        hint_start=0, hint_step=1, max_hints=4,
        desc="逐级给到完整解法，每题最多四级提示，话多但扶得稳。",
    ),
    "answer_only": Policy(
        name="answer_only", label="直接给答案",
        hint_start=4, hint_step=0, max_hints=1,
        desc="一错就摊答案；学生看起来进步快，但每一级提示都计入成本。",
    ),
}


def list_policies() -> tuple[str, ...]:
    return tuple(POLICIES)


def get_policy(name: str) -> Policy:
    try:
        return POLICIES[name]
    except KeyError as exc:
        raise ValueError(
            "未知教法 %r，允许的：%s" % (name, ", ".join(POLICIES))
        ) from exc


def hint_weight(level: int) -> int:
    """一次第 `level` 级提示折算成几次「基础提示」。

    口径是「信息量」：第 0 级只重述题意（1 份），第 1 级点定理（2 份），
    …… 第 4 级把完整解法摊开（5 份）。所以直接给答案一次就等价于
    五次重述——不是随手调的权重，而是「提示级别暴露了多少信息」。

    这样 `answer_only`（一上来就 level 4）即使只提示一次，成本也高于
    `loose` 给两次低级别提示，fitness 才不会选出最会嚼碎答案的教法。
    """
    if type(level) is not int or not 0 <= level <= 4:
        raise ValueError("提示级别必须是 0..4 的整数，收到 %r" % (level,))
    return level + 1
