"""按「保留下来的两对」做组内交错留出评测。

为什么不直接 `--evaluate-all`：协议登记了 6 趟，第三组（s44）按操作者决定取消，
其中 F_s44 一次都没开始、没有可评测检查点，`evaluate_group` 会直接拒绝整组。
这里复刻它的调度顺序（同一 order_seed、同一交错洗牌），只把「未开始」的成员
跳过，并在结果里写一条 warning；其余四趟仍然是一次交错评测，不存在逐趟挑着跑。

不改 app/ 下任何文件——留出评测会校验冻结源码指纹，改了就再也评不了。
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import executor  # noqa: E402
from app.config import Config  # noqa: E402
from app.evaluate import (  # noqa: E402
    claim_evaluation, finish_evaluation, index_tasks, load_holdout_tasks,
    plan_seed, prepare_evaluation, run_plan, write_evaluation, write_plan,
)
from app.llm import LLMClient  # noqa: E402
from app.protocol import load_protocol, member_config  # noqa: E402
from app.store import SnapshotStore  # noqa: E402

PROTOCOL = ROOT / "runs" / "protocol_v5.json"


async def main(plan_only: bool = False) -> int:
    protocol = load_protocol(PROTOCOL, data_dir=str(ROOT / "data"))
    tasks = load_holdout_tasks(ROOT / "data" / "holdout.json", executor)
    prepared: dict[str, tuple[Config, dict]] = {}
    skipped: list[str] = []

    for member in protocol["members"]:
        run_id = member["run_id"]
        summary = SnapshotStore(root=ROOT / "runs", run_id=run_id).load_summary()
        if int(summary.get("gens_run") or 0) == 0:
            skipped.append(run_id)
            continue
        cfg = member_config(protocol, member)
        prep = prepare_evaluation(run_id=run_id, cfg=cfg, tasks=tasks,
                                  store_root=str(ROOT / "runs"))
        manifest = prep["manifest"]
        if (manifest.get("protocol_id") != protocol["id"] or
                manifest["group_runs"] != [m["run_id"] for m in protocol["members"]] or
                manifest["dataset"] != protocol["dataset"] or
                manifest["ancestors"] != protocol["ancestors"] or
                manifest["initial_population"] != protocol["initial_population"]):
            raise SystemExit(f"{run_id} 与登记的组不一致，拒绝评测")
        prepared[run_id] = (cfg, prep)

    if skipped:
        for _, prep in prepared.values():
            prep["warnings"].append(
                "以下登记运行未开始（人工取消），不参与评测：" + ", ".join(skipped))

    order_seed = plan_seed(protocol["id"])
    plan = [s for _, prep in prepared.values() for s in prep["plan"]]
    random.Random(order_seed).shuffle(plan)
    print(f"成员 {len(prepared)} 个，跳过 {skipped}；槽位 {len(plan)} 个；order_seed={order_seed}")
    if plan_only:
        for rid, (_, prep) in prepared.items():
            print(f"  {rid}: {len(prep['plan'])} 槽位，检查点 {sorted(prep['populations'])}")
        return 0

    for run_id, (_, prep) in prepared.items():
        prep["plan"] = [s for s in plan if s.run_id == run_id]
        prep["order_seed"] = order_seed
        write_plan(prep["plan"], run_id=run_id, root=str(ROOT / "runs"),
                   order_seed=order_seed)
        claim_evaluation(str(ROOT / "runs"), run_id)

    genes = {(rid, cp, person): g for rid, (_, prep) in prepared.items()
             for (cp, person), g in prep["genes_by"].items()}
    cfg0 = next(iter(prepared.values()))[0]
    async with LLMClient(cfg0, run_id=f"holdout_group_{protocol['id'][:10]}") as chat:
        rows = await run_plan(plan=plan, genes_by=genes,
                              tasks_by_id=index_tasks(tasks), engine=executor,
                              chat=chat, concurrency=cfg0.concurrency)

    for run_id, (cfg, prep) in prepared.items():
        result = finish_evaluation(run_id, cfg, tasks, prep,
                                   [r for r in rows if r["run_id"] == run_id])
        path = write_evaluation(result, root=str(ROOT / "runs"))
        print(f"{run_id}: 末代均分 {result['checkpoints'][-1]['mean']} -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--plan-only" in sys.argv)))
