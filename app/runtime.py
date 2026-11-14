"""运行装配：从配置与数据集造出一个可跑的 `RunState`。

这一层原先误放在 `tests/doubles.py`（`build_state` / `seed_dedup`），后果是
「41 条测试全过、冒烟能跑三代，但 `app/` 里没有任何生产路径能新建一次运行」：

- `state.challenges` 永远是空 dict → `challenge_for_generation` 恒返回 None
  → 第 8/15 代**不会注入任何挑战题**，实验里「测能力天花板」的机制等于不存在；
- 挑战池的签名从不进 `dedup_set` → 运行内出的题可能与挑战题撞签名，
  防泄漏形同虚设（规格 §5）。

装配层只做三件事，**不碰模型、不碰网络**，所以可以在没有推理服务的机器上装配、
用替身把 20 代跑完再等真服务：

1. 从种子文件加载初代 `n_pop` 人（逐字断言在 `genotype.load_seed_genotypes` 里）；
2. 祖先取初代的前 `n_ancestor` 人：**新建 `Individual` 对象、成绩独立**，
   genes 与 meta 共享——这两者都是 frozen，不会被写坏；
3. 挑战池各题按 id 排序后与 `cfg.challenge_gens` 配对建 `Challenge`，
   池子全部签名进 `dedup_set`。

对应规格：§2.3（数据集签名互斥）、§6.3（挑战窗口）、§8（初代与祖先）。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import Config
from .genotype import GeneHistory, Genes, Individual, Meta, load_seed_genotypes
from .llm import ProtocolError
from .orchestrator import Challenge, Dataset, RunState
from .store import SnapshotStore, rng_from_dict
from .integrity import dataset_manifest, source_manifest, digest, comparison_config

DEFAULT_SEEDS_PATH = "prompts/seed_genotypes.json"

# 状态载荷的版本号。改了结构就升版本，老快照会被显式拒绝而不是「尽力兼容」。
STATE_SCHEMA = "run_state/2"


# --------------------------------------------------------------------------
# 挑战对象
# --------------------------------------------------------------------------

def build_challenges(cfg: Config, dataset: Dataset) -> dict[str, Challenge]:
    """挑战池 → `Challenge` 对象，按池内 id 排序后依次配 `cfg.challenge_gens`。

    排序是为了确定性：数据集给出的顺序不该改变「哪一代注入哪道题」，
    否则同一个 run_id 换个装载顺序就跑出不同的实验。

    池子里的题比注入代多（20 代默认 [8,15]，池子五道）是正常的——多出来的题
    只参与 `dedup_set` 占位（防止运行内题目撞车），不会被注入。但反过来
    **池子少于注入代必须报错**：`zip` 会静默截断，结果是有代次悄悄不注入，
    跑出来的数据看起来正常、实际上少了一个窗口。
    """
    pool_ids = sorted({t["id"] for t in dataset.challenge_pool()})
    gens = list(cfg.challenge_gens)
    if not pool_ids:
        raise ValueError("挑战池为空：dataset.challenge_pool() 没给出任何题")
    if len(pool_ids) < len(gens):
        raise ValueError(
            f"挑战池只有 {len(pool_ids)} 道题，但配置了 {len(gens)} 个注入代 "
            f"{gens}；不能靠 zip 截断静默少注入"
        )
    return {
        pool_id: Challenge(task_id=pool_id, born_gen=born,
                           window=cfg.challenge_window)
        for pool_id, born in zip(pool_ids, gens)
    }


# --------------------------------------------------------------------------
# 去重占位
# --------------------------------------------------------------------------

def prime_dedup(state: RunState, dataset: Dataset) -> None:
    """把数据集与挑战池的签名全部放进 `dedup_set`，并建好挑战对象。

    两条来源都并进去：`dataset.signatures()` 按契约应已含挑战池，但那是 C 的
    实现承诺，这里再显式并一次池子签名——集合幂等，代价为零；一旦 C 的
    `signatures()` 漏了池子，防泄漏不会跟着一起漏。
    """
    sigs = set(dataset.signatures())
    for task in dataset.challenge_pool():
        sig = task.get("signature")
        if isinstance(sig, str):
            sigs.add(sig)
    state.dedup_set |= sigs
    state.challenges = build_challenges(state.cfg, dataset)


# --------------------------------------------------------------------------
# 新建运行
# --------------------------------------------------------------------------

def new_run(*, cfg: Config, dataset: Dataset, run_id: str,
            seeds_path: str | Path = DEFAULT_SEEDS_PATH,
            rng: random.Random | None = None,
            ancestors: Sequence[Individual] | None = None) -> RunState:
    """装配一次新运行的初始状态（第 0 代，尚未跑任何代）。

    `rng` 可注入：B5 calibration 与测试需要固定随机源；默认 `random.Random(cfg.seed)`。
    `ancestors` 可注入：B5 完成后祖先应换成 calibration 选定的人，而不是初代前三。

    这里不做 `cfg.require_runtime_ready()`——装配本身不花钱，也不发请求；
    占位符检查留给真正要发请求的那一层（`run.py` 的正式运行分支）。
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 不能为空：它是快照目录名与所有追溯的锚点")

    people = load_seed_genotypes(seeds_path, max_chars=cfg.max_genotype_chars)
    if len(people) != cfg.n_pop:
        # load_seed_genotypes 已断言六份且 id 唯一；这里再核一次与 n_pop 的对应，
        # 防止将来有人改 n_pop 却忘了种子的份数。
        raise ValueError(
            f"种子文件给出 {len(people)} 份初代，但 n_pop={cfg.n_pop}；"
            "两处必须一致"
        )

    if ancestors is None:
        if len(people) < cfg.n_ancestor:
            raise ValueError(
                f"初代只有 {len(people)} 人，取不出 n_ancestor={cfg.n_ancestor} 个祖先"
            )
        # 新建 Individual：成绩字段（current_total/est/n_samples）独立，
        # 否则祖先会被当成当代个体一起计分。
        anc = [Individual(genes=p.genes, meta=p.meta)
               for p in people[:cfg.n_ancestor]]
    else:
        anc = list(ancestors)
        if len(anc) != cfg.n_ancestor:
            raise ValueError(
                f"注入的祖先有 {len(anc)} 个，但 n_ancestor={cfg.n_ancestor}"
            )

    state = RunState(
        run_id=run_id,
        cfg=cfg,
        rng=rng if rng is not None else random.Random(cfg.seed),
        next_population=people,
        ancestors=anc,
        gene_history=GeneHistory(),
        dedup_set=set(),
        challenges={},
    )
    prime_dedup(state, dataset)
    state.frozen_manifest = {
        'schema': 'run-manifest/1', 'run_id': run_id, 'config': cfg.to_dict(),
        'config_fingerprint': cfg.semantic_fingerprint(),
        'comparison_fingerprint': comparison_config(cfg),
        'initial_population': [_ind_full(p) for p in people],
        'ancestors': [_ind_full(p) for p in anc],
        'dataset': dataset_manifest(dataset, cfg.n_gen),
        'source': source_manifest(), 'parent_run_id': None, 'from_gen': None,
        'protocol_id': None, 'group_runs': [run_id],
    }
    state.phase = "init"
    state.last_gen = 0
    return state


