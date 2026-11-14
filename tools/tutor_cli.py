"""生成 `web/tutor.json` —— 学生端「回放模式」的数据源。

## 它是什么 / 不是什么

这是**离线 fixture 生成器**：不联网、不调模型、不读 API key。
所有数字都由本地引擎真实算出，不是手填的展示数据：

    statement   <- executor.solver_view()   （白名单三键，绝不含参考值）
    verdict     <- executor.judge()         （判分只此一处）
    err         <- diagnose.classify()
    mastery     <- bkt.mastery_of()         （随每轮答题真实变化 =「日新」）
    difficulty  <- DifficultyBook.get()
    why.gap     <- difficulty - mastery
    review_plan <- schedule.due_items()
    memory.conf <- 该事件之后该 KC 的 BKT 掌握度（越高表示错因已被克服）

`generated_at` 和事件时间都是**固定值**，所以同一命令跑两次逐字节相同——
回放数据是给评委看的证据，不能每次打开都不一样。

## 用法

    python tools/tutor_cli.py --sid demo --turns 4 --out web/tutor.json
    python tools/tutor_cli.py --sid demo --turns 8 --out web/tutor.json

`--offline` 接受但无效果（本来就不联网），留着是为了让 B/C 按分工表
敲命令时不会报「未知参数」。
"""
from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import executor                       # noqa: E402
from app.student import bkt, kc, schedule      # noqa: E402
from app.student.diagnose import (              # noqa: E402
    ERR_LABELS, classify, next_task, why,
)
from app.student.difficulty import DifficultyBook  # noqa: E402

PROFILE_VERSION = "manual-v1"
DEFAULT_AT = "2026-11-14T15:40:00+08:00"

# 辅助答对（看过提示再答对）在掌握度里算「半次独立答对」。
# 不取 0：演示曲线会变成只降不升；不取 1：等于回到「提示后答对也算独立」的老问题。
ASSISTED_WEIGHT = 0.5

# 提示梯度（契约 §6.5，0 基）。文案是规则文本，不经过模型。
HINT_TEXT = {
    0: "先别算，把题目问的是什么、积分区间（或矩阵阶数）再念一遍。",
    1: "回想对应的定理：定积分逐项积分、Γ(n+1)=n!、行列式按行展开。",
    2: "我们把第一步的中间量写出来，你接着往下算一步。",
    3: "看一道同类型的小例题，照着它的步骤再做一次。",
    4: "这一步的完整解法在这里，看完请独立重做一遍。",
}

# 8 道简单题，全部由 executor 自己造（题面 / 签名 / 答案都不手写）。
# 三个 KC 各 2~3 道，保证 next_task 补弱项时不会没题可出。
_RAW_POOL: tuple[dict, ...] = (
    {"template": "poly", "params": {"coeffs": [3, -2, 1], "lower": -1, "upper": 2}},
    {"template": "poly", "params": {"coeffs": [0, 0, 5], "lower": 1, "upper": 2}},
    {"template": "poly", "params": {"coeffs": [2, 1], "lower": 0, "upper": 3}},
    {"template": "gamma", "params": {"c": 1, "n": 2, "rate": 2}},
    {"template": "gamma", "params": {"c": 1, "n": 4, "rate": 2}},
    {"template": "gaussian_odd", "params": {"c": 2, "k": 1, "rate": 3}},
    {"template": "det", "params": {"matrix": [[2, 1], [1, 3]]}},
    {"template": "det", "params": {"matrix": [[2, -1, 0], [3, 5, 1], [-2, 4, 6]]}},
)

