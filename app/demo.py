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
    SCHEMA, SCHEMA_VERSION, compare_passes, validate, verdict_of,
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
    # 整趟挂掉不许归纳。C 这边的判据是 status=aborted（全题 infra 才置位）；
    # 也认旧的 transport_error 标记，兼容 B 侧历史数据。
    # 注意区分：**部分题**故障仍可学习（下面逐题排除），只有整趟失败才拒绝。
    if (pass1.get("status") == "aborted"
            or any(t.get("transport_error") for t in pass1["per_task"])):
        raise ValueError("调用失败的运行不能生成学习规则")
    fail: dict[str, list[dict]] = {}
    infra_ids: list[str] = []
    for t in pass1["per_task"]:
        if t["correct"]:
            continue
        # 基础设施故障不进学习。2026-11-13 实测：K11 因推理 token 超预算被截断，
        # 学习器把它当成"行列式算法不行"，自动写出一条 Bareiss 主元规则 —— 一条
        # 完全对症不了的假规则，还会被注入下一趟的 prompt。人不会被自己的手滑
        # 教会东西，机器也不该。所以超时/截断/接口报错一律隔离上报，不参与归纳。
        if t.get("infra"):
            infra_ids.append(t["id"])
            continue
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
    # 被排除的故障要留在账上，否则"这趟没学到东西"会被误读成"模型全对了"
    if infra_ids:
        st["infra_excluded"] = sorted(set(st.get("infra_excluded") or []) | set(infra_ids))
        print(f"  ⚠ {len(infra_ids)} 题因基础设施异常被排除在学习之外："
              f"{'、'.join(infra_ids)}（不是能力问题，别据此加规则）", flush=True)
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
def load_bank(path: Path | None = None) -> list[dict]:
    """返回 bank 行：{"family", "answer_format", "mock_wrong_reason", "task"}。

    兼容两种磁盘格式：
      · verify_bank.json —— 每条已包好 `{"task": {...}, "family": ...}`
      · calibration.json / common_dev.json —— 裸 task 数组
    为什么要兼容：verify_bank 是附录 A 的**执行器验收用例**（∫(x+1)dx 一类），
    拿它做能力演示必然 10/10，20 代曲线会是一条毫无信息的直线。演示要用的
    是 calibration 那种有难度梯度的题集，而它是裸 task 格式，直接喂会 KeyError。
    """
    path = path or BANK_FILE
    if not path.exists():
        raise SystemExit(
            f"✗ 找不到验证题库 {path}\n"
            "  先运行 python tests/make_verify_bank.py\n"
            "  题库必须冻结：现场生成 = 换题 = 两次不可比"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["tasks"] if isinstance(payload, dict) else payload

    out: list[dict] = []
    for i, r in enumerate(rows):
        if "task" in r:                       # 已包装格式
            out.append(r)
            continue
        if "statement" not in r:              # 既没包装也不是 task
            raise SystemExit(f"✗ 题库 {path} 第 {i} 条既无 task 也无 statement")
        out.append({
            "family": r.get("family") or r.get("type") or "?",
            "answer_format": (r.get("answer_format")
                              or "integer, fraction or finite decimal"),
            "task": r,
        })
    return out


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


def _assert_statement_visible(system_prompt: str, user_prompt: str, statement: str) -> None:
    """题面必须真的出现在发给模型的消息里，否则立刻停。

    为什么值得一条硬断言：题面丢失时模型照样吐合法 JSON，判分照样返回 wrong，
    整趟跑完只是通过率安静地变成 0 —— 没有任何一个环节会抛错。
    本地能拦住这件事的地方，只有这里。
    """
    if not statement or not statement.strip():
        raise SystemExit("✗ 题面为空，无法作答")
    if statement not in system_prompt and statement not in user_prompt:
        raise SystemExit(
            "✗ 题面没有进入发给模型的消息\n"
            f"  题面: {statement[:60]}\n"
            "  查 prompts/solver.md 的占位符与 demo.py 的消息构造")


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
    # B 的 app/llm.py 是正式实现（LLMClient.chat，异步）；它没有 complete()。
    # C 侧的双跑编排要的是同步三元组，所以先找 B 的，找不到再退回独立实现。
    _llm = None
    try:
        from app import llm as _llm  # type: ignore
        _complete = _llm.complete
    except (ImportError, AttributeError):
        try:
            from app import llm_standalone as _llm  # type: ignore
            _complete = _llm.complete
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"✗ 缺少模型客户端：{e}\n"
                "  需要 app/llm.py（B）或 app/llm_standalone.py（C 的备用实现）：\n"
                "      complete(messages: list[dict]) -> (text, tokens_in, tokens_out)"
            ) from e

    # 预算从 config.json 读，不再吃模块常量。
    # `deepseek-flash` 是推理模型：内容生成前先烧 reasoning_tokens，5 阶行列式
    # 一道题实测 3700+ 推理 token。按 1200 给预算，推理尚未收敛就被砍断，
    # content 为空 → 判成 unanswered，看着像"模型不会做"。
    _budget_fn = getattr(_llm, "budget", None)
    solver_max_tokens = _budget_fn("solver", 1200) if callable(_budget_fn) else 1200

    def _ask(messages: list[dict]):
        """带预算的调用；B 的实现若不接受 max_tokens 就退回裸调用。"""
        try:
            return _complete(messages, max_tokens=solver_max_tokens)
        except TypeError:
            return _complete(messages)

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

        # 两条消息，不是一条：`solver.md` 是 **system** prompt（规格 §3.3），
        # 它的全文只有「求解给定数学题 + 输出格式 + 策略占位符」，从来没有
        # `{statement}` 这个占位符。题面必须走 user 消息。
        #
        # 2026-11-13 22:40 修：原先写成下面这样，`replace` 是静默空操作 ——
        #     prompt = solver_tpl.replace("{solve_prompt}", ...).replace("{statement}", stmt)
        # 模型收到的只有「求解给定数学题」+ 格式要求，**没有题目本身**。
        # 表现是 10 题全错、答案长度全落在 12–17 字符。这类 bug 不会报错，
        # 只会安静地把通过率打成 0，所以下面留了一条硬断言。
        system_prompt = (solver_tpl
                         .replace("{solve_prompt}", "逐步求解，最后只输出答案。")
                         .replace("{check_instruction}", check)).strip()
        user_prompt = f"{view['statement']}\n\n答案格式：{view['answer_format']}"

        _assert_statement_visible(system_prompt, user_prompt, view["statement"])

        raw = _ask([{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}]) if system_prompt \
            else _ask([{"role": "user", "content": user_prompt}])
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


