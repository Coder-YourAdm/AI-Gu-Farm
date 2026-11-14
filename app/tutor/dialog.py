"""助教对话状态机：六个动作，返回契约 4.5 的 12 个字段。

动作：start / submit / skip / hint / why / reset
状态：按 sid 存在内存里（serve.py 的 _TUTORS），演示用 `demo`、实时用 `live01`。

判分直接调 A 的 `executor.judge`；错因分类、选题、可解释性、复习排期分别调
A 的 `student.diagnose` / `student.difficulty` / `student.schedule`——
这里不复制第二套实现（吃了 ancestor 双实现的教训）。

一条刻意的规则：**用过提示再答对，不提高掌握度**。BKT 测的是「独立会做」，
把带提示的正确算进去，掌握度会虚高（评审在 0b73d1b 上点过这条）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .. import executor
from ..student import bkt, diagnose, schedule
from ..student.difficulty import DifficultyBook
from ..student.kc import KC_IDS, kc_of
from .hint import MAX_LEVEL, next_level, render

ANSWER_FORMAT = {
    "integral": "integer, fraction or finite decimal",
    "det": "integer only",
}


class Tutor:
    """一个会话一个实例；状态只在内存。"""

    def __init__(self, sid: str, pool: list[dict], root: str | None = None):
        if not isinstance(pool, list) or not pool:
            raise ValueError("题库为空，Tutor 无法出题")
        self.sid = str(sid)
        self.pool = pool
        self.root = root
        self.book = DifficultyBook()          # 内存难度簿，不落盘
        self._reset_state()

    # ---------------- 状态 ----------------

    def _reset_state(self) -> None:
        self.turn = 0
        self.task: dict | None = None
        self.verdict: str | None = None
        self.err = "none"
        self.hint_level = 0
        self.last_err: str | None = None
        self.events: list[dict] = []
        self.mastery = {kc: bkt.BKTParams().init for kc in KC_IDS}

    def _pick_next(self) -> dict:
        return diagnose.next_task(self.pool, self.mastery, self.book)

    def _judge(self, answer) -> str:
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            return "unanswered"
        raw = json.dumps({"value": str(answer)}, ensure_ascii=False)
        return executor.judge(self.task, raw)

    def _record(self, correct: bool, *, hint_used: bool) -> None:
        kc = kc_of(self.task)
        self.events.append({
            "kc": kc,
            "correct": bool(correct),
            "hint_used": bool(hint_used),
            "hint_level": self.hint_level,
            "err": self.err,
            "task": self.task.get("signature"),
            # schedule.due_items 需要带时区的时间（L1 事件流的标准字段名是 t）
            "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        # 独立答对全额计入；看过提示再答对只算「半次独立答对」（与离线 fixture 同口径）。
        # 不取 0 是因为演示时曲线会只降不升；不取 1 就是评审点过的「提示后也涨」。
        if correct:
            full = bkt.update(self.mastery[kc], True)
            self.mastery[kc] = (full if not hint_used
                                else self.mastery[kc] + 0.5 * (full - self.mastery[kc]))
        else:
            self.mastery[kc] = bkt.update(self.mastery[kc], False)
        self.book.update(self.task, self.mastery[kc], bool(correct))

    # ---------------- 六个动作 ----------------

    def _start(self) -> None:
        self._reset_state()
        self.task = self._pick_next()
        self.turn = 1
        self.verdict = None
        self.err = "none"
        self.hint_level = 0
        self.last_err = None

    def _submit(self, answer) -> None:
        if self.task is None:
            self._start()
        self.turn += 1
        verdict = self._judge(answer)
        hint_used = self.hint_level > 0
        if verdict == "right":
            self.verdict = "right"
            self.err = "none"
            self.last_err = None
            self._record(True, hint_used=hint_used)
            self.task = self._pick_next()
            self.hint_level = 0
        else:
            self.verdict = "wrong"
            raw = None if verdict == "unanswered" else json.dumps(
                {"value": str(answer)}, ensure_ascii=False)
            self.err = diagnose.classify(self.task, raw)
            self.last_err = self.err
            self._record(False, hint_used=hint_used)
            self.hint_level = next_level(self.hint_level, False)

    def _skip(self) -> None:
        if self.task is None:
            self._start()
        self.turn += 1
        self.verdict = "wrong"
        self.err = "giveup"
        self.last_err = "giveup"
        self._record(False, hint_used=self.hint_level > 0)
        self.task = self._pick_next()
        self.hint_level = 0

    def _hint(self) -> None:
        if self.task is None:
            self._start()
        self.hint_level = min(MAX_LEVEL, next_level(self.hint_level, False))

    # ---------------- 视图 ----------------

    def _view(self, t0: float) -> dict:
        kc = kc_of(self.task)
        return {
            "ok": True,
            "turn": int(self.turn),
            "kc": kc,
            "verdict": self.verdict,
            "err": self.err,
            "statement": self.task["statement"],
            "answer_format": ANSWER_FORMAT[self.task["type"]],
            "hint": {
                "level": int(self.hint_level),
                "text": render(self.hint_level, self.task, kc),
            },
            "mastery": {k: round(float(v), 4) for k, v in self.mastery.items()},
            "why": diagnose.why(self.task, self.mastery, self.book,
                                self.last_err, []),
            "review": [{"kc": r["kc"], "due": str(r["due"])[:10]}
                       for r in schedule.plan_items(self.events)],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    def handle(self, action: str, answer=None) -> dict:
        t0 = time.perf_counter()
        if action == "start":
            self._start()
        elif action == "submit":
            self._submit(answer)
        elif action == "skip":
            self._skip()
        elif action == "hint":
            self._hint()
        elif action == "why":
            if self.task is None:
                self._start()
        elif action == "reset":
            self._start()
        else:
            raise ValueError(f"未知动作 {action!r}，允许 start/submit/skip/hint/why/reset")
        return self._view(t0)
