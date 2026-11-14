"""挑战状态机。规格：方案/技术规格_v5.md §6.3。

**这里只放状态机，不碰主循环。** 主循环的接入（g8/g15 注入、每代提交观测、
manifest 记录 slots.challenge）由 evolve_run.py 负责，不在此文件。

三个函数，纯函数式：入参不被修改，返回新对象。这样测试不需要 mock，
也不会因为调用顺序产生隐式状态。
"""

from __future__ import annotations

import copy
from typing import Any

# 五态。scheduled 表示已排期但还没到 born_gen；active 表示注入后观察中。
STATES = ("scheduled", "active", "retired", "censored")

# 正常退役原因。其余（run_aborted / budget_limit）只在运行提前中止时写入，
# 对外含义是「观察被截断」，不能当成「此题结束」来算统计。
RETIRE_NORMAL = ("threshold_reached", "window_closed")


class ScheduleError(ValueError):
    """排期违反窗口约束。CLI 创建协议时应直接失败，不许运行时悄悄截短窗口。"""


def validate_schedule(cfg: dict) -> None:
    """校验 challenge_gens 的窗口约束，违反则 raise ScheduleError。

    规格原文（§6.3）：
      · 每题只在固定 born_gen 注入一次，两窗口必须完整且不重叠
      · born + window - 1 <= n_gen
      · 相邻 born 间隔 >= window

    这两条不是风格建议。违反第一条，最后一个窗口的观察会被代数上限截断，
    退役原因永远走不到 window_closed，统计口径就废了；违反第二条，两个窗口
    的观察期重叠，同一代的结果同时计入两道题，「3 人答对」的阈值失去意义。
    """
    if "n_gen" not in cfg:
        raise ScheduleError("config 缺少 n_gen，无法校验挑战窗口")
    n_gen = int(cfg["n_gen"])
    window = int(cfg.get("challenge_window", 6))
    if window < 1:
        raise ScheduleError(f"challenge_window={window} 必须 >= 1")

    gens = cfg.get("challenge_gens")
    if gens is None:
        raise ScheduleError("config 缺少 challenge_gens（可以为空列表表示不挑战）")
    gens = [int(g) for g in gens]
    if len(gens) == 0:
        # 五代 pilot 允许空，规格：challenge_gens 可为空且不做留出评测
        raise ScheduleError("challenge_gens 为空：本次不挑战，不应调用 validate_schedule")

    for g in gens:
        if g < 0:
            raise ScheduleError(f"born_gen={g} 不能为负")
        if g + window - 1 > n_gen:
            raise ScheduleError(
                f"born_gen={g} + window={window} - 1 = {g + window - 1} 超过 n_gen={n_gen}："
                "最后一个窗口观察不满，退役原因永远走不到 window_closed")

    for a, b in zip(gens, gens[1:]):
        if b <= a:
            raise ScheduleError(f"challenge_gens 必须严格升序，实际 {gens}")
        if b - a < window:
            raise ScheduleError(
                f"相邻 born 间隔 {b - a} < window={window}：两个窗口观察期重叠，"
                "同一代结果会同时计入两道题")


def plan(cfg: dict, challenges: list[dict], gen: int,
         pool: list[dict] | None = None) -> list[dict]:
    """返回本代应当注入的挑战题列表；没有则返回 []。

    调用时机：每代出题之前。gen 在 cfg["challenge_gens"] 里时才可能有返回。

    幂等性要求（规格明写「恢复时以状态和已提交 gen 判断，禁止重复注入」）：
      同一 (gen, task_id) 被调用两次，第二次必须返回 []。
      判断依据不能用内存里的标记，只能用 challenges 里已记录的 observations ——
      因为 --resume 是从 state.json 重建的，内存标记那时已经没了。

    pool 传入 data/challenge_pool.json 的题目列表时，task_id 用题库里的真实 ID
    （X01…），返回体里同时带 template/params/statement/reference，主循环可直接
    评测；不传则只生成状态骨架，task_id 用 C01/C02 占位。
    """
    gens = [int(g) for g in cfg.get("challenge_gens") or []]
    if gen not in gens:
        return []

    # 幂等：已注入过（有 born_gen 相同的题，或某题的观测里已有本代）就不再注入
    for c in challenges:
        if int(c.get("born_gen", -1)) == gen:
            return []
        if any(int(o.get("gen", -1)) == gen for o in c.get("observations") or []):
            return []

    idx = gens.index(gen)
    window = int(cfg.get("challenge_window", 6))

    task_id = f"C{idx + 1:02d}"
    row = None
    if pool:
        row = pool[idx % len(pool)]
        task_id = row.get("id") or task_id

    ch: dict[str, Any] = {
        "task_id": task_id,
        "born_gen": gen,
        "window": window,
        "state": "active",
        "observations": [],
        "first_success_attempt": None,
        "threshold_attempt": None,
        "retired_reason": None,
    }
    if row is not None:
        # 题库字段原样带过去。reference 存在这里，但导出白名单会剥掉，
        # 与普通题的处置一致 —— 挑战题面同样不能进静态页面。
        for k in ("template", "params", "statement", "reference", "type", "signature"):
            if k in row:
                ch[k] = row[k]
    return [ch]


