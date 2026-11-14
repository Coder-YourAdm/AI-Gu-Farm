"""完整代事务：出题 → 槽位 → 作答 → 判分 → 排名 → 挑战 → 重组 → 记录。

对应技术规格 §4（槽位）、§6（排名/祖先/挑战）、§7（代事务）、§8（代记录）。

本模块**不实现**任何数学判分、题面生成或数据集构造。它们由 A 的 `executor.py`
与 C 的 `datasets.py` 提供，这里只通过 `Engine` / `Dataset` 两个 Protocol 调用。
这样做的直接好处：B4 阶段可以用 `tests/doubles.py` 的替身把整代事务跑通，
真模块到位后不改一行调度代码，只换注入对象。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import time
import copy
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, Sequence

from app.config import Config
from app.genotype import (
    Genes,
    GeneHistory,
    Individual,
    Meta,
    clone_population,
    build_solver_system,
    gene_key,
)
from app.llm import CallResult, ProtocolError, build_solver_messages
from .async_tools import gather_strict
from .budget import BudgetExceeded
from .llm_async import TransportFailure

SCHEMA_VERSION = "5.1"

# 槽位最终状态。pending 只存在于一代事务中间态，不落盘。
SLOT_STATES = ("pending", "right", "wrong", "unanswered", "transport_error")

# 出题重做时可回传的固定错误码（规格 §3.2 三条规则的规则 2）。
SETTER_ERROR_CODES = ("schema", "range", "duplicate")


# --------------------------------------------------------------------------
# 外部依赖的契约
# --------------------------------------------------------------------------

class Engine(Protocol):
    """A 的 `executor.py` 六个函数。签名逐字对齐技术规格 §2.3。

    传模块或实例都可以：`app.executor` 本身就是满足这个 Protocol 的对象。
    """

    def validate_task(self, raw: dict) -> tuple[bool, str]: ...

    def normalize_task(self, raw: dict, *, task_id: str, source: str,
                       by: str | None, gen: int | None) -> dict: ...

    def solve_exact(self, task: dict) -> dict: ...

    def solver_view(self, task: dict) -> dict: ...

    def signature(self, task: dict) -> str: ...

    def judge(self, task: dict, raw_answer: str | None) -> str: ...


class Dataset(Protocol):
    """C 的 `datasets.py` 里本模块需要的部分。

    只暴露「按代取共同卷」与「挑战池」两件事。留出卷正文**不在此接口内**，
    主调度无法读到它（规格 §5 的隔离要求）。
    """

    def common_dev(self, gen: int) -> list[dict]: ...

    def challenge_pool(self) -> list[dict]: ...

    def signatures(self) -> set[str]: ...


class ChatClient(Protocol):
    async def chat(self, role: str, messages: Sequence[Mapping[str, str]], *,
                   slot_id: str | None = ..., attempt: int = ...) -> CallResult: ...


class BrokenReference(RuntimeError):
    """`judge` 因 `reference` 结构损坏而抛 `ValueError` 时转成这个。

    契约 §6 末段：这属于数据/执行器故障，不是模型答错。必须中止该代，
    绝不能 catch 成 `wrong`——否则「执行器坏了」会被记成「模型不会做」，
    整份实验的结论就失真了（规格 §15）。
    """


# --------------------------------------------------------------------------
# 出题：消息拼装与收集
# --------------------------------------------------------------------------

SETTER_SYSTEM_TEMPLATE = (
    "你是出题人，只输出一个 JSON 对象，不要答案、解释或代码块。\n"
    "你出的题交给另外五份策略。两人或三人答对得最高出题分；\n"
    "一人或四人答对得中档；全对或全错得零分。\n"
    "只能从下面模板选择，所有数值必须满足边界：\n"
    "poly: params={coeffs:[a0,...,ad],lower:l,upper:u}，d=1..4，系数 -5..5，\n"
    "末项非零；l=-3..3，u 为 l+1..l+6 的整数。\n"
    "gamma: params={n,rate,c}，n=1..9，rate=1..8，c 为 ±1、±2、±3、±4、±5。\n"
    "gaussian_odd: params={k,rate,c}，k=0..5，其余同 gamma。\n"
    "det: params={matrix}，2..5 阶方阵，元素 -9..9 的整数。\n"
    "只能输出 template 与 params 两个字段，禁止添加 level、答案或其他键。\n"
    '输出示例：{"template":"gamma","params":{"n":4,"rate":2,"c":1}}\n'
    "策略偏好：{set_prompt}"
)

SETTER_USER = "按固定模板出一道题，只输出 JSON 对象。"

# 追加块上限（规格 §3.2 规则 2：最多 5 条）
MAX_OCCUPIED_SHOWN = 5


def build_setter_messages(genes: Genes, *, error: str | None = None,
                          occupied: Sequence[dict] | None = None,
                          detail: str | None = None,
                          recent: Sequence[dict] | None = None,
                          rejected: dict | None = None,
                          seen: Sequence[dict] | None = None
                          ) -> list[dict[str, str]]:
    """第二次内容尝试的消息。

    首次尝试 `error is None`，只有固定 user 句。重出时在 user 尾部追加一个
    固定 JSON 块：错误码 + 本代已接受题的模板与参数摘要（≤5 条）。

    v5.1 运行中发现两处不够用，按「只影响去重效率、不含答案」的原则补两项：
      * `detail`：validate_task 给出的具体拒绝原因（如元素越界），只描述约束；
      * `recent`：该个体自己最近出过的题（≤3 条），防止它反复交同一道旧题。
    仍然绝不追加参考答案、判分结果、他人策略文本或自然语言建议。
    """
    # 模板里有 JSON 花括号，不能用 str.format；用定点替换
    system = SETTER_SYSTEM_TEMPLATE.replace("{set_prompt}", genes.set_prompt)
    user = SETTER_USER
    # 首次出题就带上「最近已用过」的参数：不带的话模型会反复交同一道题，
    # 重试链把单代拖到 20 分钟以上（实测 g17）。参数是公开信息，不含答案。
    if seen:
        user = user + "\n" + json.dumps(
            {"seen": [{"template": t["template"], "params": t["params"]}
                      for t in list(seen)[:8]]},
            ensure_ascii=False, separators=(",", ":"))
    if error is not None:
        if error not in SETTER_ERROR_CODES:
            raise ValueError(f"未定义的重出错误码: {error!r}")
        block = {
            "error": error,
            "occupied": [{"template": t["template"], "params": t["params"]}
                         for t in list(occupied or [])[:MAX_OCCUPIED_SHOWN]],
        }
        if detail:
            block["detail"] = str(detail)[:80]
        if rejected:
            # 回显上一次被拒的那道题：没有它，重试提示与上一次逐字相同，
            # 模型会反复采样到同一个众数（实测连交 8 次同一道题）。
            block["rejected"] = rejected
        if recent:
            block["recent"] = [{"template": t["template"], "params": t["params"]}
                               for t in list(recent)[:3]]
        user = user + "\n" + json.dumps(block, ensure_ascii=False,
                                        separators=(",", ":"))
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


@dataclass
class TaskAttempt:
    """一次出题尝试的结果，用于 aborted 记录与排障。"""

    individual_id: str
    attempt_no: int
    round_no: int
    accepted: bool
    reason: str | None = None
    call_id: str | None = None
    task_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "individual_id": self.individual_id,
            "attempt_no": self.attempt_no,
            "round_no": self.round_no,
            "accepted": self.accepted,
            "reason": self.reason,
            "call_id": self.call_id,
            "task_id": self.task_id,
        }


def _normalize_reason(reason: str) -> str:
    """把 validate_task 的返回理由收敛到三个固定错误码。"""
    for code in SETTER_ERROR_CODES:
        if code in reason:
            return code
    return "schema"


def _occupied_summary(accepted: Sequence[dict]) -> list[dict]:
    """本代已接受题目的摘要，按接受顺序取最后 5 条。"""
    return list(accepted)[-MAX_OCCUPIED_SHOWN:]


def task_from_raw(raw: Any, *, individual_id: str, gen: int, engine: Engine
                  ) -> tuple[dict | None, str | None]:
    """校验 → 规范化 → 精确求解 → 返回带 reference 的 Task。去重由调用方裁决。

    去重不放在这里，因为裁决必须按固定 ID 顺序进行：并发回包先后不能决定
    谁先抢到同一道题（规格 §7 末段）。
    """
    if not isinstance(raw, Mapping):
        return None, "schema"
    ok, reason = engine.validate_task(dict(raw))
    if not ok:
        return None, _normalize_reason(reason)
    try:
        task = engine.normalize_task(dict(raw), task_id="", source="evolved",
                                     by=individual_id, gen=gen)
    except (ValueError, KeyError) as exc:
        # 这里是「模型出的题字段不全」，不是数据故障：KeyError 来自 normalize_task
        # 内部直接索引缺字段，属于 schema 不合格，按拒收处理即可，不能让整代崩。
        # 与 judge_call 里那个 KeyError 分支不是一回事（那个是 reference 损坏）。
        return None, "schema"
    try:
        reference = engine.solve_exact(task)
    except (ArithmeticError, ValueError):
        # 只接「这组参数算不出来」这类由数据引起的问题。
        # TypeError/NameError/AttributeError 是执行器本身的 bug，必须往上抛：
        # 如果在这里吞掉，它会伪装成 range 错误 → 整代重做 → invalid_generation，
        # 最后看起来像「模型出不了题」，真正的问题被埋掉。
        return None, "range"
    if not isinstance(reference, Mapping):
        return None, "range"
    task = dict(task)
    task["reference"] = dict(reference)
    sig = task.get("signature") or engine.signature(task)
    task["signature"] = sig
    return task, None


async def collect_tasks(*, gen: int, evaluated: Sequence[Individual], cfg: Config,
                        engine: Engine, chat: ChatClient, occupied_sigs: set[str],
                        recent_tasks: Sequence[dict] = (),
                        ) -> tuple[list[dict] | None, int, list[TaskAttempt], str | None]:
    """出题阶段。返回 (valid_tasks | None, refetch_round, attempts, abort_reason)。

    外层是「整代重做」轮次（首轮 + cfg.refetch_rounds），内层是每人的内容尝试
    （cfg.task_attempts）。只重跑还没交出合格题的人，已接受题保留——它们各自
    只属于一个人，不会冲突，重跑也不会让槽位数变化。
    """
    accepted: list[dict] = []
    attempts: list[TaskAttempt] = []
    attempt_no: dict[str, int] = {ind.id: 0 for ind in evaluated}
    error_hint: dict[str, str] = {}
    error_detail: dict[str, str] = {}
    rejected_task: dict[str, dict] = {}

    by_id = sorted(evaluated, key=lambda i: i.id)

    for rnd in range(cfg.refetch_rounds + 1):
        pending = [ind for ind in by_id if ind.id not in {t["by"] for t in accepted}]
        for _ in range(cfg.task_attempts):
            if not pending:
                break

            async def _one(ind: Individual) -> tuple[CallResult, int]:
                attempt_no[ind.id] += 1
                n = attempt_no[ind.id]
                show = (error_hint.get(ind.id)
                        if (cfg.attach_occupied_params and n > 1) else None)
                own = [t for t in recent_tasks if t.get("by") == ind.id][-3:] if show else []
                seen = (list(recent_tasks) + list(accepted))[-8:]
                msgs = build_setter_messages(
                    ind.genes,
                    error=show,
                    occupied=_occupied_summary(accepted),
                    detail=error_detail.get(ind.id) if show else None,
                    recent=own,
                    rejected=rejected_task.get(ind.id) if show else None,
                    seen=seen,
                )
                res = await chat.chat("setter", msgs,
                                      slot_id=f"g{gen:02d}_set_{ind.id}",
                                      attempt=n)
                return res, n

            results = await gather_strict([_one(ind) for ind in pending])

            still: list[Individual] = []
            for ind, (res, n) in zip(pending, results):
                if res.is_transport_error:
                    attempts.append(TaskAttempt(ind.id, n, rnd, False,
                                                "transport_error", res.call_id))
                    return None, rnd, attempts, "transport_error"
                if not res.content_ok:
                    reason = "schema"
                    attempts.append(TaskAttempt(ind.id, n, rnd, False,
                                                f"format:{res.parse_error}",
                                                res.call_id))
                    error_hint[ind.id] = reason
                    still.append(ind)
                    continue

                task, reason = task_from_raw(res.parsed, individual_id=ind.id,
                                             gen=gen, engine=engine)
                if task is not None and task["signature"] in occupied_sigs:
                    task, reason = None, "duplicate"
                if task is None:
                    # 把具体拒绝原因留给第二次尝试：只描述约束，不含答案
                    error_detail.pop(ind.id, None)
                    if reason in ("schema", "range") and isinstance(res.parsed, dict):
                        try:
                            ok2, why = engine.validate_task(dict(res.parsed))
                        except Exception:
                            ok2, why = True, ""
                        if not ok2 and why:
                            error_detail[ind.id] = str(why)
                    if isinstance(res.parsed, dict) and {"template", "params"} <= set(res.parsed):
                        rejected_task[ind.id] = {"template": res.parsed["template"],
                                                 "params": res.parsed["params"]}
                    attempts.append(TaskAttempt(ind.id, n, rnd, False, reason,
                                                res.call_id))
                    error_hint[ind.id] = reason or "schema"
                    still.append(ind)
                    continue

                task["id"] = f"T{gen:02d}_{len(accepted) + 1:02d}"
                accepted.append(task)
                occupied_sigs.add(task["signature"])
                attempts.append(TaskAttempt(ind.id, n, rnd, True, None,
                                            res.call_id, task["id"]))
            pending = still

        if len(accepted) == len(evaluated):
            return accepted, rnd, attempts, None

    return None, cfg.refetch_rounds, attempts, "invalid_generation"


# --------------------------------------------------------------------------
# 槽位
# --------------------------------------------------------------------------

@dataclass
class Slot:
    slot_id: str
    role: str                    # peer / common / ancestor / challenge
    task_id: str
    individual_id: str
    status: str = "pending"
    call_ids: list[str] = field(default_factory=list)
    raw_answer: str | None = None

    def to_dict(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "role": self.role,
            "task_id": self.task_id,
            "individual_id": self.individual_id,
            "status": self.status,
            "call_ids": list(self.call_ids),
            "raw_answer": self.raw_answer,
        }


def allocate_slots(*, gen: int, population: Sequence[Individual],
                   tasks: Sequence[dict], ancestors: Sequence[Individual],
                   common_tasks: Sequence[dict],
                   challenge_task: dict | None) -> list[Slot]:
    """固定 30 互答 + 24 共同卷 + 18 祖先 + 6 挑战（活跃时）。

    互答一人 5 道（别人的题），共同卷一人 4 道（同一组），祖先三人各答六道演化题。
    全部按 ID 排序生成，槽位编号与生成顺序无关，可复现。
    """
    slots: list[Slot] = []
    ordered_pop = sorted(population, key=lambda i: i.id)
    ordered_tasks = sorted(tasks, key=lambda t: t["id"])

    for solver in ordered_pop:
        for task in ordered_tasks:
            if task["by"] == solver.id:
                continue           # 自己的题不答
            slots.append(Slot(
                slot_id=f"g{gen:02d}_peer_{task['id']}_{solver.id}",
                role="peer", task_id=task["id"], individual_id=solver.id))

    for solver in ordered_pop:
        for task in sorted(common_tasks, key=lambda t: t["id"]):
            slots.append(Slot(
                slot_id=f"g{gen:02d}_common_{task['id']}_{solver.id}",
                role="common", task_id=task["id"], individual_id=solver.id))

    for anc in sorted(ancestors, key=lambda i: i.id):
        for task in ordered_tasks:
            slots.append(Slot(
                slot_id=f"g{gen:02d}_ancestor_{task['id']}_{anc.id}",
                role="ancestor", task_id=task["id"], individual_id=anc.id))

    if challenge_task is not None:
        for solver in ordered_pop:
            slots.append(Slot(
                slot_id=f"g{gen:02d}_challenge_{challenge_task['id']}_{solver.id}",
                role="challenge", task_id=challenge_task["id"],
                individual_id=solver.id))
    return slots


def judge_call(engine: Engine, task: dict, res: CallResult, *,
               slot_id: str = "") -> tuple[str, str | None]:
    """模型回包 -> (三态, 记录用的原始答案)。

    **唯一的判分入口**：一代事务与留出评测（`evaluate.py`）都走这里，
    避免两处各写一遍、把「judge 收文本还是收值」这件事情写岔——B4 就这么错过一次。

    入参口径见契约 §6：`judge` 收的是**文本**，它自己 `json.loads`。B 这边已经
    做过本地 JSON 提取修复（去围栏、剥离外围文字），所以把抽取成功的对象重新
    序列化成规范文本再交过去。契约允许的只有「本地提取修复」，不是「格式错就重发」。
    直接传裸值 `"3/4"` 会让 `json.loads` 失败，全对的一代被判成全员零分。
    """
    raw: str | None = None
    judge_input: str | None = None
    if res.content_ok and isinstance(res.parsed, Mapping):
        value = res.parsed.get("value")
        raw = value if isinstance(value, str) else None
        judge_input = json.dumps(dict(res.parsed), ensure_ascii=False)
    try:
        verdict = engine.judge(task, judge_input)
    except ValueError as exc:
        # 契约 §6 的约定路径：reference 结构损坏抛 ValueError。
        # 这是数据故障，不是模型答错；由调用方中止该代 / 该检查点，不记成 wrong。
        raise BrokenReference(f"{slot_id} 参考值损坏：{exc}") from exc
    except KeyError as exc:
        # 契约要求 ValueError，走到这里说明 A 侧的实现回退了（2026-11-13 之前
        # `_ref_fraction` 直接 reference["value"] 取值，缺字段抛 KeyError）。
        # 仍按数据故障中止，不让整趟 4500 次调用崩掉；但把异常类型写进信息，
        # 让日志里一眼能看出「不是数据坏了，是实现回退了」，不静默掩盖。
        raise BrokenReference(
            f"{slot_id} 参考值损坏：{exc!r}（KeyError 形态，"
            "契约要求 ValueError，疑似 A 侧校验回退）"
        ) from exc
    if verdict not in ("right", "wrong", "unanswered"):
        raise ProtocolError(
            f"judge 返回未定义状态 {verdict!r}（{slot_id}）；"
            "只允许 right/wrong/unanswered"
        )
    return verdict, raw


async def fill_slots(*, slots: Sequence[Slot], genes_by_id: Mapping[str, Genes],
                     tasks_by_id: Mapping[str, dict], engine: Engine,
                     chat: ChatClient, cfg: Config) -> list[CallResult]:
    """每个槽位独立新上下文；同一全局并发由 LLMClient 的 Semaphore 管。"""
    async def _one(slot: Slot) -> CallResult:
        task = tasks_by_id[slot.task_id]
        view = engine.solver_view(task)
        system_prompt = build_solver_system(genes_by_id[slot.individual_id])
        messages = build_solver_messages(
            system_prompt, view["statement"], view["answer_format"])
        res = await chat.chat("solver", messages, slot_id=slot.slot_id, attempt=1)
        slot.call_ids.append(res.call_id)
        if res.is_transport_error:
            slot.status = "transport_error"
            return res
        slot.status, slot.raw_answer = judge_call(engine, task, res,
                                                  slot_id=slot.slot_id)
        return res

    return list(await gather_strict([_one(s) for s in slots]))


# --------------------------------------------------------------------------
# 计分与排名（规格 §6.1）
# --------------------------------------------------------------------------

def count_right(slots: Sequence[Slot]) -> int:
    return sum(1 for s in slots if s.status == "right")


def scores(common_slots: Sequence[Slot], peer_slots_for_own_task: Sequence[Slot],
           challenge_slot: Slot | None, cfg: Config) -> tuple[int, float, float, float]:
    assert len(common_slots) == cfg.n_common == 4, len(common_slots)
    assert len(peer_slots_for_own_task) == cfg.n_pop - 1 == 5, len(peer_slots_for_own_task)
    solve = count_right(common_slots)
    c = count_right(peer_slots_for_own_task)
    setter = min(c, 5 - c) / 2
    bounty = cfg.bounty if (challenge_slot is not None
                            and challenge_slot.status == "right") else 0.0
    return solve, setter, bounty, solve + cfg.set_weight * setter + bounty


def rank_population(population: Sequence[Individual], rng: random.Random
                    ) -> list[Individual]:
    """按 (-est, -current_total) 升序；同键组内先按 ID 排再用本地 RNG shuffle。

    先按 ID 排好再 shuffle，是为了让抽签结果只取决于保存状态的 RNG，
    不受并发回包顺序影响。
    """
    ordered = sorted(population, key=lambda i: (-(i.est or 0.0),
                                                -i.current_total, i.id))
    out: list[Individual] = []
    for _, group in itertools.groupby(
            ordered, key=lambda i: (-(i.est or 0.0), -i.current_total)):
        tied = sorted(group, key=lambda i: i.id)
        rng.shuffle(tied)
        out.extend(tied)
    return out


def ancestor_metrics(ancestor_slots: Sequence[Slot],
                     tasks_by_id: Mapping[str, dict]) -> dict:
    """规格 §6.2。统一走 A 的 `app.ancestor`，不允许两份实现各写一遍。

    `unanswered` 记为错但单独统计；缺题型给 None 而不是 0；
    `balanced` 只在两型都有值时计算；`null` 不当 0。

    字段名以 A 的 `generation_failures()` 为准（`task_mix`，不是 `task_counts`）。
    `export.py` 的 ANC_ALLOWED 只放四个 failure 字段，所以改字段名不影响前端。
    """
    from . import ancestor as ancestor_module

    payload = [slot.to_dict() for slot in ancestor_slots]
    task_types = {task_id: task["type"] for task_id, task in tasks_by_id.items()}
    return ancestor_module.generation_failures(payload, task_types)


def per_task_stats(slots: Sequence[Slot],
                   tasks_by_id: Mapping[str, dict]) -> dict:
    """每题记录 assigned/received/right/unanswered。分母不因缺失而下降。"""
    stats: dict[str, dict] = {}
    for slot in slots:
        if slot.role not in ("peer", "challenge"):
            continue
        row = stats.setdefault(slot.task_id, {
            "type": tasks_by_id[slot.task_id]["type"],
            "by": tasks_by_id[slot.task_id].get("by"),
            "assigned": 0, "received": 0, "right": 0,
            "wrong": 0, "unanswered": 0,
        })
        row["assigned"] += 1
        if slot.raw_answer is not None:
            row["received"] += 1
        if slot.status in ("right", "wrong", "unanswered"):
            row[slot.status] += 1
    for row in stats.values():
        row["p_t"] = round(row["right"] / row["assigned"], 10) if row["assigned"] else None
    return stats


# --------------------------------------------------------------------------
# 挑战状态（规格 §6.3）
# --------------------------------------------------------------------------

@dataclass
class Challenge:
    task_id: str
    born_gen: int
    window: int
    state: str = "scheduled"        # scheduled/active/retired/censored
    observations: list[dict] = field(default_factory=list)
    first_success_attempt: int | None = None
    threshold_attempt: int | None = None
    retired_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "born_gen": self.born_gen,
            "window": self.window,
            "state": self.state,
            "observations": list(self.observations),
            "first_success_attempt": self.first_success_attempt,
            "threshold_attempt": self.threshold_attempt,
            "retired_reason": self.retired_reason,
        }


def challenge_for_generation(challenges: Mapping[str, Challenge], gen: int
                             ) -> Challenge | None:
    """本代是否要注入挑战。只在固定 born_gen 注入一次，进入窗口后置 active。"""
    for ch in challenges.values():
        if ch.state in ("scheduled", "active") and ch.born_gen == gen:
            ch.state = "active"
            return ch
    for ch in challenges.values():
        if ch.state == "active" and ch.born_gen <= gen < ch.born_gen + ch.window:
            return ch
    return None


def update_challenge(ch: Challenge, *, gen: int, challenge_slots: Sequence[Slot],
                     cfg: Config) -> None:
    """先赏金（已在计分时完成）再更新状态；只在完整有效代提交观察。"""
    correct = count_right(challenge_slots)
    attempt = len(ch.observations) + 1
    ch.observations.append({
        "gen": gen,
        "attempt": attempt,
        "correct": correct,
        "p_t": round(correct / cfg.n_pop, 10),
    })
    if ch.first_success_attempt is None and correct >= 1:
        ch.first_success_attempt = attempt
    if ch.threshold_attempt is None and correct >= cfg.threshold_correct:
        ch.threshold_attempt = attempt
    if ch.threshold_attempt is not None:
        ch.state = "retired"
        ch.retired_reason = "threshold_reached"
    elif attempt >= ch.window:
        ch.state = "retired"
        ch.retired_reason = "window_closed"


def _challenge_view(state: RunState, *, closed_reason: str | None = None) -> list[dict]:
    """代记录里的挑战快照；`closed_reason` 非空时把未退役的标成 censored。

    规格 §6.3 要求「运行提前中止时为 run_aborted 或 budget_limit，对外写观察被截断」。
    **只改返回的副本**，不动 `state.challenges` 本体：中止后代记录写 censored，
    但内存里若还是 active，续跑时仍能按窗口继续注入；否则一次传输抖动就会让整个
    挑战窗口作废（那一道题只观测了 1 次就被永久关闭）。
    """
    rows: list[dict] = []
    for ch in state.challenges.values():
        row = ch.to_dict()
        if closed_reason is not None and row["state"] in ("scheduled", "active"):
            row["state"] = "censored"
            row["retired_reason"] = closed_reason
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# 运行状态
# --------------------------------------------------------------------------

@dataclass
class RunState:
    run_id: str
    cfg: Config
    rng: random.Random
    next_population: list[Individual]
    ancestors: list[Individual]
    gene_history: GeneHistory
    dedup_set: set[str]
    challenges: dict[str, Challenge]
    last_gen: int = 0
    phase: str = "init"
    frozen_manifest: dict = field(default_factory=dict)
    restored: bool = False
    # 最近接受过的题（by/template/params，最多 30 条），只用于重出提示
    recent_tasks: list[dict] = field(default_factory=list)

    def genes_by_id(self) -> dict[str, Genes]:
        out = {ind.id: ind.genes for ind in self.next_population}
        for anc in self.ancestors:
            out[anc.id] = anc.genes
        return out


# --------------------------------------------------------------------------
# 代记录（规格 §8）
# --------------------------------------------------------------------------

def _ind_payload(ind: Individual) -> dict:
    return {
        "genes": {
            "solve_prompt": ind.genes.solve_prompt,
            "set_prompt": ind.genes.set_prompt,
            "self_check": ind.genes.self_check,
        },
        "meta": {
            "id": ind.meta.id,
            "parents": list(ind.meta.parents),
            "origin": ind.meta.origin,
            "born_gen": ind.meta.born_gen,
        },
    }


def _aggregate_usage(results: Sequence[CallResult]) -> dict:
    def _sum(key: str) -> int | None:
        vals = [r.usage.get(key) for r in results
                if isinstance(r.usage, Mapping) and type(r.usage.get(key)) is int]
        return (sum(vals) if vals and len(vals) == len(results)
                and all(r.attempts == 1 for r in results) else None)

    return {
        "call_count": len(results),
        "input_tokens": _sum("input_tokens"),
        "output_tokens": _sum("output_tokens"),
        "reasoning_tokens": _sum("reasoning_tokens"),
        "cost": None,                 # 单价未定，unknown 不当 0
    }


def _role_timing(results: Sequence[CallResult]) -> dict:
    out: dict[str, int] = {}
    for r in results:
        out[r.role] = out.get(r.role, 0) + int(r.latency_ms or 0)
    return out


def build_aborted_record(*, state: RunState, gen: int, reason: str,
                         attempts: Sequence[TaskAttempt], refetch_round: int,
                         results: Sequence[CallResult] = (),
                         warnings: Sequence[str] = (),
                         censor_reason: str = "run_aborted") -> dict:
    """失败代写单独记录，不提交为 completed。slots 为空。

    保留题目尝试与已完成调用，供事后判断是「模型出不了题」还是「我们的校验有 bug」。

    挑战状态走 `_challenge_view(closed_reason=censor_reason)`：未退役的（scheduled /
    active）在**记录里**标成 `censored`，符合规格 §6.3「运行提前中止时对外写观察被截断」。
    注意只改记录的视图，不动 `state.challenges` 本体——中止后若续跑（--resume），
    内存里的挑战仍是 active，能继续按窗口注入；否则一次传输抖动就会把整个挑战窗口废掉。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": state.run_id,
        "condition": state.cfg.condition,
        "gen": gen,
        "status": "aborted",
        "reason": reason,
        "M": sum(1 for a in attempts if a.accepted),
        "refetch_round": refetch_round,
        "task_counts": {"integral": 0, "det": 0},
        "slots": {},                                  # 未创建任何解题槽位
        "slot_total": 0,
        "attempts": [a.to_dict() for a in attempts],
        "ancestor": None,
        "individuals": [],
        "ranking": [],
        "evaluated_population": [_ind_payload(i) for i in state.next_population],
        "next_population": None,
        "challenges": _challenge_view(state, closed_reason=censor_reason),
        "timing": {"wall_ms": None, "role_ms": _role_timing(results)},
        "usage": _aggregate_usage(results),
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------
# 一代事务（规格 §7）
# --------------------------------------------------------------------------

