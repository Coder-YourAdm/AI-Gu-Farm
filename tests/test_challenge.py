"""挑战状态机的验收测试。规格：方案/技术规格_v5.md §6.3。

这份测试就是实现的验收标准，不是参考样例。
`python -m pytest tests/test_challenge.py -q` 全绿，challenge.py 才算能用。

测试只断言规格明写的行为，不约束内部结构 —— 你怎么存 observations 都行。
"""

from __future__ import annotations

import copy

import pytest

from app import challenge as ch


CFG20 = {"n_gen": 20, "challenge_gens": [8, 15], "challenge_window": 6,
         "threshold_correct": 3}
CFG15 = {"n_gen": 15, "challenge_gens": [4, 10], "challenge_window": 6,
         "threshold_correct": 3}


def _impl_ready() -> bool:
    """实现填进去之前整组跳过。

    为什么不是让它红：这 12 条是给 challenge.py 的验收标准，不是给主干的。
    未实现时全量 pytest 会凭空多 12 个 fail，把「285 passed」的基线搅浑 ——
    现场有人跑一次 pytest 看到一片红，会以为代码坏了。
    实现一填进去，skipif 自动失效，这 12 条立刻变成真验收。
    """
    try:
        ch.plan(CFG20, [], 8)
    except NotImplementedError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _impl_ready(),
                                reason="app/challenge.py 尚未实现，验收测试待启用")


# ────────────────────────── 窗口排期校验 ──────────────────────────

def test_schedule_accepts_spec_defaults():
    """20 代 [8,15] 和 15 代 [4,10] 是规格里写死的两个默认值，必须通过。"""
    ch.validate_schedule(CFG20)
    ch.validate_schedule(CFG15)


def test_schedule_rejects_window_past_last_gen():
    """15 + 6 - 1 = 20 刚好贴边（通过），16 + 6 - 1 = 21 越界（必须拒绝）。

    越界不能靠运行时截短窗口补救：截短后最后一个窗口观察不满 6 代，
    退役原因永远走不到 window_closed，那批题的统计口径就废了。
    """
    ch.validate_schedule({**CFG20, "challenge_gens": [8, 15]})
    with pytest.raises(ch.ScheduleError):
        ch.validate_schedule({**CFG20, "challenge_gens": [8, 16]})


def test_schedule_rejects_overlapping_windows():
    """相邻 born 间隔必须 >= window。8 和 12 间隔 4 < 6，两个窗口的观察期重叠。"""
    with pytest.raises(ch.ScheduleError):
        ch.validate_schedule({**CFG20, "challenge_gens": [8, 12]})
    # 间隔 6 刚好合法
    ch.validate_schedule({**CFG20, "challenge_gens": [8, 14]})


def test_schedule_rejects_empty_or_unsorted():
    with pytest.raises(ch.ScheduleError):
        ch.validate_schedule({**CFG20, "challenge_gens": [15, 8]})
    with pytest.raises(ch.ScheduleError):
        ch.validate_schedule({**CFG20, "challenge_gens": []})


# ────────────────────────── 注入 ──────────────────────────

def test_plan_injects_only_on_born_gen():
    got = ch.plan(CFG20, [], 8)
    assert len(got) == 1
    assert got[0]["born_gen"] == 8
    assert got[0]["window"] == 6
    assert got[0]["state"] == "active"
    assert got[0]["task_id"]

    # 非 born_gen 不注入
    assert ch.plan(CFG20, [], 7) == []
    assert ch.plan(CFG20, [], 9) == []


def test_plan_is_idempotent_on_retry():
    """恢复后禁止重复注入。

    判断依据只能是 challenges 里已提交的 observations，不能靠内存标记 ——
    --resume 从 state.json 重建时内存标记已经没了。所以这里用「已经带 g8 观测
    的状态」再调一次 plan，必须返回空。
    """
    first = ch.plan(CFG20, [], 8)
    chal = first[0]
    chal = ch.observe(chal, 8, [{"id": "G01", "correct": 1}, {"id": "G02", "correct": 0}])

    assert ch.plan(CFG20, [chal], 8) == []