# 学生的历史答题记录（append-only L1，严格按时间升序，BKT 与顺序相关）。
# 三个 KC 强弱不同，回放才有看头。末尾两条故意留成「昨天答错」：
# 它们是待复查项，其中至少有一个不会被本轮出题选中，于是 review_plan 不为空。
_HISTORY: tuple[tuple[str, str, str, bool, str], ...] = (
    ("evt_0031", "2026-11-10T09:00:00+08:00", "integral_definite", False, "calc"),
    ("evt_0032", "2026-11-10T09:12:00+08:00", "integral_definite", False, "sign"),
    ("evt_0044", "2026-11-11T20:00:00+08:00", "integral_definite", True, "none"),
    ("evt_0051", "2026-11-11T20:20:00+08:00", "integral_improper", False, "concept"),
    ("evt_0052", "2026-11-12T10:00:00+08:00", "integral_improper", True, "none"),
    ("evt_0053", "2026-11-12T10:15:00+08:00", "integral_improper", True, "none"),
    ("evt_0061", "2026-11-12T21:00:00+08:00", "det", True, "none"),
    ("evt_0054", "2026-11-13T08:00:00+08:00", "integral_improper", False, "calc"),
    ("evt_0062", "2026-11-13T09:00:00+08:00", "det", False, "misread"),
)


def _pool() -> list[dict]:
    """造题并统一校验：任何一道不合法都立刻报错，不静默跳过。"""
    out = []
    for i, raw in enumerate(_RAW_POOL, start=1):
        ok, reason = executor.validate_task(raw)
        if not ok:
            raise ValueError("第 %d 道演示题不合法：%s" % (i, reason))
        out.append(executor.normalize_task(
            raw, task_id="P%02d" % i, source="tutor-replay", by=None, gen=None))
    return out


def _literal(x: Fraction) -> str:
    """Fraction -> 合法答案字面量（整数不带斜杠，分数写 a/b）。"""
    if x.denominator == 1:
        return str(x.numerator)
    return "%d/%d" % (x.numerator, x.denominator)


def _wrong_value(ref: Fraction, r: int) -> Fraction:
    """造一个「一定是错、但形态合法」的模拟答案。

    交替用 `-ref`（符号错）和 `2*ref`（差 2 倍）来展示两种错因。
    **必须排除等于 ref 的候选**：ref=0 时 -ref 和 2*ref 都还是 0，
    那样模拟的「错答」会被判 right，自检会当场拦下。
    """
    primary = -ref if r % 2 == 0 else 2 * ref
    return ref + 1 if primary == ref else primary


def _answer(raw_value: str) -> str:
    return json.dumps({"value": raw_value}, ensure_ascii=False)


def _history_events() -> list[dict]:
    return [{"id": eid, "t": t, "kc": name, "correct": correct, "err": err}
            for eid, t, name, correct, err in _HISTORY]


def _last_error(events: list[dict], kc_id: str) -> str | None:
    """该 KC 最近一次答错的错因。第一轮出题时也要能读到历史，不能显示「暂无」。"""
    for event in reversed(events):
        if event.get("kc") == kc_id and event.get("correct") is False:
            return event.get("err")
    return None


def _mastery_map(events: list[dict]) -> dict[str, float]:
    """按 KC 折叠事件流。**带提示答对只按半次独立答对计入**。

    为什么：BKT 测的是「独立会做」。提示后答对直接按全量更新，掌握度会虚高，
    画像就不可信了（评审在 0b73d1b 上点过这条）。这里不改 A 的 bkt 公式，
    只在折叠时对 assisted 事件取半步，口径写进 tutor.json 的 notes。
    """
    out: dict[str, float] = {}
    for name in kc.KC_IDS:
        p = bkt.BKTParams().init
        for ev in events:
            if ev.get("kc") != name:
                continue
            full = bkt.update(p, bool(ev["correct"]))
            p = p + ASSISTED_WEIGHT * (full - p) if ev.get("assisted") else full
        out[name] = p
    return out


def _memory_text(err: str) -> str:
    return ERR_LABELS.get(err, "一次需要复盘的答题")


