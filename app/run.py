"""CLI 入口。

B2 阶段只交付 `--dry-run`（规格 §9）。其余子命令先明确报「未实现」，
不返回假成功——赛场上分不清「没跑」和「跑成功」是最贵的错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    MissingConfigError,
    call_plan,
    per_gen_calls,
)


def _fmt_money(cfg: Config) -> str:
    if cfg.budget_amount is None:
        return "未设置（必须明确选择：填费用上限，或声明仅按时间/调用上限控制）"
    cur = cfg.budget_currency or "（币种未设置）"
    return f"{cfg.budget_amount} {cur}"


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
    print(format_pair(pair_evaluations(e, f)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.run", description="AI 养蛊机运行入口")
    p.add_argument("--config", type=str, default="config.dev.json")
    p.add_argument("--dry-run", action="store_true", help="只按公式算调用数，不发请求")
    p.add_argument("--all-seeds", action="store_true", help="dry-run 时一并显示 3 趟合计")
    p.add_argument("--gens", type=int, default=None, help="覆盖 n_gen（仅开发配置）")
    p.add_argument("--pilot", action="store_true")
    p.add_argument("--lock-protocol", type=str, default=None)
    p.add_argument("--protocol", type=str, default=None)
    p.add_argument("--conditions", type=str, default=None)
    p.add_argument("--resume", type=str, default=None)
    p.add_argument("--branch", type=str, default=None)
    p.add_argument("--to-gen", type=int, default=None)
    # B7 留出评测：独立命令，只读冻结检查点
    p.add_argument("--evaluate", type=str, default=None,
                   help="对指定 run_id 做留出评测（只读快照）")
    p.add_argument("--plan-only", dest="plan_only", action="store_true",
                   help="只生成并落盘评测计划，不发起任何模型调用")
    p.add_argument("--pair", type=str, default=None,
                   help="两份评测结果配对：--pair E_run,F_run")
    p.add_argument("--holdout", type=str, default="data/holdout.jsonl",
                   help="留出卷 JSONL 路径（C 交付）")
    p.add_argument("--runs-root", dest="runs_root", type=str, default="runs",
                   help="快照与评测结果的根目录")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.lock_protocol:
        return _not_implemented("--lock-protocol")
    if args.resume:
        return _not_implemented("--resume")
    if args.protocol and not args.dry_run:
        return _not_implemented("--protocol 正式运行")
    if args.pilot:
        return _not_implemented("--pilot")

    if not Path(args.config).exists():
        print(f"配置文件不存在: {args.config}", file=sys.stderr)
        return 2

    try:
        cfg = Config.load(args.config)
        if args.gens is not None:
            raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
            raw["n_gen"] = args.gens
            cfg = Config.from_dict(raw, source_path=f"{args.config} (n_gen→{args.gens})")
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        return _dry_run(cfg, args)
    if args.evaluate:
        return _evaluate(cfg, args)
    if args.pair:
        return _pair(cfg, args)

    # 没有 --dry-run 也没给其他动作：说明用法
    print("未指定动作。开发阶段用：python -m app.run --dry-run --config config.dev.json",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
