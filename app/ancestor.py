"""祖先指标。

权威契约：`方案/技术规格_v5.md` §6.2（v5.1）。

术语先说清：这里的「祖先」是 **3 个冻结的参照个体**（初代在 calibration 上
排名前三，选定后基因与推理协议不再变），不是「血统来源」。它们每代都答
六道演化题，用来衡量「新题对固定参照组有多难」。按 lineage 分层算失败率
不在 v5 定义里。

四条硬规则：
1. `per_task_failure = 1 - correct/3`，`unanswered` 记为错但单独计数。
2. 某题型本代无题 -> 该分层值为 `None`，**不拿另一题型填补**。
3. `balanced = 0.5*integral + 0.5*det`，两型都有才算，否则 `None`。
4. `None` 不当 0、不跨缺口拼均线；均线只在最近 window 个连续非 None 值上算。
"""

import json

N_ANCESTOR = 3          # 冻结祖先个数
ROLLING_WINDOW = 3      # 大屏三代均线
TASK_TYPES = ("integral", "det")
VALID_STATUS = ("right", "wrong", "unanswered", "transport_error")


class InvalidGeneration(Exception):
    """本代出现 transport_error 或结构异常：该代不参与排名，整趟应中止。"""


def _field(obj, name):
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def _status(slot) -> str:
    value = _field(slot, "status")
    if value not in VALID_STATUS:
        raise ValueError("未知槽位状态：%r" % (value,))
    return value


def _reject_transport(slots) -> None:
    bad = sum(1 for s in slots if _status(s) == "transport_error")
    if bad:
        raise InvalidGeneration("本代有 %d 个 transport_error 槽位，该代无效" % bad)


def _mean(values):
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# 单题与单代
# ---------------------------------------------------------------------------

def per_task_failure(slots) -> float:
    """同一道题上 3 个祖先的失败率：1 - correct/3。"""
    _reject_transport(slots)
    if len(slots) != N_ANCESTOR:
        raise ValueError("单题祖先槽位必须是 %d 个，收到 %d" % (N_ANCESTOR, len(slots)))
    correct = sum(1 for s in slots if _status(s) == "right")
    return 1 - correct / N_ANCESTOR


def generation_failures(slots, task_types: dict) -> dict:
    """一整代的祖先指标（6 道题 × 3 个祖先 = 18 个槽位）。

    slots       祖先槽位列表，每个至少含 task_id / solver / status
    task_types  {task_id: "integral" | "det"}，由 B 从 Task 表里取

    返回：
        integral_failure / det_failure / balanced_failure / mixed_failure
        task_mix            本代题型数量
        per_task            逐题明细（correct / failure / type）
        unanswered_count    unanswered 数量（算错，但单独统计）
    """
    _reject_transport(slots)

    by_task: dict[str, list] = {}
    for s in slots:
        by_task.setdefault(_field(s, "task_id"), []).append(s)

    per_task = {}
    unanswered = 0
    for task_id, group in by_task.items():
        if task_id not in task_types:
            raise ValueError("题目类型未知：%r（task_types 里没有它）" % (task_id,))
        task_type = task_types[task_id]
        if task_type not in TASK_TYPES:
            raise ValueError("题目类型必须是 integral 或 det，收到 %r" % (task_type,))
        if len(group) != N_ANCESTOR:
            raise ValueError(
                "题 %s 的祖先槽位应为 %d 个，收到 %d" % (task_id, N_ANCESTOR, len(group))
            )
        correct = sum(1 for s in group if _status(s) == "right")
        unanswered += sum(1 for s in group if _status(s) == "unanswered")
        per_task[task_id] = {
            "type": task_type,
            "correct": correct,
            "failure": 1 - correct / N_ANCESTOR,
        }

    integral = _mean([v["failure"] for v in per_task.values() if v["type"] == "integral"])
    det = _mean([v["failure"] for v in per_task.values() if v["type"] == "det"])
    balanced = None if (integral is None or det is None) else 0.5 * integral + 0.5 * det
    mixed = _mean([v["failure"] for v in per_task.values()])

    mix = {"integral": 0, "det": 0}
    for v in per_task.values():
        mix[v["type"]] += 1

    return {
        "integral_failure": integral,
        "det_failure": det,
        "balanced_failure": balanced,
        "mixed_failure": mixed,
        "task_mix": mix,
        "per_task": per_task,
        "unanswered_count": unanswered,
    }


