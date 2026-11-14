"""留出评测（技术规格 §5/§9；分工方案 B7）。

三条硬约束，先说清楚，因为它们是这份文件存在的理由：

1. **只读冻结检查点**。不改任何快照、不碰主循环状态、不反向触发重跑选优。
2. **接口层面隔离**。主循环 `orchestrator.py` 不 import 本模块，`Dataset` 协议里
   没有任何 holdout 方法——主调度拿不到留出题正文。这不是口头约定，是
   `tests/test_evaluate.py::test_main_loop_cannot_reach_holdout` 钉住的事实。
3. **顺序可复现**。计划 `(run, checkpoint, individual, task)` 一次生成、固定随机序，
   重跑同一计划得到同一顺序；题目与检查点被打散，不会「先把第 0 代测完再测第 5 代」。

数字口径（规格 §9）：
    holdout_slots = n_checkpoints * n_pop * n_holdout = 5 * 6 * 20 = 600 / 趟
检查点集合 `{0} ∪ {5,10,...}≤G ∪ {G}`，由 `Config.checkpoints` 给出，不在这里硬编码。

失败处理：只评测存在的检查点；锁定的末代缺失写 **NA**，绝不用「最后一个有效代」
冒充完成（规格 §5 末段）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.config import Config
from app.genotype import Genes, Individual, Meta, build_solver_system, load_seed_genotypes
from app.llm import CallResult, ProtocolError, build_solver_messages
from app.orchestrator import Engine, ChatClient, judge_call
from app.store import SnapshotStore, atomic_write_json
from .integrity import task_digest, digest, source_manifest
from .async_tools import gather_strict

SCHEMA_VERSION = "5.1"

# 留出卷的题型配额（规格 §5：integral=10、det=10）。只做核对，不参与选题。
HOLDOUT_MIX = {"integral": 10, "det": 10}

STATUSES = ("right", "wrong", "unanswered", "transport_error")


# --------------------------------------------------------------------------
# 计划（规格 §9：预生成 (run, checkpoint, individual, task) 并固定随机顺序）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalSlot:
    """一个留出评测槽位。`run_id` 只用于计划和日志，不参与调度。"""

    run_id: str
    checkpoint: int
    individual_id: str
    task_id: str

    @property
    def slot_id(self) -> str:
        return f"holdout_g{self.checkpoint:02d}_{self.task_id}_{self.individual_id}"

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "checkpoint": self.checkpoint,
                "individual_id": self.individual_id, "task_id": self.task_id,
                "slot_id": self.slot_id}


def plan_seed(run_id: str) -> int:
    """由 run_id 稳定派生计划种子：同一次运行重跑评测，顺序必然一致。"""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id 不能为空：计划顺序要按运行冻结")
    digest = hashlib.sha256(f"holdout:{run_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def build_plan(*, run_id: str,
               individuals_by_checkpoint: Mapping[int, Sequence[str]],
               task_ids: Sequence[str],
               seed: int | None = None) -> list[EvalSlot]:
    """生成全部槽位并打乱成固定顺序。

    先按 `(checkpoint, individual, task)` 排好序再 shuffle —— 顺序只取决于
    输入集合与 seed，与字典插入顺序、并发回包先后都无关。
    `individuals_by_checkpoint` 是按检查点的六人 ID：不同检查点的人本来就不同
    （第 5 代是 G05_*，第 0 代是 G00_S*），不能拉平成一个列表。
    """
    if not task_ids:
        raise ValueError("留出题集为空")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("留出题 ID 有重复")
    combos: list[tuple[int, str, str]] = []
    for cp in sorted(individuals_by_checkpoint):
        ids = list(individuals_by_checkpoint[cp])
        if not ids:
            raise ValueError(f"检查点 {cp} 没有个体")
        if len(set(ids)) != len(ids):
            raise ValueError(f"检查点 {cp} 的个体 ID 有重复")
        for ind in sorted(ids):
            for tid in sorted(task_ids):
                combos.append((cp, ind, tid))

    rng = random.Random(plan_seed(run_id) if seed is None else seed)
    rng.shuffle(combos)
    return [EvalSlot(run_id=run_id, checkpoint=cp, individual_id=ind, task_id=tid)
            for cp, ind, tid in combos]


def expected_slots(individuals_by_checkpoint: Mapping[int, Sequence[str]],
                   n_tasks: int) -> int:
    return sum(len(v) for v in individuals_by_checkpoint.values()) * n_tasks


# --------------------------------------------------------------------------
# 检查点：只读快照 / 初代种子
# --------------------------------------------------------------------------

def individual_from_payload(payload: Mapping[str, Any]) -> Individual:
    """快照里的 `{genes, meta}` -> Individual。成绩字段不还原（评测不该看历史分）。"""
    genes, meta = payload["genes"], payload["meta"]
    return Individual(
        genes=Genes(solve_prompt=genes["solve_prompt"],
                    set_prompt=genes["set_prompt"],
                    self_check=genes["self_check"]),
        meta=Meta(id=meta["id"], parents=tuple(meta["parents"]),
                  origin=meta["origin"], born_gen=meta["born_gen"]),
    )


def population_at_checkpoint(*, store_root: str | Path, run_id: str, checkpoint: int,
                             seed_path: str | Path = "prompts/seed_genotypes.json",
                             max_chars: int = 200) -> tuple[list[Individual], str]:
    """只取该代实际参评的六人，包括运行开始时冻结的第 0 代。"""
    store = SnapshotStore(root=store_root, run_id=run_id)
    data = store.load_generation(checkpoint)       # 只接受 completed
    rec = data.get("record") or {}
    payload = rec.get("evaluated_population") or []
    if not payload:
        raise ProtocolError(f"检查点 {checkpoint} 的快照里没有可评测的个体")
    return [individual_from_payload(p) for p in payload], "snapshot"


def population_or_none(**kwargs) -> tuple[list[Individual] | None, str]:
    """缺快照不抛异常，返回 `(None, "missing")`：失败运行只评测存在的检查点。"""
    try:
        return population_at_checkpoint(**kwargs)
    except ProtocolError:
        return None, "missing"


# --------------------------------------------------------------------------
# 跑计划
# --------------------------------------------------------------------------

async def run_plan(*, plan: Sequence[EvalSlot],
                   genes_by: Mapping[tuple[int, str], Genes],
                   tasks_by_id: Mapping[str, dict],
                   engine: Engine, chat: ChatClient,
                   concurrency: int = 10) -> list[dict]:
    """逐槽位新上下文作答并判分。槽位之间无共享状态。

    调用失败单独归档；汇总时受影响的检查点记 NA，不视为能力答错。
    """
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(slot: EvalSlot) -> dict:
        task = tasks_by_id[slot.task_id]
        view = engine.solver_view(task)
        key = (slot.run_id, slot.checkpoint, slot.individual_id)
        genes = genes_by[key] if key in genes_by else genes_by[(slot.checkpoint, slot.individual_id)]
        system_prompt = build_solver_system(genes)
        messages = build_solver_messages(system_prompt, view["statement"],
                                         view["answer_format"])
        try:
            async with sem:
                res: CallResult = await chat.chat("solver", messages,
                    slot_id=f'{slot.run_id}_{slot.slot_id}', attempt=1)
        except Exception as exc:
            return {**slot.to_dict(), 'call_id':None, 'status':'transport_error',
                    'raw_answer':None, 'error':f'{type(exc).__name__}: {exc}',
                    'attempts':None, 'latency_ms':None, 'usage':{}}
        row = {**slot.to_dict(), "call_id": res.call_id, "status": None,
               "raw_answer": None, "attempts": res.attempts,
               "latency_ms": res.latency_ms, 'usage':res.usage}
        if res.is_transport_error:
            row["status"] = "transport_error"
            return row
        status, raw = judge_call(engine, task, res, slot_id=slot.slot_id)
        row["status"] = status
        row["raw_answer"] = raw
        return row

    return list(await gather_strict([_one(s) for s in plan]))


# --------------------------------------------------------------------------
# 汇总（规格 §9：每检查点六人 0–20、均分、积分/行列式各 0–10、逐题通过率）
# --------------------------------------------------------------------------

def _task_type(tasks_by_id: Mapping[str, dict], task_id: str) -> str:
    return tasks_by_id[task_id]["type"]


def summarize(*, run_id: str, plan: Sequence[EvalSlot], rows: Sequence[dict],
              tasks_by_id: Mapping[str, dict],
              populations: Mapping[int, Sequence[Individual]],
              sources: Mapping[int, str],
              condition: str, n_gen: int,
              missing_checkpoints: Sequence[int] = (),
              warnings: Sequence[str] = ()) -> dict:
    """把逐槽位结果汇总成检查点表。分数为 None 表示 NA（缺数据），不是 0。"""
    task_ids = sorted({s.task_id for s in plan})
    n_tasks = len(task_ids)
    by_checkpoint = sorted({s.checkpoint for s in plan})

    checkpoints: list[dict] = []
    for cp in by_checkpoint:
        cp_rows = [r for r in rows if r["checkpoint"] == cp]
        if len(cp_rows) != n_tasks * len(populations[cp]):
            raise ProtocolError(
                f"检查点 {cp} 槽位数不符：{len(cp_rows)} != "
                f"{n_tasks} × {len(populations[cp])}")

        people: list[dict] = []
        for ind in sorted(populations[cp], key=lambda i: i.meta.id):
            mine = [r for r in cp_rows if r["individual_id"] == ind.meta.id]
            counts = {s: sum(1 for r in mine if r["status"] == s) for s in STATUSES}
            integral = sum(1 for r in mine
                           if _task_type(tasks_by_id, r["task_id"]) == "integral"
                           and r["status"] == "right")
            det = sum(1 for r in mine
                      if _task_type(tasks_by_id, r["task_id"]) == "det"
                      and r["status"] == "right")
            people.append({
                "id": ind.meta.id,
                "score": counts["right"],            # 0..n_tasks
                "integral": integral,                # 0..10
                "det": det,                          # 0..10
                "n_tasks": n_tasks,
                "statuses": counts,
            })

        per_task = {}
        for tid in task_ids:
            got = [r for r in cp_rows if r["task_id"] == tid]
            per_task[tid] = {
                "type": _task_type(tasks_by_id, tid),
                "assigned": len(got),
                "right": sum(1 for r in got if r["status"] == "right"),
                "unanswered": sum(1 for r in got if r["status"] == "unanswered"),
                "transport_error": sum(1 for r in got
                                       if r["status"] == "transport_error"),
            }
            per_task[tid]["p_t"] = (round(per_task[tid]["right"]
                                          / per_task[tid]["assigned"], 10)
                                    if per_task[tid]["assigned"] else None)

        scores = [p["score"] for p in people]
        checkpoints.append({
            "checkpoint": cp,
            "source": sources[cp],
            "individuals": people,
            "mean": round(sum(scores) / len(scores), 10) if scores else None,
            "integral_mean": (round(sum(p["integral"] for p in people) / len(people), 10)
                              if people else None),
            "det_mean": (round(sum(p["det"] for p in people) / len(people), 10)
                         if people else None),
            "per_task": per_task,
        })

    # 锁定的末代缺失 -> NA 条目（不跳过、不用最后有效代冒充）
    for cp in missing_checkpoints:
        checkpoints.append({
            "checkpoint": cp, "source": "missing", "individuals": [],
            "mean": None, "integral_mean": None, "det_mean": None,
            "per_task": {}, "na": True,
        })

    checkpoints.sort(key=lambda c: c["checkpoint"])

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "condition": condition,
        "n_gen": n_gen,
        "n_holdout": n_tasks,
        "n_slots": len(rows),
        "checkpoints": checkpoints,
        "plan": [s.to_dict() for s in plan],
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------
# 一次完整评测
# --------------------------------------------------------------------------

def index_tasks(tasks: Sequence[dict]) -> dict[str, dict]:
    """留出题入索引：只读、不改，签名必须唯一。"""
    out: dict[str, dict] = {}
    for task in tasks:
        tid = task.get("id")
        if not isinstance(tid, str) or not tid:
            raise ValueError("留出题必须带非空 id")
        if tid in out:
            raise ValueError(f"留出题 ID 重复：{tid}")
        for key in ("type", "statement", "reference", "signature", "template", "params"):
            if key not in task:
                raise ValueError(f"留出题 {tid} 缺字段 {key}")
        if task["type"] not in ("integral", "det"):
            raise ValueError(f"留出题 {tid} 的 type 非法：{task['type']}")
        if task["signature"] in {t["signature"] for t in out.values()}:
            raise ValueError(f"留出题签名重复：{task['signature']}")
        out[tid] = task
    return out


def check_mix(tasks_by_id: Mapping[str, dict], mix: Mapping[str, int] = HOLDOUT_MIX) -> None:
    """题型配额核对。不符直接报错：留出卷组成是预登记内容，不能悄悄变。"""
    got = {"integral": 0, "det": 0}
    for task in tasks_by_id.values():
        got[task["type"]] += 1
    if dict(got) != dict(mix):
        raise ValueError(f"留出卷题型组成不符：{got} != {dict(mix)}")


def holdout_signatures(tasks_by_id: Mapping[str, dict]) -> set[str]:
    """主循环只该看到这个东西：签名索引，用来看互斥，看不到题面。"""
    return {t["signature"] for t in tasks_by_id.values()}


def _run_task_signatures(store_root: str | Path, run_id: str,
                         checkpoints: Iterable[int]) -> set[str]:
    """从快照里收集本次运行自己出过的题的签名，用来查「留出被污染」。"""
    sigs: set[str] = set()
    store = SnapshotStore(root=store_root, run_id=run_id)
    for cp in checkpoints:
        try:
            data = json.loads(store.snapshot_path(cp).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ProtocolError(f'Cannot audit snapshot {cp}') from exc
        sigs.update((data.get('record') or {}).get('exposed_signatures') or [])
        for task in (data.get("record") or {}).get("tasks") or []:
            if task.get("signature"):
                sigs.add(task["signature"])
    return sigs


def prepare_evaluation(*, run_id: str, cfg: Config, tasks: Sequence[dict],
                       store_root: str | Path = "runs",
                       seed_path: str | Path = "prompts/seed_genotypes.json",
                       order_seed: int | None = None,
                       check_mix_quota: bool = True) -> dict:
    """装载题集、还原检查点、生成计划 —— **一次模型调用都不发**。

    单独拆出来是为了「先审计划再花钱」：600 个槽位可以先落盘核对
    （检查点 × 人数 × 题数是否等于 dry-run），确认无误再开跑。
    """
    store = SnapshotStore(root=store_root, run_id=run_id)
    manifest = store.load_manifest()
    if manifest.get('parent_run_id'):
        raise ProtocolError('Demo branches are not independent evaluation runs')
    if manifest.get('config_fingerprint') != cfg.semantic_fingerprint():
        raise ProtocolError('Evaluation config differs from the frozen run')
    if manifest.get('source') != source_manifest():
        raise ProtocolError('Code/prompts differ from the frozen run')
    for member in manifest.get('group_runs') or [run_id]:
        other = SnapshotStore(root=store_root, run_id=member)
        if other.load_summary().get('status') not in ('completed', 'aborted'):
            raise ProtocolError(f'Run {member} has not terminated; holdout is locked')
    if manifest.get('dataset', {}).get('holdout_digest') != task_digest(tasks):
        raise ProtocolError('Holdout differs from the frozen dataset')
    tasks_by_id = index_tasks(tasks)
    if check_mix_quota:
        check_mix(tasks_by_id)

    warnings: list[str] = []
    populations: dict[int, list[Individual]] = {}
    sources: dict[int, str] = {}
    missing: list[int] = []

    for cp in cfg.checkpoints:
        pop, source = population_or_none(store_root=store_root, run_id=run_id,
                                         checkpoint=cp, seed_path=seed_path,
                                         max_chars=cfg.max_genotype_chars)
        if pop is None:
            missing.append(cp)
            continue
        if len(pop) != cfg.n_pop:
            raise ProtocolError(
                f"检查点 {cp} 的个体数 {len(pop)} != n_pop {cfg.n_pop}")
        populations[cp] = pop
        sources[cp] = source

    if not populations:
        raise ProtocolError(f"运行 {run_id} 没有任何可评测的检查点")

    # 末代缺失：保留在 missing 里，summary 会写成 NA（不拿更早的代冒充）
    for cp in list(missing):
        if cp != cfg.n_gen:
            warnings.append(f"检查点 {cp} 缺 completed 快照，已跳过（该代未成功完成）")
    if cfg.n_gen in missing:
        warnings.append(f"锁定末代 {cfg.n_gen} 缺快照，记为 NA；"
                        "不使用最后一个有效代代替")

    individuals_by_checkpoint = {cp: [i.meta.id for i in pop]
                                 for cp, pop in populations.items()}
    plan = build_plan(run_id=run_id,
                      individuals_by_checkpoint=individuals_by_checkpoint,
                      task_ids=sorted(tasks_by_id), seed=order_seed)

    genes_by: dict[tuple[int, str], Genes] = {}
    for cp, pop in populations.items():
        for ind in pop:
            genes_by[(cp, ind.meta.id)] = ind.genes

    # 留出题与本次运行出过的题是否撞签名：撞了就说明留出没留出，必须留痕
    overlap = holdout_signatures(tasks_by_id) & _run_task_signatures(
        store_root, run_id, store.gens())
    if overlap:
        raise ProtocolError(f'Holdout contamination: {len(overlap)} overlapping signatures')

    return {"tasks_by_id": tasks_by_id, "plan": plan,
            "populations": populations, "sources": sources,
            "missing": missing, "warnings": warnings, "genes_by": genes_by,
            "order_seed": plan_seed(run_id) if order_seed is None else order_seed,
            'manifest': manifest}


async def evaluate_run(*, run_id: str, cfg: Config, engine: Engine, chat: ChatClient,
                       tasks: Sequence[dict], store_root: str | Path = "runs",
                       seed_path: str | Path = "prompts/seed_genotypes.json",
                       order_seed: int | None = None,
                       concurrency: int | None = None,
                       check_mix_quota: bool = True) -> dict:
    """对一次运行做留出评测。只读 `runs/snapshots/{run_id}`，不写任何代数据。"""
    prep = prepare_evaluation(run_id=run_id, cfg=cfg, tasks=tasks,
                              store_root=store_root, seed_path=seed_path,
                              order_seed=order_seed,
                              check_mix_quota=check_mix_quota)
    if prep['manifest'].get('protocol_id'):
        raise ProtocolError('Formal evaluation must interleave the registered group')
    tasks_by_id, plan = prep["tasks_by_id"], prep["plan"]
    if evaluation_path(store_root, run_id).exists():
        raise ProtocolError('Evaluation already exists; refusing to replace evidence')
    write_plan(plan, run_id=run_id, root=store_root, order_seed=prep['order_seed'])
    claim_evaluation(store_root, run_id)

    rows = await run_plan(plan=plan, genes_by=prep["genes_by"],
                          tasks_by_id=tasks_by_id, engine=engine, chat=chat,
                          concurrency=concurrency if concurrency is not None
                          else cfg.concurrency)

    return finish_evaluation(run_id, cfg, tasks, prep, rows)


def finish_evaluation(run_id, cfg, tasks, prep, rows):
    result = summarize(run_id=run_id, plan=prep['plan'], rows=rows,
                       tasks_by_id=prep['tasks_by_id'],
                       populations=prep["populations"], sources=prep["sources"],
                       condition=cfg.condition, n_gen=cfg.n_gen,
                       missing_checkpoints=prep["missing"],
                       warnings=prep["warnings"])
    result["n_checkpoints"] = len(cfg.checkpoints)
    result["order_seed"] = prep["order_seed"]
    result["usage"] = _aggregate_rows(rows)
    result['answers'] = rows
    result['seed'] = cfg.seed
    result['data_seed'] = cfg.data_seed
    result['protocol_id'] = prep['manifest'].get('protocol_id')
    result['comparison_fingerprint'] = prep['manifest']['comparison_fingerprint']
    result['initial_fingerprint'] = digest(prep['manifest']['initial_population'])
    result['ancestor_fingerprint'] = digest(prep['manifest']['ancestors'])
    result['holdout_digest'] = task_digest(tasks)
    result['status'] = 'incomplete' if any(r['status']=='transport_error' for r in rows) else 'completed'
    if result['status'] == 'incomplete':
        for cp in result['checkpoints']:
            if any(r['status']=='transport_error' and r['checkpoint']==cp['checkpoint'] for r in rows):
                cp.update(mean=None, integral_mean=None, det_mean=None, na=True)
                for person in cp['individuals']:
                    if person['statuses']['transport_error']:
                        person.update(score=None, integral=None, det=None)
                for task in cp['per_task'].values():
                    if task['transport_error']:
                        task['p_t'] = None
        result['warnings'].append('Transport errors: affected checkpoint scores are NA, not capability failures')
    return result


def claim_evaluation(root, run_id):
    """A started attempt cannot be silently retried or selected after seeing holdout."""
    if evaluation_path(root, run_id).exists():
        raise ProtocolError('Evaluation already exists')
    atomic_write_json(Path(root) / 'evaluations' / f'{run_id}.started.json',
                      {'run_id':run_id, 'status':'started'}, overwrite=False)


async def evaluate_group(protocol, *, tasks, engine, chat, store_root='runs', plan_only=False):
    from .protocol import member_config
    prepared = {}
    for member in protocol['members']:
        run_id = member['run_id']
        cfg = member_config(protocol, member)
        prep = prepare_evaluation(run_id=run_id, cfg=cfg, tasks=tasks, store_root=store_root)
        manifest = prep['manifest']
        if (manifest.get('protocol_id') != protocol['id'] or
            manifest['group_runs'] != [m['run_id'] for m in protocol['members']] or
            manifest['dataset'] != protocol['dataset'] or
            manifest['ancestors'] != protocol['ancestors'] or
            manifest['initial_population'] != protocol['initial_population']):
            raise ProtocolError('Run does not match registered group')
        if evaluation_path(store_root, run_id).exists() or (
            Path(store_root) / 'evaluations' / f'{run_id}.started.json').exists():
            raise ProtocolError(f'Evaluation already started: {run_id}')
        prepared[run_id] = (cfg, prep)
    order_seed = plan_seed(protocol['id'])
    plan = [s for _, prep in prepared.values() for s in prep['plan']]
    random.Random(order_seed).shuffle(plan)
    write_plan(plan, run_id=f"group_{protocol['id']}", root=store_root, order_seed=order_seed)
    if plan_only:
        return {'n_slots':len(plan), 'plan':[s.to_dict() for s in plan]}
    for run_id, (_, prep) in prepared.items():
        # Each per-run plan preserves its relative position in the group schedule.
        prep['plan'] = [s for s in plan if s.run_id == run_id]
        prep['order_seed'] = order_seed
        write_plan(prep['plan'], run_id=run_id, root=store_root, order_seed=order_seed)
        claim_evaluation(store_root, run_id)
    genes = {(run_id, cp, person):g for run_id, (_, prep) in prepared.items()
             for (cp, person), g in prep['genes_by'].items()}
    rows = await run_plan(plan=plan, genes_by=genes, tasks_by_id=index_tasks(tasks),
                          engine=engine, chat=chat, concurrency=cfg.concurrency)
    results = []
    for run_id, (cfg, prep) in prepared.items():
        result = finish_evaluation(run_id, cfg, tasks, prep, [r for r in rows if r['run_id']==run_id])
        write_evaluation(result, root=store_root)
        results.append(result)
    return results


def _aggregate_rows(rows: Sequence[dict]) -> dict:
    statuses = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    out = {'calls':sum(r.get('call_id') is not None for r in rows),
           'call_total_lower_bound':any(r.get('call_id') is None for r in rows),
           'slot_total':len(rows), 'statuses':statuses, 'cost':None,
           'latency_ms_total':sum(int(r.get('latency_ms') or 0) for r in rows)}
    for key in ('input_tokens','output_tokens','reasoning_tokens'):
        values = [r.get('usage', {}).get(key) for r in rows]
        out[key] = (sum(values) if values and all(type(v) is int for v in values)
                    and all(r.get('attempts') == 1 for r in rows) else None)
    return out


# --------------------------------------------------------------------------
# 落盘与配对（规格 §9 末段：E_G − F_G 及各自相对 g0 的变化）
# --------------------------------------------------------------------------

def evaluation_path(root: str | Path, run_id: str) -> Path:
    return Path(root) / "evaluations" / f"{run_id}.json"


def plan_path(root: str | Path, run_id: str) -> Path:
    return Path(root) / "plans" / f"{run_id}.json"


def write_plan(plan: Sequence[EvalSlot], *, run_id: str,
               root: str | Path = "runs",
               order_seed: int | None = None) -> Path:
    """先落盘再开跑：600 个槽位可以在花钱之前被核对一遍。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "n_slots": len(plan),
        "order_seed": plan_seed(run_id) if order_seed is None else order_seed,
        "plan": [s.to_dict() for s in plan],
    }
    path = plan_path(root, run_id)
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')) != payload:
            raise ProtocolError('Frozen evaluation plan differs')
        return path
    return atomic_write_json(path, payload, overwrite=False)