# ────────────────────────────── 归档 ──────────────────────────────
def _atomic_json(path: Path, obj: dict, *, overwrite: bool = False) -> None:
    """tmp + os.replace 落盘。半截文件比没有文件更危险：页面会读到一个残缺的 JSON。"""
    if path.exists() and not overwrite:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _archive_pass(directory: Path, result: dict) -> None:
    """每趟结束把整趟结果归档成 `pass{n}.json`（取自 B 的 `_archive_pass` 语义）。

    为什么整趟也要落盘：`calls_pass{n}.jsonl` 只存逐题记录，复算一遍要重跑模型。
    整趟汇总（含 state_version、token 账）是"这一趟确实跑过"的最小证据。

    失败趟必须立刻停：一趟全挂说明模型根本调不通，此时若继续 learn_from()，
    会把"接口 500"归纳成"某族题不会做"，凭空造出一条规则再拿去测 pass2。
    宁可交白卷并留下 `failure.json`，也不许用半趟数据编出提升。
    """
    _atomic_json(directory / f"pass{result['pass']}.json", result)
    if result.get("status") == "aborted":
        _atomic_json(directory / "failure.json",
                     {"status": "aborted", "pass": result["pass"],
                      "reason": "transport_error"}, overwrite=True)
        raise SystemExit("✗ 模型调用失败，已归档；不生成学习规则或准确率提升结论")


