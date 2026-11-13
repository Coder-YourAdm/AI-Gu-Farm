#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双跑演示编排（日新赛道 · 现场跑两次）。

赛道规则 → 代码约束
--------------------
「两次之间作品自己从第一次学，人不能改代码 / 参数 / 提示词」
    → 两次运行读**同一份** config.json、同一份 prompts/，
      唯一变化是 runs/<pair>/state/current.json（由 learn_from() 自动写）。
      启动与结束时各对 config.json、prompts/ 求 SHA-256 并比对，
      不一致直接终止 —— 这是给评委看的证据，不是自证。

「单次最好在 1 分钟内」
    → 单次 = 1 个精英个体 × 冻结验证题库（10 题），并发 6，实测 20–35 秒。
      不是整代 78 次调用（那要 2–3 分钟，必超时）。

「怎么退回第一次之前的版本」
    → 每次跑之前先把 state 拍快照到 state/v0_seed.json，
      `rollback` 子命令逐字节恢复并回读校验。

与 A / B 的接口（严格照技术规格 §2.3，不是我自己定的）
--------------------------------------------------------
    A: app/executor.py
        judge(task: dict, raw_answer: str | None) -> str
        返回 **"right" / "wrong" / "unanswered"**（字符串，三元）
        可选 solver_view(task) -> {"type","statement","answer_format"}
    B: app/llm.py
        complete(messages: list[dict]) -> (text, tokens_in, tokens_out)
        （或返回带 .text / .tokens_in / .tokens_out 的对象，两种都认）

用法
----
    python -m app.demo pair --backend mock          # 假模型，先跑通链路与页面
    python -m app.demo pair --backend real          # 真模型
    python -m app.demo rollback --pair demo_pair_xx --to v0_seed
    python -m app.demo status --pair demo_pair_xx
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evolve_schema import (  # noqa: E402
    SCHEMA, SCHEMA_VERSION, validate, verdict_of,
)

RUNS = ROOT / "runs"
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
WEB = ROOT / "web"

BANK_FILE = DATA / "verify_bank.json"
CONFIG_FILE = ROOT / "config.json"

# 单次运行并发度。6 是实测稳的值：再高容易触发限流，再低单次超过 40 秒
CONCURRENCY = 6

# 族级规则模板：**只根据 pass1 的失败族自动生成**，不预置任何具体题的答案
RULE_LIB: dict[str, tuple[str, str]] = {
    "integral": (
        "积分中间结果一律写成 Fraction(分子,分母)，保留完整分子分母，禁止中途约分为小数",
        "分部积分后逐项复核符号；定积分代入前检查端点是否为奇点，是则改用极限",
    ),
    "det": (
        "Bareiss 消元每步检查主元；主元为 0 时交换行、结果乘 -1，再继续消元",
        "消元结束前用对角线乘积自检一次，与展开式首项符号比对",
    ),
    "balanced": (
        "等式两边同乘同除时同步更新所有项，不允许只改一侧",
        "得到答案后代回原式验算一次，不通过则重算",
    ),
    "mixed": (
        "先判定题目属于积分还是行列式，再选对应解法，不得混用两种记号",
        "答案格式按 answer_format 输出，不额外附加单位或解释",
    ),
}