def write_evaluation(result: Mapping[str, Any], *, root: str | Path = "runs") -> Path:
    return atomic_write_json(evaluation_path(root, result["run_id"]), dict(result), overwrite=False)


def load_evaluation(path_or_run_id: str | Path, *, root: str | Path = "runs") -> dict:
    p = Path(path_or_run_id)
    if not p.exists():
        p = evaluation_path(root, str(path_or_run_id))
    return json.loads(p.read_text(encoding="utf-8"))


def _mean_by_checkpoint(result: Mapping[str, Any]) -> dict[int, float | None]:
    return {c["checkpoint"]: c["mean"] for c in result["checkpoints"]}


def pair_evaluations(e: Mapping[str, Any], f: Mapping[str, Any]
                     ) -> dict:
    """E/F 配对：逐检查点的 E−F 及各自相对 g0 的变化。

    缺失检查点或 NA 一律给 None——不跳过失败种子、不换配对对象（规格 §5）。
    """
    if e.get('condition') != 'E' or f.get('condition') != 'F':
        raise ProtocolError('Pair requires E then F')
    for key in ('seed','data_seed','n_gen','n_holdout','holdout_digest','comparison_fingerprint',
                'initial_fingerprint','ancestor_fingerprint','protocol_id'):
        if key not in e or key not in f or e[key] != f[key]:
            raise ProtocolError(f'Incompatible pair: {key}')
    em, fm = _mean_by_checkpoint(e), _mean_by_checkpoint(f)
    cps = sorted(set(em) | set(fm))
    e0, f0 = em.get(0), fm.get(0)

    rows = []
    for cp in cps:
        e_mean, f_mean = em.get(cp), fm.get(cp)
        diff = None if e_mean is None or f_mean is None else round(e_mean - f_mean, 10)
        rows.append({
            "checkpoint": cp,
            "e_mean": e_mean,
            "f_mean": f_mean,
            "diff": diff,
            "e_delta_from_g0": (None if e_mean is None or e0 is None
                                else round(e_mean - e0, 10)),
            "f_delta_from_g0": (None if f_mean is None or f0 is None
                                else round(f_mean - f0, 10)),
        })

    g = e['n_gen']
    final = next((r for r in rows if r["checkpoint"] == g), None) if g is not None else None
    return {
        "schema_version": SCHEMA_VERSION,
        "e_run": e.get("run_id"),
        "f_run": f.get("run_id"),
        "n_holdout": e.get("n_holdout", f.get("n_holdout")),
        "locked_gen": g,
        "rows": rows,
        "final": final,
        "note": "diff = E_mean − F_mean；delta_from_g0 为相对本趟第 0 代的变化；"
                "null 表示该检查点缺失或为 NA，未做任何替代",
    }


