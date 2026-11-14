#!/usr/bin/env python
"""把 `data/holdout.json` 转成 B 的 `--holdout` 默认要的 JSONL。

**为什么需要这一步**

B 的 `app/evaluate.py::load_holdout_tasks()` 读的是 **JSONL**，一题一行；
`app/run.py` 的默认值是 `data/holdout.jsonl`，注释里写着「C 交付后才有」。

而我这边 `data/holdout.json` 是一个 JSON 数组。格式不对，B7 留出评测一启动就报
「留出卷不存在」，这个错误还会被误读成「C 没交题」。

**只写三个键**：`id` / `template` / `params`。
`reference` 由 B 侧 `engine.solve_exact()` 重算，不采信文件里的值
（他的注释原话：「留出卷的答案只能来自执行器」）。多写反而制造两个真值来源。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "holdout.json"
OUT = ROOT / "data" / "holdout.jsonl"

# B 的 HOLDOUT_MIX：integral=10 / det=10，不符直接报错。这里先自检再写盘。
EXPECT_MIX = {"integral": 10, "det": 10}


def load_rows() -> list[dict]:
    data = json.loads(SRC.read_text(encoding="utf-8"))
    return data["tasks"] if isinstance(data, dict) else data


def build_lines(rows: list[dict]) -> list[str]:
    lines = []
    for r in rows:
        item = {"id": r["id"], "template": r["template"], "params": r["params"]}
        lines.append(json.dumps(item, ensure_ascii=False, sort_keys=False))
    return lines


def check(rows: list[dict]) -> None:
    mix = {"integral": 0, "det": 0}
    for r in rows:
        mix[r["type"]] += 1
    if mix != EXPECT_MIX:
        raise SystemExit(f"题型配额不符：{mix} != {EXPECT_MIX}")
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise SystemExit("留出题 ID 有重复")


def main() -> int:
    rows = load_rows()
    check(rows)
    lines = build_lines(rows)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"✓ {OUT.relative_to(ROOT)}  {len(lines)} 行  {OUT.stat().st_size} 字节")
    print(f"  题型 integral/det = {EXPECT_MIX['integral']}/{EXPECT_MIX['det']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
