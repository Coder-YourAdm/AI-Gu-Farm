"""贝叶斯知识追踪（BKT）：每次答题后更新「这个知识点你会不会」的概率。

四个参数是**人类常识先验，不是拟合值**（答辩要主动这么说）：
    init    初始掌握概率
    transit 练一次就学会的概率
    slip    会做却答错的概率
    guess   不会却蒙对的概率

为什么不用深度学习：pyKT 那些模型要训练数据、不可解释，而这里必须能
当场手算复核——评委问「0.31 是怎么来的」，我们要能指着公式说出来。

公式（标准 BKT，与 OATutor 实现一致）：
    答对：p1 = p(1-slip) / [ p(1-slip) + (1-p)·guess ]
    答错：p1 = p·slip    / [ p·slip    + (1-p)(1-guess) ]
    然后：p' = p1 + (1-p1)·transit

只许纯浮点、不许引入任何第三方依赖。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BKTParams:
    init: float = 0.20
    transit: float = 0.15
    slip: float = 0.10
    guess: float = 0.25

    def __post_init__(self) -> None:
        for name in ("init", "transit", "slip", "guess"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"BKTParams.{name} 必须是数字，收到 {value!r}")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"BKTParams.{name} 必须在 [0,1]，收到 {value!r}")
        # slip + guess >= 1 时模型会「越答错越像会」，属于参数坏掉，直接拒
        if self.slip + self.guess >= 1.0:
            raise ValueError(
                f"slip + guess 必须 < 1，当前 {self.slip} + {self.guess}"
            )


# 三个 KC 当前共用同一组保守先验。有真实答题日志后，按 KC 分别拟合时
# **只改这里**，其它地方不用动。
DEFAULT: dict[str, "BKTParams"] = {
    "integral_definite": BKTParams(),
    "integral_improper": BKTParams(),
    "det": BKTParams(),
}


def update(p: float, correct: bool, params: BKTParams = BKTParams()) -> float:
    """一次答题后的后验掌握概率。

    `correct` 必须严格是 bool——`1` 和 `0` 会被拒，避免把 `correct`
    写成整数后悄悄走错分支（这是本项目 `validate_task` 里同一个 bool 坑）。
    """
    if not isinstance(p, (int, float)) or isinstance(p, bool):
        raise ValueError(f"p 必须是数字，收到 {p!r}")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p 必须在 [0,1]，收到 {p!r}")
    if type(correct) is not bool:
        raise ValueError(f"correct 必须是 bool，收到 {correct!r}")

    if correct:
        hit = 1.0 - params.slip
        miss = params.guess
    else:
        hit = params.slip
        miss = 1.0 - params.guess

    denom = p * hit + (1.0 - p) * miss
    if denom <= 0.0:
        # 正常参数下不会发生；真发生说明参数坏到分母为 0，不能硬算出一个数
        raise ValueError(
            "BKT 分母为 0（p=%r, slip=%r, guess=%r）"
            % (p, params.slip, params.guess)
        )

    known = p * hit / denom
    return known + (1.0 - known) * params.transit


def mastery_of(kc: str, events: list[dict],
               params: dict | None = None) -> float:
    """把 L1 事件流折叠成该 KC 的当前掌握度。

    - 只统计 `event["kc"] == kc` 的事件，其它 KC 不干扰；
    - **按传入顺序折叠**（L1 是 append-only，天然时间有序；BKT 本身
      与顺序相关，所以不要在调用前 shuffle）；
    - `params` 可传 `BKTParams` 或 `{kc: BKTParams}` 覆盖默认值；
    - 无事件时返回该参数的 `init`，而不是返回 0——0 会被误读成
      「确定不会」，而实际是「还没见过」。
    """
    if isinstance(params, BKTParams):
        chosen = params
    elif isinstance(params, dict):
        chosen = params.get(kc, DEFAULT.get(kc, BKTParams()))
    elif params is None:
        chosen = DEFAULT.get(kc, BKTParams())
    else:
        raise ValueError("params 必须是 BKTParams / dict / None，收到 %s"
                         % type(params).__name__)

    p = chosen.init
    for i, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"第 {i} 条事件不是对象")
        if event.get("kc") != kc:
            continue
        if "correct" not in event:
            raise ValueError(f"第 {i} 条事件缺 correct 字段：{event!r}")
        correct = event["correct"]
        if type(correct) is not bool:
            raise ValueError(
                f"第 {i} 条事件 correct 必须是 bool，收到 {correct!r}"
            )
        p = update(p, correct, chosen)
    return p


# 0..5 六档，阈值写死，方便前端画进度条和答辩手算
LEVEL_THRESHOLDS: tuple[float, ...] = (0.20, 0.40, 0.60, 0.75, 0.90)


def level(mastery: float) -> int:
    """掌握度 → 0..5 级。

    升级规则在 `Tutor` 里另有「必须无提示独立答对两次」的约束；
    这里只做数值分档，不负责判定升级（两件事不要混在一起）。
    """
    if not isinstance(mastery, (int, float)) or isinstance(mastery, bool):
        raise ValueError(f"mastery 必须是数字，收到 {mastery!r}")
    if not 0.0 <= mastery <= 1.0:
        raise ValueError(f"mastery 必须在 [0,1]，收到 {mastery!r}")
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if mastery < threshold:
            return i
    return len(LEVEL_THRESHOLDS)