def format_report(result: Mapping[str, Any]) -> str:
    """检查点表：六人 0–20、均分、积分/行列式分层。"""
    lines = [f"留出评测 {result['run_id']}  condition={result['condition']}  "
             f"n_gen={result['n_gen']}  n_holdout={result['n_holdout']}  "
             f"slots={result['n_slots']}"]
    for cp in result["checkpoints"]:
        head = f"\n[检查点 {cp['checkpoint']:>2}]  source={cp['source']}"
        if cp.get("na") or cp["mean"] is None:
            lines.append(head + "  -> NA（缺快照，未用其他代代替）")
            continue
        lines.append(head + f"  均分={cp['mean']:.2f}  "
                            f"积分均分={cp['integral_mean']:.2f}  "
                            f"行列式均分={cp['det_mean']:.2f}")
        for p in cp["individuals"]:
            st = p["statuses"]
            lines.append(f"    {p['id']:<9} {p['score']:>2}/{p['n_tasks']}  "
                         f"积分 {p['integral']:>2}  行列式 {p['det']:>2}   "
                         f"right/wrong/unanswered/err = "
                         f"{st['right']}/{st['wrong']}/{st['unanswered']}/"
                         f"{st['transport_error']}")
    if result.get("warnings"):
        lines.append("\n警告：")
        lines.extend(f"  - {w}" for w in result["warnings"])
    return "\n".join(lines)


