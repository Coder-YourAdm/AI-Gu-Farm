"""第 0 步：难度探针（120 次调用/模型）。

目的：在花 360 次做正式 calibration 之前，先判断通过率落在哪。
  20%-80%   -> 直接用这个模型跑正式 calibration
  >80%      -> 模板太好识别，需要规格附录 B 的 gamma_sum 变体（要动 A/C 的代码）
  <20%      -> 太难，调参数范围或换模型档

规模：6 份种子基因 × 20 道 calibration 题 × 1 次 = 120 次解题调用。
只用解题角色，不跑出题/互答/算子，因此这不是 pilot，不产生正式实验数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import executor as ex  # noqa: E402
from app.config import Config  # noqa: E402
from app.genotype import build_solver_system, load_seed_genotypes  # noqa: E402
from app.llm_async import LLMClient, build_solver_messages  # noqa: E402


async def one(client, person, task) -> dict:
    view = ex.solver_view(task)
    res = await client.chat(
        "solver",
        build_solver_messages(build_solver_system(person.genes),
                              view["statement"], view["answer_format"]),
        slot_id=f"probe_{person.id}_{task['id']}",
    )
    status = ex.judge(task, res.text) if res.ok else "unanswered"
    return {
        "person": person.id,
        "task": task["id"],
        "type": task["type"],
        "status": status,
        "call_status": res.status,
        "attempts": res.attempts,
        "error": res.error,
        "latency_ms": res.latency_ms,
        "in": (res.usage or {}).get("input_tokens"),
        "out": (res.usage or {}).get("output_tokens"),
        "reason": (res.usage or {}).get("reasoning_tokens"),
        "parse_error": res.parse_error,
    }


async def run(tag: str, cfg: Config, tasks: list[dict], people) -> dict:
    rows: list[dict] = []
    t0 = time.perf_counter()
    async with LLMClient(cfg, run_id=f"probe_{tag}") as client:
        jobs = [one(client, p, t) for p in people for t in tasks]
        rows = await asyncio.gather(*jobs)
    wall = time.perf_counter() - t0

    def rate(items) -> float | None:
        return round(sum(r["status"] == "right" for r in items) / len(items), 4) if items else None

    by_type = {}
    for typ in sorted({r["type"] for r in rows}):
        by_type[typ] = rate([r for r in rows if r["type"] == typ])

    per_person = {}
    for p in people:
        mine = [r for r in rows if r["person"] == p.id]
        per_person[p.id] = {
            "mean": rate(mine),
            "right": sum(r["status"] == "right" for r in mine),
            "unanswered": sum(r["status"] == "unanswered" for r in mine),
        }

    lat = [r["latency_ms"] for r in rows]
    summary = {
        "tag": tag,
        "model": cfg.model,
        "max_tokens_solver": cfg.max_tokens_solver,
        "calls": len(rows),
        "pass_rate": rate(rows),
        "by_type": by_type,
        "unanswered_rate": round(sum(r["status"] == "unanswered" for r in rows) / len(rows), 4),
        "wrong_rate": round(sum(r["status"] == "wrong" for r in rows) / len(rows), 4),
        "latency_p50": int(statistics.median(lat)),
        "latency_p95": int(sorted(lat)[int(len(lat) * 0.95) - 1]),
        "tokens_in": sum(r["in"] or 0 for r in rows),
        "tokens_out": sum(r["out"] or 0 for r in rows),
        "reasoning_tokens": sum(r["reason"] or 0 for r in rows),
        "wall_s": round(wall, 1),
        "throughput_calls_per_min": round(len(rows) / wall * 60, 1),
        "transport_errors": sum(1 for r in rows if r["call_status"] != "ok"),
        "retried_calls": sum(1 for r in rows if r["attempts"] > 1),
        "per_person": per_person,
        "in_band": bool(0.2 <= (rate(rows) or 0) <= 0.8),
    }
    out = ROOT / "logs" / f"probe_difficulty_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True,
                    choices=["flash-think", "chat-plain"])
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--cap", type=int, default=None, help="覆盖 max_tokens_solver")
    ap.add_argument("--concurrency", type=int, default=None, help="覆盖并发上限")
    args = ap.parse_args()

    cfg = Config.load(ROOT / "config.local.json")
    if args.variant == "flash-think":
        tag = "flash_think"
        cfg = replace(cfg, model="deepseek-flash", max_tokens_solver=2000)
    else:
        tag = "chat_plain"
        cfg = replace(cfg, model="deepseek-chat", max_tokens_solver=400)
    if args.cap:
        cfg = replace(cfg, max_tokens_solver=args.cap)
        tag = f"{tag}_cap{args.cap}"
    if args.concurrency:
        cfg = replace(cfg, concurrency=args.concurrency)
        tag = f"{tag}_c{args.concurrency}"

    people = load_seed_genotypes(ROOT / "prompts" / "seed_genotypes.json")
    tasks = json.loads((ROOT / "data" / "calibration.json").read_text(encoding="utf-8"))
    print(f"{args.variant}: model={cfg.model} cap={cfg.max_tokens_solver} "
          f"calls={len(people)*len(tasks)*args.reps}")

    async def all_reps():
        for _ in range(args.reps):
            await run(tag, cfg, tasks, people)

    asyncio.run(all_reps())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