def _self_check(payload: dict, min_turns: int) -> None:
    """输出前自检。任何一条不过就不要写文件——脏数据进前端比没有更糟。"""
    if len(payload["kcs"]) != 3:
        raise ValueError("kcs 必须恰 3 条")
    if [r["id"] for r in payload["kcs"]] != list(kc.KC_IDS):
        raise ValueError("kcs 的 id / 顺序必须与 KC_IDS 一致")
    if len(payload["turns"]) < min_turns:
        raise ValueError("turns 数量不足：%d < %d"
                         % (len(payload["turns"]), min_turns))
    for turn in payload["turns"]:
        if turn.get("role") != "tutor":
            continue
        w = turn.get("why")
        if not isinstance(w, dict):
            raise ValueError("第 %s 轮 tutor 缺 why" % turn.get("n"))
        for key in ("mastery", "difficulty", "gap", "last_error", "memory"):
            if key not in w:
                raise ValueError("第 %s 轮 why 缺 %s" % (turn.get("n"), key))
        if not isinstance(w["mastery"], (int, float)) or not 0 <= w["mastery"] <= 1:
            raise ValueError("第 %s 轮 mastery 不是 [0,1] 的数" % turn.get("n"))
        if not isinstance(w["memory"], list):
            raise ValueError("第 %s 轮 why.memory 不是数组" % turn.get("n"))
    text = json.dumps(payload, ensure_ascii=False)
    for banned in ('"reference"', "holdout", '"raw_response"',
                   "api_key", "REPLACE_WITH"):
        if banned in text:
            raise ValueError("回放数据含敏感字样：%s" % banned)


