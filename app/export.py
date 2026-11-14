#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开历史导出（白名单实现）。

依据
----
技术规格 §1   L26   export.py：公开历史与结果页；过滤参考值和非公开数据
技术规格 §8   L460  代记录字段；L486 individuals 的 total/est/n_samples 必须都在
技术规格 §8   L516  只白名单提取题面、展示答案、曲线、分数和运行说明；
                    测试题集正文、完整快照及原始请求不放到静态根目录
技术规格 §3.1 L224  基因 meta 字段：id / parents / origin / born_gen
技术规格 §6.3 L384  挑战状态字段

铁律
----
白名单逐字段 copy。**禁止** dict(rec)、{**rec}、json.dump(rec) 这类整体透传——
一个字段写错，标准答案就进了静态目录，成绩作废。

用法
----
    python app/export.py --run runs/E_s42_20261112 --out web/history.json
    python app/export.py --run runs/fake_s42 --out web/history.json --lenient
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "5.1"
EXPORT_SCHEMA = "export/1"
ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# 白名单：字段名逐字照抄技术规格，一个都不许改名（§8 L486 的三方对接约束）
# --------------------------------------------------------------------------
TOP_ALLOWED = (
    "schema_version", "run_id", "condition", "gen", "status", "M", "refetch_round",
    "task_counts", "slots", "ancestor", "individuals", "ranking",
    "timing", "usage", "warnings",
)
IND_ALLOWED = ("id", "solve", "set", "bounty", "total", "est", "n_samples")
ANC_ALLOWED = ("integral_failure", "det_failure", "balanced_failure", "mixed_failure")
SLOT_ALLOWED = ("peer", "common", "ancestor", "challenge")
TASKCOUNT_ALLOWED = ("integral", "det")
USAGE_ALLOWED = ("call_count", "input_tokens", "output_tokens",
                 "reasoning_tokens", "cost")
TIMING_ALLOWED = ("wall_ms", "role_ms")
CHAL_ALLOWED = ("task_id", "born_gen", "window", "state",
                "first_success_attempt", "threshold_attempt", "retired_reason")
CHAL_OBS_ALLOWED = ("gen", "attempt", "correct", "p_t")
GENE_META_ALLOWED = ("id", "parents", "origin", "born_gen")

#: §8 L486 明确要求三者必须都落盘，缺一事后无法解释淘汰原因
IND_REQUIRED = ("total", "est", "n_samples")

#: 出现即视为泄漏的键名（§8 L516 只允许白名单）
FORBIDDEN_KEYS = frozenset({
    "reference", "references", "gold", "golden",
    "raw_response", "raw_request", "raw", "prompt_tokens_raw",
    "api_key", "authorization", "auth_header", "messages",
})
#: 出现即视为泄漏的字符串片段
FORBIDDEN_SNIPPETS = ("holdout.jsonl", "holdout_", "Bearer ", "sk-")


