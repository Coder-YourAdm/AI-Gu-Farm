"""SM-2 简化版复习排期：答对拉长间隔、答错重置，按知识点聚合到期项。

为什么不用完整的 FSRS：那个要 21 个拟合权重和大量历史日志，12 小时版
拿不到。这里三条公式就能演示「三天后它主动来找你复习」，而且每一步
都能手算复核。正式版可无缝换 `open-spaced-repetition/py-fsrs`（MIT），
接口保持 `item_after / due_items` 不变。

时间一律 ISO 8601 带时区；比较时统一转 UTC，避免 `+08:00` 与 `Z`
混在一起出现「明明没到期却推给你」的假复习。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

MIN_EASE: float = 1.3
MAX_EASE: float = 2.8
INITIAL_INTERVAL: float = 1.0
GROWTH: float = 1.7


def item_after(correct: bool, interval: float,
               ease: float) -> tuple[float, float]:
    """一次答题后的 (下次间隔天数, 新 ease)。

    答对：``interval' = max(1, interval * 1.7 * ease)``，ease 微增；
    答错：间隔重置为 1 天，ease 下调（但不低于 1.3，避免「ease hell」）。
    """
    if type(correct) is not bool:
        raise ValueError(f"correct 必须是 bool，收到 {correct!r}")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool):
        raise ValueError(f"interval 必须是数字，收到 {interval!r}")
    if interval < 0:
        raise ValueError(f"interval 不能为负，收到 {interval!r}")
    if not isinstance(ease, (int, float)) or isinstance(ease, bool):
        raise ValueError(f"ease 必须是数字，收到 {ease!r}")

    if correct:
        new_interval = max(INITIAL_INTERVAL, interval * GROWTH * ease)
        new_ease = min(MAX_EASE, ease + 0.05)
    else:
        new_interval = INITIAL_INTERVAL
        new_ease = max(MIN_EASE, ease - 0.2)
    return new_interval, new_ease


def _parse(value: Any) -> datetime:
    """解析 ISO 字符串 / datetime；无时区时按 UTC 处理，但**保留原 tzinfo**。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"时间不是合法 ISO 8601：{value!r}") from exc
    else:
        raise ValueError("时间必须是 ISO 字符串或 datetime，收到 %s"
                         % type(value).__name__)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_utc(value: Any) -> datetime:
    """统一转 UTC，只用于比较。"""
    return _parse(value).astimezone(timezone.utc)


def due_items(events: list[dict], now, ease: float = 2.5) -> list[dict]:
    """从 L1 事件流算出「现在该复习哪些 KC」，按到期时间升序返回。

    每个 KC 独立折叠：从 `interval=1`、初始 `ease` 起，按时间顺序走
    `item_after`，最后一次答题时间 + 间隔天数 = 到期时间。

    返回项：
        {"kc", "due", "last", "interval", "ease", "streak", "reason"}
    `due` / `last` 都是带时区的 ISO 字符串；`interval` 是天数（float）。

    只返回 `due <= now` 的项。没答过题的 KC 不会出现——它还没有记忆可复习。
    """
    if not isinstance(events, list):
        raise ValueError("events 必须是列表")
    now_utc = _as_utc(now)

    grouped: dict[str, list[tuple[datetime, str, bool, object]]] = {}
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ValueError(f"第 {i} 条事件不是对象")
        kc = ev.get("kc")
        if not isinstance(kc, str) or not kc:
            raise ValueError(f"第 {i} 条事件缺 kc：{ev!r}")
        correct = ev.get("correct")
        if type(correct) is not bool:
            raise ValueError(f"第 {i} 条事件 correct 必须是 bool：{ev!r}")
        raw_t = ev.get("t")
        local = _parse(raw_t)                 # 保留原时区，用于输出
        grouped.setdefault(kc, []).append(
            (local.astimezone(timezone.utc), str(raw_t), correct, local.tzinfo)
        )

    out: list[dict] = []
    for kc, rows in grouped.items():
        rows.sort(key=lambda r: r[0])       # L1 天然有序，这里再兜一层
        interval, cur_ease = INITIAL_INTERVAL, ease
        streak = 0
        for _, _, correct, _ in rows:
            interval, cur_ease = item_after(correct, interval, cur_ease)
            streak = streak + 1 if correct else 0
        last_dt, last_raw, last_correct, local_tz = rows[-1]
        due = last_dt + timedelta(days=interval)
        if due > now_utc:
            continue
        reason = ("上次答错，重置为 1 天后复查"
                  if not last_correct
                  else "连续答对 %d 次，间隔 %.1f 天" % (streak, interval))
        out.append({
            "kc": kc,
            "due": due.astimezone(local_tz).isoformat(),
            "last": last_raw,
            "interval": round(interval, 3),
            "ease": round(cur_ease, 3),
            "streak": streak,
            "reason": reason,
        })

    out.sort(key=lambda r: (r["due"], r["kc"]))
    return out


# `plan_items` 借 `due_items` 的折叠逻辑，用一个足够远的 now 让过滤恒不触发。
# 不改 `due_items` 一行，已有 528 条测试完全不受影响。
_ALL_PLAN_NOW = "9999-12-31T00:00:00+00:00"


def plan_items(events: list[dict], ease: float = 2.5) -> list[dict]:
    """列出**所有** KC 的下次复习计划，不管是否已到期。

    `due_items()` 回答「今天该复习哪些」，只留 `due <= now` 的项；
    `plan_items()` 回答「接下来分别什么时候复习」——契约 §4.3 的
    `review_plan` 属于后者，示例里 `due=2026-11-17` 本来就在生成时间之后。

    两者共用同一套间隔折叠公式，不存在第二份排期实现。没答过题的 KC
    同样不会出现在计划里（它还没有记忆可复习）。
    """
    return due_items(events, _ALL_PLAN_NOW, ease=ease)