def format_pair(pair: Mapping[str, Any]) -> str:
    """E/F 配对表。NA 一律显示 NA，不填 0、不拿别的代补。"""
    fmt = lambda v: "NA" if v is None else f"{v:.2f}"      # noqa: E731
    signed = lambda v: "NA" if v is None else f"{v:+.2f}"  # noqa: E731
    lines = [f"E/F 配对  E={pair['e_run']}  F={pair['f_run']}  "
             f"n_holdout={pair['n_holdout']}  锁定末代={pair['locked_gen']}",
             f"{'检查点':>6}{'E 均分':>9}{'F 均分':>9}{'E−F':>9}"
             f"{'E 相对g0':>11}{'F 相对g0':>11}"]
    for row in pair["rows"]:
        lines.append(f"{row['checkpoint']:>6}{fmt(row['e_mean']):>9}"
                     f"{fmt(row['f_mean']):>9}{signed(row['diff']):>9}"
                     f"{signed(row['e_delta_from_g0']):>11}"
                     f"{signed(row['f_delta_from_g0']):>11}")
    final = pair.get("final")
    if final:
        lines.append("")
        lines.append(f"末代（{final['checkpoint']}）主结果："
                     f"E−F = {signed(final['diff'])}，"
                     f"E 相对 g0 = {signed(final['e_delta_from_g0'])}，"
                     f"F 相对 g0 = {signed(final['f_delta_from_g0'])}")
    lines.append("")
    lines.append("null/NA 表示该检查点缺失或失败，未做任何替代（规格 §5 末段）。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 题集装载（C 交付 data/holdout.jsonl 后由 CLI 调用）
# --------------------------------------------------------------------------

def load_holdout_tasks(path: str | Path, engine: Engine, *,
                       expect_mix: bool = True) -> list[dict]:
    """读留出卷 JSONL，逐题重算 reference 后返回 Task 列表。

    每行接受两种写法：
      {"id": "H01", "template": "gamma", "params": {...}}      <- 只给题源
      {"id": "H01", "template": ..., "params": ..., ...}       <- 已带题面/答案
    无论哪种，**reference 都由 `engine.solve_exact` 重算**，不采信文件里的值：
    留出卷的答案只能来自执行器（契约「不要相信任何 claim」）。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"留出卷不存在: {p}")
    tasks: list[dict] = []
    seen: set[str] = set()
    text = p.read_text(encoding='utf-8')
    if p.suffix == '.json':
        source_rows = json.loads(text)
        if isinstance(source_rows, dict):
            source_rows = source_rows['tasks']
    else:
        source_rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    for lineno, raw in enumerate(source_rows, 1):
        tid = raw.get("id") or f"H{len(tasks) + 1:02d}"
        if tid in seen:
            raise ValueError(f"{p}:{lineno} 题 ID 重复: {tid}")
        seen.add(tid)
        source = {"template": raw.get("template"), "params": raw.get("params")}
        ok, reason = engine.validate_task(source)
        if not ok:
            raise ValueError(f"{p}:{lineno} 题 {tid} 未通过校验: {reason}")
        task = engine.normalize_task(source, task_id=tid, source="holdout",
                                     by=None, gen=None)
        task["reference"] = engine.solve_exact(task)
        tasks.append(task)
    by_id = index_tasks(tasks)
    if expect_mix:
        check_mix(by_id)
    return tasks