# ────────────────────────────── 指纹 ──────────────────────────────
def sha_of_paths(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        if not p.exists():
            continue
        h.update(p.relative_to(ROOT).as_posix().encode() + b"\0")
        h.update(p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def frozen_fingerprint() -> dict:
    cfg = [CONFIG_FILE] if CONFIG_FILE.exists() else []
    prm = sorted(PROMPTS.glob("*.md")) + sorted(PROMPTS.glob("*.json")) if PROMPTS.exists() else []
    return {
        "config_sha256": sha_of_paths(cfg) if cfg else "no-config",
        "prompts_sha256": sha_of_paths(prm) if prm else "no-prompts",
        "n_prompt_files": len(prm),
    }


# ────────────────────────────── 状态 ──────────────────────────────
def seed_state() -> dict:
    return {
        "version": "v0_seed",
        "rules": [],
        "memories": [],
        "rule_weight": {},
        "verify_depth": 1,
        "genes": {},
        "parent_version": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def pair_dir(pair: str) -> Path:
    return RUNS / pair


def state_path(pair: str, which: str = "current") -> Path:
    return pair_dir(pair) / "state" / f"{which}.json"


def save_state(pair: str, state: dict, as_name: str) -> Path:
    p = state_path(pair, as_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


def load_state(pair: str, which: str = "current") -> dict:
    p = state_path(pair, which)
    if not p.exists():
        raise SystemExit(f"✗ 找不到状态文件 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


# ────────────────────────────── 学习 ──────────────────────────────
def learn_from(pass1: dict, prev_state: dict) -> dict:
    """**pass1 的唯一产物**：把失败模式编译成规则 + 记忆 + 状态参数。

    这是「作品自己学」的全部实现。人不写一个字：
    规则由族级失败率触发，记忆文本直接引自 pass1 的错误原因。
    """
    st = json.loads(json.dumps(prev_state))
    fail: dict[str, list[dict]] = {}
    for t in pass1["per_task"]:
        if not t["correct"]:
            fail.setdefault(t["family"], []).append(t)

    total_by_fam: dict[str, int] = {}
    for t in pass1["per_task"]:
        total_by_fam[t["family"]] = total_by_fam.get(t["family"], 0) + 1

    new_rules, new_mems = [], []
    for fam, items in sorted(fail.items(), key=lambda kv: -len(kv[1])):
        lib = RULE_LIB.get(fam)
        if lib is None:
            continue
        ids = [t["id"] for t in items]
        new_rules.append({
            "id": f"R{len(st['rules']) + len(new_rules) + 1}",
            "from_pass": 1,
            "scope": fam,
            "text": lib[0],
            "secondary": lib[1],
            "evidence": f"{'、'.join(ids)} 共 {len(items)}/{total_by_fam[fam]} 题失败",
            "trigger_fail_rate": round(len(items) / max(1, total_by_fam[fam]), 3),
        })
        key = f"rule_weight[{fam}]"
        st["rule_weight"][key] = round(min(0.5, st["rule_weight"].get(key, 0.0) + 0.35), 3)

        for t in items[:2]:
            new_mems.append({
                "id": f"M{len(st['memories']) + len(new_mems) + 1}",
                "from_pass": 1,
                "kind": "case",
                "family": fam,
                "text": f"{t['id']} 首错原因：{t.get('reason') or '答案与期望值不符'}",
                "used_in_pass": 2,
            })

    st["rules"].extend(new_rules)
    st["memories"].extend(new_mems)
    if new_rules:
        st["verify_depth"] = min(3, int(st.get("verify_depth", 1)) + 1)
    st["version"] = "v1_learned"
    st["parent_version"] = prev_state["version"]
    return st


def build_check_instruction(state: dict) -> str:
    """把 state 里的规则渲染成注入 solver prompt 的检查指令。

    这是「规则生效」的唯一通道：规则不是代码，是拼进提示词的一段文字。
    因此改动一定落在 state，绝不会落在 prompts/solver.md 本身。
    """
    rules = state.get("rules") or []
    if not rules:
        return ""
    lines = ["作答前逐条执行以下校验（来自历史失败的自动总结）："]
    for r in rules:
        lines.append(f"- [{r['id']}] {r['text']}")
        if r.get("secondary"):
            lines.append(f"  复核：{r['secondary']}")
    return "\n".join(lines)


# ────────────────────────────── 题库 ──────────────────────────────
def load_bank() -> list[dict]:
    """返回 bank 行：{"family", "answer_format", "mock_wrong_reason", "task"}。"""
    if not BANK_FILE.exists():
        raise SystemExit(
            f"✗ 找不到验证题库 {BANK_FILE}\n"
            "  先运行 python tests/make_verify_bank.py\n"
            "  题库必须冻结：现场生成 = 换题 = 两次不可比"
        )
    payload = json.loads(BANK_FILE.read_text(encoding="utf-8"))
    rows = payload["tasks"] if isinstance(payload, dict) else payload
    for i, r in enumerate(rows):
        if "task" not in r:
            raise SystemExit(f"✗ 题库第 {i} 条缺 task 子对象，请重新跑 make_verify_bank.py")
    return rows


def reference_text(task: dict) -> str:
    """把 reference 转成人类/模型可读的答案串，仅 mock 与日志用。"""
    ref = task.get("reference") or {}
    kind = ref.get("kind")
    if kind == "rational":
        return f"{ref.get('numerator')}/{ref.get('denominator')}"
    if kind == "integer":
        return str(ref.get("value"))
    return json.dumps(ref, ensure_ascii=False)


def solver_view_local(row: dict) -> dict:
    """规格 §2.3 的 SolverTask：键恰为 type / statement / answer_format。

    A 提供 solver_view() 时优先用他的；没有就按规格自己构造白名单。
    """
    return {
        "type": row["task"]["type"],
        "statement": row["task"]["statement"],
        "answer_format": row.get("answer_format", "integer, fraction or finite decimal"),
    }


# ────────────────────────────── 后端 ──────────────────────────────
_WARNED: set[str] = set()


def _coerce_verdict(v: Any) -> tuple[bool, str | None]:
    """把 judge() 的返回值归一成 (ok, reason)。

    规格 §2.3 定义 judge 返回 **str**：right / wrong / unanswered。
    但 A 若先实现成 tuple，也不该让现场演示直接崩 —— 告警一次后兼容。
    """
    if isinstance(v, str):
        s = v.strip().lower()
        return (s == "right"), (None if s == "right" else s)
    if isinstance(v, tuple) and v:
        if "tuple" not in _WARNED:
            print("  ⚠ judge() 返回了 tuple，规格 §2.3 规定返回 str，请 A 校正",
                  file=sys.stderr)
            _WARNED.add("tuple")
        return bool(v[0]), (None if v[0] else (str(v[1]) if len(v) > 1 else "wrong"))
    raise TypeError(f"judge() 返回值类型不支持：{type(v).__name__}")


def wrong_answer_text(task: dict) -> str:
    """造一个**格式合法但值不等**的答案，用于 mock 的「答错」分支。

    刻意不用 "0"：真答案本身可能就是 0（积分为零、行列式为零），
    那样 judge 会判 right，mock 的「想答错」与实际判分打架。
    """
    ref = task.get("reference") or {}
    if ref.get("kind") == "integer":
        return str(int(ref["value"]) + 1)
    if ref.get("kind") == "rational":
        num = int(ref["numerator"]) + int(ref["denominator"])
        return "%s/%s" % (num, ref["denominator"])
    return "0"


def make_solver(backend: str) -> tuple[Callable, str]:
    """返回 (solve_one, model_id)。solve_one(row, state) -> dict。"""
    if backend == "mock":
        # 判分必须走 A 的 judge()。mock 只假装「模型会不会做对」，
        # 「做对了没有」这件事不能也由 mock 说了算 —— 否则页面上的
        # 准确率是抽签结果，不是判分结果。
        try:
            from app.executor import judge as _mock_judge  # type: ignore
        except Exception:  # noqa: BLE001
            _mock_judge = None

        def solve_one(row: dict, state: dict) -> dict:
            """假模型：**确定性**模拟，供链路联调与现场排练。

            每题的“能力抽签值” u = sha256(id) 归一化，规则只能把 p 抬高，
            因此 pass2 在每道题上只会变好或不变 —— 规则型学习应有的单调性。
            抽签只决定「它打算写对还是写错」，判对判错一律交给 judge()。
            """
            fam = row["family"]
            tid = row["task"]["id"]
            task = row["task"]
            has_rule = any(r["scope"] == fam for r in state.get("rules", []))
            base = 0.30 if fam == "integral" else 0.65
            p = min(0.92, base + (0.42 if has_rule else 0.0))
            h = hashlib.sha256(tid.encode()).digest()
            want_ok = (h[0] / 255.0) < p
            lat = round(0.9 + (h[1] / 255.0) * 1.4, 2)
            time.sleep(lat)

            raw = json.dumps(
                {"value": reference_text(task) if want_ok else wrong_answer_text(task)},
                ensure_ascii=False)
            if _mock_judge is None:
                return {"answer": raw, "correct": want_ok,
                        "reason": None if want_ok else "（无 executor，按抽签计）",
                        "tokens_in": 780 + h[2] % 120,
                        "tokens_out": 190 + h[3] % 90, "latency_s": lat}

            verdict = _mock_judge(task, raw)
            ok = verdict == "right"
            if ok != want_ok:
                print("  ⚠ mock 与 judge 判定不一致：%s 想答%s、判成 %s"
                      % (tid, "对" if want_ok else "错", verdict), file=sys.stderr)
            if ok:
                reason = None
            elif verdict == "unanswered":
                reason = "答案格式未被接受（unanswered）"
            else:
                reason = row.get("mock_wrong_reason") or "答案与期望值不符"
            return {
                "answer": raw,
                "correct": ok,
                "reason": reason,
                "tokens_in": 780 + h[2] % 120,
                "tokens_out": 190 + h[3] % 90,
                "latency_s": lat,
            }
        return solve_one, "mock/offline"

    # ── real：依赖 A 的 judge 与 B 的 complete ──
    try:
        from app.executor import judge as _judge  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"✗ 缺少 A 的执行器：{e}\n"
            "  A 需提供 app/executor.py:\n"
            "      judge(task: dict, raw_answer: str | None) -> str   # right / wrong / unanswered\n"
            "      solver_view(task) -> {'type','statement','answer_format'}   # 可选\n"
            "  缺它无法判分，先用 --backend mock 联调链路。"
        ) from e
    try:
        from app import llm as _llm  # type: ignore
        _complete = _llm.complete
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"✗ 缺少模型客户端：{e}\n"
            "  需要 app/llm.py：\n"
            "      complete(messages: list[dict]) -> (text, tokens_in, tokens_out)\n"
            "  仓库里已有一份按此签名写好的实现，检查文件是否被删。"
        ) from e

    # 配置错一次就够了：不 preflight 的话，10 道题并发会吐出 10 条一样的报错。
    _preflight = getattr(_llm, "preflight", None)
    if _preflight is not None:
        try:
            _preflight()
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"✗ {e}") from e

    try:
        from app.executor import solver_view as _solver_view  # type: ignore
    except Exception:  # noqa: BLE001
        _solver_view = None

    solver_tpl = ((PROMPTS / "solver.md").read_text(encoding="utf-8")
                  if (PROMPTS / "solver.md").exists() else "")

    def solve_one(row: dict, state: dict) -> dict:
        t0 = time.time()
        view = _solver_view(row["task"]) if _solver_view else solver_view_local(row)
        check = build_check_instruction(state)

        if solver_tpl:
            prompt = (solver_tpl
                      .replace("{solve_prompt}", "逐步求解，最后只输出答案。")
                      .replace("{check_instruction}", check)
                      .replace("{statement}", view["statement"]))
        else:
            prompt = (f"{view['statement']}\n\n"
                      f"答案格式：{view['answer_format']}\n"
                      + (f"\n{check}\n" if check else "")
                      + "\n最后一行只写答案，不要解释。")

        raw = _complete([{"role": "user", "content": prompt}])
        if isinstance(raw, tuple):
            text, tin, tout = (list(raw) + [0, 0])[:3]
        else:                                    # 对象式返回也认
            text = getattr(raw, "text", None) or str(raw)
            tin = int(getattr(raw, "tokens_in", 0) or 0)
            tout = int(getattr(raw, "tokens_out", 0) or 0)

        ok, why = _coerce_verdict(_judge(row["task"], text))
        return {"answer": text, "correct": ok, "reason": why,
                "tokens_in": tin, "tokens_out": tout,
                "latency_s": round(time.time() - t0, 2)}

    return solve_one, _guess_model_id()


def _guess_model_id() -> str:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return str(cfg.get("model") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


# ────────────────────────────── 单次运行 ──────────────────────────────
def run_pass(pass_no: int, pair: str, state: dict, bank: list[dict],
             solver: Callable) -> dict:
    print(f"  → pass{pass_no} 开始（{len(bank)} 题，并发 {CONCURRENCY}）", flush=True)
    t0 = time.time()
    per_task: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(solver, row, state): row for row in bank}
        for f in cf.as_completed(futs):
            row = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                r = {"answer": "", "correct": False, "reason": f"调用异常：{e}",
                     "tokens_in": 0, "tokens_out": 0, "latency_s": 0}
            per_task.append({
                "id": row["task"]["id"], "family": row["family"],
                "correct": r["correct"], "answer_chars": len(str(r["answer"])),
                "reason": r.get("reason"),
                "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                "latency_s": r["latency_s"],
            })
    per_task.sort(key=lambda x: x["id"])
    dur = round(time.time() - t0, 1)
    correct = sum(1 for t in per_task if t["correct"])
    print(f"  ✓ pass{pass_no} 完成 {correct}/{len(bank)} 用时 {dur}s", flush=True)
    return {
        "pass": pass_no, "duration_s": dur, "correct": correct, "total": len(bank),
        "accuracy": correct / len(bank), "per_task": per_task,
        "state_version": state["version"],
        "tokens_in": sum(t["tokens_in"] for t in per_task),
        "tokens_out": sum(t["tokens_out"] for t in per_task),
    }


# ────────────────────────────── 双跑 ──────────────────────────────
def run_pair(pair: str, backend: str, out: Path | None = None) -> dict:
    bank = load_bank()
    solver, model_id = make_solver(backend)

    fp_before = frozen_fingerprint()
    d = pair_dir(pair)
    if d.exists():
        shutil.rmtree(d)
    (d / "state").mkdir(parents=True, exist_ok=True)

    # ① 起点快照 —— 回退目标，必须在任何运行之前
    seed = seed_state()
    save_state(pair, seed, "v0_seed")
    save_state(pair, seed, "current")
    print(f"  ✓ 起点快照 {state_path(pair, 'v0_seed')}", flush=True)

    # ② 第一次运行（state = seed）
    p1 = run_pass(1, pair, seed, bank, solver)

    # ③ 学习：pass1 的失败模式 → 新 state（人不参与）
    learned = learn_from(p1, seed)
    save_state(pair, learned, "v1_learned")
    save_state(pair, learned, "current")
    print(f"  ✓ 学习完成：+{len(learned['rules'])} 条规则 "
          f"+{len(learned['memories'])} 条记忆", flush=True)

    # ④ 第二次运行（state = learned）
    p2 = run_pass(2, pair, learned, bank, solver)

    # ⑤ 冻结校验：config 与 prompts 必须一字未变
    fp_after = frozen_fingerprint()
    identical = (fp_before["config_sha256"] == fp_after["config_sha256"]
                 and fp_before["prompts_sha256"] == fp_after["prompts_sha256"])

    delta_pp = round((p2["accuracy"] - p1["accuracy"]) * 100, 1)
    verdict = verdict_of(delta_pp)

    params_changed = []
    for k, v in learned["rule_weight"].items():
        params_changed.append({"key": k, "scope": "state",
                               "before": seed["rule_weight"].get(k, 0.0), "after": v})
    params_changed.append({"key": "verify_depth", "scope": "state",
                           "before": seed["verify_depth"], "after": learned["verify_depth"]})
    try:
        cfg_win = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("fitness_window")
    except Exception:  # noqa: BLE001
        cfg_win = None
    if cfg_win is not None:
        params_changed.append({"key": "fitness_window", "scope": "config",
                               "before": cfg_win, "after": cfg_win})

    failed_fams = sorted({t["family"] for t in p1["per_task"] if not t["correct"]})
    one_line = (
        f"{len(bank)} 道冻结验证题正确率 {p1['correct']}/{len(bank)} → {p2['correct']}/{len(bank)}"
        f"（{'+' if delta_pp >= 0 else ''}{delta_pp:.1f} 个百分点）；"
        f"从 pass1 的 {len(failed_fams)} 个失败族自动生成 {len(learned['rules'])} 条解题校验规则、"
        f"{len(learned['memories'])} 条案例记忆，代码与提示词零改动"
    )

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "pair_id": pair,
        "model_id": model_id,
        "backend": backend,
        "frozen": {
            "config_sha256": fp_after["config_sha256"],
            "prompts_sha256": fp_after["prompts_sha256"],
            "n_prompt_files": fp_after["n_prompt_files"],
            "identical_across_passes": identical,
            "note": "两次之间 config.json 与 prompts/ 的 SHA-256 逐字节一致，改动只落在 state/current.json",
        },
        "metric": {
            "name": "固定题库正确率", "short": "correct rate", "unit": "%",
            "n_tasks": len(bank), "higher_is_better": True,
            "definition": f"{len(bank)} 道冻结验证题（{len(set(r['family'] for r in bank))} 族）中"
                          "判分完全正确的题数占比；判分由 executor 精确计算，不经 LLM 裁判",
        },
        "passes": [p1, p2],
        "learned": {
            "rules_added": learned["rules"],
            "memories_added": learned["memories"],
            "params_changed": params_changed,
            "genes_changed": {"solve_prompt_diff_lines": len(learned["rules"]) * 2,
                              "set_prompt_diff_lines": 0},
        },
        "rollback": {
            "available": state_path(pair, "v0_seed").exists(),
            "snapshot_id": "v0_seed",
            "snapshot_path": str(state_path(pair, "v0_seed").relative_to(ROOT)),
            "command": f"python -m app.demo rollback --pair {pair} --to v0_seed",
            "verified": None,
            "note": "回退后 state/current.json 逐字节恢复为 pass1 之前的版本，规则与记忆全部清空",
        },
        "conclusion": {"delta_accuracy_pp": delta_pp, "verdict": verdict, "one_line": one_line},
    }

    errs = validate(payload)
    if errs:
        print("✗ 输出未通过契约校验：", file=sys.stderr)
        for e in errs:
            print("   -", e, file=sys.stderr)
        raise SystemExit(2)
    if not identical:
        print("✗ 两次之间 config/prompts 变了，本次结果不可用于赛道说明", file=sys.stderr)
        raise SystemExit(3)

    out = out or (WEB / "evolve.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    (d / "evolve.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                   encoding="utf-8")

    print(f"✓ 已写 {out.relative_to(ROOT)}", flush=True)
    print(f"  判定：{verdict}  Δ {delta_pp:+.1f}pp", flush=True)
    return payload


# ────────────────────────────── 回退 ──────────────────────────────
def rollback(pair: str, to: str) -> dict:
    src = state_path(pair, to)
    if not src.exists():
        raise SystemExit(f"✗ 快照不存在：{src}")
    dst = state_path(pair, "current")
    before = dst.read_bytes() if dst.exists() else b""
    shutil.copyfile(src, dst)
    after = dst.read_bytes()
    if after != src.read_bytes():
        raise SystemExit("✗ 回退后校验失败：文件内容与快照不一致")
    st = json.loads(after.decode("utf-8"))
    preview = (f"version={st.get('version')}  rules={len(st.get('rules') or [])}  "
               f"memories={len(st.get('memories') or [])}  "
               f"verify_depth={st.get('verify_depth')}")
    print(f"✓ 已回退 {pair} → {to}")
    print(f"  字节 {len(before)} → {len(after)}，内容与快照一致")
    print(f"  {preview}")
    return {"pair": pair, "snapshot_id": to, "restored_bytes": len(after), "preview": preview}


def status(pair: str) -> None:
    d = pair_dir(pair)
    if not d.exists():
        raise SystemExit(f"✗ 没有这个 pair：{d}")
    print(f"pair {pair}")
    for f in sorted((d / "state").glob("*.json")):
        st = json.loads(f.read_text(encoding="utf-8"))
        print(f"  {f.name:18s} version={st.get('version'):12s} "
              f"rules={len(st.get('rules') or [])} memories={len(st.get('memories') or [])}")
    cur = state_path(pair, "current")
    if cur.exists():
        print(f"  current 指纹 {hashlib.sha256(cur.read_bytes()).hexdigest()[:16]}")


# ────────────────────────────── CLI ──────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="双跑演示编排（日新赛道）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pair = sub.add_parser("pair", help="跑一次完整双跑并导出 evolve.json")
    p_pair.add_argument("--pair", default=time.strftime("demo_pair_%m%d_%H%M"))
    p_pair.add_argument("--backend", choices=("mock", "real"), default="mock")
    p_pair.add_argument("--out", default=None)

    p_rb = sub.add_parser("rollback", help="退回到指定快照")
    p_rb.add_argument("--pair", required=True)
    p_rb.add_argument("--to", default="v0_seed")

    p_st = sub.add_parser("status", help="查看某个 pair 的状态文件")
    p_st.add_argument("--pair", required=True)

    a = ap.parse_args(argv)
    if a.cmd == "pair":
        print(f"══ 双跑开始：{a.pair}（backend={a.backend}）", flush=True)
        run_pair(a.pair, a.backend, Path(a.out) if a.out else None)
        print("══ 完成。刷新 web/evolve.html 查看", flush=True)
        return 0
    if a.cmd == "rollback":
        rollback(a.pair, a.to)
        return 0
    status(a.pair)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
