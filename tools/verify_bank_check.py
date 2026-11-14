# -*- coding: utf-8 -*-
"""核对 `data/verify_bank.json`（C 交付的十道验证题）。

两件事分开查，别混：

1. **判分**：把期望 reference 当答案喂进去必须 `right`；喂「合法但不等」的值
   必须 `wrong`；喂非 JSON 必须 `unanswered` 且不抛异常。
2. **题面**：库里写的 `statement` 是否与 `executor.statement()` 生成的一致。
   不一致不影响判分（judge 不读题面），但会让演示和真实流程对不上。

用法：
    python tools/verify_bank_check.py
"""

import json
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import executor

BANK = Path(__file__).resolve().parent.parent / "data" / "verify_bank.json"


def _fraction(reference: dict) -> Fraction:
    if reference["kind"] == "integer":
        return Fraction(int(reference["value"]))
    return Fraction(int(reference["numerator"]), int(reference["denominator"]))


def _literal(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else "%s/%s" % (
        value.numerator, value.denominator)


def main() -> int:
    bank = json.loads(BANK.read_text(encoding="utf-8"))
    rows = bank["tasks"]

    grade_bad = 0
    stmt_bad = 0

    print("=== 判分（judge 三态）===")
    for row in rows:
        task, ref = row["task"], row["task"]["reference"]
        value = _fraction(ref)

        right = executor.judge(task, json.dumps({"value": _literal(value)}))
        wrong = executor.judge(task, json.dumps({"value": _literal(value + 1)}))
        garbage = executor.judge(task, "not json")

        ok = (right == "right" and wrong == "wrong" and garbage == "unanswered")
        if not ok:
            grade_bad += 1
        print("[%s] %-4s %-10s right=%s wrong=%s garbage=%s" % (
            "OK  " if ok else "FAIL", row["task"]["id"], row["family"],
            right, wrong, garbage))

    print("\n=== 题面（与 executor.statement 比对）===")
    for row in rows:
        task = row["task"]
        expected = executor.statement(task["template"], task["params"])
        if expected != task["statement"]:
            stmt_bad += 1
            print("[DIFF] %-4s 题库：%s" % (task["id"], task["statement"]))
            print("       应为：%s" % expected)

    total = len(rows)
    print("\n判分一致：%d/%d   题面一致：%d/%d" % (
        total - grade_bad, total, total - stmt_bad, total))
    return 1 if (grade_bad or stmt_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
