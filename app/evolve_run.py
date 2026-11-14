"""20 代演化运行器：产出 runs/<run_id>/history.jsonl，供大屏读真数据。

**为什么单独一个模块**

`demo.py` 是双跑（2 代）对照，只回答一个问题：学习有没有用。
大屏问的是另一个问题：20 代里种群怎么演化、祖先题失败率怎么动。
两者共用 `learn_from` / `build_check_instruction` / `judge`，但节奏不同：
双跑每趟 20 题只要一个总数，演化每代要评整个种群 + 三族冻结祖先题。

**每个数字都必须来自一次真实调用**

大屏最容易造假的地方不是曲线，是"个体分"。如果 6 个体的分只差在随机噪声上，
那叫抽签，不叫演化。所以这里让基因**真的进 prompt**：

    genes.solve_prompt  → system prompt 的 {solve_prompt} 占位符
    genes.self_check    → 是否注入按 state 规则渲染的检查指令

于是"self_check=true 的个体比 false 的高"是跑出来的，不是写出来的。

**每代测什么（全部落 usage / timing）**

    ancestor  3 族冻结题（积分 / 行列式 / 平衡）各 n_ancestor 道，由当代冠军代言
    common    每个个体 n_common 道共同题（同一批，横向可比）
    set       答案可用率（非空、未被 max_tokens 截断）→ 格式自检分

**已知偏差，写在 manifest 里，不藏**

    · 没有出题（setter）角色，`set` 用"答案可用率"占位，非 B 的出题分
    · 没有 peer 互评，`slots.peer = 0`
    · 挑战题取自 data/challenge_pool.json，运行前按 challenge_gens 绑定 ID 与
      注入时点，运行中不换题；代数不足以容纳完整窗口时不挑战，并写进 manifest
    · 初代 6 个体由 3 条种子铺满，只有 3 份不同基因（manifest 注明）

**E/F 对照怎么跑**

    E（演化）：python -m app.evolve_run --run-id E_x --gens 20
    F（冻结）：python -m app.evolve_run --run-id F_x --gens 20 --freeze

    F 不是"少跑"：`--freeze` 只关掉淘汰/交叉/变异，出题、作答、祖先评测全部照常，
    调用数与 E 同口径。否则两条曲线不可比，"对照"就没有意义。

**断点续跑**

    python -m app.evolve_run --run-id E_x --gens 20 --resume

    每代结束原子写 `runs/<run_id>/state.json`（含规则、逐 ID 分数序列、种群）。
    中断后用 `--resume` 从最后一代接着跑，不重跑已完成部分。
    条件（E/F）以快照为准，续跑时不许改。

用法：
    export LLM_API_KEY=...
    python -m app.evolve_run --run-id E_real_20261114
    python app/export.py --run runs/E_real_20261114 --out web/history.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm_standalone as L                      # noqa: E402
from app import challenge as CH                          # noqa: E402
from app.demo import (                                   # noqa: E402
    CONCURRENCY, RUNS, WEB, _coerce_verdict,
    build_check_instruction, frozen_fingerprint, learn_from,
    load_bank, seed_state, solver_view_local,
)
from app.executor import judge as _judge                  # noqa: E402

CONFIG = ROOT / "config.json"
PROMPTS = ROOT / "prompts"

#: 规则条数上限。规则是拼进提示词的文字，累积 20 代会把 prompt 撑爆，
#: 反而挤掉题目。保留最近的 N 条，等于"只记最近还犯的错"。
MAX_RULES_IN_PROMPT = 6

#: 三族祖先题的构成：族名 → 从哪些题型里抽
ANCESTOR_MIX = {
    "integral_failure": ("integral",),
    "det_failure": ("det",),
    "balanced_failure": ("integral", "det"),
}

SEED_GENES = (
    {"solve_prompt": "逐步求解，最后只输出答案。", "self_check": False},
    {"solve_prompt": "先判断题型，再选对应方法；分数一律用精确分数运算，不写近似小数。",
     "self_check": True},
    {"solve_prompt": "先写出解题提纲（几步、每步算什么），再逐步计算，最后一行只写答案。",
     "self_check": True},
)


def load_cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# ────────────────────────────── 题库 ──────────────────────────────
def build_pools(seed: int) -> tuple[dict[str, list[dict]], list[dict]]:
    """切成冻结的祖先族 + 可轮换的共同题池。

    祖先题必须全程冻结 —— 换题就失去"同一把尺子量 20 代"的意义。
    共同题每代换一批，否则模型把题背下来，曲线照样上升但没意义。
    """
    rng = random.Random(seed)
    calib = load_bank(ROOT / "data" / "calibration.json")
    common_pool = load_bank(ROOT / "data" / "common_dev.json")

    by_type: dict[str, list[dict]] = {}
    for row in calib:
        by_type.setdefault(row["family"], []).append(row)

    ancestors: dict[str, list[dict]] = {}
    used: set[str] = set()
    for fam, types in ANCESTOR_MIX.items():
        picked: list[dict] = []
        for t in types:
            cands = [r for r in by_type.get(t, []) if r["task"]["id"] not in used]
            rng.shuffle(cands)
            need = 3 if len(types) == 1 else 2
            for r in cands[:need]:
                picked.append(r)
                used.add(r["task"]["id"])
        ancestors[fam] = picked

    rng.shuffle(common_pool)
    return ancestors, common_pool


# ────────────────────────────── 单次作答 ──────────────────────────────
def _template() -> str:
    p = PROMPTS / "solver.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def make_solver(max_tokens: int):
    tpl = _template()

    def solve(row: dict, genes: dict, check: str) -> dict:
        t0 = time.time()
        view = solver_view_local(row)
        system = (tpl.replace("{solve_prompt}", genes["solve_prompt"])
                  .replace("{check_instruction}", check if genes.get("self_check") else "")
                  ).strip()
        user = f"{view['statement']}\n\n答案格式：{view['answer_format']}"
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": user}]
        try:
            text, tin, tout = L.complete(msgs, max_tokens=max_tokens)
            ok, why = _coerce_verdict(_judge(row["task"], text))
            usable = bool((text or "").strip())
            # 预算被推理吃光 = 我们给的额度不够，不是模型不会。2026-11-13 双跑里
            # 这个区别没分清，学习器从"截断"里归纳出了假规则。这里同一口径：
            # 空答案且输出顶到上限 → 记 infra，不进能力分、不进学习。
            truncated = (not usable) and tout >= max_tokens - 50
            return {"correct": ok, "reason": why or ("输出被 max_tokens 截断" if truncated else None),
                    "usable": usable, "infra": truncated,
                    "tokens_in": tin, "tokens_out": tout,
                    "latency_s": round(time.time() - t0, 2)}
        except Exception as exc:  # noqa: BLE001
            # 基建故障不算能力，但也不能悄悄丢掉：记 infra，分数不计入能力分
            return {"correct": False, "reason": f"调用异常：{exc}", "usable": False,
                    "infra": True, "tokens_in": 0, "tokens_out": 0,
                    "latency_s": round(time.time() - t0, 2)}
    return solve


def run_batch(jobs: list[tuple], solver, workers: int) -> list[dict]:
    """jobs = [(row, genes, check), ...]，并发执行，返回同序结果。"""
    out: list[dict] = [None] * len(jobs)  # type: ignore[list-item]
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(solver, row, genes, check): i
                for i, (row, genes, check) in enumerate(jobs)}
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
    return out


# ────────────────────────────── 演化算子 ──────────────────────────────
def crossover(a: dict, b: dict, gid: str, gen: int) -> dict:
    """交叉：解题策略取前半句 + 后半句，自检开关取或。

    参数是**个体**（含 genes / meta），不是 genes —— 这里踩过一次坑：
    传 genes 进来会在 a["solve_prompt"] 上 KeyError，而崩的是第 0 代的
    演化步，表现为"跑了几分钟一行没写"。算子必须和个体结构对齐。

    刻意做成"句子级"而不是"字符级"：字符级交叉会产出语法破碎的 prompt，
    而 prompt 是给模型读的，破碎即等于关掉这项能力 —— 那不是交叉，是噪音。
    """
    ga, gb = a["genes"], b["genes"]
    pa = [s for s in ga["solve_prompt"].replace("；", "。").split("。") if s.strip()]
    pb = [s for s in gb["solve_prompt"].replace("；", "。").split("。") if s.strip()]
    head = pa[0] if pa else ""
    tail = pb[-1] if pb else ""
    text = (head + "。" + tail + "。") if tail and tail != head else (head + "。")
    return {"genes": {"solve_prompt": text.strip()[:200],
                      "self_check": bool(ga.get("self_check") or gb.get("self_check"))},
            "meta": {"id": gid, "parents": [a.get("_id"), b.get("_id")],
                     "origin": "crossover", "born_gen": gen + 1}}


def mutate(p: dict, gid: str, gen: int, rule_text: str | None) -> dict:
    """变异：把一条真实学到的规则写进解题策略，或翻转自检开关。"""
    genes = dict(p["genes"])
    if rule_text:
        genes["solve_prompt"] = (genes["solve_prompt"].rstrip("。") + "。" +
                                 rule_text.split("：")[-1].strip("。") + "。")[:200]
    else:
        genes["self_check"] = not genes.get("self_check")
    return {"genes": genes,
            "meta": {"id": gid, "parents": [p.get("_id")], "origin": "mutation",
                     "born_gen": gen + 1}}


def next_population(pop: list[dict], ranking: list[str], state: dict,
                    gen: int, gained: int, cfg: dict, *, freeze: bool = False) -> list[dict]:
    """生成下一代。`freeze=True` 时基因原样返回 —— 这是 F 条件的全部实现。

    **F 不是「少跑」，是「同样跑、只关掉选择」**

    技术规格 §7 与大纲 §13 对 F 的定义是「gene 全程不变，依然产生题目并做全部评测」。
    所以 F 必须照样出题、照样答题、照样记祖先失败率，调用数一点不少；
    差别只有一个：不淘汰、不交叉、不变异。否则 E/F 的调用次数不同，
    两条曲线不可比，F 也就失去了「对照」的意义。

    ID 在 F 条件下保持不变，因为同一个体要被反复测量 —— `totals_hist` 按 ID 累积，
    `fitness_window` 才有东西可取。若给 F 每代换新 ID，等于每代清零重测，
    那就是「同基因不同样本」，不是「同一基因的历史」。
    """
    n_pop = int(cfg.get("n_pop", 6))
    n_survive = int(cfg.get("n_survive", 3))
    n_cross = int(cfg.get("n_crossover", 2))
    n_mut = int(cfg.get("n_mutate", 1))

    if freeze:
        return list(pop)

    survivors = [p for p in pop if p["_id"] in ranking[:n_survive]]
    nxt = list(survivors)
    for j in range(n_cross):
        a, b = random.sample(survivors, 2)
        nxt.append(crossover(a, b, f"G{gen:02d}X{j + 1}", gen))
    rule_text = (state["rules"][-1]["text"] if gained and state.get("rules") else None)
    for j in range(n_mut):
        nxt.append(mutate(survivors[j % len(survivors)],
                          f"G{gen:02d}M{j + 1}", gen, rule_text))
    nxt = nxt[:n_pop]
    for p in nxt:
        p.setdefault("_id", p["meta"]["id"])
    return nxt


def load_resume(run_dir: Path) -> dict | None:
    """读断点状态。没有 `state.json` 返回 None（调用方按全新运行处理）。

    为什么单独存 `state.json` 而不从 `history.jsonl` 反推：
    history 里没有 `state.rules`（学习到的规则）和逐 ID 的 `totals_hist`
    （跨代分数序列）。少了前者，续跑段的 `check_instruction` 会突然变空；
    少了后者，`fitness_window` 的均值会从零重算，第 1 代就把降噪关了。
    两者都会让「续跑段」和「从头跑」不是同一个实验，所以必须单独快照。
    """
    state_path = run_dir / "state.json"
    hist_path = run_dir / "history.jsonl"
    if not state_path.exists() or not hist_path.exists():
        return None
    snap = json.loads(state_path.read_text(encoding="utf-8"))
    lines = [json.loads(x) for x in hist_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    pop = [{"genes": p["genes"], "meta": p["meta"], "_id": p["meta"]["id"]}
           for p in snap["population"]]
    return {
        "next_gen": int(snap["next_gen"]),
        "population": pop,
        "totals_hist": {k: list(v) for k, v in snap["totals_hist"].items()},
        "state": snap["state"],
        "lines": lines,
        "freeze": bool(snap.get("freeze", False)),
    }


def save_state(run_dir: Path, *, next_gen: int, population: list[dict],
               totals_hist: dict[str, list[float]], state: dict, freeze: bool) -> None:
    """原子写：先写临时文件再 replace，避免半写状态被下次 resume 读到。"""
    payload = {
        "next_gen": next_gen,
        "population": [{"genes": p["genes"], "meta": p["meta"]} for p in population],
        "totals_hist": totals_hist,
        "state": state,
        "freeze": freeze,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    tmp = run_dir / "state.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, run_dir / "state.json")


# ────────────────────────────── 主循环 ──────────────────────────────
def run(run_id: str, gens: int | None = None, seed: int | None = None,
        *, freeze: bool = False, resume: bool = False) -> dict:
    cfg = load_cfg()
    n_gen = int(gens if gens is not None else cfg.get("n_gen", 20))
    n_pop = int(cfg.get("n_pop", 6))
    n_common = int(cfg.get("n_common", 4))
    # n_survive / n_crossover / n_mutate 只在 next_population() 里读，
    # 主循环不再持有这三个值 —— 免得出现"两处各自读 config、改一处忘另一处"。
    window = int(cfg.get("fitness_window", 2))
    set_weight = float(cfg.get("set_weight", 2.0))
    bounty_v = float(cfg.get("bounty", 2.0))
    workers = int(cfg.get("concurrency", CONCURRENCY))
    max_tok = L.budget("solver", 4000)
    data_seed = int(seed if seed is not None else cfg.get("data_seed", 1001))

    # ── 挑战题：运行前把题和时点定死 ────────────────────────────────
    # 规格：「正式运行前固定两道题的 ID、题面、参考答案与注入时点」，
    # 且「题目不能随运行结果替换」。所以这里只取题库前 N 道，按 born_gen
    # 顺序绑定，全过程不再重选 —— 即便某道题第一代就被攻克也不换。
    chal_window = int(cfg.get("challenge_window", 6))
    chal_threshold = int(cfg.get("threshold_correct", 3))
    chal_gens = [int(g) for g in (cfg.get("challenge_gens") or [])]
    challenge_pool: list[dict] = []
    if chal_gens and chal_gens[-1] + chal_window - 1 > n_gen:
        # 缩短跑（pilot/冒烟）容不下完整窗口。这里选择「本次不挑战」而不是
        # 「把窗口截短」：截短后最后一个窗口观察不满，退役原因永远走不到
        # window_closed，那批题的统计口径就废了。
        print(f"⚠ n_gen={n_gen} 容不下 born_gen={chal_gens[-1]}+window={chal_window}，"
              f"本次不注入挑战（属已声明偏差，会写进 manifest）", flush=True)
        chal_gens = []
    if chal_gens:
        CH.validate_schedule({**cfg, "n_gen": n_gen})
        pool_all = json.loads((ROOT / "data" / "challenge_pool.json")
                              .read_text(encoding="utf-8"))
        if len(pool_all) < len(chal_gens):
            raise SystemExit(f"✗ challenge_pool 只有 {len(pool_all)} 道，"
                             f"不够 {len(chal_gens)} 个窗口")
        challenge_pool = pool_all[:len(chal_gens)]
    eff_cfg = {**cfg, "challenge_gens": chal_gens, "n_gen": n_gen}

    ancestors, common_pool = build_pools(data_seed)
    solver = make_solver(max_tok)

    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    hist_path = run_dir / "history.jsonl"

    state = seed_state()
    # 挑战状态挂在 state 上，跟着 state.json 一起快照与恢复。
    # 不另开文件：恢复时状态机与规则/分数序列必须来自同一份快照，
    # 分两个文件写就会有一边成功一边失败的中间态。
    state.setdefault("challenges", [])
    fp = frozen_fingerprint()
    model_id = L.preflight()["model"]

    # ── 起始状态：全新运行 or 断点续跑 ──────────────────────────────
    resumed = load_resume(run_dir) if resume else None
    if resume and resumed is None:
        raise SystemExit(f"✗ {run_dir} 没有可恢复的 state.json，不能 --resume")

    if resumed:
        start_gen = resumed["next_gen"]
        pop = resumed["population"]
        totals_hist = resumed["totals_hist"]
        state = resumed["state"]
        lines = resumed["lines"]
        # 条件以快照为准：续跑不许悄悄把 E 变成 F，否则同一 run_id 下混了两种实验
        if freeze != resumed["freeze"]:
            print(f"⚠ 快照 freeze={resumed['freeze']}，本次传 {freeze}，以快照为准", flush=True)
        freeze = resumed["freeze"]
        print(f"↻ 从 g{start_gen} 续跑（已有 {len(lines)} 代，freeze={freeze}）", flush=True)
    else:
        start_gen = 0
        hist_path.write_text("", encoding="utf-8")
        pop = []
        for i in range(n_pop):
            genes = dict(SEED_GENES[i % len(SEED_GENES)])
            pop.append({"genes": genes,
                        "meta": {"id": f"G{i + 1:02d}", "parents": [], "origin": "seed",
                                 "born_gen": 0},
                        "_id": f"G{i + 1:02d}"})
        totals_hist = {p["_id"]: [] for p in pop}
        lines = []

    champion = pop[0]
    challenges: list[dict] = list(state.get("challenges") or [])
    chal_txt = "、".join(f"{c.get('id')}@g{g}"
                        for c, g in zip(challenge_pool, chal_gens)) or "无"
    print(f"→ 运行 {run_id}：{n_gen} 代 × 每代 {n_pop} 个体，model={model_id}，"
          f"预算 {max_tok} tok，并发 {workers}，freeze={freeze}，挑战 {chal_txt}",
          flush=True)

    for gen in range(start_gen, n_gen + 1):
        t_gen = time.time()
        check = build_check_instruction(state)

        # ── 1. 每个个体在共同的 n_common 题上作答（横向可比）
        slice_i = (gen * n_common) % max(1, len(common_pool) - n_common)
        common = common_pool[slice_i: slice_i + n_common]
        jobs = [(row, p["genes"], check) for p in pop for row in common]
        res = run_batch(jobs, solver, workers)

        inds = []
        usage = {"call_count": 0, "input_tokens": 0, "output_tokens": 0,
                 "reasoning_tokens": 0, "cost": None}
        infra_n = 0

        # ── 1.5 挑战题：活跃期全员必答，固定 n_pop 个槽位
        # 位置很关键：必须在算个体分数之前。赏金要进本代 total，才能对本代排序
        # 施加选择压力；放到排序之后结算，赏金就退化成「只写进记录」的一个数字，
        # 与大纲「挑战赏金产生选择压力」直接相违。
        bounty_by_id = {p["_id"]: 0.0 for p in pop}
        chal_note: list[dict] = []
        for c in CH.plan(eff_cfg, challenges, gen, pool=challenge_pool):
            challenges.append(c)
        active_chals = [c for c in challenges
                        if c.get("state") == "active" and c.get("template")]
        for c in active_chals:
            cres = run_batch([({"task": c, "family": "challenge"}, p["genes"], check)
                              for p in pop], solver, workers)
            results = []
            infra_c = 0
            for p, r in zip(pop, cres):
                usage["call_count"] += 1
                usage["input_tokens"] += r["tokens_in"]
                usage["output_tokens"] += r["tokens_out"]
                if r["infra"]:
                    infra_c += 1
                    infra_n += 1
                else:
                    results.append({"id": p["_id"], "correct": 1 if r["correct"] else 0})
                    if r["correct"]:
                        bounty_by_id[p["_id"]] += bounty_v
            # 只有整代全员都拿到有效结果才提交观测。缺一个人就会把 p_t 和
            # attempt 算错，而 attempt 直接决定窗口何时关闭 —— 这个数一旦错，
            # 退役时点跟着错，且事后无法从 history 里纠正。
            if infra_c == 0:
                upd = CH.observe(c, gen, results, threshold=chal_threshold)
                challenges = [upd if x.get("task_id") == c["task_id"] else x
                              for x in challenges]
                chal_note.append({"task_id": c["task_id"], "gen": gen, "submitted": True,
                                  "correct": sum(r["correct"] for r in results),
                                  "state": upd["state"], "reason": upd["retired_reason"]})
            else:
                chal_note.append({"task_id": c["task_id"], "gen": gen,
                                  "submitted": False, "infra": infra_c})

        for k, p in enumerate(pop):
            chunk = res[k * n_common:(k + 1) * n_common]
            cap = [r for r in chunk if not r["infra"]]
            acc = (sum(1 for r in cap if r["correct"]) / len(cap)) if cap else 0.0
            usable = (sum(1 for r in cap if r["usable"]) / len(cap)) if cap else 0.0
            infra_n += sum(1 for r in chunk if r["infra"])
            solve_score = round(5.0 * acc, 2)
            set_score = round(set_weight * usable, 2)
            bon = round(bounty_by_id.get(p["_id"], 0.0), 2)
            total = round(solve_score + set_score + bon, 2)
            totals_hist[p["_id"]].append(total)
            k_samples = min(window, len(totals_hist[p["_id"]]))
            est = round(sum(totals_hist[p["_id"]][-k_samples:]) / k_samples, 3)
            inds.append({"id": p["_id"], "solve": solve_score, "set": set_score,
                         "bounty": bon, "total": total, "est": est,
                         "n_samples": max(1, k_samples)})
            for r in chunk:
                usage["call_count"] += 1
                usage["input_tokens"] += r["tokens_in"]
                usage["output_tokens"] += r["tokens_out"]

        ranking = [i["id"] for i in sorted(inds, key=lambda x: -x["est"])]
        champion = next(p for p in pop if p["_id"] == ranking[0])

        # ── 2. 三族冻结祖先题，由当代冠军代言
        anc_jobs, anc_keys = [], []
        for fam, rows in ancestors.items():
            for row in rows:
                anc_jobs.append((row, champion["genes"], check))
                anc_keys.append(fam)
        anc_res = run_batch(anc_jobs, solver, workers)
        ancestor: dict[str, float] = {}
        for r, fam in zip(anc_res, anc_keys):
            usage["call_count"] += 1
            usage["input_tokens"] += r["tokens_in"]
            usage["output_tokens"] += r["tokens_out"]
            if r["infra"]:
                infra_n += 1
                continue
            d = ancestor.setdefault(fam, [0, 0])
            d[1] += 1
            d[0] += 0 if r["correct"] else 1
        ancestor_out = {fam: round(v[0] / v[1], 6) if v[1] else None
                        for fam, v in ((f, ancestor.get(f, [0, 0])) for f in ancestors)}

        # ── 3. 学习：用本代的失败归纳规则（真实失败才归纳）
        per_task = []
        for (row, _g, _c), r in zip(jobs, res):
            per_task.append({"id": row["task"]["id"], "family": row["family"],
                             "correct": r["correct"], "infra": r["infra"],
                             "reason": r.get("reason")})
        before_rules = len(state.get("rules") or [])
        state = learn_from({"per_task": per_task}, state)
        if len(state["rules"]) > MAX_RULES_IN_PROMPT:
            keep = state["rules"][-MAX_RULES_IN_PROMPT:]
            state["rules"] = keep
        gained = len(state["rules"]) - before_rules

        wall_ms = int((time.time() - t_gen) * 1000)
        rec = {
            "schema_version": "5.1", "run_id": run_id,
            "condition": "F" if freeze else cfg.get("condition", "E"),
            "gen": gen, "status": "completed", "M": n_pop, "refetch_round": 0,
            "task_counts": {"integral": sum(1 for r in common if r["family"] == "integral"),
                            "det": sum(1 for r in common if r["family"] == "det")},
            "slots": {"peer": 0, "common": n_common * n_pop,
                      "ancestor": len(anc_jobs),
                      "challenge": len(active_chals) * n_pop},
            "ancestor": ancestor_out,
            "individuals": inds,
            "ranking": ranking,
            "evaluated_population": [{"genes": p["genes"], "meta": p["meta"]} for p in pop],
            "next_population": None,
            "challenges": copy.deepcopy(challenges),
            "timing": {"wall_ms": wall_ms,
                       "role_ms": {"common": wall_ms, "ancestor": 0}},
            "usage": usage,
            "warnings": [
                *([f"挑战 {', '.join(c['task_id'] for c in active_chals)} 本代全员必答，"
                   f"答对赏金 +{bounty_v:g}，赏金计入本代 total 并参与排序"]
                  if active_chals else []),
                *([f"本代挑战观测未提交："
                   f"{sum(1 for x in chal_note if not x.get('submitted'))} 道题存在基础设施异常，"
                   f"attempt 不推进"] if any(not x.get("submitted") for x in chal_note) else []),
                *(["本次未注入挑战题（challenge_gens 为空，或代数不足以容纳完整窗口），"
                   "slots.challenge=0 —— 属已声明偏差，不是运行故障"] if not chal_gens else []),
                *([f"{infra_n} 次调用属基础设施异常，已排除在能力分之外"] if infra_n else []),
            ],
        }

        # ── 4. 下一代：精英保留 + 交叉 + 变异（F 条件下原样返回）
        if gen < n_gen:
            nxt = next_population(pop, ranking, state, gen, gained, cfg, freeze=freeze)
            for p in nxt:
                p.setdefault("_id", p["meta"]["id"])
                # 新个体从零开始记分。**不能**继承父代的成绩历史：
                # 那会让 est 变成"世袭均分"，页面上的分就不再是这一个体自己考的。
                # 首代 n_samples=1，est 就是本代实测分，口径干净。
                # F 条件下 ID 不变，这里命中已有键 —— 历史照常累积，正是要的。
                totals_hist.setdefault(p["_id"], [])
            rec["next_population"] = [{"genes": p["genes"], "meta": p["meta"]} for p in nxt]
            pop = nxt

        with hist_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lines.append(rec)
        # 挑战状态跟着 state 一起快照。恢复时用它判断「本代是否已注入过」，
        # 这是 idempotent 注入的唯一依据 —— 内存标记在 --resume 之后不存在。
        state["challenges"] = challenges
        # 每代落一次快照，供 --resume 从断点续跑（原子写，不会读到半截文件）
        save_state(run_dir, next_gen=gen + 1, population=pop,
                   totals_hist=totals_hist, state=state, freeze=freeze)

        anc_txt = " ".join(f"{k.split('_')[0]}={v:.2f}" if v is not None else f"{k}=?"
                           for k, v in ancestor_out.items())
        bon_txt = ""
        if active_chals:
            seg = "  ".join(
                f"{x['task_id']} {x.get('correct', '?')}/{n_pop}"
                + (f"→{x['reason']}" if x.get("state") == "retired" else "")
                for x in chal_note)
            bon_txt = f" | 挑战 {seg}"
        print(f"  g{gen:<2} 冠军 {ranking[0]} total={max(i['total'] for i in inds):.2f} "
              f"| 祖先失败率 {anc_txt} | 规则 {len(state['rules'])} 条"
              f"{bon_txt} | {wall_ms}ms",
              flush=True)

    manifest = {
        "run_id": run_id,
        "backend": "real",
        "is_mock": False,
        "model_id": model_id,
        # 锁定项必须显式写进 manifest，不能只留 config 的哈希：
        # 哈希能证明"配置不同"，但读不出"不同在哪"，评审时无法核对锁了什么。
        "condition": "F" if freeze else cfg.get("condition", "E"),
        "freeze": freeze,
        "fitness_window": window,
        "data_seed": data_seed,
        "n_gen": n_gen,
        "n_pop": n_pop,
        "resumed_from_gen": start_gen,
        "config_sha256": fp["config_sha256"],
        "prompts_sha256": fp["prompts_sha256"],
        "max_tokens_solver": max_tok,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # ── 挑战协议：ID 与注入时点运行前定死，运行中不可换题 ──────────
        # 只写 id/template/signature 与窗口参数，**不写 statement 与 reference**。
        # manifest 会进仓库，第 5 节的导出白名单也可能带它；题面一旦进去，
        # 等于把挑战题泄给了解题方，这一轮挑战就作废了。
        "challenge_gens": chal_gens,
        "challenge_window": chal_window,
        "threshold_correct": chal_threshold,
        "challenge_bounty": bounty_v,
        "challenge_tasks": [
            {"task_id": c.get("id"), "template": c.get("template"),
             "signature": c.get("signature"), "born_gen": g, "window": chal_window}
            for c, g in zip(challenge_pool, chal_gens)
        ],
        "challenge_summary": CH.summary(challenges),
        "notes": [
            "set 分 = 答案可用率 × set_weight，本运行器无出题（setter）角色",
            "ancestor 由当代冠军代言，非全员评估",
            "学习输入只取本代共同题的失败；祖先题是量尺，不参与训练，否则等于用考卷当练习册",
            *(["挑战题已注入，见 challenge_tasks。赏金计入本代 total 并参与排序；"
               "挑战题不进共同卷均值、不进祖先尺、不进学习输入"]
              if challenge_pool else
              ["挑战题未注入（challenge_gens 为空，或代数不足以容纳完整窗口），"
               "slots.challenge=0 —— 属已声明偏差，不是运行故障"]),
            f"规则最多保留 {MAX_RULES_IN_PROMPT} 条以控制提示词长度",
            f"初代种群由 {len(SEED_GENES)} 条种子按 i%{len(SEED_GENES)} 铺满 {n_pop} 个个体，"
            f"因此 {n_pop} 个个体中只有 {min(len(SEED_GENES), n_pop)} 份不同基因",
            *(["F 条件：基因全程冻结，不淘汰/不交叉/不变异；出题与评测照常，调用数与 E 同口径"]
              if freeze else []),
            *([f"本次从 g{start_gen} 断点续跑，前 {start_gen} 代来自同目录已有 history"]
              if start_gen else []),
        ],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    calls = sum(r["usage"]["call_count"] for r in lines)
    try:
        shown = hist_path.relative_to(ROOT)
    except ValueError:          # RUNS 被指到仓库外（测试、临时目录）
        shown = hist_path
    print(f"✓ 完成 {len(lines)} 代 → {shown}（{calls} 次调用）", flush=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="20 代演化（真跑，产 history.jsonl）")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--gens", type=int, default=None, help="默认读 config.n_gen")
    ap.add_argument("--seed", type=int, default=None, help="题库抽样种子")
    ap.add_argument("--freeze", action="store_true",
                    help="F 条件：基因全程冻结，不淘汰/不交叉/不变异，出题与评测照常")
    ap.add_argument("--resume", action="store_true",
                    help="从 runs/<run-id>/state.json 断点续跑，不重跑已完成的代")
    ap.add_argument("--export", action="store_true", help="跑完顺手导出到 web/")
    a = ap.parse_args(argv)

    if not os.environ.get("LLM_API_KEY"):
        print("✗ 没有 LLM_API_KEY（config.api_key_env 指向它）", file=sys.stderr)
        return 2
    run(a.run_id, a.gens, a.seed, freeze=a.freeze, resume=a.resume)

    if a.export:
        from app.export import main as export_main
        return export_main(["--run", str(RUNS / a.run_id),
                            "--out", str(WEB / "history.json")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
