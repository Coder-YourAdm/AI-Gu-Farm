"""`web/` 数据契约自检：放进 web/ 之前先过这一关。

前端「一行不用改」的前提是数据本身自洽。这个脚本把徐潇提的三条
验收点做成机器可验的检查，A 生成完、C 拉取后都能跑：

    python tools/check_web_contract.py                     # 检查存在的文件
    python tools/check_web_contract.py --require-compare    # compare 缺失也算失败

退出码 0 = 全过，1 = 有错（错误信息逐行打印，对应页面标红）。

公式来自 `docs/AI可读_实施方案.md` §6.6；等 `app/evalsim/fitness.py`
落地后，把 `_expected_cost` / `_expected_fitness` 改成直接 import 它，
避免出现两份公式。目前它只做**校验**，不参与生成。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TUTOR = ROOT / "web" / "tutor.json"
COMPARE = ROOT / "web" / "compare.json"

KC_IDS = ("integral_definite", "integral_improper", "det")
BANNED = ("reference", "holdout", "raw_response", "api_key", "REPLACE_WITH")
TUTOR_TOP = ("profile_version", "generated_at", "student", "kcs", "turns",
             "review_plan", "memory_timeline")
COMPARE_TOP = ("seed", "students", "policies", "rows", "winner")
ROW_FIELDS = ("student", "policy", "gain", "cost", "fitness",
              "items", "hints", "items_to_mastery")
WHY_FIELDS = ("mastery", "difficulty", "gap", "last_error", "memory")

TOL = 0.002          # 与页面「差 > 0.002 标红」完全一致


def _expected_cost(items: float, hints: float,
                   base_items: float = 20, base_hints: float = 20) -> float:
    """§6.6：0.3·(items/20) + 0.7·(hints/20)。"""
    return 0.3 * (items / base_items) + 0.7 * (hints / base_hints)


def _expected_fitness(gain: float, cost: float,
                       weight: float = 0.25) -> float:
    """§6.6：gain − 0.25·cost。"""
    return gain - weight * cost


def _load(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, f"找不到 {path}"
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        return None, f"{path} 读取/解析失败：{exc}"
    if not isinstance(data, dict):
        return None, f"{path} 顶层不是对象"
    return data, None


def check_tutor(path: Path = TUTOR) -> list[str]:
    """徐潇的四条验收 + 结构化约束。返回问题列表，空列表 = 通过。"""
    data, err = _load(path)
    if err:
        return [err]
    assert data is not None
    problems: list[str] = []

    for key in TUTOR_TOP:
        if key not in data:
            problems.append(f"tutor.json 缺顶层字段 {key!r}")

    kcs = data.get("kcs")
    if not isinstance(kcs, list) or len(kcs) != 3:
        problems.append("tutor.json 的 kcs 必须恰 3 条")
    elif [r.get("id") for r in kcs if isinstance(r, dict)] != list(KC_IDS):
        problems.append("tutor.json 的 kcs id/顺序必须是 "
                        "integral_definite / integral_improper / det")

    turns = data.get("turns")
    if not isinstance(turns, list) or not turns:
        problems.append("tutor.json 的 turns 必须是非空数组")
    else:
        for i, turn in enumerate(turns):
            if not isinstance(turn, dict):
                problems.append(f"turns[{i}] 不是对象")
                continue
            if turn.get("role") != "tutor":
                continue
            why = turn.get("why")
            if not isinstance(why, dict):
                problems.append(f"第 {turn.get('n', i)} 条 tutor 轮缺 why")
                continue
            for key in WHY_FIELDS:
                if key not in why:
                    problems.append(f"第 {turn.get('n', i)} 条 why 缺 {key!r}")
            if isinstance(why.get("mastery"), (int, float)) and \
               isinstance(why.get("difficulty"), (int, float)) and \
               isinstance(why.get("gap"), (int, float)):
                expected = why["difficulty"] - why["mastery"]
                if abs(why["gap"] - expected) > 1e-4:
                    problems.append(
                        f"第 {turn.get('n', i)} 条 why.gap 应 = "
                        f"difficulty−mastery = {expected:.4f}")

    # 全文（含嵌套）不许出现参考值相关字样
    text = json.dumps(data, ensure_ascii=False)
    for word in BANNED:
        if word in text:
            problems.append(f"tutor.json 出现敏感字样 {word!r}")
    return problems


def check_compare(path: Path = COMPARE) -> list[str]:
    """§4.4 五项顶层字段 + 9 行 rows + 逐行公式自检。"""
    data, err = _load(path)
    if err:
        return [err]
    assert data is not None
    problems: list[str] = []

    for key in COMPARE_TOP:
        if key not in data:
            problems.append(f"compare.json 缺顶层字段 {key!r}")

    students, policies = data.get("students"), data.get("policies")
    if not isinstance(students, list) or not students:
        problems.append("compare.json 的 students 必须是非空数组")
        students = []
    if not isinstance(policies, list) or not policies:
        problems.append("compare.json 的 policies 必须是非空数组")
        policies = []

    rows = data.get("rows")
    if not isinstance(rows, list):
        problems.append("compare.json 的 rows 必须是数组")
        rows = []
    expected_n = len(students) * len(policies)
    if expected_n and len(rows) != expected_n:
        problems.append(f"compare.json 的 rows 应有 {expected_n} 行，实际 {len(rows)}")

    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"rows[{i}] 不是对象")
            continue
        for key in ROW_FIELDS:
            if key not in row:
                problems.append(f"rows[{i}] 缺字段 {key!r}")
        pair = (row.get("student"), row.get("policy"))
        if pair in seen:
            problems.append(f"rows[{i}] 组合重复：{pair}")
        seen.add(pair)
        if students and row.get("student") not in students:
            problems.append(f"rows[{i}] student 不在 students 里：{pair[0]!r}")
        if policies and row.get("policy") not in policies:
            problems.append(f"rows[{i}] policy 不在 policies 里：{pair[1]!r}")

        try:
            items = float(row["items"])
            hints = float(row["hints"])
            gain = float(row["gain"])
            cost = float(row["cost"])
            fitness = float(row["fitness"])
        except (KeyError, TypeError, ValueError):
            continue        # 缺字段已在上面报过，不重复报

        exp_cost = _expected_cost(items, hints)
        if abs(cost - exp_cost) > TOL:
            problems.append(
                f"rows[{i}] cost={cost} 与 §6.6 公式 {exp_cost:.3f} 不符"
                f"（差 {abs(cost - exp_cost):.4f} > {TOL}）")
        exp_fit = _expected_fitness(gain, exp_cost)
        if abs(fitness - exp_fit) > TOL:
            problems.append(
                f"rows[{i}] fitness={fitness} 与 gain−0.25·cost={exp_fit:.4f} "
                f"不符（差 {abs(fitness - exp_fit):.4f} > {TOL}）")

    winner = data.get("winner")
    if not isinstance(winner, dict):
        problems.append("compare.json 的 winner 必须是对象")
    else:
        if students and winner.get("student") not in students:
            problems.append("winner.student 不在 students 里")
        if policies and winner.get("policy") not in policies:
            problems.append("winner.policy 不在 policies 里")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验 web/ 数据契约")
    ap.add_argument("--tutor", default=str(TUTOR))
    ap.add_argument("--compare", default=str(COMPARE))
    ap.add_argument("--require-compare", action="store_true",
                    help="compare.json 尚未生成时也判失败（用于最终冻结）")
    args = ap.parse_args(argv)

    problems: list[str] = []
    tutor_path, compare_path = Path(args.tutor), Path(args.compare)

    print("[1/2] tutor.json ...")
    p = check_tutor(tutor_path)
    problems += ["  tutor: " + x for x in p]
    print("  OK" if not p else "  %d 个问题" % len(p))

    print("[2/2] compare.json ...")
    if not compare_path.exists() and not args.require_compare:
        print("  SKIP（尚未生成；加 --require-compare 可强制检查）")
    else:
        p = check_compare(compare_path)
        problems += ["  compare: " + x for x in p]
        print("  OK" if not p else "  %d 个问题" % len(p))

    if problems:
        print("\n".join(problems))
        print("\n结果：FAIL（%d 项）" % len(problems))
        return 1
    print("\n结果：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