def describe_run(state: RunState) -> dict:
    """装配结果的自检摘要：不跑模型也能一眼看出「该有的东西在不在」。"""
    gens = sorted((ch.born_gen, ch.task_id) for ch in state.challenges.values())
    return {
        "run_id": state.run_id,
        "condition": state.cfg.condition,
        "n_pop": len(state.next_population),
        "population_ids": [i.id for i in state.next_population],
        "n_ancestor": len(state.ancestors),
        "ancestor_ids": [i.id for i in state.ancestors],
        "challenge_injections": [{"gen": g, "task_id": t} for g, t in gens],
        "challenge_window": state.cfg.challenge_window,
        "dedup_size": len(state.dedup_set),
        "seed": state.cfg.seed,
        "last_gen": state.last_gen,
        "phase": state.phase,
    }


# --------------------------------------------------------------------------
# 状态往返（续跑的地基）
# --------------------------------------------------------------------------
#
# 为什么不能只存 `{phase, last_gen}`：续跑要还原四样东西，缺一样都会静默改变实验：
#
#   gene_history  → 缺了 fitness_window 的降噪从头开始，排序标准悄悄换了
#   dedup_set     → 缺了运行内可能重新出出已经出过的题（规格 §2.3 互斥失效）
#   challenges    → 缺了第 8 代的挑战会重新注入一次，观测被算两遍
#   current_total / est / n_samples → 缺了无法解释「上一代为什么淘汰他」
#
# 所以这里把整个 `RunState` 序列化。任何字段不符就抛 ProtocolError，
# 不做「尽力兼容」——续跑时静默降级比直接报错贵得多。