class ExportError(RuntimeError):
    """导出失败：宁可中断，也不输出可疑文件。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _num(v: Any) -> Any:
    """浮点统一截到 6 位，避免 15 位有效数字上大屏。"""
    if isinstance(v, float):
        return round(v, 6)
    return v


def _scalar_map(d: Any, allowed: Iterable[str]) -> dict:
    """按白名单取标量字段；结构体、数组一律丢弃。"""
    out: dict = {}
    if not isinstance(d, dict):
        return out
    for k in allowed:
        if k in d:
            v = d[k]
            if v is None or isinstance(v, (int, float, str, bool)):
                out[k] = _num(v)
    return out


# --------------------------------------------------------------------------
# 单代导出
# --------------------------------------------------------------------------
def export_generation(rec: dict, lenient: bool = False) -> tuple[dict, list[str]]:
    """把一条代记录裁剪成公开结构。返回 (裁剪结果, 问题列表)。"""
    problems: list[str] = []
    out: dict = {}

    for key in TOP_ALLOWED:
        if key not in rec:
            continue
        val = rec[key]

        if key == "task_counts":
            out[key] = _scalar_map(val, TASKCOUNT_ALLOWED)
        elif key == "slots":
            out[key] = _scalar_map(val, SLOT_ALLOWED)
        elif key == "ancestor":
            out[key] = _scalar_map(val, ANC_ALLOWED)
        elif key == "individuals":
            inds = []
            for item in (val or []):
                if isinstance(item, dict):
                    inds.append(_scalar_map(item, IND_ALLOWED))
            out[key] = inds
        elif key == "ranking":
            out[key] = [str(x) for x in (val or [])]
        elif key == "timing":
            t = val if isinstance(val, dict) else {}
            out[key] = {
                "wall_ms": _num(t.get("wall_ms")),
                "role_ms": {k2: _num(v2)
                            for k2, v2 in (t.get("role_ms") or {}).items()},
            }
        elif key == "usage":
            out[key] = _scalar_map(val, USAGE_ALLOWED)
        elif key == "warnings":
            out[key] = [str(w)[:300] for w in (val or [])]
        elif val is None or isinstance(val, (int, float, str, bool)):
            out[key] = _num(val)
        # 其余结构（evaluated_population / next_population / challenges）单独处理，
        # 绝不整体透传

    # §8 L486：individuals 三个字段必须都在
    for idx, ind in enumerate(out.get("individuals") or []):
        missing = [f for f in IND_REQUIRED if f not in ind]
        if missing:
            msg = (f"gen={rec.get('gen')} individuals[{idx}] "
                   f"缺字段 {missing}（§8 要求 total/est/n_samples 必须落盘）")
            problems.append(msg)

    if problems and not lenient:
        raise ExportError("；".join(problems))
    return out, problems


# --------------------------------------------------------------------------
# 血脉与挑战（从完整记录里抽公开子集）
# --------------------------------------------------------------------------
def build_lineage(recs: list[dict], include_genes: bool = False) -> dict:
    """从 meta 抽血脉关系；include_genes 时附带基因文本（点开才看）。

    基因文本是**参赛者写的 prompt**，不是参考值，所以展示它不违反 §8 的白名单
    要求；但仍按 §3.1 的长度上限截断，避免异常数据把页面撑爆。
    """
    lineage: dict = {}
    for rec in recs:
        g = rec.get("gen")
        for arr_name in ("evaluated_population", "next_population"):
            for item in (rec.get(arr_name) or []):
                if not isinstance(item, dict):
                    continue
                meta = item.get("meta")
                if not isinstance(meta, dict):
                    continue
                mid = meta.get("id")
                if not mid:
                    continue
                node = lineage.setdefault(str(mid), {
                    "id": str(mid), "parents": [], "origin": None,
                    "born_gen": None, "seen_gens": [],
                })
                node["parents"] = [str(p) for p in (meta.get("parents") or [])]
                node["origin"] = meta.get("origin")
                node["born_gen"] = meta.get("born_gen")
                if g not in node["seen_gens"]:
                    node["seen_gens"].append(g)
                if include_genes:
                    genes = item.get("genes")
                    if isinstance(genes, dict):
                        node["genes"] = {
                            "solve_prompt": str(genes.get("solve_prompt") or "")[:400],
                            "set_prompt": str(genes.get("set_prompt") or "")[:400],
                            "self_check": (bool(genes["self_check"])
                                           if "self_check" in genes else None),
                        }
    return lineage


def build_challenges(recs: list[dict]) -> list[dict]:
    """按 task_id 去重，后者（更新的状态）覆盖前者；不带题目正文。"""
    latest: dict = {}
    for rec in recs:
        for ch in (rec.get("challenges") or []):
            if not isinstance(ch, dict):
                continue
            tid = ch.get("task_id")
            if not tid:
                continue
            node = _scalar_map(ch, CHAL_ALLOWED)
            node["observations"] = [
                _scalar_map(o, CHAL_OBS_ALLOWED) for o in (ch.get("observations") or [])
            ]
            latest[str(tid)] = node
    return [latest[k] for k in sorted(latest)]


def build_summary(recs: list[dict]) -> dict:
    """结果页用的汇总：末代冠军 + 末代祖先指标。"""
    if not recs:
        return {}
    last = recs[-1]
    inds = [i for i in (last.get("individuals") or []) if isinstance(i, dict)]
    winner = None
    if inds:
        top = max(inds, key=lambda i: (i.get("est") if i.get("est") is not None
                                       else float("-inf")))
        winner = {"id": top.get("id"), "est": _num(top.get("est")),
                  "total": _num(top.get("total"))}
    return {
        "n_gen_completed": len(recs),
        "last_gen": last.get("gen"),
        "winner": winner,
        "final_ancestor": _scalar_map(last.get("ancestor"), ANC_ALLOWED),
    }


# --------------------------------------------------------------------------
# 泄漏扫描（导出前后各扫一次）
# --------------------------------------------------------------------------
def find_leaks(obj: Any, path: str = "$") -> list[str]:
    """递归找禁用键名与禁用片段。"""
    hits: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k)
            if ks.lower() in FORBIDDEN_KEYS:
                hits.append(f"{path}.{ks} (禁用键名)")
            hits.extend(find_leaks(v, f"{path}.{ks}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(find_leaks(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        for snip in FORBIDDEN_SNIPPETS:
            if snip in obj:
                hits.append(f"{path} 含片段 {snip!r}")
    return hits


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def resolve_is_mock(run_dir: Path) -> tuple[bool, str]:
    """判定这份数据是不是假数据。返回 `(is_mock, 依据说明)`。

    为什么导出器要自己算这个字段：`web/history.json` 会进静态目录，观众打开大屏
    看到 20 代曲线，无法从任何数字上分辨它是 `runs/fake_s42` 还是真跑出来的。
    `run_id` 里带 "fake" 只是命名习惯，不是机制。

    判定方向只有一个：**证明不了是真的，就标成假的。**
    反过来（缺证明就当真）会把 mock 数据混进正式结论，那是比赛里唯一不可挽回的错。
    解除红标的唯一条件：`runs/<run_id>/manifest.json` 里 `backend == "real"`
    或 `is_mock == false`。

    这个字段不来自代记录，是导出器自己算的，所以不违反 §8 的白名单要求
    （白名单管的是「不许把记录里的字段整份透传」）。
    """
    man = run_dir / "manifest.json"
    if man.exists():
        try:
            obj = json.loads(man.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return True, f"{man.name} 不是合法 JSON，按假数据标记"
        if isinstance(obj, dict):
            if isinstance(obj.get("is_mock"), bool):
                return obj["is_mock"], f"manifest.is_mock={obj['is_mock']}"
            backend = str(obj.get("backend") or "").strip().lower()
            if backend == "real":
                return False, "manifest.backend=real"
            if backend:
                return True, f"manifest.backend={backend}"

    name = run_dir.name.lower()
    if name.startswith(("fake", "mock")):
        return True, f"目录名 {run_dir.name} 是假数据/联调数据的命名"

    return True, ("没有 manifest.json 能证明是 --backend real 的产出，"
                  "按假数据标记（宁误标假，不误标真）")


def load_history(path: Path) -> list[dict]:
    """读 history.jsonl：只收 status=completed，同代后者覆盖（§8 L512 不把半行当一代）。"""
    if not path.exists():
        raise ExportError(f"找不到 {path}")
    by_gen: dict = {}
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExportError(f"{path}:{lineno} JSON 解析失败: {exc}") from exc
            if not isinstance(rec, dict):
                raise ExportError(f"{path}:{lineno} 不是对象")
            if rec.get("status") != "completed":
                continue
            gen = rec.get("gen")
            if gen is None:
                continue
            by_gen[gen] = rec
    return [by_gen[g] for g in sorted(by_gen)]


def export_run(run_dir: Path, out_path: Path, lenient: bool = False,
               include_genes: bool = False) -> dict:
    recs = load_history(run_dir / "history.jsonl")
    if not recs:
        raise ExportError(f"{run_dir}/history.jsonl 里没有 status=completed 的代")

    gens: list[dict] = []
    problems: list[str] = []
    for rec in recs:
        cleaned, probs = export_generation(rec, lenient=lenient)
        gens.append(cleaned)
        problems.extend(probs)

    first = recs[0]
    is_mock, mock_reason = resolve_is_mock(run_dir)
    payload = {
        "schema": EXPORT_SCHEMA,
        "schema_version": first.get("schema_version", SCHEMA_VERSION),
        "run_id": first.get("run_id"),
        "condition": first.get("condition"),
        "is_mock": is_mock,
        "mock_reason": mock_reason,
        "n_gen": len(gens),
        "generations": gens,
        "lineage": build_lineage(recs, include_genes=include_genes),
        "challenges": build_challenges(recs),
        "summary": build_summary(recs),
        "problems": problems,
    }

    leaks = find_leaks(payload)
    if leaks:
        raise ExportError("导出结果仍含敏感内容，已中止：\n  " + "\n  ".join(leaks[:20]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)  # 原子发布，避免大屏读到半个文件
    return payload


def verify(out_path: Path) -> int:
    """§8 L516 的两条硬自检：reference 计数必须为 0，留出题面不得出现。

    2026-11-13 增：`is_mock` 必须是真布尔值。缺了这个字段，页面就只能猜数据真假，
    而「分不清真假」比「显示假数据」危险得多。
    """
    text = out_path.read_text(encoding="utf-8")
    checks = {
        "reference 出现次数": text.count('"reference"'),
        "holdout 出现次数": text.count("holdout"),
        "raw_response 出现次数": text.count('"raw_response"'),
        "api_key 出现次数": text.count("api_key"),
    }
    bad = {k: v for k, v in checks.items() if v}
    for k, v in checks.items():
        print(f"  {k}: {v}")

    probed = json.loads(text)
    flag = probed.get("is_mock")
    if not isinstance(flag, bool):
        bad["is_mock 缺失或不是布尔值"] = repr(flag)
        print(f"  is_mock: {flag!r} ← 必须是 true / false")
    else:
        print(f"  is_mock: {str(flag).lower()}（{probed.get('mock_reason', '')}）")

    if bad:
        print("✗ 自检未通过：" + ", ".join(f"{k}={v}" for k, v in bad.items()))
        return 2
    print("✓ 自检通过：无参考值、无留出题面、无原始请求、无 api_key；is_mock 已声明")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公开历史导出（白名单）")
    ap.add_argument("--run", required=True, help="runs/<run_id> 目录")
    ap.add_argument("--out", default="web/history.json", help="输出路径")
    ap.add_argument("--lenient", action="store_true",
                    help="缺 total/est/n_samples 时只告警不中断（仅用于假数据联调）")
    ap.add_argument("--include-genes", action="store_true",
                    help="附带基因 prompt 文本（页面点开才显示）；不含参考值")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    out_path = Path(args.out)
    try:
        payload = export_run(run_dir, out_path, lenient=args.lenient,
                             include_genes=args.include_genes)
    except ExportError as exc:
        print(f"✗ 导出失败: {exc}", file=sys.stderr)
        return 1

    print(f"✓ 已导出 {out_path}（{payload['n_gen']} 代 / "
          f"{len(payload['lineage'])} 个个体 / "
          f"{len(payload['challenges'])} 道挑战）")
    print(f"  数据真伪: {'假（mock）' if payload['is_mock'] else '真（real）'}"
          f" —— {payload['mock_reason']}")
    if payload["problems"]:
        print(f"⚠ {len(payload['problems'])} 条字段告警：")
        for p in payload["problems"][:10]:
            print("   -", p)

    # 顺手刷新 file:// 兜底包（只在输出落在 web/ 时有意义）
    if out_path.resolve().parent == (ROOT / "web").resolve():
        try:
            from app.webdata import bundle
        except ModuleNotFoundError:      # `python app/export.py` 时 sys.path 里没有仓库根
            sys.path.insert(0, str(ROOT))
            from app.webdata import bundle
        bundle()

    return verify(out_path)


if __name__ == "__main__":
    raise SystemExit(main())
