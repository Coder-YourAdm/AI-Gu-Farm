"""吞吐与稳定性压测：同一批题，按给定并发跑 N 次解题调用，量出真实吞吐。

用途：SiliconFlow 上 15 路并发只有 ~40 次/分，而正式 6 趟要 1.4 万次。
先量出「提高并发能不能线性提速、会不会把重试率推上去」，再决定锁哪个并发。

用法：python tools/probe_throughput.py --concurrency 30 --retries 4 --calls 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=30)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--calls", type=int, default=60)
    args = ap.parse_args()

    cfg = Config.load(ROOT / "config.local.json")
    cfg = replace(cfg, concurrency=args.concurrency, network_retries=args.retries)
    people = load_seed_genotypes(ROOT / "prompts" / "seed_genotypes.json")
    tasks = json.loads((ROOT / "data" / "calibration.json").read_text(encoding="utf-8"))

    jobs = [(p, tasks[i % len(tasks)]) for i, p in enumerate(people * 10)][:args.calls]

    async def one(p, t):
        view = ex.solver_view(t)
        r = await client.chat("solver",
                              build_solver_messages(build_solver_system(p.genes),
                                                    view["statement"], view["answer_format"]),
                              slot_id=f"thr_{p.id}_{t['id']}")
        return r

    t0 = time.perf_counter()
    async with LLMClient(cfg, run_id=f"probe_thr_c{args.concurrency}") as client:
        results = await asyncio.gather(*[one(p, t) for p, t in jobs])
    wall = time.perf_counter() - t0

    ok = sum(1 for r in results if r.ok)
    retried = sum(1 for r in results if r.attempts > 1)
    failed = sum(1 for r in results if not r.ok)
    attempts = sum(r.attempts for r in results)
    lat = sorted(r.latency_ms for r in results)
    print(json.dumps({
        "concurrency": args.concurrency, "retries": args.retries,
        "calls": len(results), "ok": ok, "failed": failed,
        "retried_calls": retried, "http_attempts": attempts,
        "wall_s": round(wall, 1), "calls_per_min": round(len(results) / wall * 60, 1),
        "latency_p50_ms": lat[len(lat) // 2], "latency_p95_ms": lat[int(len(lat) * 0.95) - 1],
        "projected_14781_calls_h": round(14781 / (len(results) / wall * 60) / 60, 1),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