def test_plan_second_window_independent():
    """两个窗口互不干扰：g8 的题退役后，g15 照样注入。"""
    chal = ch.plan(CFG20, [], 8)[0]
    chal["state"] = "retired"
    got = ch.plan(CFG20, [chal], 15)
    assert len(got) == 1 and got[0]["born_gen"] == 15


# ────────────────────────── 观测与退役 ──────────────────────────

def _new_chal():
    return ch.plan(CFG20, [], 8)[0]


def test_observe_does_not_mutate_input():
    chal = _new_chal()
    before = copy.deepcopy(chal)
    ch.observe(chal, 8, [{"id": "G01", "correct": 1}])
    assert chal == before, "observe 必须返回新对象，不许就地改入参"


def test_retire_on_threshold():
    """累计 3 人答对 → threshold_reached。

    注意这里是**累计**，不是单代。规格的意图是「这道题被攻克了」，
    所以跨代累加；单代 3 人对 6 个体的种群来说等于当天就要退役。
    """
    chal = _new_chal()
    chal = ch.observe(chal, 8, [{"id": "G01", "correct": 1}])
    assert chal["state"] == "active"
    chal = ch.observe(chal, 9, [{"id": "G02", "correct": 1}])
    assert chal["state"] == "active"
    chal = ch.observe(chal, 10, [{"id": "G03", "correct": 1}])
    assert chal["state"] == "retired"
    assert chal["retired_reason"] == "threshold_reached"
    assert chal["first_success_attempt"] == 1


def test_retire_on_window_closed():
    """6 代都没达标 → window_closed。attempt 计数从 1 开始。"""
    chal = _new_chal()
    for i in range(6):
        chal = ch.observe(chal, 8 + i, [{"id": "G01", "correct": 0}])
    assert chal["state"] == "retired"
    assert chal["retired_reason"] == "window_closed"


def test_p_t_is_attempt_over_window():
    """§6.3 样例：attempt=1, window=6 → p_t=0.1666666667。"""
    chal = _new_chal()
    chal = ch.observe(chal, 8, [{"id": "G01", "correct": 0}])
    obs = chal["observations"][-1]
    assert obs["attempt"] == 1
    assert abs(obs["p_t"] - 1 / 6) < 1e-9


def test_observe_after_retire_is_noop():
    """退役后不再接收观测。否则 retired 的题还会继续长 observations，
    attempt 和 p_t 都会被污染，后面算统计时口径混乱。"""
    chal = _new_chal()
    for i in range(3):
        chal = ch.observe(chal, 8 + i, [{"id": f"G0{i+1}", "correct": 1}])
    assert chal["state"] == "retired"
    n = len(chal["observations"])
    after = ch.observe(chal, 30, [{"id": "G06", "correct": 1}])
    assert len(after["observations"]) == n


# ────────────────────────── 汇总 ──────────────────────────

def test_summary_separates_completed_from_truncated():
    """正常退役与观察被截断必须分开计数。

    合成一个「完成率」就把「跑完没达标」和「根本没跑完」混成一件事，
    对外表述会变成「挑战题完成率 67%」这种没法解释的话。
    """
    a = {**_new_chal(), "state": "retired", "retired_reason": "threshold_reached"}
    b = {**_new_chal(), "state": "retired", "retired_reason": "window_closed"}
    c = {**_new_chal(), "state": "censored", "retired_reason": "run_aborted"}
    d = {**_new_chal(), "state": "active", "retired_reason": None}

    s = ch.summary([a, b, c, d])
    assert s["total"] == 4
    assert s["by_state"]["retired"] == 2
    assert s["by_state"]["censored"] == 1
    assert s["by_state"]["active"] == 1
    assert s["retired_normal"] == 2
    assert s["truncated"] == 1
