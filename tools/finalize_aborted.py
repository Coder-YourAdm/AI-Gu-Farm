"""把被人工取消的 runs 落成「终止态」，让留出评测的门禁可以正常放行。

两条都必须如实记录，不许伪装成完成：
  * E_s44：已经跑完 4 代，补一条 aborted 代记录（gen=5，reason=operator_cancelled）；
  * F_s44：一次都没开始，写 manifest + summary，status=aborted，
    reason=operator_cancelled_before_start，不含任何代数。

它不做任何模型调用，也不改动已完成的代。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Config  # noqa: E402
from app.orchestrator import build_aborted_record  # noqa: E402
from app.runner import summarize_run  # noqa: E402
from app.runtime import state_from_payload, state_to_payload  # noqa: E402
from app.store import SnapshotStore, atomic_write_json, rng_from_dict  # noqa: E402

RUNS = ROOT / "runs"


def finalize_started(run_id: str) -> None:
    store = SnapshotStore(root=RUNS, run_id=run_id)
    cfg = Config.from_dict(store.load_manifest()["config"])
    last = max(g for g in store.completed_gens() if g > 0)
    snap = store.load_generation(last)
    rng = rng_from_dict(snap["rng"])
    state = state_from_payload(snap["state"], cfg=cfg, run_id=run_id, rng=rng)
    state.phase = "aborted"
    record = build_aborted_record(state=state, gen=last + 1,
                                  reason="operator_cancelled",
                                  attempts=[], refetch_round=0)
    store.save_generation(record, rng=rng, state_payload=state_to_payload(state))
    store.append_history(record)
    summary = summarize_run([], store)
    summary["status"] = "aborted"
    atomic_write_json(store.summary_path, summary)
    print(f"{run_id}: 末代 {last}，已补 aborted 记录 gen={last + 1}")


def record_not_started(run_id: str, template_run: str, condition: str,
                       protocol_path: str = "runs/protocol_v5.json") -> None:
    store = SnapshotStore(root=RUNS, run_id=run_id)
    if store.run_dir.exists():
        raise SystemExit(f"{run_id} 目录已存在，拒绝覆盖")
    store.run_dir.mkdir(parents=True)
    tpl = SnapshotStore(root=RUNS, run_id=template_run).load_manifest()
    proto = json.loads((ROOT / protocol_path).read_text(encoding="utf-8"))
    member = next(m for m in proto["members"] if m["run_id"] == run_id)
    cfg = Config.from_dict({**proto["config"], "condition": condition,
                            "seed": member["seed"]})
    manifest = dict(tpl)
    manifest.update(run_id=run_id, config=cfg.to_dict(),
                    config_fingerprint=cfg.semantic_fingerprint(),
                    comparison_fingerprint=None)
    atomic_write_json(store.manifest_path, manifest)
    atomic_write_json(store.summary_path, {
        "run_id": run_id, "gens_run": 0, "completed": 0,
        "aborted": [{"gen": 0, "status": "not_started",
                     "reason": "operator_cancelled_before_start"}],
        "call_total": 0, "call_total_lower_bound": False,
        "slot_total": 0, "challenge_slots": 0, "snapshots": [],
        "status": "aborted",
    })
    print(f"{run_id}: 未开始，已写 manifest + aborted summary")


if __name__ == "__main__":
    finalize_started(sys.argv[1] if len(sys.argv) > 1 else "E_s44_7f878e7097")
    record_not_started(sys.argv[2] if len(sys.argv) > 2 else "F_s44_7f878e7097",
                       "F_s43_7f878e7097", "F")