# ---------------------------------------------------------------------------
# 三代均线
# ---------------------------------------------------------------------------

def rolling_mean(values, window: int = ROLLING_WINDOW):
    """连续窗口均值：遇到 None 就断，不跨缺口、不把 None 当 0。

    连续非 None 值不足 window 个时，用已有的那几个平均（1 个就是它自己）。
    """
    if window < 1:
        raise ValueError("window 必须 >= 1，收到 %r" % (window,))
    out = []
    run = []
    for value in values:
        if value is None:
            run = []
            out.append(None)
            continue
        run.append(value)
        segment = run[-window:]
        out.append(sum(segment) / len(segment))
    return out


def ancestor_report(generations, window: int = ROLLING_WINDOW):
    """把逐代结果拼成大屏要的形状：每代四个值 + 各自的三代均线。

    generations: [{"gen": 0, "slots": [...], "task_types": {...}}, ...]
    """
    rows = []
    for g in generations:
        m = generation_failures(g["slots"], g["task_types"])
        row = {
            "gen": g["gen"],
            "integral_failure": m["integral_failure"],
            "det_failure": m["det_failure"],
            "balanced_failure": m["balanced_failure"],
            "mixed_failure": m["mixed_failure"],
            "task_mix": m["task_mix"],
            "unanswered_count": m["unanswered_count"],
        }
        rows.append(row)

    for key in ("integral_failure", "det_failure", "balanced_failure", "mixed_failure"):
        series = rolling_mean([r[key] for r in rows], window)
        for row, value in zip(rows, series):
            row[key + "_mean"] = value
    return rows


# ---------------------------------------------------------------------------
# ancestors.json（B5 产出，A4 只读）
# ---------------------------------------------------------------------------

def load_ancestors(path: str) -> list[dict]:
    """读取并校验 B5 产出的 ancestors.json，返回三个祖先。

    约定格式（见 data/ancestors.example.json）：
        {"ancestors": [{"id": ..., "genes": {solve_prompt/set_prompt/self_check},
                        "calibration_mean": ...}, x3]}
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "ancestors" not in data:
        raise ValueError("ancestors.json 顶层必须是含 ancestors 键的对象")
    ancestors = data["ancestors"]
    if not isinstance(ancestors, list) or len(ancestors) != N_ANCESTOR:
        raise ValueError("ancestors 必须恰好 %d 个" % N_ANCESTOR)

    seen = set()
    for a in ancestors:
        if not isinstance(a, dict):
            raise ValueError("ancestors 的每个元素必须是对象")
        for key in ("id", "genes"):
            if key not in a:
                raise ValueError("祖先条目缺 %s 字段" % key)
        if a["id"] in seen:
            raise ValueError("祖先 id 重复：%r" % (a["id"],))
        seen.add(a["id"])
        genes = a["genes"]
        if not isinstance(genes, dict):
            raise ValueError("祖先 %s 的 genes 必须是对象" % a["id"])
        for key in ("solve_prompt", "set_prompt", "self_check"):
            if key not in genes:
                raise ValueError("祖先 %s 的 genes 缺 %s" % (a["id"], key))
    return ancestors


def pick_ancestors(calibration_means: dict, rng, n: int = N_ANCESTOR) -> list[str]:
    """按 calibration 均分取前 n 名；同分用传入的 RNG 打散（禁止全局 random）。

    B5 自己实现也行，这里给一份口径一致的参考，免得两边选出不同的祖先。
    """
    if len(calibration_means) < n:
        raise ValueError("候选个体不足 %d 个" % n)

    ordered = sorted(calibration_means.items(), key=lambda kv: (-kv[1], kv[0]))
    picked = []
    i = 0
    while i < len(ordered) and len(picked) < n:
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        group = [kv[0] for kv in ordered[i:j + 1]]
        if len(group) > 1:
            rng.shuffle(group)
        picked.extend(group)
        i = j + 1
    return picked[:n]
