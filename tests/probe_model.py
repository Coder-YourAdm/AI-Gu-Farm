#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探一个具体模型在这套题上的真实通过率、延迟与格式合规度。

为什么要有它：规格 §13 要求 pilot 通过率落在 20%–80%。太高（flash 95–100%）
养蛊没有上升空间，曲线必然是平的；太低（全错）什么也学不到。选模型不能靠
参数规模猜，只能拿题量一次。

与 probe_difficulty.py 的区别：那个读 config.json 的当前模型，用来量"现在这套
题够不够难"；这个直接指定 model / base_url，用来**选**模型。

用法：
    python tests/probe_model.py --model Qwen/Qwen3.5-9B --n 8
    python tests/probe_model.py --model Qwen/Qwen3.5-4B --n 8 --family det
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo import solver_view_local          # noqa: E402
from app.executor import judge                  # noqa: E402

PROMPTS = ROOT / "prompts"


def call(base_url: str, key: str, model: str, msgs: list[dict],
         max_tokens: int, timeout: float = 180) -> tuple[str, int, int, str]:
    body = json.dumps({"model": model, "messages": msgs, "temperature": 0.0,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    ch = d["choices"][0]
    msg = ch.get("message") or {}
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
    u = d.get("usage") or {}
    return (text, int(u.get("prompt_tokens") or 0),
            int(u.get("completion_tokens") or 0), str(ch.get("finish_reason")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="https://api.siliconflow.cn/v1")
    ap.add_argument("--key-env", default="SILICON_KEY")
    ap.add_argument("--data", default="data/calibration.json")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--family", default=None, help="只测某一族：integral / det")
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)

    key = os.environ.get(a.key_env) or ""
    if not key:
        print(f"✗ 环境变量 {a.key_env} 为空", file=sys.stderr)
        return 2

    rows = json.loads((ROOT / a.data).read_text(encoding="utf-8"))
    if a.family:
        rows = [r for r in rows if r["type"] == a.family]
    random.Random(a.seed).shuffle(rows)
    rows = rows[:a.n]

    tpl = (PROMPTS / "solver.md").read_text(encoding="utf-8") if (PROMPTS / "solver.md").exists() else ""
    system = tpl.replace("{solve_prompt}", "逐步求解，最后只输出答案。") \
                .replace("{check_instruction}", "").strip()

    def one(row):
        row = {"task": row, "answer_format": "integer, fraction or finite decimal"}
        v = solver_view_local(row)
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": f"{v['statement']}\n\n答案格式：{v['answer_format']}"}]
        t0 = time.time()
        try:
            text, tin, tout, fin = call(a.base_url, key, a.model, msgs, a.max_tokens)
        except Exception as e:  # noqa: BLE001
            return {"id": row["task"]["id"], "family": row["task"]["type"], "ok": None,
                    "err": str(e)[:120], "lat": round(time.time() - t0, 1),
                    "tin": 0, "tout": 0, "fin": "error"}
        return {"id": row["task"]["id"], "family": row["task"]["type"],
                "ok": judge(row["task"], text), "err": None,
                "lat": round(time.time() - t0, 1), "tin": tin, "tout": tout,
                "fin": fin, "chars": len(text.strip())}

    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        res = list(ex.map(one, rows))

    ok = sum(1 for r in res if r["ok"] == "right")
    un = sum(1 for r in res if r["ok"] == "unanswered" or r["ok"] is None)
    lat = sorted(r["lat"] for r in res)
    print(f"\n模型 {a.model} · {len(res)} 题（{a.data}）")
    for r in res:
        print(f"  {r['id']:<8} {r['family']:<9} {str(r['ok']):<11} "
              f"{r['lat']:>5.1f}s tok {r['tin']}/{r['tout']} finish={r['fin']}"
              + (f" err={r['err']}" if r["err"] else ""))
    print(f"\n通过率 {ok}/{len(res)} = {ok / len(res) * 100:.1f}%  "
          f"| 未作答 {un} | 延迟中位 {lat[len(lat) // 2]:.1f}s "
          f"| 输出 token 中位 {sorted(r['tout'] for r in res)[len(res) // 2]}")

    out = ROOT / "runs" / f"probe_model_{a.model.replace('/', '_')}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps({
        "model": a.model, "n": len(res), "right": ok,
        "accuracy": round(ok / len(res), 4),
        "max_tokens": a.max_tokens, "tasks": res,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"落盘 {out.relative_to(ROOT)}/result.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