# ────────────────────────────── 单次运行 ──────────────────────────────
def run_pass(pass_no: int, pair: str, state: dict, bank: list[dict],
             solver: Callable) -> dict:
    print(f"  → pass{pass_no} 开始（{len(bank)} 题，并发 {CONCURRENCY}）", flush=True)
    t0 = time.time()
    per_task: list[dict] = []
    calls: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(solver, row, state): row for row in bank}
        for f in cf.as_completed(futs):
            row = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                # infra=True：这是"我们这边出错"，不是"模型不会"。
                # 判分统计里两者都算 0 分，但学习器必须区别对待，
                # 否则会从自己的故障里归纳出假规则。
                r = {"answer": "", "correct": False, "reason": f"调用异常：{e}",
                     "tokens_in": 0, "tokens_out": 0, "latency_s": 0, "infra": True}
            per_task.append({
                "id": row["task"]["id"], "family": row["family"],
                "correct": r["correct"], "answer_chars": len(str(r["answer"])),
                "reason": r.get("reason"), "infra": bool(r.get("infra")),
                "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
                "latency_s": r["latency_s"],
            })
            # 原始回答落盘（runs/ 下，不进 web/）。
            # 没有它就只能看到 answer_chars=12，查不出是「算错」还是「没看懂题」——
            # 22:40 那次 0/10 就是靠这一条才定位到题面丢失。
            calls.append({
                "pass": pass_no, "task_id": row["task"]["id"],
                "family": row["family"], "correct": r["correct"],
                "reason": r.get("reason"), "answer": str(r["answer"])[:2000],
                "latency_s": r["latency_s"],
                "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
            })
    per_task.sort(key=lambda x: x["id"])
    dur = round(time.time() - t0, 1)
    correct = sum(1 for t in per_task if t["correct"])
    print(f"  ✓ pass{pass_no} 完成 {correct}/{len(bank)} 用时 {dur}s", flush=True)

    cdir = pair_dir(pair)
    cdir.mkdir(parents=True, exist_ok=True)
    with (cdir / f"calls_pass{pass_no}.jsonl").open("w", encoding="utf-8") as fh:
        for c in sorted(calls, key=lambda x: x["task_id"]):
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    n_infra = sum(1 for t in per_task if t["infra"])
    # 整趟全挂 = 传输层失败，不是能力结论。这个判定必须在这里做：
    # 上层拿到 aborted 就只能归档退出，没法把它读成"模型只答对 0 题"。
    status = "aborted" if per_task and n_infra == len(per_task) else "ok"
    return {
        "pass": pass_no, "duration_s": dur, "correct": correct, "total": len(bank),
        # aborted 时准确率给 None 而不是 0.0：0% 是"测了，全错"，
        # None 才是"没测成"。把前者渲染出来等于用我们的断网指责模型。
        "accuracy": None if status == "aborted" else correct / len(bank),
        "per_task": per_task,
        "state_version": state["version"], "status": status, "infra": n_infra,
        "tokens_in": sum(t["tokens_in"] for t in per_task),
        "tokens_out": sum(t["tokens_out"] for t in per_task),
    }


