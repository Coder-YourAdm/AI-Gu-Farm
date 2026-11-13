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
TOP_KEYS = [
    "schema", "schema_version", "pair_id",
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
    """按赛道口径判定。注意：**倒退也算合格交付**，编造上升曲线才是失败。"""
    if delta_pp >= tol:
        return "improved"
    if delta_pp <= -tol:
        return "regressed"
    return "no_change"
