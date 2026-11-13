"""结果页（多趟独立运行 × 留出评测）数据契约。

这个文件是 **C 与留出评测产出的唯一对接点**。页面 `web/result.html` 只认这里的
键名，一个都不改；谁改字段谁负责同步改本文件与 `tests/test_result.py`。

技术规格出处：
  §1  L26   export.py 负责「公开历史与结果页」
  §2.2      题集规模：holdout 20 道
  §10 L596  「所有数字追溯到 run_id」
  §10 L598  「出现负结果仍可形成完整交付」

设计取舍（不是随手写的）：
  1. `is_mock` 是**必备键**。假数据必须自己声明是假的，页面据此挂红标。
     少这个键就直接判不合格——宁可白屏，也不能让 mock 数字冒充现场结果。
  2. `conclusion.evidence[].run_id` 必须是 `runs` 里真实存在的一趟。
     结论句可以写得保守，但不能引用不存在的数据。
  3. 同一批 run 里，同名祖先的 label 与 color 必须逐字一致。
     规格 §6.2 要求冻结组复用同一套祖先；换了名字或换了颜色，对照就失效了。
  4. `check_conclusion_fit()` 单独做「结论 vs 数据方向」核对：
     说 improved 但多数趟在掉分，就报错。这是防吹牛的机器闸门。
"""

SCHEMA = "result-board"
SCHEMA_VERSION = 1

TOP_KEYS = [
    "schema", "schema_version", "is_mock", "model_id",
    "metric", "lineage_order", "runs", "conclusion", "provenance",
]

METRIC_KEYS = ["name", "unit", "n_tasks", "higher_is_better", "definition"]

RUN_KEYS = [
    "run_id", "tag", "condition", "generations", "status",
    "calls", "duration_min", "started_at", "lineages",
]

LINEAGE_KEYS = ["key", "label", "kind", "color", "final_rank", "n_individuals", "holdout"]

HOLDOUT_KEYS = ["correct", "total"]

CONC_KEYS = ["level", "text"]

LEVELS = ("positive", "none", "negative")

KINDS = ("champion", "ancestor")


def champion_of(run: dict) -> dict | None:
    for l in run.get("lineages") or []:
        if l.get("kind") == "champion":
            return l
    return None


def best_ancestor(run: dict) -> dict | None:
    best = None
    for l in run.get("lineages") or []:
        if l.get("kind") == "champion":
            continue
        if best is None or l["holdout"]["correct"] > best["holdout"]["correct"]:
            best = l
    return best


def delta_pp(run: dict) -> float | None:
    """冠军相对「最好祖先」的百分点差。正数=冠军赢，负数=祖先赢。"""
    c, b = champion_of(run), best_ancestor(run)
    if not c or not b:
        return None
    total = c["holdout"]["total"]
    if not total:
        return None
    return (c["holdout"]["correct"] - b["holdout"]["correct"]) / total * 100.0