async def _run_generation(*, gen: int, state: RunState, engine: Engine,
                         dataset: Dataset, chat: ChatClient,
                         operators: Any | None = None,
                         ) -> dict:
    """完整一代。返回代记录；aborted 记录也已写入 `state` 之外的返回值。

    顺序严格照 §7：出题 → 校验/求解/去重 → 槽位 → 作答 → 判分 →
    计分排名 → 挑战 → 重组 → 记录。
    """
    cfg = state.cfg
    t0 = time.perf_counter()
    evaluated = list(state.next_population)

    valid, refetch, attempts, abort_reason = await collect_tasks(
        gen=gen, evaluated=evaluated, cfg=cfg, engine=engine, chat=chat,
        occupied_sigs=state.dedup_set, recent_tasks=state.recent_tasks,
    )
    if valid is None:
        state.phase = "aborted"
        return build_aborted_record(state=state, gen=gen, reason=abort_reason,
                                    attempts=attempts, refetch_round=refetch)

    # 记住本代每题是谁出的：下一次重出时把这个人自己的最近三题回放给它，
    # 防止「同一份 set_prompt 反复交同一道题」把整代拖进 invalid_generation。
    state.recent_tasks.extend(
        {"by": t["by"], "template": t["template"], "params": t["params"]} for t in valid)
    del state.recent_tasks[:-30]

    tasks_by_id = {t["id"]: t for t in valid}
    common = dataset.common_dev(gen)
    common_ids = {t["id"] for t in common}
    overlap = common_ids & set(tasks_by_id)
    if overlap:
        raise ProtocolError(f"共同卷题号与演化题冲突: {sorted(overlap)}")
    tasks_by_id.update({t["id"]: t for t in common})

    challenge = challenge_for_generation(state.challenges, gen)
    challenge_task = None
    if challenge is not None:
        pool = {t["id"]: t for t in dataset.challenge_pool()}
        challenge_task = pool[challenge.task_id]
        tasks_by_id[challenge_task["id"]] = challenge_task

    slots = allocate_slots(gen=gen, population=evaluated, tasks=valid,
                           ancestors=state.ancestors, common_tasks=common,
                           challenge_task=challenge_task)
    role_counts = {role: sum(1 for s in slots if s.role == role)
                   for role in ("peer", "common", "ancestor", "challenge")}
    expected = {"peer": cfg.n_pop * (cfg.n_pop - 1),
                "common": cfg.n_pop * cfg.n_common,
                "ancestor": cfg.n_ancestor * len(valid),
                "challenge": cfg.n_pop if challenge_task else 0}
    if role_counts != expected:
        raise ProtocolError(f"槽位数不符：{role_counts} != {expected}")

    try:
        results = await fill_slots(slots=slots, genes_by_id=state.genes_by_id(),
                                   tasks_by_id=tasks_by_id, engine=engine,
                                   chat=chat, cfg=cfg)
    except BrokenReference as exc:
        # 契约 §6 / 规格 §15：参考值坏了就中止该代，不排名、不写历史
        state.phase = "aborted"
        return build_aborted_record(
            state=state, gen=gen, reason="invalid_generation", attempts=attempts,
            refetch_round=refetch,
            warnings=[f"参考值损坏，中止该代（未计分、未排名）：{exc}"],
        )
    if any(s.status == "transport_error" for s in slots):
        state.phase = "aborted"
        return build_aborted_record(
            state=state, gen=gen, reason="transport_error", attempts=attempts,
            refetch_round=refetch, results=results,
            warnings=["传输错误使本代无效，未计分"],
        )

    # ---- 计分（赏金在挑战状态更新之前定值） ----
    own_task = {t["by"]: t["id"] for t in valid}
    rows: list[dict] = []
    for ind in sorted(evaluated, key=lambda i: i.id):
        common_slots = [s for s in slots
                        if s.role == "common" and s.individual_id == ind.id]
        peer_slots = [s for s in slots
                      if s.role == "peer" and s.task_id == own_task[ind.id]]
        chal_slot = next((s for s in slots if s.role == "challenge"
                          and s.individual_id == ind.id), None)
        solve, setter, bounty, total = scores(common_slots, peer_slots,
                                              chal_slot, cfg)
        ind.current_total = total
        rows.append({"id": ind.id, "solve": solve, "set": setter,
                     "bounty": bounty, "total": total})

    # ---- 追加历史后取窗口均值：等价于「假如现在提交」的估计，但写入推迟到此代确定成功 ----
    for ind in evaluated:
        ind.est = state.gene_history.estimate_including(
            ind.genes, cfg.fitness_window, ind.current_total)
        # n_samples 含当代，落盘后可还原「这个 est 由几个样本得出」
        ind.n_samples = state.gene_history.n_samples(ind.genes) + 1

    ranking = rank_population(evaluated, state.rng)
    ranking_ids = [i.id for i in ranking]

    # ---- 挑战状态 ----
    if challenge is not None:
        chal_slots = [s for s in slots if s.role == "challenge"]
        update_challenge(challenge, gen=gen, challenge_slots=chal_slots, cfg=cfg)

    # ---- 重组 ----
    next_population: list[Individual] | None = None
    breed_stats: dict | None = None
    if gen < cfg.n_gen:
        if cfg.condition == "F":
            next_population = clone_population(evaluated)
            breed_stats = {"condition": "F", "operator_calls": 0,
                           "cloned": len(next_population)}
        else:
            if operators is None:
                raise ProtocolError(f"condition={cfg.condition} 需要算子，但未注入")
            result = await operators.breed(gen=gen, ranked=ranking)
            next_population = result.population
            breed_stats = {"condition": cfg.condition, **result.stats.to_dict()}

    # ---- 全部成功后才把当代分写进历史 ----
    if next_population is not None or gen == cfg.n_gen:
        for ind in evaluated:
            state.gene_history.append(ind.genes, ind.current_total)

    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": state.run_id,
        "condition": cfg.condition,
        "gen": gen,
        "status": "completed",
        "M": len(valid),
        "refetch_round": refetch,
        "task_counts": {
            "integral": sum(1 for t in valid if t["type"] == "integral"),
            "det": sum(1 for t in valid if t["type"] == "det"),
        },
        "slots": role_counts,
        "answers": [s.to_dict() for s in slots],
        "slot_total": len(slots),
        # 78 = 6 次出题 + 72 个解题槽位。两个数分开落盘，别混：
        # §8 的 slots 只数解题槽位，而「每代 78 次调用」含 6 次 setter。
        "setter_calls": len(evaluated),
        "call_total": len(evaluated) + len(slots),
        "attempts": [a.to_dict() for a in attempts],
        "ancestor": ancestor_metrics(
            [s for s in slots if s.role == "ancestor"], tasks_by_id),
        "individuals": [
            {**row,
             "est": round(next(i.est for i in evaluated if i.id == row["id"]), 10),
             "n_samples": next(i.n_samples for i in evaluated if i.id == row["id"])}
            for row in rows
        ],
        "ranking": ranking_ids,
        "per_task": per_task_stats(
            [s for s in slots if s.role in ("peer", "challenge")], tasks_by_id),
        "tasks": [{"id": t["id"], "type": t["type"], "template": t["template"],
                   "params": t["params"], "signature": t["signature"],
                   "statement": t["statement"], "by": t["by"],
                   "reference": t["reference"]} for t in valid],
        "common_dev_ids": sorted(common_ids),
        "evaluated_population": [_ind_payload(i) for i in evaluated],
        "next_population": ([_ind_payload(i) for i in next_population]
                            if next_population is not None else None),
        "breed": breed_stats,
        "challenges": _challenge_view(state),          # 正常代不截断，等价原样
        "timing": {"wall_ms": int((time.perf_counter() - t0) * 1000),
                   "role_ms": _role_timing(results)},
        "usage": _aggregate_usage(results),
        "warnings": [],
    }

    state.next_population = next_population if next_population is not None else []
    state.last_gen = gen
    state.phase = "completed"
    return record


