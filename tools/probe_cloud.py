"""第 0 步真实链路探针：走 app.llm_async.LLMClient，发 3 次真实调用。

与 tools/probe_api.py 的区别：这里用的是正式代码路径——角色温度、并发闸门、
重试边界、原始响应落盘、calls.jsonl 索引、严格 JSON 提取、执行器校验与判分。
跑通它，才算「B2 真实接口连通性」这一条验收完成。

用法：
  $env:LLM_API_KEY='...'
  $env:PYTHONPATH='<repo>/.review_deps'      # 依赖尚未装进解释器时
  python tools/probe_cloud.py

只发 3 次请求（出题 / 解题 / 交叉算子），不产生实验数据，不写 runs/。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import executor as ex  # noqa: E402
from app.config import Config, call_plan  # noqa: E402
from app.genotype import (  # noqa: E402
    GenotypeError,
    build_solver_system,
    load_operator_prompts,
    load_seed_genotypes,
    parse_operator_output,
)
from app.llm_async import LLMClient, build_solver_messages  # noqa: E402


def _row(name: str, res, ok: bool, note: str = "") -> None:
    u = res.usage or {}
    print(
        f"{name:<10} {res.status:<15} {res.latency_ms:>6} ms  "
        f"in={u.get('input_tokens')} out={u.get('output_tokens')}  "
        f"parse={'ok' if res.parse_error is None else res.parse_error}  "
        f"{'PASS' if ok else 'FAIL'} {note}"
    )


async def main() -> int:
    cfg = Config.load(ROOT / "config.local.json")
    people = load_seed_genotypes(ROOT / "prompts" / "seed_genotypes.json")
    cross_sys, _mut_sys = load_operator_prompts(ROOT / "prompts")

    setter_sys = (ROOT / "prompts" / "setter.md").read_text(encoding="utf-8").replace(
        "{set_prompt}", people[0].genes.set_prompt
    )

    plan = call_plan(cfg)
    print(f"config: model={cfg.model} base_url={cfg.base_url} concurrency={cfg.concurrency} "
          f"max_calls={cfg.max_calls} wall={cfg.wall_limit_s}s")
    print(f"dry-run 账: E={plan['E']} F={plan['F']} refetch={plan['refetch_reserve']}")

    failures = 0
    async with LLMClient(cfg, run_id="probe_cloud") as client:
        # 1) 出题
        r1 = await client.chat(
            "setter",
            [{"role": "system", "content": setter_sys},
             {"role": "user", "content": "请出第 1 道题。"}],
            slot_id="probe_setter",
        )
        ok1, why = (False, "no json")
        if r1.content_ok and isinstance(r1.parsed, dict):
            ok1, why = ex.validate_task(r1.parsed)
            if ok1:
                task = ex.normalize_task(r1.parsed, task_id="P1", source="probe",
                                         by=people[0].id, gen=0)
                why = f"{task['signature']} -> {task['reference']}"
            else:
                why = f"非法出题: {why}"
        _row("setter", r1, ok1, str(why))
        failures += 0 if ok1 else 1

        # 2) 解题：附录 A 的 I1（∫₀¹(x+1)dx = 3/2）
        ref_task = ex.normalize_task(
            {"template": "poly", "params": {"coeffs": [1, 1], "lower": 0, "upper": 1}},
            task_id="P2", source="probe", by=None, gen=0,
        )
        view = ex.solver_view(ref_task)
        r2 = await client.chat(
            "solver",
            build_solver_messages(build_solver_system(people[0].genes),
                                  view["statement"], view["answer_format"]),
            slot_id="probe_solver",
        )
        status2 = ex.judge(ref_task, r2.text) if r2.ok else "unanswered"
        _row("solver", r2, status2 == "right", f"judge={status2} ref={ref_task['reference']}")
        failures += 0 if status2 == "right" else 1

        # 3) 交叉算子
        user3 = json.dumps(
            {"parent_a": vars(people[0].genes), "parent_b": vars(people[1].genes)},
            ensure_ascii=False,
        )
        r3 = await client.chat(
            "operator",
            [{"role": "system", "content": cross_sys},
             {"role": "user", "content": user3}],
            slot_id="probe_crossover",
        )
        ok3 = False
        note3 = "no json"
        if r3.content_ok and isinstance(r3.parsed, dict):
            try:
                g3 = parse_operator_output(r3.parsed, max_chars=cfg.max_genotype_chars)
                distinct = g3 != people[0].genes and g3 != people[1].genes
                ok3 = distinct
                note3 = ("解析通过且与两父不同" if distinct
                         else "子代与父代完全相同（视为交叉未发生）")
            except GenotypeError as e:
                note3 = f"schema: {e}"
        _row("operator", r3, ok3, note3)
        failures += 0 if ok3 else 1

    # 索引行必须带 temperature，且按角色取值
    calls = (ROOT / "logs" / "calls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    tail = [json.loads(line) for line in calls[-3:]]
    temps = {r["role"]: r.get("temperature") for r in tail}
    print(f"calls.jsonl 最近三行温度: {temps}")

    print("RESULT:", "PASS" if failures == 0 else f"FAIL({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
