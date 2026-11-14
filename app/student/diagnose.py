"""错因诊断与选题：把「答错了」翻译成「为什么错」和「下一题做什么」。

## 边界（重要，别写成第二套判分）

判对错**只由 `executor.judge()` 决定**，本模块不重复实现判分。
`classify()` 干的事情是：judge 说 wrong 之后，把学生写的那个数**取出来**
和参考值做精确比较，判断错在哪一类（符号 / 计算 / 读题 / 概念）。
所以这里复用 `executor` 的三个解析器，但顺序与 `judge` 保持一致——
这是「取出用于分类的数值」，不是「重新判一次分」。

为什么必须用 `Fraction`：判据里有「同分母、分子差 1」「相差整数倍」
这类整数关系，浮点会把 `1/3` 和 `0.3333333` 看成不同的数，分类全乱。

六条判据按序命中即返回（契约 §6.4），顺序不能调：
    1. None / unanswered            -> giveup
    2. 数值 = -ref 且 != ref        -> sign
    3. 同分母、分子差 1             -> calc
    4. 与 ref 相差整数倍(分母整除)  -> calc
    5. det 且差值可被 2 整除        -> misread
    6. 其余                         -> concept
答对统一返回 `none`（ERR_KINDS 里的一员），让调用方不用再分叉。
"""
from __future__ import annotations

import json
from fractions import Fraction

from .. import executor
from . import kc
from .difficulty import DifficultyBook

ERR_KINDS: tuple[str, ...] = (
    "sign", "calc", "concept", "misread", "giveup", "none",
)

# err kind -> 给「为什么给我这道题」直接展示的中文短句。
# B 的 hint.render() 会给更长的教学话术；这里只负责一句能看懂的归因。
ERR_LABELS: dict[str, str] = {
    "sign": "答案符号反了",
    "calc": "方法对了但算错了数",
    "concept": "解题思路或公式记混了",
    "misread": "题目条件看错了",
    "giveup": "上一题没作答",
    "none": "上一题独立做对",
}

_NO_ERROR_TEXT = "本知识点暂无错题记录"


def _value_of(raw_answer: str) -> str:
    """从 `{"value": "..."}` 里取出字符串。调用前 judge 已保证格式合法。"""
    payload = json.loads(raw_answer)
    if not isinstance(payload, dict) or set(payload) != {"value"}:
        raise ValueError("答案不是 {\"value\": \"...\"} 结构：%r" % (raw_answer,))
    value = payload["value"]
    if not isinstance(value, str):
        raise ValueError("value 不是字符串：%r" % (value,))
    return value.strip()


def _fraction_of(task: dict, value: str) -> Fraction:
    """把学生写的字面量还原成精确数，解析顺序与 `judge` 完全一致。

    det 只接整数；积分依次试 integer -> fraction -> decimal。
    这里不判断对错，只负责「把数取出来」。
    """
    if task.get("type") == "det":
        return Fraction(executor.parse_integer(value))
    try:
        return Fraction(executor.parse_integer(value))
    except ValueError:
        try:
            return executor.parse_fraction(value)
        except ValueError:
            return executor.parse_decimal(value)


def _classify_wrong(answer: Fraction, ref: Fraction, task_type: str) -> str:
    """judge 已判 wrong 之后的六条判据（2~6 条），全部整数精确比较。"""
    # 2) 符号错：数值正好是参考值的相反数（ref=0 时 -0==0，不成立，继续往下）
    if answer == -ref and answer != ref:
        return "sign"

    # 3) 同分母、分子差 1：约分后分母相同且只差 1，典型的"最后一步算错"
    if (answer.denominator == ref.denominator
            and abs(answer.numerator - ref.numerator) == 1):
        return "calc"

    # 4) 相差整数倍（分母整除）：比值化简后分母为 1 且不是 1 倍。
    #    校准里观察到的「gamma 答 3/2、参考 3/4」就是这一条（差 2 倍）。
    #    注意不能用「差值是整数」——det 的差值几乎总是整数，会把
    #    判据 5 的 misread 全吃掉，这是顺序设计的坑。
    if ref != 0:
        ratio = answer / ref
        if ratio.denominator == 1 and ratio != 1:
            return "calc"

    # 5) 行列式：差值是偶数 -> 更像抄错行列 / 看错元素，而不是不会算
    if task_type == "det":
        diff = answer - ref
        if diff.denominator == 1 and diff != 0 and diff.numerator % 2 == 0:
            return "misread"

    # 6) 其余归概念错。宁可粗，不可错（分工表里的判断 2）
    return "concept"