def observe(chal: dict, gen: int, results: list[dict],
            threshold: int = 3) -> dict:
    """提交一代的观测，返回更新后的 chall（不修改入参）。

    results 里每项形如 {"id": "G01", "correct": 1|0}。只应在**完整有效代**调用 ——
    infra 异常（截断/超时/传输错误）的代不算有效代，那份观测进来会把
    p_t 和 attempt 都算错。

    更新顺序有硬要求（规格原文）：「当轮正确结果先用于赏金，再更新
    first_success、threshold」。赏金结算由调用方在 observe 之前完成，本函数
    只负责推进状态 —— 这个分工保证赏金永远读的是更新前的状态，答对的人
    不会因为阈值刚好在本代达标而漏拿赏金。

    退役判定：
      · 累计答对人数 >= threshold（3）→ state="retired",
        retired_reason="threshold_reached"
      · attempt 达到 window（6）仍不达标 → state="retired",
        retired_reason="window_closed"
      · 二者都未达 → 保持 active

    p_t 的定义见 §6.3 样例：0.1666666667 = 1/6，即 attempt / window。
    """
    out = copy.deepcopy(chal)
    if out.get("state") != "active":
        # 退役/截断后不再接收观测。否则 retired 的题还会继续长 observations，
        # attempt 和 p_t 被污染，统计口径混乱。
        return out

    obs = out.setdefault("observations", [])
    if any(int(o.get("gen", -1)) == gen for o in obs):
        # 同一代重复提交（--resume 重跑该代时会发生）一律忽略，不许把 attempt 推高
        return out

    window = int(out.get("window", 6))
    attempt = len(obs) + 1
    correct = sum(1 for r in results if r.get("correct"))
    obs.append({
        "gen": gen,
        "attempt": attempt,
        "correct": correct,
        "p_t": round(attempt / window, 10),
        "by": [r["id"] for r in results if r.get("correct") and "id" in r],
    })

    if correct > 0 and out.get("first_success_attempt") is None:
        out["first_success_attempt"] = attempt

    cumulative = sum(int(o.get("correct") or 0) for o in obs)
    if cumulative >= threshold:
        out["state"] = "retired"
        out["retired_reason"] = "threshold_reached"
        if out.get("threshold_attempt") is None:
            out["threshold_attempt"] = attempt
    elif attempt >= window:
        out["state"] = "retired"
        out["retired_reason"] = "window_closed"
    else:
        out["state"] = "active"

    return out


def summary(challenges: list[dict]) -> dict[str, Any]:
    """汇总，供 manifest 与页面展示。

    正常退役与观察被截断分开计数：合成一个「完成率」就把「跑完没达标」
    和「根本没跑完」混成一件事，对外会说出「挑战题完成率 67%」这种
    没法解释的话。
    """
    by_state = {s: 0 for s in STATES}
    retired_normal = 0
    truncated = 0
    for c in challenges:
        st = c.get("state") or "scheduled"
        by_state[st] = by_state.get(st, 0) + 1
        reason = c.get("retired_reason")
        if st == "retired" and reason in RETIRE_NORMAL:
            retired_normal += 1
        elif st == "censored" or (reason is not None and reason not in RETIRE_NORMAL):
            truncated += 1

    return {
        "total": len(challenges),
        "by_state": by_state,
        "retired_normal": retired_normal,
        "truncated": truncated,
        "threshold_reached": sum(1 for c in challenges
                                 if c.get("retired_reason") == "threshold_reached"),
        "window_closed": sum(1 for c in challenges
                             if c.get("retired_reason") == "window_closed"),
    }
