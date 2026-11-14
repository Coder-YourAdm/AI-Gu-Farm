"""B CLI: dry-run, calibration, pilot, frozen E/F runs and group evaluation."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    MissingConfigError,
    call_plan,
    per_gen_calls,
)
from .orchestrator import BrokenReference


def _fmt_money(cfg: Config) -> str:
    if cfg.budget_amount is None:
        return "未知；当前使用时间/调用上限，不能承诺货币费用上限"
    cur = cfg.budget_currency or "（币种未设置）"
    return f"{cfg.budget_amount} {cur}（缺少可靠定价，真实运行将拒绝）"


def _dry_run(cfg: Config, args: argparse.Namespace) -> int:
    plan = call_plan(cfg)

    setter_per_gen = cfg.n_pop
    solver_per_gen = per_gen_calls(cfg) - setter_per_gen
    operator_total = plan["operator_calls"]

    lines: list[str] = []
    add = lines.append

    add("=" * 68)
    add("dry-run｜不发起任何网络请求，只按公式算调用数")
    add("=" * 68)
    add("")
    add("【配置来源】")
    add(f"  config       : {cfg.source_path or '（内存构造）'}")
    add(f"  schema       : {cfg.schema_version}")
    add(f"  condition    : {cfg.condition}")
    add(f"  model        : {cfg.model}")
    add(f"  base_url     : {cfg.base_url}")
    add(f"  seed/data    : {cfg.seed} / {cfg.data_seed}")
    add(f"  配置指纹      : {cfg.semantic_fingerprint()[:16]}…")
    if "REPLACE_WITH" in cfg.model or "REPLACE_WITH" in cfg.base_url:
        add("  ⚠ model/base_url 仍是占位符：dry-run 允许，真实运行会被拒绝")
    add("")

    add("【结构参数】")
    add(f"  种群 n_pop={cfg.n_pop}｜代数 n_gen={cfg.n_gen}｜共同卷 n_common={cfg.n_common}"
        f"｜祖先 n_ancestor={cfg.n_ancestor}")
    add(f"  留 3 淘 3：survive={cfg.n_survive} crossover={cfg.n_crossover}"
        f" mutate={cfg.n_mutate}")
    add(f"  挑战：born={cfg.challenge_gens} window={cfg.challenge_window}"
        f" threshold={cfg.threshold_correct}")
    add(f"  降噪窗口 fitness_window={cfg.fitness_window}"
        f"｜出题分权重 set_weight={cfg.set_weight}｜赏金 bounty={cfg.bounty}")
    add("")

    add("【每代调用结构】")
    add(f"  出题 setter        : {setter_per_gen:>5}")
    add(f"  互答 solver        : {cfg.n_pop * (cfg.n_pop - 1):>5}")
    add(f"  共同卷 solver      : {cfg.n_pop * cfg.n_common:>5}"
        f"   （{cfg.n_pop} 人 × {cfg.n_common} 题）")
    add(f"  祖先 solver        : {cfg.n_ancestor * cfg.n_pop:>5}"
        f"   （{cfg.n_ancestor} 祖先 × {cfg.n_pop} 题）")
    add(f"  小计 per_gen       : {plan['per_gen']:>5}"
        f"   = setter {setter_per_gen} + solver {solver_per_gen}")
    add(f"  挑战活跃代额外     : {cfg.n_pop:>5}"
        f"   （每代 +{cfg.n_pop}，不产生个体）")
    add("")

    add("【整趟四栏】")
    add(f"  E（完整演化）      : {plan['E']:>5}")
    add(f"  F（冻结对照）      : {plan['F']:>5}")
    add(f"  A（出题分权重 0）  : {plan['A']:>5}"
        f"   （与 E 同口径；当前 set_weight={cfg.set_weight}）")
    add(f"  refetch_reserve    : {plan['refetch_reserve']:>5}"
        f"   （失败才发生，单列，不并入以上）")
    add("")

    add("【分项说明】")
    add(f"  算子调用           : {operator_total:>5}"
        f"   （(n_gen-1) × (crossover+mutate) = {cfg.n_gen - 1} × "
        f"{cfg.n_crossover + cfg.n_mutate}；末代不产生子代）")
    add(f"  挑战槽位合计       : {plan['challenge_slots']:>5}"
        f"   （满窗口上界，按剩余代数封顶）")
    add(f"  留出检查点         : {plan['n_checkpoints']:>5}"
        f"   {cfg.checkpoints}")
    add(f"  留出调用/趟        : {plan['holdout_slots']:>5}"
        f"   = {plan['n_checkpoints']} 检查点 × {cfg.n_pop} 人 × "
        f"{plan['n_holdout']} 题 = {plan['n_checkpoints'] * cfg.n_pop * plan['n_holdout']}")
    add("")

    add("【三个条件组的角色分解】")
    add(f"  {'条件':<6}{'setter':>8}{'solver':>9}{'operator':>10}{'合计':>8}")
    for cond in ("E", "F", "A"):
        total = plan[cond]
        if cond == "F":
            op = 0
        else:
            op = operator_total
        setter = cfg.n_gen * setter_per_gen
        solver = total - setter - op
        add(f"  {cond:<6}{setter:>8}{solver:>9}{op:>10}{total:>8}")
    add("")

    if args.all_seeds:
        runs = 3
        add("【全量（E/F 各 3 趟）】")
        add(f"  E×3 + F×3          : {(plan['E'] + plan['F']) * runs:>5}"
            f"   不含 A 组与重试")
        add("")

    add("【成本与时间】")
    add(f"  模型单价           : 未设置（dry-run 不猜价格；正式前填入并注明来源日期）")
    add(f"  费用上限           : {_fmt_money(cfg)}")
    add(f"  时间上限           : {cfg.wall_limit_s if cfg.wall_limit_s is not None else '未设置'}")
    add(f"  HTTP 尝试上限      : {cfg.max_calls if cfg.max_calls is not None else '未设置'}（含网络重试）")
    add(f"  并发               : {cfg.concurrency}")
    add(f"  网络重试           : {cfg.network_retries} 次/请求（超时、连接错误、408/429、可重试 5xx）")
    add(f"  出题内容尝试       : 每人 {cfg.task_attempts} 次 × 最多 {cfg.refetch_rounds + 1} 轮"
        f" = 每人最多 {cfg.task_attempts * (cfg.refetch_rounds + 1)} 次")
    add("")
    add("校准（6 策略 × 20 题 × 3 次 = 360 次）与全部重试均未计入以上任一行。")
    add("以上为无重试理论值；实际调用与费用在账单表中独立报告。")
    add("")

    print("\n".join(lines))
    return 0


def _not_implemented(name: str) -> int:
    print(f"[未实现] {name}：B2 阶段只交付 --dry-run。"
          f"该子命令将在后续步骤（B6/B7/B8）实现，此处不返回假成功。",
          file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# 跑代（--run / --resume）
# --------------------------------------------------------------------------

#: 数据集模块里可接受的装载函数名（按顺序试第一个存在的）。
#: 契约写在 `orchestrator.Dataset` 上：需要 `common_dev(gen)` / `challenge_pool()`
#: / `signatures()` 三个方法；装载函数接收 `data_dir`。
_DATASET_LOADERS = ("load_dataset", "make_dataset", "build_dataset")


def _load_dataset(args: argparse.Namespace):
    """`--dataset-module`（默认 `app.datasets`）→ Dataset 实例。

    B 不自己造数据集：共同卷与挑战池是 C 的交付物。这里只负责把「找不到」
    说清楚，而不是回退到替身——那样跑出来的东西看起来正常、其实没有数据。
    """
    import importlib

    names = [args.dataset_module] if args.dataset_module else ["app.dataset_adapter"]
    problems: list[str] = []
    for name in names:
        try:
            mod = importlib.import_module(name)
        except ImportError as exc:
            problems.append(f"{name}: 导入失败（{exc}）")
            continue
        for fn_name in _DATASET_LOADERS:
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                return fn(data_dir=args.data_dir), f"{name}.{fn_name}"
        problems.append(f"{name}: 没有 {'/'.join(_DATASET_LOADERS)} 中任何一个函数")
    raise RuntimeError(
        "数据集未就绪，无法开始跑代：\n  " + "\n  ".join(problems) +
        "\n数据集的共同卷与挑战池属 C 的交付物（Dataset 协议见 "
        "app/orchestrator.py 的 Dataset）；B 侧不提供替身兜底。"
    )


def _default_run_id(cfg: Config, pilot: bool) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{'pilot' if pilot else 'run'}_{cfg.condition}_{cfg.seed}_{stamp}"


def _check_run_id(run_id: str) -> str | None:
    if not run_id or not run_id.strip():
        return "run_id 不能为空"
    if any(ch in run_id for ch in ("/", "\\", ":", "..")) or run_id.strip() != run_id:
        return f"run_id {run_id!r} 含路径字符或首尾空格；它会被当作目录名"
    return None


def _print_generation(rec: dict) -> None:
    chal = (rec.get("slots") or {}).get("challenge", 0)
    head = (f"  第 {rec['gen']:>2} 代  status={rec['status']:<9} "
            f"M={rec['M']}  槽位={rec['slot_total']}"
            f"（挑战 {chal}）  调用={rec.get('call_total')}  "
            f"{rec.get('wall_ms')} ms")
    print(head)
    if rec["status"] != "completed":
        print(f"    中止原因: {rec.get('reason')}；该代不排名、不进历史、不可作为续跑点")
        return
    for row in rec.get("individuals") or []:
        est = row.get("est")
        print(f"    {row['id']:<10} solve={row['solve']} set={row['set']} "
              f"bounty={row['bounty']} total={row['total']} "
              f"est={'NA' if est is None else round(est, 3)} n={row.get('n_samples')}")
    print(f"    排名: {' > '.join(rec.get('ranking') or [])}")
    for ch in rec.get("challenges") or []:
        if ch.get("observations"):
            print(f"    挑战 {ch['task_id']}: {ch['state']}"
                  f"{'/' + ch['retired_reason'] if ch.get('retired_reason') else ''}"
                  f"  观测 {len(ch['observations'])} 次")


def _run(cfg: Config, args: argparse.Namespace) -> int:
    """`--run` 新建一趟；`--resume RUN_ID` 从最后一个 completed 快照续跑。"""
    import asyncio

    import app.executor as executor
    from .genotype import Operators
    from .llm import LLMClient, ProtocolError
    from .runner import ensure_fresh_target, run_loop, summarize_run
    from .runtime import new_run, resume_report, branch_run, _ind_from_full
    from .store import SnapshotStore, atomic_write_json

    resume_id = args.resume
    is_resume = bool(resume_id)
    run_id = ((args.branch or f'{resume_id}_demo_{time.time_ns()}') if is_resume
              else args.run_id or _default_run_id(cfg, bool(args.pilot)))
    bad = _check_run_id(run_id)
    if bad:
        print(bad, file=sys.stderr)
        return 2

    try:
        cfg.require_runtime_ready()
    except MissingConfigError as e:
        print(f"配置未就绪: {e}", file=sys.stderr)
        return 2

    store = SnapshotStore(root=args.runs_root, run_id=run_id)
    source_store = SnapshotStore(root=args.runs_root, run_id=resume_id) if is_resume else None

    # 先看有没有可续跑的快照，再看数据集：`--resume` 打错名字时，
    # 「这趟根本没跑过」比「数据集模块缺失」更接近真实原因。
    if is_resume and not source_store.completed_gens():
        print(f"{run_id} 没有任何 completed 快照，无法续跑；"
              f"若要重新开始请用 --run", file=sys.stderr)
        return 2

    try:
        dataset, ds_source = _load_dataset(args)
    except (RuntimeError, TypeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        if is_resume:
            state = branch_run(cfg=cfg, source_store=source_store, target_store=store, dataset=dataset)
            rep = resume_report(source_store)
            print(f"续跑 {run_id}：已完成到第 {rep['last_completed']} 代，"
                  f"从第 {rep['resume_from']} 代接上")
            if rep["aborted_or_broken"]:
                print(f"  另有非完成代 {rep['aborted_or_broken']}（只作证据，"
                      "不作续跑点）")
        else:
            ensure_fresh_target(store)
            protocol = getattr(args, 'protocol_data', None)
            if not args.pilot and protocol is None:
                raise ProtocolError('正式运行必须指定 --protocol；开发联调用 --pilot')
            ancestors = ([_ind_from_full(p) for p in protocol['ancestors']] if protocol else None)
            if not protocol and args.calibration:
                from .protocol import read_calibration
                report = read_calibration(cfg, args.calibration, data_dir=args.data_dir)
                ancestors = [_ind_from_full(p) for p in report['ancestors']]
            state = new_run(cfg=cfg, dataset=dataset, run_id=run_id, ancestors=ancestors)
            if protocol:
                state.frozen_manifest.update(protocol_id=protocol['id'],
                    group_runs=[m['run_id'] for m in protocol['members']])
    except (ProtocolError, ValueError) as e:
        print(f"装配失败: {e}", file=sys.stderr)
        return 2

    print(f"运行 {run_id}  条件={cfg.condition}  目标代数={cfg.n_gen}"
          f"{f'（本次到第 {args.to_gen} 代）' if args.to_gen else ''}")
    print(f"  数据集 {ds_source}  n_pop={cfg.n_pop}  n_common={cfg.n_common}"
          f"  n_ancestor={cfg.n_ancestor}  fitness_window={cfg.fitness_window}")
    print(f"  并发 {cfg.concurrency}  模型 {cfg.model}")
    print(f"  从第 {state.last_gen + 1} 代开始；快照写入 {store.run_dir}")

    async def _go():
        async with LLMClient(cfg, run_id=run_id) as chat:
            operators = (None if cfg.condition == "F"
                         else Operators(cfg, state.rng, chat))
            return await run_loop(state=state, engine=executor, dataset=dataset,
                                  chat=chat, operators=operators, store=store,
                                  until=args.to_gen, on_generation=_print_generation)

    try:
        records = asyncio.run(_go())
    except (ProtocolError, BrokenReference) as e:
        print(f"运行中止: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n用户中断；已完成代的快照均已落盘，可用 --resume 续跑",
              file=sys.stderr)
        return 130

    summary = store.load_summary()
    summary["config_fingerprint"] = cfg.semantic_fingerprint()
    summary["config"] = {
        "condition": cfg.condition, "n_gen": cfg.n_gen, "model": cfg.model,
        "base_url": cfg.base_url, "seed": cfg.seed,
        "fitness_window": cfg.fitness_window,
    }
    summary["dataset"] = ds_source
    summary["pilot"] = bool(args.pilot)
    path = atomic_write_json(
        Path(args.runs_root) / "summaries" / f"{run_id}.json", summary)

    print(f"\n完成 {summary['completed']} 代，中止 {len(summary['aborted'])} 代；"
          f"累计调用 {summary['call_total']}，解题槽位 {summary['slot_total']}")
    if summary["aborted"]:
        print(f"  中止: {summary['aborted']}")
    print(f"  快照: {store.gens()}")
    print(f"  汇总: {path}")
    return 0 if not summary["aborted"] else 3


# --------------------------------------------------------------------------
# B7 留出评测（独立命令；主循环跑完才允许执行）
# --------------------------------------------------------------------------

def _evaluate(cfg: Config, args: argparse.Namespace) -> int:
    """`--evaluate RUN_ID`：只读冻结检查点，输出独立 evaluation 文件。

    明确拒绝在「运行还没结束」时执行：结果不能反向触发重跑选优（规格 §5）。
    """
    import asyncio

    import app.executor as executor
    from .evaluate import (evaluate_run, format_report, load_holdout_tasks,
                           plan_path, prepare_evaluation, write_evaluation,
                           write_plan)
    from .llm import LLMClient, ProtocolError

    run_id = args.evaluate
    holdout = Path(args.holdout)
    if not holdout.exists():
        print(f"留出卷不存在: {holdout}（C 交付 data/holdout.jsonl 后才有）",
              file=sys.stderr)
        return 2

    try:
        tasks = load_holdout_tasks(holdout, executor)
    except (ValueError, FileNotFoundError) as e:
        print(f"留出卷解析失败: {e}", file=sys.stderr)
        return 2

    if args.plan_only:
        # 不校验 model/base_url：这一步不花钱，只审计划
        try:
            prep = prepare_evaluation(run_id=run_id, cfg=cfg, tasks=tasks,
                                      store_root=args.runs_root)
        except ProtocolError as e:
            print(f"计划生成失败: {e}", file=sys.stderr)
            return 2
        path = write_plan(prep["plan"], run_id=run_id, root=args.runs_root,
                          order_seed=prep["order_seed"])
        print(f"计划槽位 {len(prep['plan'])} 个；检查点 "
              f"{sorted(prep['populations'])}；缺失 {prep['missing'] or '无'}")
        for w in prep["warnings"]:
            print(f"  ⚠ {w}")
        print(f"计划已写入 {path}（尚未发起任何模型调用）")
        return 0

    try:
        cfg.require_runtime_ready()
    except MissingConfigError as e:
        print(f"配置未就绪: {e}", file=sys.stderr)
        return 2

    async def _go():
        async with LLMClient(cfg, run_id=f"holdout_{run_id}") as chat:
            return await evaluate_run(run_id=run_id, cfg=cfg, engine=executor,
                                      chat=chat, tasks=tasks,
                                      store_root=args.runs_root)

    try:
        result = asyncio.run(_go())
    except ProtocolError as e:
        print(f"评测中止: {e}", file=sys.stderr)
        return 2

    path = write_evaluation(result, root=args.runs_root)
    print(format_report(result))
    print(f"\n结果已写入 {path}")
    return 0


def _pair(cfg: Config, args: argparse.Namespace) -> int:
    """`--pair E,F`：两份评测结果做配对，输出 E−F 与各自相对 g0 的变化。"""
    from .evaluate import format_pair, load_evaluation, pair_evaluations

    parts = [p.strip() for p in args.pair.split(",") if p.strip()]
    if len(parts) != 2:
        print("--pair 需要两个运行 ID，用逗号分隔：--pair E1,F1", file=sys.stderr)
        return 2
    try:
        e = load_evaluation(parts[0], root=args.runs_root)
        f = load_evaluation(parts[1], root=args.runs_root)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"读取评测结果失败: {exc}", file=sys.stderr)
        return 2
    from .llm import ProtocolError
    try:
        print(format_pair(pair_evaluations(e, f)))
    except ProtocolError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _evaluate_group(protocol, args):
    import asyncio
    from . import executor
    from .evaluate import evaluate_group, load_holdout_tasks, format_report
    from .llm import LLMClient
    cfg = Config.from_dict(protocol['config'])
    tasks = load_holdout_tasks(args.holdout, executor)
    if not args.plan_only:
        cfg.require_runtime_ready()
    async def go():
        async with LLMClient(cfg, run_id=f"holdout_group_{protocol['id']}") as chat:
            return await evaluate_group(protocol, tasks=tasks, engine=executor, chat=chat,
                store_root=args.runs_root, plan_only=args.plan_only)
    result = asyncio.run(go())
    if args.plan_only:
        print(f"Frozen group plan: {result['n_slots']} slots; no model calls")
        return 0
    for item in result:
        print(format_report(item))
    return 0 if all(r['status']=='completed' for r in result) else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.run", description="AI 养蛊机运行入口")
    p.add_argument("--config", type=str, default="config.dev.json")
    p.add_argument("--dry-run", action="store_true", help="只按公式算调用数，不发请求")
    p.add_argument("--all-seeds", action="store_true", help="dry-run 时一并显示 3 趟合计")
    p.add_argument("--gens", type=int, default=None,
                   help="覆盖 n_gen（改变配置指纹，续跑必须带同样的值）")
    p.add_argument("--run", action="store_true",
                   help="正式跑代：新建一趟并跑到 n_gen")
    p.add_argument("--run-id", dest="run_id", type=str, default=None,
                   help="本趟 ID（快照目录名）；默认 run_{条件}_{seed}_{时间}")
    p.add_argument("--pilot", action="store_true",
                   help="开发期探索跑；run_id 前缀 pilot_，与正式趟分开归档")
    p.add_argument("--data-dir", dest="data_dir", type=str, default="data",
                   help="数据集目录（交给数据集装载函数）")
    p.add_argument("--dataset-module", dest="dataset_module", type=str, default=None,
                   help="提供 Dataset 的模块名，默认 app.datasets（C 交付）")
    p.add_argument("--lock-protocol", type=str, default=None)
    p.add_argument("--protocol", type=str, default=None)
    p.add_argument("--conditions", type=str, default=None)
    p.add_argument("--resume", type=str, default=None,
                   help="从该 run_id 的最后一个 completed 快照续跑")
    p.add_argument("--branch", type=str, default=None)
    p.add_argument("--to-gen", dest="to_gen", type=int, default=None,
                   help="本次只跑到第 N 代（不改 n_gen，可再 --resume 接着跑）")
    # B7 留出评测：独立命令，只读冻结检查点
    p.add_argument("--evaluate", type=str, default=None,
                   help="对指定 run_id 做留出评测（只读快照）")
    p.add_argument("--plan-only", dest="plan_only", action="store_true",
                   help="只生成并落盘评测计划，不发起任何模型调用")
    p.add_argument("--pair", type=str, default=None,
                   help="两份评测结果配对：--pair E_run,F_run")
    p.add_argument("--holdout", type=str, default="data/holdout.json",
                   help="留出卷 JSONL 路径（C 交付）")
    p.add_argument("--runs-root", dest="runs_root", type=str, default="runs",
                   help="快照与评测结果的根目录")
    p.add_argument('--calibrate', metavar='OUTPUT_JSON', help='6 seeds × 20 calibration tasks × 3 repetitions')
    p.add_argument('--calibration', help='Calibration report required to lock a protocol')
    p.add_argument('--seeds', default=None, help='Pre-registered seeds, e.g. 42,43,44')
    p.add_argument('--evaluate-all', action='store_true', help='Interleaved evaluation of all registered runs')
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.pilot:
        args.run = True              # --pilot 只改归档口径，不改变「要跑」这件事

    if not Path(args.config).exists():
        print(f"配置文件不存在: {args.config}", file=sys.stderr)
        return 2

    try:
        cfg = Config.load(args.config)
        if args.gens is not None:
            raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
            if args.protocol:
                raise ConfigError('Locked protocol does not accept --gens')
            raw["n_gen"] = args.gens
            if args.pilot:
                raw['challenge_gens'] = []
            cfg = Config.from_dict(raw, source_path=f"{args.config} (n_gen→{args.gens})")
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    from .llm import ProtocolError
    from .protocol import load_protocol, member_config, lock_protocol, calibrate
    try:
        if args.protocol:
            args.protocol_data = load_protocol(args.protocol, data_dir=args.data_dir)
            cfg = Config.from_dict(args.protocol_data['config'])
        if args.calibrate:
            import asyncio
            from .llm import LLMClient
            from .store import atomic_write_json
            cfg.require_runtime_ready()
            if Path(args.calibrate).exists():
                raise ProtocolError('Calibration output already exists')
            async def _calibrate():
                async with LLMClient(cfg, run_id=f'calibration_{time.time_ns()}') as chat:
                    return await calibrate(cfg, chat, data_dir=args.data_dir)
            result = asyncio.run(_calibrate())
            atomic_write_json(args.calibrate, result, overwrite=False)
            print(f"Calibration {result['status']}; pass rate: {result['mean_pass_rate']}; saved {args.calibrate}")
            return 0 if result['status']=='completed' else 3
        if args.lock_protocol:
            if not args.calibration:
                raise ProtocolError('--lock-protocol requires --calibration')
            seeds = [int(s) for s in args.seeds.split(',')] if args.seeds else [cfg.seed]
            conditions = tuple(args.conditions.split(',')) if args.conditions else ('E','F')
            locked = lock_protocol(cfg, args.calibration, args.lock_protocol, seeds=seeds,
                                   data_dir=args.data_dir, conditions=conditions)
            print(f"Locked {locked['id']} with {len(locked['members'])} registered runs")
            return 0
        if args.protocol and args.evaluate_all:
            return _evaluate_group(args.protocol_data, args)
        if args.protocol and not args.dry_run and not args.pair and not args.evaluate:
            if args.resume:
                source = __import__('app.store', fromlist=['SnapshotStore']).SnapshotStore(args.runs_root, args.resume)
                return _run(Config.from_dict(source.load_manifest()['config']), args)
            if args.to_gen is not None:
                raise ProtocolError('Formal runs cannot stop early via --to-gen; use a demo branch')
            members = args.protocol_data['members']
            if args.run_id:
                members = [m for m in members if m['run_id']==args.run_id]
                if not members:
                    raise ProtocolError('run_id is not registered in protocol')
            elif not args.all_seeds:
                raise ProtocolError('Use --all-seeds or a registered --run-id')
            code = 0
            for member in members:
                args.run_id = member['run_id']
                code = max(code, _run(member_config(args.protocol_data, member), args))
            return code
        if args.protocol and args.evaluate:
            raise ProtocolError('Formal holdout requires --evaluate-all for interleaved group scheduling')
    except (ProtocolError, ConfigError, ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.dry_run:
        return _dry_run(cfg, args)
    if args.evaluate:
        return _evaluate(cfg, args)
    if args.pair:
        return _pair(cfg, args)
    if args.resume or args.run:
        if args.evaluate or args.pair:
            print("--run/--resume 不能与 --evaluate/--pair 同时使用",
                  file=sys.stderr)
            return 2
        if args.resume:
            from .store import SnapshotStore
            try:
                cfg = Config.from_dict(SnapshotStore(args.runs_root, args.resume).load_manifest()['config'])
            except ProtocolError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        return _run(cfg, args)

    # 没有 --dry-run 也没给其他动作：说明用法
    print("未指定动作。常用：\n"
          "  python -m app.run --dry-run            只算调用数\n"
          "  python -m app.run --run --gens 1       跑一代（需 Model/base_url 就位）\n"
          "  python -m app.run --resume <run_id>    从最后一个 completed 快照续跑\n"
          "  python -m app.run --evaluate <run_id>  留出评测",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
