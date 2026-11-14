"""跑批驱动：把「一代事务」串成「一趟运行」，并在每代完成后落盘。

分工（为什么单独一层）：

- `orchestrator.run_generation()` 只负责**一代**，不碰磁盘、不知道自己是第几趟；
- `store.SnapshotStore` 只负责**写文件**，不知道状态长什么样；
- 这一层把两者接起来：跑完一代 → 序列化整份状态 → 原子写 → 追加历史 → 继续。

续跑的正确性全靠这里的写入时机：**快照只在 `run_generation` 返回之后写**，
而 `run_generation` 在整代成功前不会更新 `state.last_gen`。因此任何时刻断电，
最后一个 `phase=completed` 的文件就是唯一可信的续跑点，中止代只留证据不留位子。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

from .orchestrator import Engine, RunState, run_generation, build_aborted_record
from .runtime import state_to_payload
from .store import SnapshotStore, atomic_write_json
from .llm import ProtocolError
from .budget import RequestBudget, BudgetExceeded
import asyncio
import json


def ensure_fresh_target(store: SnapshotStore) -> None:
    """新建运行前确认目标目录是空的。

    复用 run_id 不会报错，只会把两趟的 `gNN.json` 混在一个目录里——续跑时看到的
    「最后一代」可能是另一趟的，而报告里两者都叫同一个 run_id。宁可拒绝。
    """
    existing = store.gens()
    if existing or store.manifest_path.exists():
        raise ValueError(
            f"目录 {store.run_dir} 已有 {len(existing)} 代快照 "
            f"（{existing}）。换一个 --run-id，"
            "或用 --resume 从那趟续跑"
        )


async def run_loop(*, state: RunState, engine: Engine, dataset: Any,
                   chat: Any, store: SnapshotStore, operators: Any | None = None,
                   until: int | None = None,
                   on_generation: Callable[[dict], None] | None = None,
                   before_generation: Callable[[int], None] | None = None,
                   ) -> list[dict]:
    """从 `state.last_gen + 1` 连跑到 `until`（默认 `cfg.n_gen`）。

    中止（`status != completed`）立即停：不跳过、不掩盖、不把这一代当成功。
    中止代的快照照样落盘，但它 `phase=aborted`，`restore_run` 不会选它。
    """
    target = state.cfg.n_gen if until is None else min(until, state.cfg.n_gen)
    if state.restored:
        raise ProtocolError('Restored state must run in a new branch; use --branch')
    if not state.frozen_manifest:
        raise ProtocolError('Run must be assembled with runtime.new_run')
    if state.run_id != store.run_id:
        raise ProtocolError('State and snapshot destination have different run IDs')
    if state.phase == 'aborted' or any(g > state.last_gen for g in store.gens()):
        raise ProtocolError('Run already has failed evidence; continue only in a new demo branch')
    if not store.manifest_path.exists():
        ensure_fresh_target(store)
        store.write_manifest(state.frozen_manifest)
        from .orchestrator import _ind_payload
        record0 = {'schema_version':'5.1', 'run_id':state.run_id, 'gen':0,
                   'status':'completed', 'condition':state.cfg.condition,
                   'evaluated_population':[_ind_payload(p) for p in state.next_population],
                   'next_population':[_ind_payload(p) for p in state.next_population]}
        state.phase = 'completed'
        store.save_generation(record0, rng=state.rng, state_payload=state_to_payload(state))
        store.append_history(record0)
    if state.last_gen >= target:
        atomic_write_json(store.summary_path, {**summarize_run([], store),
            'status':'completed' if state.last_gen == state.cfg.n_gen else 'interrupted'})
        return []

    budget = getattr(chat, 'budget', None) or RequestBudget(state.cfg.wall_limit_s, state.cfg.max_calls)
    atomic_write_json(store.summary_path, {**summarize_run([], store), 'status':'running'})

    out: list[dict] = []
    for gen in range(state.last_gen + 1, target + 1):
        if before_generation is not None:
            before_generation(gen)

        t0 = time.perf_counter()
        try:
            record = await asyncio.wait_for(run_generation(gen=gen, state=state, engine=engine,
                                          dataset=dataset, chat=chat, operators=operators),
                                          budget.remaining())
        except (Exception, asyncio.CancelledError) as exc:
            state.phase = 'aborted'
            reason = ('budget_limit' if isinstance(exc, (BudgetExceeded, TimeoutError)) else
                      'cancelled' if isinstance(exc, asyncio.CancelledError) else 'execution_error')
            record = (getattr(exc, 'record', None) or getattr(exc.__cause__, 'record', None)
                      or build_aborted_record(state=state, gen=gen, reason=reason,
                          attempts=[], refetch_round=0, warnings=[f'{type(exc).__name__}: {exc}'],
                          censor_reason='budget_limit' if reason == 'budget_limit' else 'run_aborted'))
            record['reason'] = reason
            if reason == 'budget_limit':
                for challenge in record['challenges']:
                    if challenge['state'] == 'censored':
                        challenge['retired_reason'] = 'budget_limit'
            record['usage']['cost'] = None
            record['call_total_lower_bound'] = True
        record["wall_ms"] = int((time.perf_counter() - t0) * 1000)

        # 先落盘再通报：日志说「第 N 代完成」时，磁盘上必须已经有它
        store.save_generation(record, rng=state.rng,
                              state_payload=state_to_payload(state))
        store.append_history(record)
        out.append(record)
        summary = summarize_run(out, store)
        summary['status'] = ('aborted' if record['status'] != 'completed' else
                             'completed' if gen == state.cfg.n_gen else 'interrupted')
        summary['budget'] = budget.to_dict()
        atomic_write_json(store.summary_path, summary)

        if on_generation is not None:
            on_generation(record)
        if record["status"] != "completed":
            break
    return out


def summarize_run(records: Sequence[dict], store: SnapshotStore) -> dict:
    """从全部落盘代汇总，包括分支继承历史；未知调用/费用不补零。"""
    # Summaries cover the whole run, including inherited branch history.
    records = [json.loads(store.snapshot_path(g).read_text(encoding='utf-8'))['record']
               for g in store.gens() if g > 0]
    completed = [r for r in records if r["status"] == "completed"]
    aborted = [r for r in records if r["status"] != "completed"]

    def _calls(rec: dict) -> tuple[int, bool]:
        if "call_total" in rec:
            return int(rec["call_total"] or 0), not rec.get('call_total_lower_bound', False)
        return len(rec.get("attempts") or []), False

    counts = [_calls(r) for r in records]
    return {
        "run_id": store.run_id,
        "gens_run": len(records),
        "completed": len(completed),
        "aborted": [{"gen": r["gen"], "status": r["status"],
                     "reason": r.get("reason")} for r in aborted],
        "call_total": sum(c for c, _ in counts),
        "call_total_lower_bound": not all(exact for _, exact in counts),
        "slot_total": sum(int(r.get("slot_total") or 0) for r in records),
        "challenge_slots": sum(int((r.get("slots") or {}).get("challenge", 0))
                               for r in records),
        "snapshots": store.gens(),
    }