def validate(payload: dict) -> list[str]:
    """返回问题列表，空列表 = 合格。不抛异常，方便页面与 CI 共用。"""
    errs: list[str] = []

    if payload.get("schema") != SCHEMA:
        errs.append(f"schema 必须是 {SCHEMA!r}，实际 {payload.get('schema')!r}")
    for k in TOP_KEYS:
        if k not in payload:
            errs.append(f"缺顶层键 {k}")
    if errs:
        return errs

    if payload["schema_version"] != SCHEMA_VERSION:
        errs.append(f"schema_version 必须是 {SCHEMA_VERSION}，实际 {payload['schema_version']}")

    m = payload["metric"]
    for k in METRIC_KEYS:
        if k not in m:
            errs.append(f"metric 缺键 {k}")

    runs = payload["runs"]
    if not isinstance(runs, list) or not runs:
        return errs + ["runs 必须非空列表"]

    seen_ids: set[str] = set()
    order = payload["lineage_order"]
    if not isinstance(order, list) or not order:
        errs.append("lineage_order 必须非空列表")

    ancestor_fingerprint: dict[str, tuple[str, str]] = {}

    for run in runs:
        rid = run.get("run_id", "?")
        for k in RUN_KEYS:
            if k not in run:
                errs.append(f"run {rid} 缺键 {k}")
        if rid in seen_ids:
            errs.append(f"run_id 重复：{rid}")
        seen_ids.add(rid)

        ls = run.get("lineages") or []
        if not ls:
            errs.append(f"run {rid} 的 lineages 为空")
            continue

        n_champ = sum(1 for l in ls if l.get("kind") == "champion")
        if n_champ != 1:
            errs.append(f"run {rid} 必须恰好 1 名 champion，实际 {n_champ}")

        keys = []
        for l in ls:
            lk = l.get("key", "?")
            keys.append(lk)
            for k in LINEAGE_KEYS:
                if k not in l:
                    errs.append(f"run {rid} lineage {lk} 缺键 {k}")
            if l.get("kind") not in KINDS:
                errs.append(f"run {rid} lineage {lk} 的 kind 非法：{l.get('kind')!r}")

            h = l.get("holdout") or {}
            for k in HOLDOUT_KEYS:
                if k not in h:
                    errs.append(f"run {rid} lineage {lk} 的 holdout 缺键 {k}")
                    continue
            c_num, t_num = h.get("correct"), h.get("total")
            if isinstance(c_num, int) and isinstance(t_num, int):
                if t_num <= 0:
                    errs.append(f"run {rid} lineage {lk} 的 holdout.total 必须 > 0")
                elif not (0 <= c_num <= t_num):
                    errs.append(f"run {rid} lineage {lk} 的 holdout {c_num}/{t_num} 越界")
                if t_num != m.get("n_tasks"):
                    errs.append(f"run {rid} lineage {lk} 的 holdout.total={t_num} 与 metric.n_tasks={m.get('n_tasks')} 不一致")
            elif c_num is not None:
                errs.append(f"run {rid} lineage {lk} 的 holdout 必须是整数")

            rk, n = l.get("final_rank"), l.get("n_individuals")
            if isinstance(rk, int) and isinstance(n, int):
                if not (1 <= rk <= n):
                    errs.append(f"run {rid} lineage {lk} 的 final_rank={rk} 超出 1..{n}")
            elif rk is not None:
                errs.append(f"run {rid} lineage {lk} 的 final_rank 必须是整数")

            # 祖先跨 run 必须同名同色（规格 §6.2：冻结组复用同一套祖先）
            if l.get("kind") == "ancestor":
                fp = (l.get("label"), l.get("color"))
                if lk in ancestor_fingerprint and ancestor_fingerprint[lk] != fp:
                    errs.append(f"祖先 {lk} 在不同 run 间 label/color 不一致：{ancestor_fingerprint[lk]} vs {fp}")
                ancestor_fingerprint.setdefault(lk, fp)

        if order and sorted(keys) != sorted(order):
            errs.append(f"run {rid} 的 lineage keys 与 lineage_order 不一致：{keys} vs {order}")

    # 结论
    c = payload["conclusion"]
    for k in CONC_KEYS:
        if k not in c:
            errs.append(f"conclusion 缺键 {k}")
    if c.get("level") not in LEVELS:
        errs.append(f"conclusion.level 非法：{c.get('level')!r}，只允许 {LEVELS}")
    if not str(c.get("text") or "").strip():
        errs.append("conclusion.text 不能为空 —— 一句话结论是硬要求")
    for ev in c.get("evidence") or []:
        if ev.get("run_id") not in seen_ids:
            errs.append(f"conclusion.evidence 引用了不存在的 run_id：{ev.get('run_id')!r}")

    p = payload["provenance"]
    if "payload_sha256" not in p:
        errs.append("provenance 缺键 payload_sha256")

    return errs


def check_conclusion_fit(payload: dict, tol_pp: float = 2.0) -> list[str]:
    """核对「结论方向」与「数据方向」是否一致。

    留出集只有 20 题，单题就是 5 个百分点，所以默认容差 2pp 只用来区分「持平」与
    「有方向」。判定规则故意设得笨：多数趟的方向说了算，不许少数派结论冒充整体。
    """
    errs: list[str] = []
    deltas = []
    for run in payload.get("runs") or []:
        d = delta_pp(run)
        if d is not None:
            deltas.append(d)
    if not deltas:
        return ["无法计算任何一趟的 delta，结论无数据支撑"]

    up = sum(1 for d in deltas if d >= tol_pp)
    down = sum(1 for d in deltas if d <= -tol_pp)
    level = (payload.get("conclusion") or {}).get("level")

    if level == "positive" and not (up > down):
        errs.append(f"结论说 positive，但 {up} 趟升 / {down} 趟降，方向不支持")
    if level == "negative" and not (down > up):
        errs.append(f"结论说 negative，但 {up} 趟升 / {down} 趟降，方向不支持")
    if level == "none" and (up == len(deltas) or down == len(deltas)):
        errs.append(f"结论说 none，但 {len(deltas)} 趟方向一致（{up} 升 / {down} 降），"
                    "既然全都一致就不该写「未观察到改善」")
    return errs
