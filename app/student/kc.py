"""知识点（KC）映射：模板族 → 3 个知识点。

为什么只有 3 个：现有 `executor.py` 只有 4 族模板（poly / gamma /
gaussian_odd / det），12 小时版不新增模板，所以 KC 也不能凭空多出来。
`gamma` 与 `gaussian_odd` 都是反常积分，归到同一个 KC——它们共享
「∫₀^∞ x^n e^{-rx} 型」的解题策略，分开反而让掌握度样本太稀。

映射是**确定性**的 5 行字典，不需要人工标 125 题，也不需要模型判断。
"""
from __future__ import annotations

import json
from pathlib import Path

KC_IDS: tuple[str, ...] = ("integral_definite", "integral_improper", "det")

KC_NAMES: dict[str, str] = {
    "integral_definite": "定积分",
    "integral_improper": "反常积分",
    "det": "行列式",
}

# 模板 → KC。新增模板族时必须同时在这里登记，否则 kc_of 会明确报错。
TEMPLATE_TO_KC: dict[str, str] = {
    "poly": "integral_definite",
    "gamma": "integral_improper",
    "gaussian_odd": "integral_improper",
    "det": "det",
}

PARTITIONS: tuple[str, ...] = (
    "calibration", "common_dev", "challenge_pool", "holdout",
)


def kc_of(task: dict) -> str:
    """返回一道题所属的知识点 id。

    `task` 可以是出题对象 `{"template": ..., "params": ...}`，
    也可以是 `normalize_task()` 产出的完整 Task（十个字段）。

    未知模板一律抛 `ValueError`——**不要静默归到某个 KC**，
    那样掌握度会被一道不属于该知识点的题污染，而且没人查得出来。
    """
    if not isinstance(task, dict):
        raise ValueError("kc_of 需要 dict，收到 %s" % type(task).__name__)
    template = task.get("template")
    if not isinstance(template, str):
        raise ValueError("题目缺少 template 字段（或不是字符串）")
    try:
        return TEMPLATE_TO_KC[template]
    except KeyError as exc:
        raise ValueError(
            "未知模板 %r，未在 TEMPLATE_TO_KC 登记；已登记的：%s"
            % (template, ", ".join(sorted(TEMPLATE_TO_KC)))
        ) from exc


def audit(data_dir: str | Path = "data") -> dict[str, int]:
    """统计四份题集里每个 KC 的题数，任一 KC 为 0 就报错。

    为什么要有这条检查：某个 KC 一道题都没有时，BKT 永远停在先验、
    大屏上会出现一条平线，看起来像"系统坏了"，其实是数据缺型。
    宁可在启动时大声报错。

    文件形式兼容两种：数组，或 `{"tasks": [...]}`（题库里两种都出现过）。
    """
    root = Path(data_dir)
    counts = {kc: 0 for kc in KC_IDS}

    for name in PARTITIONS:
        path = root / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"题集文件不存在：{path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw["tasks"] if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise ValueError(f"{path} 不是题目数组")
        for row in rows:
            kc = kc_of(row)
            if kc not in counts:
                raise ValueError(f"{path}: kc_of 返回了未登记的 KC {kc!r}")
            counts[kc] += 1

    empty = [kc for kc, n in counts.items() if n == 0]
    if empty:
        raise ValueError(
            "以下知识点在四份题集里一道题都没有：%s（当前 %s）"
            % (", ".join(empty), counts)
        )
    return counts


def name_of(kc: str) -> str:
    """KC id → 中文名，给前端直接用。"""
    try:
        return KC_NAMES[kc]
    except KeyError as exc:
        raise ValueError("未知 KC %r，允许的：%s" % (kc, ", ".join(KC_IDS))) from exc