def classify(task: dict, raw_answer: str | None) -> str:
    """返回 ERR_KINDS 之一。

    - 答对 -> `none`
    - 没答 / 格式非法 -> `giveup`
    - 答错 -> 按 §6.4 六条判据分类

    `task` 必须是含 `type` / `reference` 的完整 Task（normalize_task 产出）。
    reference 结构损坏时由 `_ref_fraction` 抛 ValueError——那是数据故障，
    不是学生答错，上层应当中止（与 judge 的行为一致）。
    """
    if not isinstance(task, dict):
        raise ValueError("classify 需要 dict 类型的 task，收到 %s"
                         % type(task).__name__)
    verdict = executor.judge(task, raw_answer)
    if verdict == "right":
        return "none"
    if verdict == "unanswered" or raw_answer is None:
        return "giveup"

    ref = executor._ref_fraction(task["reference"])
    try:
        value = _value_of(raw_answer)
        answer = _fraction_of(task, value)
    except (ValueError, TypeError, KeyError) as exc:
        # judge 说 wrong 意味着答案一定能解析成功；走到这里说明两处
        # 解析规则漂移了，属于代码 bug，显式报出来而不是猜一个分类。
        raise ValueError(
            "judge 判为 wrong 但答案无法解析，判分与诊断规则不一致：%r" % (raw_answer,)
        ) from exc
    return _classify_wrong(answer, ref, task["type"])


def next_task(pool: list[dict], mastery: dict[str, float],
              book: DifficultyBook, rng=None,
              weakest_first: bool = True) -> dict:
    """从候选池里挑下一题。

    `weakest_first=True`（默认）：先补最弱的知识点——取 mastery 里最小的
    KC，只在该 KC 的题里用 `book.pick()` 选「略高于当前水平」的一题。
    若该 KC 没有候选题，退回全池，避免为了补弱项而卡住没题出。

    `weakest_first=False`：不偏向任何 KC，用平均掌握度在全池里挑。

    选题的「难度」由 DifficultyBook 决定，本函数只负责**缩小候选范围**，
    不复刻一套选题公式（跟 ancestor 那次双实现的教训一样）。
    """
    if not isinstance(pool, list) or not pool:
        raise ValueError("候选池必须是非空数组")
    if not isinstance(mastery, dict) or not mastery:
        raise ValueError("mastery 必须是非空 dict")
    if type(weakest_first) is not bool:
        raise ValueError("weakest_first 必须是 bool")
    for key, value in mastery.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("mastery[%r] 不是数字：%r" % (key, value))
        if not 0.0 <= value <= 1.0:
            raise ValueError("mastery[%r] 必须在 [0,1]：%r" % (key, value))

    if not weakest_first:
        theta = sum(mastery.values()) / len(mastery)
        return book.pick(pool, theta, rng=rng)

    weakest = min(mastery, key=lambda k: mastery[k])
    theta = mastery[weakest]
    same_kc = [t for t in pool if kc.kc_of(t) == weakest]
    candidates = same_kc or pool
    if not same_kc:
        # 补弱项没题可出时，用最弱 KC 的水平在全池里选一道相对简单的
        theta = min(theta, sum(mastery.values()) / len(mastery))
    return book.pick(candidates, theta, rng=rng)


def why(task: dict, mastery: dict[str, float], book: DifficultyBook,
        last_err: str | None, memory_ids: list[str]) -> dict:
    """生成「为什么给我这道题」的可解释依据。

    三个数字**全部来自真实计算**，不许手写、不许编：
      - `mastery`   当前 KC 的 BKT 掌握度
      - `difficulty` DifficultyBook 里这道题的难度
      - `gap`       difficulty - mastery，>0 表示这题略高于当前水平

    `last_err` 可以传 err kind（走 ERR_LABELS 翻译），也可以直接传一句
    现成文案（不在 ERR_KINDS 里就原样使用）。`memory_ids` 是支撑这次
    判断的 L1 事件 id，前端能展开成时间轴。
    """
    if not isinstance(task, dict):
        raise ValueError("why 需要 dict 类型的 task")
    if not isinstance(mastery, dict):
        raise ValueError("mastery 必须是 dict")
    target_kc = kc.kc_of(task)
    if target_kc not in mastery:
        raise ValueError("mastery 缺知识点 %r，无法解释选题" % (target_kc,))
    m = mastery[target_kc]
    if not isinstance(m, (int, float)) or isinstance(m, bool) or not 0.0 <= m <= 1.0:
        raise ValueError("mastery[%r] 不是 [0,1] 的数字：%r" % (target_kc, m))
    d = book.get(task)

    if last_err is None:
        text = _NO_ERROR_TEXT
    elif last_err in ERR_LABELS:
        text = ERR_LABELS[last_err]
    else:
        text = str(last_err)
    if not isinstance(memory_ids, (list, tuple)):
        raise ValueError("memory_ids 必须是数组，收到 %s"
                         % type(memory_ids).__name__)

    return {
        "mastery": round(m, 4),
        "difficulty": round(d, 4),
        "gap": round(d - m, 4),
        "last_error": text,
        "memory": [str(x) for x in memory_ids],
    }
