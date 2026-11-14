#!/usr/bin/env python3
"""难度探针：量一下某份题集在真模型上的实际通过率。

为什么需要这个东西
------------------
`data/verify_bank.json` 那 10 道题逐字取自技术规格附录 A，是**执行器验收用例**——
它们的作用是证明判分代码没写错（I1 是 ∫(x+1)dx 这种），不是测模型能力。
拿它做双跑演示，deepseek-flash 10/10，天花板是必然的，不是环境问题。

技术规格 §13 要求 pilot 的平均通过率落在 **20%–80%** 警戒带内；落在带外
（尤其一片全对）说明题目太好识别，要先换题域/参数范围，而不是加代数硬撑。

用法
----
    python tests/probe_difficulty.py --data data/calibration.json --backend real
    python tests/probe_difficulty.py --data data/common_dev.json --n 20 --backend mock

产出**不是**测试断言，是一张表：逐题对错、单题耗时、分母大小、总体通过率。
它跑的是真 API，会花钱，所以不进 pytest。
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo import make_solver, seed_state  # noqa: E402


def load_rows(path: Path, n: int | None) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("tasks") or []
    rows = [{"task": t, "family": t.get("type") or t.get("family") or "?"} for t in data]
    return rows[:n] if n else rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="题集难度探针（真 API，会计费）")
    ap.add_argument("--data", default="data/calibration.json")
    ap.add_argument("--n", type=int, default=None, help="只跑前 N 道")
    ap.add_argument("--backend", choices=("real", "mock"), default="real")
    args = ap.parse_args(argv)

    rows = load_rows(ROOT / args.data, args.n)
    if not rows:
        print("✗ 题集为空", file=sys.stderr)
        return 1

    solver, model_id = make_solver(args.backend)
    state = seed_state()

    print(f"探针：{args.data}  {len(rows)} 题  backend={args.backend}  model={model_id}")
    print(f"{'id':<6}{'type':<10}{'结果':<6}{'秒':>6}  {'参考值(截断)':<28}")
    t0 = time.time()
    results = []
    for row in rows:
        try:
            r = solver(row, state)
        except Exception as e:  # noqa: BLE001
            r = {"answer": "", "correct": False, "reason": f"异常 {e}",
                 "latency_s": 0, "tokens_in": 0, "tokens_out": 0}
        ref = row["task"].get("reference") or {}
        ref_s = ref.get("value") or (f"{ref.get('numerator')}/{ref.get('denominator')}"
                                     if ref else "—")
        mark = "✓" if r["correct"] else "✗"
        print(f"{row['task']['id']:<6}{row['family']:<10}{mark:<6}{r['latency_s']:>6.1f}  "
              f"{str(ref_s)[:28]:<28}")
        results.append(r)

    # 落盘：探针会花钱，结果必须留证。不落盘等于白跑一次。
    # 为什么单独存：控制台输出会随终端滚没，而"真实通过率"是选 check 点的
    # 唯一依据，后面调难度时要拿它跟新数据对比。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "runs" / f"probe_{Path(args.data).stem}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as f:
        for row, r in zip(rows, results):
            f.write(json.dumps({
                "task_id": row["task"]["id"], "family": row["family"],
                "correct": r["correct"], "answer": r.get("answer"),
                "reason": r.get("reason"), "latency_s": r.get("latency_s"),
                "tokens_in": r.get("tokens_in"), "tokens_out": r.get("tokens_out"),
            }, ensure_ascii=False) + "\n")

    # 重复题目在同一份题集里的通过率分布，是选 check 点难度的依据
    n = len(results)
    ok = sum(1 for r in results if r["correct"])
    lat = sorted(r["latency_s"] for r in results)
    print("-" * 62)
    print(f"通过率：{ok}/{n} = {ok / n:.0%}"
          f"   （技术规格 §13 警戒带 20%–80%）")
    print(f"总耗时：{time.time() - t0:.1f}s   单题 P50 {lat[n // 2]:.1f}s   "
          f"P95 {lat[min(n - 1, int(n * 0.95))]:.1f}s")
    fam = {}
    for row, r in zip(rows, results):
        f = fam.setdefault(row["family"], [0, 0])
        f[0] += bool(r["correct"])
        f[1] += 1
    for k, (a, b) in sorted(fam.items()):
        print(f"  {k:<10} {a}/{b}")
    (out_dir / "summary.json").write_text(json.dumps({
        "data": args.data, "backend": args.backend, "model_id": model_id,
        "n": n, "correct": ok, "pass_rate": round(ok / n, 4),
        "p50_latency_s": round(lat[n // 2], 2),
        "p95_latency_s": round(lat[min(n - 1, int(n * 0.95))], 2),
        "by_family": {k: {"correct": a, "total": b} for k, (a, b) in fam.items()},
        "is_mock": args.backend != "real",
        "total_s": round(time.time() - t0, 1),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据：{out_dir.relative_to(ROOT)}/calls.jsonl + summary.json")
    if ok == n:
        print("⚠ 一片全对：题目太好识别。按 §13，先换题域/参数范围，不要加代数硬撑。")
    elif ok == 0:
        print("⚠ 一片全错：先看是不是格式问题（unanswered ≠ wrong），再换模型或题域。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
