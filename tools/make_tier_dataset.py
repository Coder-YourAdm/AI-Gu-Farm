#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成某一难度档的 calibration 题集（只产出 calibration.json）。

为什么要有它：
    校准通过率 12.8% 的根因是 `data/calibration.json` 里 det 全是 3–5 阶、
    积分 n 到 9——不是模型不会，是题太难。分档之后：
      - 原卷（难）   → 12.8%（低于 20% 下限）
      - mix（中等）  → 52.8%（落进 20–80%）
      - t1（简单）   → 92.8%（高于 80% 上限）
    mix 的定义是「只把 10 道行列式降到 2–3 阶，10 道积分原样」，改动最小。

用法：
    python tools/make_tier_dataset.py --tier mix --out data_t2
    python tools/make_tier_dataset.py --tier t1  --out data_t1

只产出 calibration.json（20 题：10 det + 10 integral）。`--calibrate` 只读这个文件。
所有签名/题面/参考值都由 `normalize_task` 计算，不手写答案。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import executor  # noqa: E402

_EASY_DET: list[list[list[int]]] = [
    [[2, 1], [1, 3]], [[-4, 7], [5, -2]], [[1, 2], [3, 4]], [[0, 2], [1, 3]],
    [[3, 0], [0, 4]], [[-1, 2], [2, -1]],
    [[1, 0, 0], [0, 2, 0], [0, 0, 3]], [[1, 2, 0], [3, 4, 0], [0, 0, 5]],
    [[2, -1, 0], [3, 5, 1], [-2, 4, 6]], [[0, 2, 1], [3, 0, 1], [4, 5, 0]],
]

_T1: list[dict] = [
    *({"template": "det", "params": {"matrix": m}} for m in _EASY_DET),
    {"template": "poly", "params": {"coeffs": [3, -2, 1], "lower": -1, "upper": 2}},
    {"template": "poly", "params": {"coeffs": [1, 1], "lower": 0, "upper": 1}},
    {"template": "poly", "params": {"coeffs": [0, 2], "lower": 0, "upper": 3}},
    {"template": "poly", "params": {"coeffs": [0, 0, 5], "lower": 1, "upper": 2}},
    {"template": "gamma", "params": {"c": 1, "n": 2, "rate": 2}},
    {"template": "gamma", "params": {"c": 2, "n": 1, "rate": 3}},
    {"template": "gamma", "params": {"c": 1, "n": 3, "rate": 2}},
    {"template": "gaussian_odd", "params": {"c": 1, "k": 1, "rate": 2}},
    {"template": "gaussian_odd", "params": {"c": 2, "k": 0, "rate": 3}},
    {"template": "gaussian_odd", "params": {"c": 1, "k": 2, "rate": 2}},
]


def _dump(rows: list[dict], out: Path) -> int:
    det = sum(1 for r in rows if r["template"] == "det")
    integral = len(rows) - det
    if det != 10 or integral != 10:
        print(f"✗ 必须是 10 det + 10 integral，现在是 {det} + {integral}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    path = out / "calibration.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ 写入 {path}（{len(rows)} 题：{det} det + {integral} integral）")
    print(f"  签名互斥：{len({r['signature'] for r in rows}) == len(rows)}")
    return 0


def _norm(raw: dict, idx: int) -> dict:
    ok, reason = executor.validate_task(raw)
    if not ok:
        raise ValueError(f"第 {idx} 题未通过校验：{reason}")
    task = executor.normalize_task(raw, task_id=f"K{idx:02d}",
                                   source="calibration-tier", by=None, gen=None)
    return {k: task[k] for k in ("id", "source", "by", "gen", "type", "template",
                                 "params", "signature", "statement", "reference")}


def build_t1(out: Path) -> int:
    rows = [_norm(raw, i) for i, raw in enumerate(_T1, start=1)]
    return _dump(rows, out)


def build_mix(out: Path) -> int:
    """以 data/calibration.json 为底，只把 det 换成 2–3 阶，积分原样。"""
    base = json.loads((ROOT / "data" / "calibration.json").read_text(encoding="utf-8"))
    easy = iter(_EASY_DET)
    rows = []
    for r in base:
        if r["template"] == "det":
            rows.append({"template": "det", "params": {"matrix": next(easy)}})
        else:
            rows.append({"template": r["template"], "params": r["params"]})
    rows = [_norm(raw, i) for i, raw in enumerate(rows, start=1)]
    return _dump(rows, out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 calibration 题集（t1 / mix 两档）")
    ap.add_argument("--tier", choices=("t1", "mix"), required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    builder = build_t1 if args.tier == "t1" else build_mix
    try:
        return builder(Path(args.out))
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