def _ind_full(ind: Individual) -> dict:
    """个体全量载荷：比代记录里的 `_ind_payload` 多三个成绩字段。"""
    return {
        "genes": {"solve_prompt": ind.genes.solve_prompt,
                  "set_prompt": ind.genes.set_prompt,
                  "self_check": ind.genes.self_check},
        "meta": {"id": ind.meta.id, "parents": list(ind.meta.parents),
                 "origin": ind.meta.origin, "born_gen": ind.meta.born_gen},
        "current_total": float(ind.current_total),
        "est": None if ind.est is None else float(ind.est),
        "n_samples": int(ind.n_samples),
    }


def _ind_from_full(payload: Mapping[str, Any]) -> Individual:
    if not isinstance(payload, Mapping):
        raise ProtocolError("个体载荷必须是对象")
    try:
        genes, meta = payload["genes"], payload["meta"]
        ind = Individual(
            genes=Genes(solve_prompt=genes["solve_prompt"],
                        set_prompt=genes["set_prompt"],
                        self_check=genes["self_check"]),
            meta=Meta(id=meta["id"], parents=tuple(meta["parents"]),
                      origin=meta["origin"], born_gen=int(meta["born_gen"])),
        )
        ind.current_total = float(payload["current_total"])
        ind.est = None if payload.get("est") is None else float(payload["est"])
        ind.n_samples = int(payload["n_samples"])
    except KeyError as exc:
        raise ProtocolError(f"个体载荷缺字段: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"个体载荷类型不符: {exc}") from exc
    return ind


def state_to_payload(state: RunState) -> dict:
    """整份运行状态 → 可 JSON 化的对象。写入每代快照的 `state` 字段。"""
    return {
        "schema": STATE_SCHEMA,
        "run_id": state.run_id,
        "condition": state.cfg.condition,
        "config_fingerprint": state.cfg.semantic_fingerprint(),
        "phase": state.phase,
        "last_gen": int(state.last_gen),
        "frozen_manifest": state.frozen_manifest,
        "population": [_ind_full(i) for i in state.next_population],
        "ancestors": [_ind_full(i) for i in state.ancestors],
        "gene_history": state.gene_history.to_dict(),
        "dedup_set": sorted(state.dedup_set),
        # 按 task_id 排序：字典序不该取决于 dict 的插入顺序
        "challenges": [c.to_dict() for c in
                       sorted(state.challenges.values(), key=lambda c: c.task_id)],
    }


def state_from_payload(payload: Mapping[str, Any], *, cfg: Config, run_id: str,
                       rng: random.Random) -> RunState:
    """快照的 `state` 字段 → `RunState`。所有不匹配都是硬错误。"""
    if not isinstance(payload, Mapping):
        raise ProtocolError("state 必须是对象")
    if payload.get("schema") != STATE_SCHEMA:
        got = payload.get("schema")
        raise ProtocolError(
            f"state schema 为 {got!r}，期望 {STATE_SCHEMA!r}；"
            "这份快照可能由旧版本写出（早期只存 phase/last_gen，不含种群与历史），"
            "无法用于续跑"
        )
    if payload.get("run_id") != run_id:
        raise ProtocolError(
            f"快照 run_id={payload.get('run_id')!r} 与请求的 {run_id!r} 不符")
    if payload.get("condition") != cfg.condition:
        raise ProtocolError(
            f"快照 condition={payload.get('condition')!r} 与当前配置 "
            f"{cfg.condition!r} 不符；换个条件续跑等于把两套实验拼在一起")
    want = cfg.semantic_fingerprint()
    got_fp = payload.get("config_fingerprint")
    if got_fp != want:
        raise ProtocolError(
            f"配置指纹不符：快照 {str(got_fp)[:16]}… vs 当前 {want[:16]}…；"
            "续跑必须用与开跑时完全相同的配置（含 --gens 覆盖后的 n_gen）"
        )

    population = [_ind_from_full(p) for p in payload.get("population") or []]
    ancestors = [_ind_from_full(p) for p in payload.get("ancestors") or []]
    if len(population) != cfg.n_pop and not (not population and payload.get('last_gen') == cfg.n_gen):
        raise ProtocolError(
            f"快照种群 {len(population)} 人，配置 n_pop={cfg.n_pop}")
    if len(ancestors) != cfg.n_ancestor:
        raise ProtocolError(
            f"快照祖先 {len(ancestors)} 个，配置 n_ancestor={cfg.n_ancestor}")

    last_gen = payload.get("last_gen")
    if type(last_gen) is not int or not (0 <= last_gen <= cfg.n_gen):
        raise ProtocolError(f"snapshot last_gen 非法：{last_gen!r}")

    state = RunState(
        run_id=run_id,
        cfg=cfg,
        rng=rng,
        next_population=population,
        ancestors=ancestors,
        gene_history=GeneHistory.from_dict(payload.get("gene_history") or {}),
        dedup_set=set(payload.get("dedup_set") or []),
        challenges={},
    )
    for raw in payload.get("challenges") or []:
        try:
            ch = Challenge(task_id=raw["task_id"], born_gen=int(raw["born_gen"]),
                           window=int(raw["window"]))
            ch.state = raw["state"]
            ch.observations = [dict(o) for o in raw.get("observations") or []]
            ch.first_success_attempt = raw.get("first_success_attempt")
            ch.threshold_attempt = raw.get("threshold_attempt")
            ch.retired_reason = raw.get("retired_reason")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(f"挑战载荷不符: {exc}") from exc
        state.challenges[ch.task_id] = ch
    if set(state.challenges) == set() and cfg.challenge_gens:
        raise ProtocolError("快照里没有任何挑战对象，但配置要求注入挑战")

    state.phase = payload.get("phase") or "init"
    state.last_gen = last_gen
    state.frozen_manifest = dict(payload.get('frozen_manifest') or {})
    if not state.frozen_manifest:
        raise ProtocolError('Snapshot lacks frozen manifest')
    state.restored = True
    return state


def restore_run(*, cfg: Config, store: SnapshotStore, run_id: str | None = None,
                dataset: Dataset | None = None) -> RunState:
    """从最后一个 completed 快照恢复运行状态。

    `dataset` 传入时会核对签名集合：数据集换了就不允许续跑（否则第 8 代之后
    出的题可能和留出卷撞车，防泄漏是在装配时才建立的）。
    """
    run_id = run_id or store.run_id
    if store.run_id != run_id:
        store = SnapshotStore(root=store.root, run_id=run_id)

    completed = store.completed_gens()
    if not completed:
        raise ProtocolError(
            f"运行 {run_id} 没有任何 completed 快照，无法续跑；"
            "若要重新开始请用 --run，不要用 --resume")
    last = completed[-1]
    missing = [g for g in range(1, last + 1) if g not in completed]
    if missing:
        raise ProtocolError(
            f"快照不连续：第 {last} 代已完成，但缺 {missing}；"
            "从断档处续跑会跳过未执行的重组，必须人工确认")

    data = store.load_generation(last)
    payload = data.get("state")
    rng = rng_from_dict(data.get("rng"))
    state = state_from_payload(payload, cfg=cfg, run_id=run_id, rng=rng)
    if state.last_gen != last:
        raise ProtocolError(
            f"快照 g{last:02d} 内的 last_gen={state.last_gen}，与文件名不符")
    if state.phase != "completed":
        raise ProtocolError(f"只从 completed 恢复，快照 phase={state.phase!r}")

    if dataset is not None and dataset_manifest(dataset, cfg.n_gen) != state.frozen_manifest['dataset']:
        raise ProtocolError('Dataset content or group order changed; refusing resume')
    if source_manifest() != state.frozen_manifest['source']:
        raise ProtocolError('Code or prompts changed; refusing resume')
    return state


def branch_run(*, cfg, source_store, target_store, dataset):
    """Restore into a new demo branch; source snapshots and failures are immutable."""
    import copy
    if source_store.run_id == target_store.run_id or target_store.has_snapshots():
        raise ProtocolError('Resume requires a fresh, different branch run_id')
    state = restore_run(cfg=cfg, store=source_store, dataset=dataset)
    parent, last = state.run_id, state.last_gen
    manifest = copy.deepcopy(state.frozen_manifest)
    manifest.update(run_id=target_store.run_id, parent_run_id=parent, from_gen=last,
                    group_runs=[target_store.run_id], protocol_id=None)
    target_store.write_manifest(manifest)
    for gen in range(last + 1):
        snap = source_store.load_generation(gen)
        record = copy.deepcopy(snap['record'])
        record.update(run_id=target_store.run_id, inherited_from=parent)
        payload = copy.deepcopy(snap['state'])
        payload.update(run_id=target_store.run_id, frozen_manifest=manifest)
        target_store.save_generation(record, rng=rng_from_dict(snap['rng']), state_payload=payload)
        target_store.append_history(record)
    state.run_id = target_store.run_id
    state.frozen_manifest = manifest
    state.restored = False
    return state


def resume_report(store: SnapshotStore) -> dict:
    """续跑前的自检：会从哪一代接上、哪些代存在但不是可续跑点。"""
    completed = store.completed_gens()
    all_gens = store.gens()
    last = completed[-1] if completed else None
    return {
        "run_id": store.run_id,
        "completed": completed,
        "aborted_or_broken": [g for g in all_gens if g not in completed],
        "resume_from": None if last is None else last + 1,
        "last_completed": last,
    }
