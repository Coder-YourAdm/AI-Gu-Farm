"""双跑演示（pass1 → pass2）数据契约。

这个文件是 **C 与 B 的唯一对接点**，谁改字段谁负责修对接。
赛道要求（选手手册 · 日新赛道）对应关系：

  「比的是什么指标，两次各是多少」   → metric + passes[].accuracy
  「学到了什么」                     → learned.rules_added / memories_added / params_changed
  「怎么退回第一次之前的版本」        → rollback

字段一经确定，`web/evolve.html` 只认这里的键名，一个都不改。
"""

SCHEMA = "evolve-demo"
SCHEMA_VERSION = 1

# 顶层必备键。少一个 evolve.html 就拒绝渲染（宁可白屏也不显示错数字）
#
# `is_mock` 是 2026-11-13 加的必备键，和 result.json 的 `is_mock` 对齐：
# 双跑页显示「4/10 → 8/10」这种最容易被当成成绩的数字，如果不声明真假，
# 现场观众无法分辨它是 mock 抽签还是真模型跑出来的。
TOP_KEYS = [
    "schema", "schema_version", "pair_id", "is_mock",
    "frozen", "metric", "passes", "learned", "rollback", "conclusion",
]

# 单次运行的必备键
PASS_KEYS = ["pass", "duration_s", "correct", "total", "accuracy", "per_task", "state_version"]

# 逐题记录的必备键
TASK_KEYS = ["id", "family", "correct", "answer_chars"]

# 每道题在两次运行中的变化类型
DELTA = ("fail_to_pass", "pass_to_fail", "stay_pass", "stay_fail")


def validate(payload: dict) -> list[str]:
    """返回问题列表，空列表 = 合格。不抛异常，方便页面与 CI 共用。"""
    errs: list[str] = []
    for k in TOP_KEYS:
        if k not in payload:
            errs.append(f"缺顶层键 {k}")
    if errs:
        return errs

    # is_mock 必须是真布尔值。字符串 "false" 在 JS 里是真值，
    # 页面会把假数据渲染成绿标 —— 这个坑不值得踩一次。
    if not isinstance(payload.get("is_mock"), bool):
        errs.append(f"is_mock 必须是 true/false 布尔值，实际 {payload.get('is_mock')!r}")

    if len(payload["passes"]) != 2:
        errs.append(f"passes 必须正好 2 项，实际 {len(payload['passes'])}")

    for p in payload["passes"]:
        for k in PASS_KEYS:
            if k not in p:
                errs.append(f"pass{p.get('pass', '?')} 缺键 {k}")
        if p.get("total") and abs(p["accuracy"] - p["correct"] / p["total"]) > 1e-9:
            errs.append(f"pass{p['pass']} 的 accuracy 与 correct/total 不一致")
        for t in p.get("per_task") or []:
            for k in TASK_KEYS:
                if k not in t:
                    errs.append(f"pass{p['pass']} 题目 {t.get('id', '?')} 缺键 {k}")

    lr = payload["learned"]
    for k in ("rules_added", "memories_added", "params_changed"):
        if k not in lr:
            errs.append(f"learned 缺键 {k}")

    fz = payload["frozen"]
    for k in ("config_sha256", "prompts_sha256", "identical_across_passes"):
        if k not in fz:
            errs.append(f"frozen 缺键 {k}")
    if fz.get("identical_across_passes") is not True:
        errs.append("frozen.identical_across_passes 必须为 true —— 两次之间代码/参数/提示词不许变")

    rb = payload["rollback"]
    for k in ("available", "snapshot_id", "command"):
        if k not in rb:
            errs.append(f"rollback 缺键 {k}")

    return errs


def verdict_of(delta_pp: float, tol: float = 2.0) -> str:
    """按赛道口径判定。注意：**倒退也算合格交付**，编造上升曲线才是失败。

    边界用严格不等号：`delta == tol` 落在噪声带内。原因见 `compare_passes` ——
    容差常被设成"一道题"的值，此时刚好一道题的变化必须算作测不出来，
    否则每次单题抖动都会翻转结论。
    """
    if delta_pp > tol:
        return "improved"
    if delta_pp < -tol:
        return "regressed"
    return "no_change"


def compare_passes(p1: dict, p2: dict) -> dict:
    """比较两趟的**能力正确率**，给出 delta、分辨率与判定。

    这里有三条不肯让步的规则，都是为了不让页面说出它自己也不确定的话：

    1. **基础设施故障不计分**。截断、超时、接口 5xx 属于我们这边的抖动，
       两趟都可能随机出现。把一次截断渲染成"退步 5pp"，是在用自己手滑
       指责模型 —— 2026-11-13 真实发生过。
    2. **容差不得小于单题分辨率**。20 题库里翻一道题就是 5pp；容差若取
       2pp，任何单题抖动都会越线，判定变成掷硬币。
    3. **退化要能被看见**。若某趟全是故障导致分母为 0，宁可给 no_change
       也不给 improved —— 不能靠"没有数据"赢。
    """
    def cap(p: dict) -> tuple[int, int, int]:
        items = p.get("per_task") or []
        ok = sum(1 for t in items if t.get("correct") and not t.get("infra"))
        n = sum(1 for t in items if not t.get("infra"))
        infra = sum(1 for t in items if t.get("infra"))
        return ok, n, infra

    ok1, n1, infra1 = cap(p1)
    ok2, n2, infra2 = cap(p2)
    acc1 = ok1 / n1 if n1 else 0.0
    acc2 = ok2 / n2 if n2 else 0.0
    delta_pp = round((acc2 - acc1) * 100, 1)
    resolution = round(100 / max(n1, n2, 1), 1)

    if not n1 or not n2:                 # 没有可比的能力样本
        verdict = "no_change"
    else:
        verdict = verdict_of(delta_pp, tol=max(resolution, 2.0))

    return {"ok1": ok1, "n1": n1, "ok2": ok2, "n2": n2,
            "delta_pp": delta_pp, "resolution_pp": resolution,
            "verdict": verdict, "infra_p1": infra1, "infra_p2": infra2}