# ────────────────────────────── 双跑 ──────────────────────────────
def run_pair(pair: str, backend: str, out: Path | None = None,
             bank_path: Path | None = None) -> dict:
    bank = load_bank(bank_path)
    solver, model_id = make_solver(backend)

    fp_before = frozen_fingerprint()
    d = pair_dir(pair)
    if d.exists():
        # 上一趟的证据不许被一次手滑覆盖掉。要重跑就换个 --pair 名。
        raise SystemExit(f"运行 {pair} 已存在；请换一个 --pair，原始证据已保留")
    (d / "state").mkdir(parents=True, exist_ok=False)

    # ① 起点快照 —— 回退目标，必须在任何运行之前
    seed = seed_state()
    save_state(pair, seed, "v0_seed")
    save_state(pair, seed, "current")
    print(f"  ✓ 起点快照 {state_path(pair, 'v0_seed')}", flush=True)

    # ② 第一次运行（state = seed）
    p1 = run_pass(1, pair, seed, bank, solver)
    _archive_pass(pair_dir(pair), p1)

    # ③ 学习：pass1 的失败模式 → 新 state（人不参与）
    learned = learn_from(p1, seed)
    save_state(pair, learned, "v1_learned")
    save_state(pair, learned, "current")
    print(f"  ✓ 学习完成：+{len(learned['rules'])} 条规则 "
          f"+{len(learned['memories'])} 条记忆", flush=True)

    # ④ 第二次运行（state = learned）
    p2 = run_pass(2, pair, learned, bank, solver)
    _archive_pass(pair_dir(pair), p2)

    # ⑤ 冻结校验：config 与 prompts 必须一字未变
    fp_after = frozen_fingerprint()
    identical = (fp_before["config_sha256"] == fp_after["config_sha256"]
                 and fp_before["prompts_sha256"] == fp_after["prompts_sha256"])

    cmp = compare_passes(p1, p2)
    ok1, n1, ok2, n2 = cmp["ok1"], cmp["n1"], cmp["ok2"], cmp["n2"]
    delta_pp, resolution, verdict = cmp["delta_pp"], cmp["resolution_pp"], cmp["verdict"]
    infra_p1, infra_p2 = cmp["infra_p1"], cmp["infra_p2"]

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

    failed_fams = sorted({t["family"] for t in p1["per_task"]
                          if not t["correct"] and not t.get("infra")})
    one_line = (
        f"{n1} 道冻结验证题能力正确率 {ok1}/{n1} → {ok2}/{n2}"
        f"（{'+' if delta_pp >= 0 else ''}{delta_pp:.1f} 个百分点，"
        f"单题分辨率 {resolution:.1f}pp）；"
        f"从 pass1 的 {len(failed_fams)} 个失败族自动生成 {len(learned['rules'])} 条解题校验规则、"
        f"{len(learned['memories'])} 条案例记忆，代码与提示词零改动"
    )
    if infra_p1 or infra_p2:
        one_line += (f"；另有 {infra_p1 + infra_p2} 次调用属基础设施异常（截断/超时），"
                     f"已排除在正确率与判定之外")

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "pair_id": pair,
        "model_id": model_id,
        "backend": backend,
        # 真假必须由生产者声明，不能靠页面猜。mock 抽签出来的 4/10 → 8/10
        # 和真模型跑出来的长得一模一样，不声明就是骗自己。
        "is_mock": backend != "real",
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
        "conclusion": {
            "delta_accuracy_pp": delta_pp,
            "verdict": verdict,
            "one_line": one_line,
            # 判定可复核所需的三样东西：分母、分辨率、被排除的故障数。
            # 少了它们，-5.0pp 到底是"退步"还是"翻了一道题"没人能看出来。
            "capability_accuracy": {"pass1": [ok1, n1], "pass2": [ok2, n2]},
            "resolution_pp": resolution,
            "infra_excluded": infra_p1 + infra_p2,
        },
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
    if out.resolve() == (WEB / "evolve.json").resolve():
        # 走统一出口：写完自动重建 data.js，file:// 双击才有数据
        from app.webdata import write_json
        write_json("evolve.json", payload, indent=1)
    else:
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


def mark_rollback_verified(res: dict) -> None:
    """把"真的回退过一次"记进 web/evolve.json。

    为什么需要这一步：`rollback.verified` 原来是写死的 `None`，页面也不显示它。
    规格点名要"回退验证"，而一个永远为 null 的字段既不能证明跑过，也不能证明没跑过。
    现在只有真的执行过一次回退，这里才会被写成 True，并留下时间与字节数。
    """
    p = WEB / "evolve.json"
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    rb = d.setdefault("rollback", {})
    rb["verified"] = True
    rb["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rb["restored_bytes"] = res.get("restored_bytes")
    rb["verify_preview"] = res.get("preview")
    from app.webdata import write_json
    write_json("evolve.json", d, indent=1)


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
    p_pair.add_argument("--bank", default=None,
                        help="题集路径，默认 data/verify_bank.json；"
                             "演示建议用 data/calibration.json（有难度梯度）")

    p_rb = sub.add_parser("rollback", help="退回到指定快照")
    p_rb.add_argument("--pair", required=True)
    p_rb.add_argument("--to", default="v0_seed")

    p_st = sub.add_parser("status", help="查看某个 pair 的状态文件")
    p_st.add_argument("--pair", required=True)

    a = ap.parse_args(argv)
    if a.cmd == "pair":
        print(f"══ 双跑开始：{a.pair}（backend={a.backend}）", flush=True)
        run_pair(a.pair, a.backend, Path(a.out) if a.out else None,
                 Path(a.bank) if a.bank else None)
        print("══ 完成。刷新 web/evolve.html 查看", flush=True)
        return 0
    if a.cmd == "rollback":
        rollback(a.pair, a.to)
        return 0
    status(a.pair)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
