"""Elo-lite 难度校准：给每道题一个可在线更新的难度值，并挑「略高于当前水平」的题。

## 关于刻度（重要，别照抄通用 Elo 公式）

通用 Elo 里 ``d`` 和 ``theta`` 是 1500 量级的 rating，所以公式写
``10 ** ((d - theta) / 400)``。本项目里难度与能力都归一到 **[0, 1]**
（`prior()` 落在 `[0.25, 0.75]`），如果直接照抄那个 400，指数项约等于 0，
`E` 恒为 0.5，难度校准就完全失效、`pick()` 退化成静态选题。

因此这里把 [0,1] 线性映回 Elo 刻度再套公式：

    d_elo     = ELO_BASE + ELO_SCALE * d
    theta_elo = ELO_BASE + ELO_SCALE * theta
    (d_elo - theta_elo) / ELO_SCALE = d - theta

代入后化简成 ``E = 1 / (1 + 10 ** (d - theta))``。
`d` 比 `theta` 高 0.5 时 `E≈0.24`，符合「题比人难，答对概率低」的直觉。
`update_rating` 同理：先在 Elo 刻度上更新 `+k·(实际-E)`，再映回 [0,1]，
所以一次答题的变动量是 `k/ELO_SCALE = 16/400 = 0.04`，不会一步跳爆。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import executor

ELO_BASE: float = 1000.0
ELO_SCALE: float = 400.0
K_FACTOR: float = 16.0
PRIOR_LO: float = 0.25
PRIOR_HI: float = 0.75
D_MIN: float = 0.05
D_MAX: float = 0.95


def expected(theta: float, d: float) -> float:
    """学生能力 `theta` 面对题目难度 `d` 时，答对的概率。

    两者都在 [0,1]；`d` 越大（题越难）或 `theta` 越小（人越弱），E 越低。
    """
    for name, value in (("theta", theta), ("d", d)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} 必须是数字，收到 {value!r}")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} 必须在 [0,1]，收到 {value!r}")
    return 1.0 / (1.0 + 10.0 ** (d - theta))


def update_rating(rating: float, other: float,
                  correct: bool, k: float = K_FACTOR) -> float:
    """在 Elo 刻度上更新一个 rating，再映回 [0,1]。

    `rating` 是「被更新的一方」，`other` 是「对手」：
    - 更新学生能力：``update_rating(theta, d, correct)``，答对则上升；
    - 更新题目难度：方向相反，见 `DifficultyBook.update`。

    返回值夹在 `[D_MIN, D_MAX]`，避免多轮之后跑到 0 或 1 再也拉不回来。
    """
    if type(correct) is not bool:
        raise ValueError(f"correct 必须是 bool，收到 {correct!r}")
    if not isinstance(k, (int, float)) or isinstance(k, bool) or k <= 0:
        raise ValueError(f"k 必须是正数，收到 {k!r}")

    e = expected(rating, other)
    actual = 1.0 if correct else 0.0
    # 落到 Elo 刻度上更新，再映射回来；等价于 rating + (k/ELO_SCALE)(实际-E)
    elo = ELO_BASE + ELO_SCALE * rating
    elo += k * (actual - e)
    return min(D_MAX, max(D_MIN, (elo - ELO_BASE) / ELO_SCALE))


def _sig(task: dict) -> str:
    """取题目标识：优先用已有 signature，缺失时按 executor 规则现算。"""
    if not isinstance(task, dict):
        raise ValueError("task 必须是 dict")
    sig = task.get("signature")
    if isinstance(sig, str) and sig:
        return sig
    if "template" in task and "params" in task:
        return executor.signature(task)
    raise ValueError("题目既没有 signature，也没有 template/params，无法标识")


class DifficultyBook:
    """题目难度表。可纯内存，也可落一份 JSON 便于跨会话延续。

    `prior()` 由 signature 的 sha256 稳定映射到 `[0.25, 0.75]`——
    同一道题在任何机器上初值都相同，不需要额外标注，也不会因 RNG 变化。
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._values: dict[str, float] = {}
        if self.path is not None and self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"难度表必须是对象：{self.path}")
            for key, value in raw.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"难度表里 {key!r} 不是数字：{value!r}")
                self._values[key] = float(value)

    def prior(self, task: dict) -> float:
        sig = _sig(task)
        digest = hashlib.sha256(sig.encode("utf-8")).digest()
        unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return PRIOR_LO + (PRIOR_HI - PRIOR_LO) * unit

    def get(self, task: dict) -> float:
        sig = _sig(task)
        return self._values.get(sig, self.prior(task))

    def update(self, task: dict, theta: float, correct: bool) -> float:
        """一次答题后更新**题目难度**，返回新难度。

        方向与 `update_rating` 相反：学生答对（尤其面对难题）说明这题
        没那么难，难度下调；答错则上调。
        """
        if type(correct) is not bool:
            raise ValueError(f"correct 必须是 bool，收到 {correct!r}")
        d = self.get(task)
        e = expected(theta, d)
        actual = 1.0 if correct else 0.0
        delta = (K_FACTOR / ELO_SCALE) * (actual - e)
        new = min(D_MAX, max(D_MIN, d - delta))
        self._values[_sig(task)] = new
        self._save()
        return new

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._values, ensure_ascii=False,
                           sort_keys=True, indent=1) + "\n",
                encoding="utf-8",
            )

    def pick(self, candidates: list[dict], theta: float,
             offset: float = 0.1, rng=None) -> dict:
        """挑「略高于当前水平」的一题：``|d - (theta + offset)|`` 最小。

        `rng` 可选；传入时先用它打散候选，让同难度题不会永远选中同一道。
        不传则按 signature 字典序取，保证同 seed 可复现。
        """
        if not candidates:
            raise ValueError("候选题目为空，无法选题")
        if not 0.0 <= theta <= 1.0:
            raise ValueError(f"theta 必须在 [0,1]，收到 {theta!r}")

        items = list(candidates)
        if rng is not None:
            rng.shuffle(items)
        else:
            items.sort(key=lambda t: _sig(t))

        target = min(1.0, theta + offset)
        return min(items, key=lambda t: abs(self.get(t) - target))
