"""3 档学生 × 3 套教法 = 9 组对照实验，产出 `web/compare.json`。

## 这是一次单变量实验

题目（`data/calibration.json` 交错取 10 题）、判分（`executor.judge`）、
掌握度（`bkt.mastery_of`）、随机种子派生方式**九组完全相同**，
唯一改变的是 `app.tutor.policy` 里的提示策略。所以 fitness 的差异
只能归因到教法，不能归因到抽题运气。

## 为什么 fitness 要减成本

`answer_only` 一错就摊答案，学生当次答对率高、增益大；但它的第 4 级
提示按 `hint_weight(4)=3` 折算，成本也最高。减掉成本之后，「话少而准」
的教法才可能胜出——这正是完整方案里那条防作弊设计。

## 口径（写进报告，不藏）

- `items` = 每组做的题数（10 题，分工表 A10 允许的时间紧降级）；
- `hints` = **加权提示次数**，第 L 级折算 `hint_weight(L)` 次，不是原始次数；
- `cost` / `fitness` 严格按契约 §6.6 公式，`tools/check_web_contract.py`
  会逐行复算，容差 0.002。

离线、幂等、不调模型、不联网。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app import executor                       # noqa: E402
from app.evalsim import fitness                 # noqa: E402
from app.evalsim.sim_student import SimStudent   # noqa: E402
from app.student import bkt, kc                 # noqa: E402
from app.tutor.policy import (                  # noqa: E402
    get_policy, hint_weight, list_policies,
)

STUDENTS: tuple[str, ...] = ("weak", "mid", "strong")
POLICIES: tuple[str, ...] = list_policies()
N_ITEMS: int = 10
CALIBRATION = ROOT / "data" / "calibration.json"


def derive_item_seed(seed: int, level: str, item: int) -> int:
    """**每题**用一条独立派生的随机流。

    这是单变量实验的关键：三套教法的提示次数不同，会吃掉不同数量的
    随机数；如果共用一个 RNG，第 2 题之后的抽签就会错开，gain 的差
    异全来自随机数而不是教法。按 (seed, 档位, 题号) 派生后，三套在
    同一道题的第一次答题结果逐位相同，gain 才可比。
    """
    digest = hashlib.sha256(
        ("%d|%s|item%d" % (seed, level, item)).encode("utf-8")
    ).digest()
    return seed + 100_000 + item * 1000 + int.from_bytes(digest[:4], "big") % 1000


def _tasks(n_items: int = N_ITEMS) -> list[dict]:
    """从校准卷里交错取题，保证三个 KC 都有样本。"""
    rows = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    integral = [r for r in rows if r["template"] != "det"]
    det = [r for r in rows if r["template"] == "det"]
    if not integral or not det:
        raise ValueError("校准卷缺少积分题或行列式题，无法交错取样")
    out = []
    for i in range(n_items):
        bucket = integral if i % 2 == 0 else det
        out.append(bucket[(i // 2) % len(bucket)])
    if len({kc.kc_of(t) for t in out}) != len(kc.KC_IDS):
        raise ValueError("取样没覆盖全部 3 个 KC，请调整交错规则")
    return out


def _wrap(literal: str | None) -> str | None:
    if literal is None:
        return None
    return json.dumps({"value": literal}, ensure_ascii=False)


def _mean_mastery(events: list[dict]) -> float:
    return sum(bkt.mastery_of(k, events) for k in kc.KC_IDS) / len(kc.KC_IDS)


def run_one(level: str, policy_name: str, *, seed: int = 42,
            tasks: list[dict] | None = None) -> dict:
    """跑一组（学生 × 教法），返回 §4.4 的一行。"""
    if level not in STUDENTS:
        raise ValueError("未知学生档位 %r" % (level,))
    policy = get_policy(policy_name)
    tasks = tasks if tasks is not None else _tasks()

    events: list[dict] = []
    hints = 0
    items = 0
    mastered_at: int | None = None

    for item_index, task in enumerate(tasks):
        student = SimStudent(level, seed=derive_item_seed(seed, level,
                                                           item_index))
        kc_id = kc.kc_of(task)
        first = executor.judge(task, _wrap(student.answer(task, 0)))
        # 能力观测只发生在「无提示的第一次答题」。方案里写了「升级必须
        # 挣来」：靠提示才做对的不算独立掌握，否则 answer_only 一摊答案
        # 就能把掌握度刷上去，fitness 会选出最会嚼碎答案的教法。
        events.append({"kc": kc_id, "correct": first == "right"})

        if first != "right":
            for lvl in policy.hint_levels():
                hints += hint_weight(lvl)
                retry = executor.judge(task, _wrap(student.answer(task, lvl)))
                # 提示后的重试仍要判分（决定还要不要继续给提示），
                # 但**不进 BKT 事件**：它只消耗成本，不产生增益。
                if retry == "right":
                    break

        items += 1
        if mastered_at is None and bkt.level(_mean_mastery(events)) >= 3:
            mastered_at = items

    pre = _mean_mastery([])
    post = _mean_mastery(events)
    g = fitness.gain(pre, post)
    c = fitness.cost(items, hints)
    f = fitness.score(g, c)

    return {
        "student": level,
        "policy": policy_name,
        "gain": round(g, 3),
        "cost": round(c, 3),
        "fitness": round(f, 3),
        "items": items,
        "hints": hints,
        "items_to_mastery": mastered_at or items,
    }


def build(seed: int = 42) -> dict:
    tasks = _tasks()
    rows = [run_one(level, name, seed=seed, tasks=tasks)
            for level in STUDENTS for name in POLICIES]

    # winner 必须回答「哪套教法最好」，不是「哪个学生分最高」。
    # 所以先按教法跨 3 档学生取平均 fitness，再在该教法里挑代表学生。
    means = {
        name: sum(r["fitness"] for r in rows if r["policy"] == name)
              / len(STUDENTS)
        for name in POLICIES
    }
    best_policy = max(POLICIES, key=lambda n: (means[n], -POLICIES.index(n)))
    best_row = max((r for r in rows if r["policy"] == best_policy),
                   key=lambda r: r["fitness"])
    return {
        "seed": seed,
        "students": list(STUDENTS),
        "policies": list(POLICIES),
        "rows": rows,
        "winner": {
            "student": best_row["student"],
            "policy": best_policy,
            "reason": "跨 3 档学生平均 fitness 最高（%.3f）；%s 是该教法下最好的一档"
                      % (means[best_policy], best_row["student"]),
        },
        "policy_means": {k: round(v, 3) for k, v in means.items()},
        "notes": [
            "单变量实验：题目/判分/掌握度/种子派生九组相同，只改提示策略",
            "升级必须挣来：只有无提示的第一次答题进 BKT，提示后重试只花成本不产生增益",
            "hints 是加权提示次数（第 L 级折算 hint_weight(L) 次），不是原始次数",
            "cost=0.3·items/20+0.7·hints/20；fitness=gain−0.25·cost，均按契约 §6.6",
            "winner 按教法跨 3 档学生平均 fitness 选出，不是取 9 行最大值",
            "模拟学生，不是真人；n=3 档 × 10 题，不做显著性主张",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 3×3 教法对照数据")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="web/compare.json")
    args = ap.parse_args(argv)

    payload = build(seed=args.seed)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")

    # 输出前自己过一遍数据侧契约自检（前端标红同口径）
    import check_web_contract
    problems = check_web_contract.check_compare(out)
    if problems:
        print("\n".join(problems))
        print("结果：FAIL，compare.json 未通过契约自检")
        return 1

    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print("✓ 已生成 %s（%d 行，契约自检 PASS）" % (shown, len(payload["rows"])))
    print("  winner: %s × %s  %s"
          % (payload["winner"]["student"], payload["winner"]["policy"],
             payload["winner"]["reason"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