class RecordingChat:
    def __init__(self, inner):
        self.inner = inner
        self.results = []
        self.roles = []

    async def chat(self, role, messages, **kwargs):
        self.roles.append(role)
        try:
            result = await self.inner.chat(role, messages, **kwargs)
        except ProtocolError as exc:
            if hasattr(exc, 'result'):
                self.results.append(exc.result)
            raise
        self.results.append(result)
        return result


async def run_generation(*, gen, state, engine, dataset, chat, operators=None):
    """Commit state only on success; infrastructure failures never select genes."""
    working = copy.deepcopy(state)
    recorded = RecordingChat(chat)
    op = copy.copy(operators) if operators is not None else None
    if op is not None:
        if hasattr(op, 'chat'):
            op.chat = recorded
        if hasattr(op, 'rng'):
            op.rng = working.rng
    budget = getattr(chat, 'budget', None)
    http_before = budget.used if budget else None
    cancellation = None
    try:
        result = await _run_generation(gen=gen, state=working, engine=engine,
                                       dataset=dataset, chat=recorded, operators=op)
    except (Exception, asyncio.CancelledError) as exc:
        cancellation = exc if isinstance(exc, asyncio.CancelledError) else None
        reason = ('cancelled' if cancellation is not None else
                  'budget_limit' if isinstance(exc, (BudgetExceeded, TimeoutError)) else
                  'transport_error' if isinstance(exc, TransportFailure) else
                  'broken_reference' if isinstance(exc, BrokenReference) else
                  'protocol_error' if isinstance(exc, ProtocolError) else 'execution_error')
        result = build_aborted_record(state=state, gen=gen, reason=reason,
                    attempts=[], refetch_round=0, results=recorded.results,
                    warnings=[f'{type(exc).__name__}: {exc}'],
                    censor_reason='budget_limit' if reason == 'budget_limit' else 'run_aborted')
    result['exposed_signatures'] = sorted(working.dedup_set - state.dedup_set)
    if result['status'] == 'completed':
        state.__dict__.update(working.__dict__)
    else:
        state.phase = 'aborted'
    result['setter_calls'] = recorded.roles.count('setter')
    result['operator_calls'] = recorded.roles.count('operator')
    result['call_total'] = len(recorded.roles)
    result['http_attempts'] = (budget.used - http_before if budget else
                               sum(r.attempts for r in recorded.results))
    result['call_total_lower_bound'] = len(recorded.results) != len(recorded.roles)
    result['usage'] = _aggregate_usage(recorded.results)
    result['usage']['call_count'] = len(recorded.roles)
    result['call_ids'] = [r.call_id for r in recorded.results]
    result['usage']['unknown_calls'] = len(recorded.roles) - len(recorded.results)
    if result['usage']['unknown_calls']:
        for key in ('input_tokens', 'output_tokens', 'reasoning_tokens'):
            result['usage'][key] = None
    if cancellation is not None:
        cancellation.record = result
        raise cancellation
    return result


async def run_generations(*, state: RunState, engine: Engine, dataset: Dataset,
                          chat: ChatClient, operators: Any | None = None,
                          from_gen: int | None = None,
                          max_gens: int | None = None) -> list[dict]:
    """连跑若干代，遇到 aborted 立即停（不跳过、不掩盖）。"""
    start = (from_gen if from_gen is not None else state.last_gen + 1)
    stop = state.cfg.n_gen if max_gens is None else min(state.cfg.n_gen,
                                                       start + max_gens - 1)
    records: list[dict] = []
    for gen in range(start, stop + 1):
        record = await run_generation(gen=gen, state=state, engine=engine,
                                      dataset=dataset, chat=chat,
                                      operators=operators)
        records.append(record)
        if record["status"] != "completed":
            break
    return records
