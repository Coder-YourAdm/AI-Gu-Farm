"""模拟学生：三档能力 × 受控的随机答题（契约 §6.7）。

它不是模型，是**可复现的假数据生成器**——12 小时版拿不到真人样本，
但必须能证明「同一套题、同一个学生，换教法会得到不同的适应度」。

基础答对率 weak/mid/strong = 0.25/0.50/0.75；用了第 L 级提示再
`+0.1·L`，上限 0.95。随机数一律走实例自己的 `random.Random(seed)`，
**不用全局 random**，否则 9 组会互相串。

错答只造两种错法：符号翻转、分子 ±1。而且每个候选都会先过
`executor.judge()` 确认真的判 `wrong`——万一题目参考值是 0（`-0 == 0`），
符号翻转不算错，这条自检会把它剔掉，避免把「答对」记成「答错」。
"""
from __future__ import annotations

import json
import random
from fractions import Fraction

from .. import executor

LEVEL_RATES: dict[str, float] = {
    "weak": 0.25,
    "mid": 0.50,
    "strong": 0.75,
}
HINT_BONUS: float = 0.10
MAX_RATE: float = 0.95


def _literal(x: Fraction) -> str:
    if x.denominator == 1:
        return str(x.numerator)
    return "%d/%d" % (x.numerator, x.denominator)


class SimStudent:
    def __init__(self, level: str, seed: int = 42,
                 giveup_rate: float = 0.0):
        if level not in LEVEL_RATES:
            raise ValueError("学生档位必须是 weak/mid/strong，收到 %r" % (level,))
        if type(seed) is not int:
            raise ValueError("seed 必须是 int，收到 %r" % (seed,))
        if not 0.0 <= giveup_rate <= 1.0:
            raise ValueError("giveup_rate 必须在 [0,1]")
        self.level = level
        self.seed = seed
        self.giveup_rate = giveup_rate
        self._rng = random.Random(seed)
        self._wrong_cache: dict[str, tuple[str, ...]] = {}
        self.calls = 0

    def correct_probability(self, hint_level: int = 0) -> float:
        if type(hint_level) is not int or not 0 <= hint_level <= 4:
            raise ValueError("hint_level 必须是 0..4 的整数")
        return min(MAX_RATE, LEVEL_RATES[self.level] + HINT_BONUS * hint_level)

    def _wrong_literals(self, task: dict) -> tuple[str, ...]:
        key = task.get("signature") or json.dumps(task.get("params"),
                                                  sort_keys=True)
        if key in self._wrong_cache:
            return self._wrong_cache[key]

        ref = executor._ref_fraction(task["reference"])
        den = ref.denominator
        candidates = (-ref,
                      ref + Fraction(1, den),
                      ref - Fraction(1, den))
        out: list[str] = []
        for value in candidates:
            if value == ref:                 # ref=0 时 -ref 仍是 0，不算错
                continue
            literal = _literal(value)
            raw = json.dumps({"value": literal}, ensure_ascii=False)
            if executor.judge(task, raw) == "wrong":
                out.append(literal)
        if not out:
            raise ValueError("造不出合法错答字面量，题可能不合法：%r" % (key,))
        self._wrong_cache[key] = tuple(out)
        return self._wrong_cache[key]

    def answer(self, task: dict, hint_level: int = 0) -> str | None:
        """返回答案**字面量**（调用方包成 `{"value": ...}` 再交给 judge）。

        `None` 表示空答/放弃；默认 `giveup_rate=0` 不产生，留这个出口是
        为了以后能单独测 `unanswered` 分支而不改签名。
        """
        if not isinstance(task, dict):
            raise ValueError("task 必须是 dict")
        self.calls += 1
        if self._rng.random() < self.giveup_rate:
            return None
        if self._rng.random() < self.correct_probability(hint_level):
            return _literal(executor._ref_fraction(task["reference"]))
        return self._rng.choice(self._wrong_literals(task))
