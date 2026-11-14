"""B7：留出评测的验收。

分工方案 B7 的四条验收逐条对应：
1. 最小版（1 检查点 × 1 人 × 2 题）跑通
2. 全量 5×6×20 = 600 次/趟，与 dry-run 的 600 对齐
3. 计划顺序固定，重跑同一计划得到同一顺序
4. 主循环进程无法打开 holdout（接口层面无路径）

另加：检查点人口还原（种子 / 快照）、末代缺失写 NA、E−F 配对、落盘往返、
题型配额、真实 JSONL 装载时的 reference 重算。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.orchestrator as orchestrator
from app.config import Config, holdout_calls
from app.evaluate import (
    HOLDOUT_MIX,
    build_plan,
    check_mix,
    evaluate_run,
    expected_slots,
    format_report,
    index_tasks,
    load_evaluation,
    load_holdout_tasks,
    pair_evaluations,
    plan_seed,
    population_at_checkpoint,
    run_plan,
    summarize,
    write_evaluation,
)
from app.orchestrator import Dataset, run_generation
from app.genotype import load_seed_genotypes

from doubles import (FakeEngine, FakeDataset, FakeOperators, ScriptedChat,
                     build_state, seed_dedup)

DEV = Path("config.dev.json")
ALL_RIGHT = lambda slot_id, statement: "ok"


def _cfg(**over) -> Config:
    raw = json.loads(DEV.read_text(encoding="utf-8"))
    raw.update(over)
    return Config.from_dict(raw, source_path="in-memory")


def _holdout_tasks(engine: FakeEngine, n_integral: int = 10, n_det: int = 10) -> list[dict]:
    """10 道 gaussian_odd + 10 道 det，签名两两不同。

    积分刻意不用 gamma：脚本替身出题只用 gamma，用它会让「留出题与运行内题目
    签名重叠」这条警告误报，测不出真正想测的东西。
    """
    out: list[dict] = []
    for i in range(n_integral):
        raw = {"template": "gaussian_odd",
               "params": {"k": i % 5, "rate": 1 + i // 5, "c": 1}}
        out.append(_make(engine, raw, f"H{i + 1:02d}"))
    for i in range(n_det):
        raw = {"template": "det",
               "params": {"matrix": _det_matrix(i)}}
        out.append(_make(engine, raw, f"H{n_integral + i + 1:02d}"))
    return out


def _det_matrix(i: int) -> list[list[int]]:
    """10 个互不相同的 3 阶方阵，元素都在 -9..9 内（超出会被 executor 拒）。"""
    return [[i % 3 + 1, 1, 0], [0, i // 3 + 1, 1], [1, 0, 2]]


def _make(engine: FakeEngine, raw: dict, task_id: str) -> dict:
    task = engine.normalize_task(raw, task_id=task_id, source="holdout",
                                 by=None, gen=None)
    task["reference"] = engine.solve_exact(task)
    return task


async def _freeze_run(tmp_path: Path, *, gens: int, n_gen: int = 5):
    """跑 `gens` 代并把每代快照写进 tmp_path，模拟一次已冻结的运行。"""
    from app.store import SnapshotStore
    from app.runtime import new_run
    from app.runner import run_loop
    from app.integrity import task_digest

    cfg = _cfg(n_gen=n_gen, challenge_gens=[])
    engine = FakeEngine()
    dataset = FakeDataset(engine, n_gens=cfg.n_gen)
    dataset.holdout_digest = task_digest(_holdout_tasks(engine))
    state = new_run(cfg=cfg, dataset=dataset, run_id='run_e')
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    operators = FakeOperators(cfg)      # condition=E 要走重组
    store = SnapshotStore(root=str(tmp_path), run_id="run_e")

    class FailingChat:
        async def chat(self, *args, **kwargs):
            raise RuntimeError('simulated terminal failure')
    await run_loop(state=state, engine=engine, dataset=dataset, chat=chat,
                   operators=operators, store=store, until=gens)
    if gens < n_gen:
        await run_loop(state=state, engine=engine, dataset=dataset, chat=FailingChat(),
                       operators=operators, store=store)
    return cfg, engine, chat


# --------------------------------------------------------------------------
# 1 最小版：1 检查点 × 1 人 × 2 题
# --------------------------------------------------------------------------

async def test_minimal_plan_one_checkpoint_one_person_two_tasks():
    cfg = _cfg(n_gen=5, challenge_gens=[])
    engine = FakeEngine()
    tasks = _holdout_tasks(engine)
    tasks_by_id = index_tasks(tasks)

    plan = build_plan(run_id="run_mini",
                      individuals_by_checkpoint={0: ["G00_S1"]},
                      task_ids=["H01", "H02"])
    assert len(plan) == 1 * 1 * 2 == expected_slots({0: ["G00_S1"]}, 2)

    people = [p for p in load_seed_genotypes("prompts/seed_genotypes.json")
              if p.meta.id == "G00_S1"]
    genes_by = {(0, "G00_S1"): people[0].genes}
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)

    rows = await run_plan(plan=plan, genes_by=genes_by, tasks_by_id=tasks_by_id,
                          engine=engine, chat=chat, concurrency=2)
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"right"}

    result = summarize(run_id="run_mini", plan=plan, rows=rows,
                       tasks_by_id=tasks_by_id,
                       populations={0: people},
                       sources={0: "seed"}, condition="E", n_gen=5)
    cp = result["checkpoints"][0]
    assert cp["individuals"][0]["score"] == 2
    assert cp["mean"] == 2.0
    assert cp["per_task"]["H01"]["p_t"] == 1.0
    assert "检查点  0" in format_report(result)


# --------------------------------------------------------------------------
# 2 全量 600 与 dry-run 对齐
# --------------------------------------------------------------------------

def test_full_plan_matches_dry_run_600():
    cfg = _cfg()                     # n_gen=20 -> 检查点 0/5/10/15/20
    assert cfg.checkpoints == [0, 5, 10, 15, 20]
    ids = {cp: [f"G{cp:02d}_S{j + 1}" for j in range(6)] for cp in cfg.checkpoints}
    plan = build_plan(run_id="run_e", individuals_by_checkpoint=ids,
                      task_ids=[f"H{i + 1:02d}" for i in range(20)])
    assert len(plan) == 600 == holdout_calls(cfg) == 5 * 6 * 20
    assert expected_slots(ids, 20) == 600
    # 十五代时是 4 检查点 × 6 × 20 = 480
    cfg15 = _cfg(n_gen=15, challenge_gens=[4, 10])
    assert cfg15.checkpoints == [0, 5, 10, 15]
    assert holdout_calls(cfg15) == 480


# --------------------------------------------------------------------------
# 3 顺序固定
# --------------------------------------------------------------------------

def test_plan_order_is_frozen_and_interleaved():
    ids = {cp: [f"G{cp:02d}_S{j + 1}" for j in range(6)] for cp in (0, 5, 10)}
    tasks = [f"H{i + 1:02d}" for i in range(20)]
    a = build_plan(run_id="run_e", individuals_by_checkpoint=ids, task_ids=tasks)
    b = build_plan(run_id="run_e", individuals_by_checkpoint=ids, task_ids=tasks)
    assert [s.to_dict() for s in a] == [s.to_dict() for s in b]
    # 同一计划换个插入顺序也该得到同一结果（顺序只取决于集合与 seed）
    rev = {cp: list(reversed(v)) for cp, v in reversed(list(ids.items()))}
    c = build_plan(run_id="run_e", individuals_by_checkpoint=rev,
                   task_ids=list(reversed(tasks)))
    assert [s.to_dict() for s in a] == [s.to_dict() for s in c]
    # 换 run_id 就是另一份顺序（否则 E/F 会共用一条顺序，看不出干扰）
    d = build_plan(run_id="run_f", individuals_by_checkpoint=ids, task_ids=tasks)
    assert [s.to_dict() for s in a] != [s.to_dict() for s in d]
    assert plan_seed("run_e") == plan_seed("run_e") != plan_seed("run_f")

    # 题目与检查点必须打散：前 30 个槽位里至少出现 3 个不同检查点
    assert len({s.checkpoint for s in a[:30]}) >= 3
    # 整条计划里同一个检查点也不该成块出现（最长连续段远小于 1/3 总量）
    longest = run = 1
    for prev, cur in zip(a, a[1:]):
        run = run + 1 if cur.checkpoint == prev.checkpoint else 1
        longest = max(longest, run)
    assert longest < len(a) // 3


# --------------------------------------------------------------------------
# 4 接口层面隔离：主循环拿不到留出题正文
# --------------------------------------------------------------------------

def test_main_loop_cannot_reach_holdout():
    """隔离是接口事实，不是口头约定：看 import 图，而不是看有没有出现过这个词。"""
    import ast

    src = Path("app/orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m == "app.evaluate" or "holdout" in m for m in imported)
    assert "holdout" not in src.lower()      # 连字符串常量都没有
    assert "data/" not in src                # 不读任何数据文件路径

    # Dataset 协议只暴露「共同卷 + 挑战池 + 签名索引」，没有留出入口
    assert not any("holdout" in name.lower() for name in dir(Dataset))
    assert not hasattr(orchestrator, "load_holdout_tasks")
    assert not hasattr(orchestrator, "evaluate_run")


async def test_prepare_evaluation_sends_no_calls_and_writes_plan(tmp_path):
    """先审计划再花钱：prepare 阶段一次模型调用都不该发。"""
    from app.evaluate import prepare_evaluation, write_plan

    cfg, engine, _ = await _freeze_run(tmp_path, gens=5, n_gen=5)
    tasks = _holdout_tasks(engine)
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)

    prep = prepare_evaluation(run_id="run_e", cfg=cfg, tasks=tasks,
                              store_root=tmp_path)
    assert chat.count() == 0                      # 一个请求都没发
    assert len(prep["plan"]) == 240
    assert sorted(prep["populations"]) == [0, 5]
    assert prep["missing"] == []

    path = write_plan(prep["plan"], run_id="run_e", root=tmp_path,
                      order_seed=prep["order_seed"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["n_slots"] == 240
    assert saved["order_seed"] == prep["order_seed"]
    assert [s["slot_id"] for s in saved["plan"]] == [s.slot_id for s in prep["plan"]]
    # 重跑一次计划：顺序必须一模一样
    again = prepare_evaluation(run_id="run_e", cfg=cfg, tasks=tasks,
                               store_root=tmp_path)
    assert [s.to_dict() for s in again["plan"]] == [s.to_dict() for s in prep["plan"]]


# --------------------------------------------------------------------------
# 5 检查点人口：0 读种子，k>0 读快照
# --------------------------------------------------------------------------

async def test_population_at_checkpoint(tmp_path):
    await _freeze_run(tmp_path, gens=5, n_gen=5)

    people0, src0 = population_at_checkpoint(store_root=tmp_path, run_id="run_e",
                                             checkpoint=0,
                                             seed_path="prompts/seed_genotypes.json")
    assert src0 == "snapshot" and len(people0) == 6
    assert {p.meta.id for p in people0} == {f"G00_S{j + 1}" for j in range(6)}

    people5, src5 = population_at_checkpoint(store_root=tmp_path, run_id="run_e",
                                             checkpoint=5)
    assert src5 == "snapshot" and len(people5) == 6
    assert all(p.meta.born_gen <= 4 for p in people5)   # 末代不重组


# --------------------------------------------------------------------------
# 6 端到端：两检查点 × 6 人 × 20 题
# --------------------------------------------------------------------------

async def test_evaluate_run_end_to_end(tmp_path):
    cfg, engine, _ = await _freeze_run(tmp_path, gens=5, n_gen=5)
    tasks = _holdout_tasks(engine)

    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    result = await evaluate_run(run_id="run_e", cfg=cfg, engine=engine, chat=chat,
                                tasks=tasks, store_root=tmp_path)

    assert result["n_holdout"] == 20
    assert [c["checkpoint"] for c in result["checkpoints"]] == [0, 5]
    assert result["n_slots"] == 2 * 6 * 20 == 240
    assert len(result["plan"]) == 240

    for cp in result["checkpoints"]:
        assert cp["mean"] == 20.0
        assert cp["integral_mean"] == 10.0
        assert cp["det_mean"] == 10.0
        assert all(p["score"] == 20 for p in cp["individuals"])
        assert all(t["p_t"] == 1.0 for t in cp["per_task"].values())

    # 留出题没与运行内题目撞签名
    assert not any("签名重叠" in w for w in result["warnings"])

    # 落盘往返
    path = write_evaluation(result, root=tmp_path)
    assert path.exists()
    again = load_evaluation("run_e", root=tmp_path)
    assert again["n_slots"] == 240 and again["checkpoints"][0]["mean"] == 20.0


async def test_evaluate_run_counts_wrong_answers(tmp_path):
    """全部答错：0–20 分数与题型分层都要真实落到 0，且分母仍是 20。"""
    cfg, engine, _ = await _freeze_run(tmp_path, gens=5, n_gen=5)
    tasks = _holdout_tasks(engine)
    chat = ScriptedChat(cfg, engine,
                        answer_policy=lambda slot_id, st: "wrong")
    result = await evaluate_run(run_id="run_e", cfg=cfg, engine=engine, chat=chat,
                                tasks=tasks, store_root=tmp_path)
    for cp in result["checkpoints"]:
        assert cp["mean"] == 0.0
        assert all(p["statuses"]["wrong"] == 20 for p in cp["individuals"])
        assert all(t["p_t"] == 0.0 for t in cp["per_task"].values())


# --------------------------------------------------------------------------
# 7 末代缺失写 NA，不用最后有效代冒充
# --------------------------------------------------------------------------

async def test_missing_final_checkpoint_is_na(tmp_path):
    cfg, engine, _ = await _freeze_run(tmp_path, gens=2, n_gen=5)   # 只跑了 2 代
    tasks = _holdout_tasks(engine)
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    result = await evaluate_run(run_id="run_e", cfg=cfg, engine=engine, chat=chat,
                                tasks=tasks, store_root=tmp_path)

    by_cp = {c["checkpoint"]: c for c in result["checkpoints"]}
    assert set(by_cp) == {0, 5}
    assert by_cp[0]["mean"] == 20.0          # 第 0 代照常评测
    assert by_cp[5]["na"] is True            # 锁定的末代 -> NA
    assert by_cp[5]["mean"] is None
    assert result["n_slots"] == 1 * 6 * 20   # NA 不产生槽位
    assert any("NA" in w for w in result["warnings"])


async def test_nonexistent_run_cannot_evaluate(tmp_path):
    """没有冻结运行不能凭当前种子文件生成评测。"""
    from app.llm import ProtocolError
    cfg = _cfg(n_gen=5, challenge_gens=[])
    engine = FakeEngine()
    chat = ScriptedChat(cfg, engine, answer_policy=ALL_RIGHT)
    with pytest.raises(ProtocolError):
        await evaluate_run(run_id='nope', cfg=cfg, engine=engine, chat=chat,
                           tasks=_holdout_tasks(engine), store_root=tmp_path)
    assert chat.count() == 0


# --------------------------------------------------------------------------
# 8 E/F 配对
# --------------------------------------------------------------------------

def test_pair_evaluations():
    e = {"run_id": "E1", "n_holdout": 20, "checkpoints": [
        {"checkpoint": 0, "mean": 8.0}, {"checkpoint": 10, "mean": 12.0},
        {"checkpoint": 20, "mean": 15.0}]}
    f = {"run_id": "F1", "n_holdout": 20, "checkpoints": [
        {"checkpoint": 0, "mean": 8.0}, {"checkpoint": 10, "mean": 9.0},
        {"checkpoint": 20, "mean": None}]}       # 末代 NA

    shared = dict(seed=42, data_seed=7, n_gen=20, holdout_digest='h',
                  comparison_fingerprint='c', initial_fingerprint='i',
                  ancestor_fingerprint='a', protocol_id='p')
    e.update(shared, condition='E')
    f.update(shared, condition='F')
    pair = pair_evaluations(e, f)
    rows = {r["checkpoint"]: r for r in pair["rows"]}
    assert rows[10]["diff"] == 3.0
    assert rows[10]["e_delta_from_g0"] == 4.0
    assert rows[10]["f_delta_from_g0"] == 1.0
    assert rows[20]["diff"] is None              # NA 不做差，不拿别的代补
    assert rows[20]["e_delta_from_g0"] == 7.0
    assert pair["locked_gen"] == 20
    assert pair["final"]["diff"] is None


# --------------------------------------------------------------------------
# 9 题型配额与 JSONL 装载
# --------------------------------------------------------------------------

def test_check_mix_rejects_wrong_composition():
    engine = FakeEngine()
    tasks = _holdout_tasks(engine, n_integral=9, n_det=11)
    with pytest.raises(ValueError, match="题型组成不符"):
        check_mix(index_tasks(tasks))
    check_mix(index_tasks(tasks), {"integral": 9, "det": 11})
    assert HOLDOUT_MIX == {"integral": 10, "det": 10}


def test_load_holdout_tasks_recomputes_reference(tmp_path):
    engine = FakeEngine()
    path = tmp_path / "holdout.jsonl"
    lines = [json.dumps({"id": f"H{i + 1:02d}", "template": "gaussian_odd",
                         "params": {"k": i % 5, "rate": 1 + i // 5, "c": 1},
                         "reference": {"kind": "integer", "value": "999"}})
             for i in range(10)]
    lines += [json.dumps({"template": "det",
                          "params": {"matrix": _det_matrix(i)}})
              for i in range(10)]
    path.write_text("\n".join(lines), encoding="utf-8")

    tasks = load_holdout_tasks(path, engine)
    assert len(tasks) == 20
    assert tasks[0]["id"] == "H01"
    assert tasks[0]["reference"] != {"kind": "integer", "value": "999"}   # 重算，不采信
    assert tasks[10]["id"] == "H11"                                       # 自动补 ID

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"template": "gamma",
                              "params": {"n": 0, "rate": 2, "c": 1}}), encoding="utf-8")
    with pytest.raises(ValueError, match="未通过校验"):
        load_holdout_tasks(bad, engine, expect_mix=False)