def build(*, sid: str = "demo", turns: int = 8,
          at: str = DEFAULT_AT) -> dict:
    """构造回放数据，纯函数（不写文件、不读环境变量），便于测试直接调用。

    `turns` 是**下限**：一个问答轮固定 4 个 turn（出题 / 答错 / 提示 /
    答对），所以实际长度是 4 的倍数且 >= turns。
    """
    if not isinstance(sid, str) or not sid:
        raise ValueError("sid 必须是非空字符串")
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
        raise ValueError("turns 必须是正整数")

    pool = _pool()
    book = DifficultyBook()                 # 纯内存，不落盘 -> 天然幂等
    events = _history_events()
    memory_timeline = []
    hint_used: dict[str, int] = {name: 0 for name in kc.KC_IDS}
    out_turns: list[dict] = []
    n = 0
    rounds = max(1, (turns + 3) // 4)

    for r in range(rounds):
        mastery = _mastery_map(events)
        task = next_task(pool, mastery, book)      # 补最弱 KC + 略高的难度
        kc_id = kc.kc_of(task)
        # 同一道题不重复出，保证回放里每一轮都是新题
        pool = [t for t in pool if t["signature"] != task["signature"]]

        last_err = _last_error(events, kc_id)
        mem_ids = [e["id"] for e in events if e["kc"] == kc_id][-2:]
        reason = why(task, mastery, book, last_err, mem_ids)
        view = executor.solver_view(task)

        ref = executor._ref_fraction(task["reference"])
        wrong_value = _wrong_value(ref, r)     # 交替展示 sign / calc
        wrong_raw = _answer(_literal(wrong_value))
        right_raw = _answer(_literal(ref))

        # 自检：模拟答案必须真的被判成预期结果，题目改了也不会悄悄演砸
        if executor.judge(task, wrong_raw) != "wrong":
            raise ValueError("第 %d 轮的错答没被判 wrong" % (r + 1))
        if executor.judge(task, right_raw) != "right":
            raise ValueError("第 %d 轮的对答没被判 right" % (r + 1))
        wrong_err = classify(task, wrong_raw)
        right_err = classify(task, right_raw)

        # ── turn 1：出题（带 why 三个数字）──────────────────────
        n += 1
        out_turns.append({
            "n": n, "role": "tutor", "kc": kc_id,
            "statement": view["statement"],
            "answer_format": view["answer_format"],
            "why": reason,
        })
        # ── turn 2：答错 ────────────────────────────────────────
        n += 1
        out_turns.append({
            "n": n, "role": "student", "kc": kc_id,
            "answer": _literal(wrong_value), "verdict": "wrong",
            "err": wrong_err,
        })
        t_wrong = "2026-11-14T15:%02d:00+08:00" % (r * 4)
        events.append({"id": "evt_%04d" % (900 + n), "t": t_wrong,
                       "kc": kc_id, "correct": False,
                       "err": wrong_err})

        # ── turn 3：给一级提示 ──────────────────────────────────
        n += 1
        hint_level = min(4, r)
        out_turns.append({
            "n": n, "role": "tutor", "kc": kc_id,
            "hint_level": hint_level, "text": HINT_TEXT[hint_level],
            "why": reason,          # 提示轮也带 why，满足「每条 tutor 轮都有 why」
        })
        hint_used[kc_id] += 1

        # ── turn 4：提示后答对（辅助，不计作独立掌握）──────────
        n += 1
        out_turns.append({
            "n": n, "role": "student", "kc": kc_id,
            "answer": _literal(ref), "verdict": "right", "err": right_err,
            "hint_used": 1,
        })
        t_right = "2026-11-14T15:%02d:00+08:00" % (r * 4 + 2)
        events.append({"id": "evt_%04d" % (900 + n), "t": t_right,
                       "kc": kc_id, "correct": True, "assisted": True,
                       "err": right_err})

        # 错因入记忆时间轴：conf = 该事件之后该 KC 的 BKT 掌握度，
        # 表示「这条错因后来被克服得怎么样」，不是随手写的 0.8。
        conf = bkt.mastery_of(kc_id, events)
        memory_timeline.append({
            "t": t_wrong[:10],
            "text": _memory_text(wrong_err),
            "conf": round(conf, 2),
            "src": ["evt_%04d" % (900 + n - 2)],
        })

        if not pool:
            break

    final_mastery = _mastery_map(events)
    kcs = []
    for name in kc.KC_IDS:
        items = sum(1 for e in events if e["kc"] == name)
        kcs.append({
            "id": name, "name": kc.name_of(name),
            "mastery": round(final_mastery[name], 4),
            "items": items,
            "hint_avg": round(hint_used[name] / items, 2) if items else 0.0,
        })

    # review_plan 是「下次复习计划」（契约 §4.3 的 due 在生成时间之后），
    # 不是「今天到期项」，所以走 plan_items 而不是 due_items。
    review_plan = [
        {"kc": r["kc"], "due": r["due"][:10], "reason": r["reason"]}
        for r in schedule.plan_items(events)
    ]

    payload = {
        "profile_version": PROFILE_VERSION,
        "generated_at": at,
        "student": {"sid": sid, "label": "示例学生"},
        "notes": [
            "本文件为离线回放 fixture：判分/掌握度/难度/复习排期均由本地引擎计算",
            "conf = 该错题之后该知识点的 BKT 掌握度，越高表示错因越已被克服",
            "带提示答对记为 assisted，只按半次独立答对计入掌握度",
            "时间与事件固定，重复运行逐字节相同",
        ],
        "kcs": kcs,
        "turns": out_turns,
        "review_plan": review_plan,
        "memory_timeline": memory_timeline,
    }
    _self_check(payload, turns)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成学生端回放数据 web/tutor.json")
    ap.add_argument("--sid", default="demo", help="学生 id（回放用 demo）")
    ap.add_argument("--turns", type=int, default=8, help="turn 数量下限，默认 8")
    ap.add_argument("--out", default="web/tutor.json", help="输出路径")
    ap.add_argument("--at", default=DEFAULT_AT, help="固定生成时间（保证幂等）")
    ap.add_argument("--offline", action="store_true",
                    help="接受但无效果：本工具本来就不联网")
    args = ap.parse_args(argv)

    payload = build(sid=args.sid, turns=args.turns, at=args.at)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print("✓ 已生成 %s" % shown)
    print("  %d 个 turn｜%d 个 KC｜%d 条复习计划｜%d 条记忆"
          % (len(payload["turns"]), len(payload["kcs"]),
             len(payload["review_plan"]), len(payload["memory_timeline"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
