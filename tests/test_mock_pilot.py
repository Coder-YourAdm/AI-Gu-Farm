# -*- coding: utf-8 -*-
"""多代 mock pilot：把「跑一趟完整实验」这件事在离线环境下走通一遍。

已有测试各管一段：
- `test_a_e2e`：一代、78 次调用、判分 right 分支
- `test_b_regressions`：断点、恢复、算子回退等单点回归

缺的是**把它们串起来**：连续 3 代（含算子交叉/变异、含挑战代）、快照齐全、
能导出并通过 export 的敏感内容自检、能从中断点续跑出一个 demo 分支。
这条路径是 pilot 之前唯一还没跑过的，而它一旦在真模型下坏掉，修起来最贵。

mock 模型的行为：
- 出题请求（max_tokens=setter）：从合法池取一道未占用签名的题，返回题面 JSON
- 解题请求（max_tokens=solver）：按题面反查 reference，返回正确字面量
- 算子请求（max_tokens=operator）：对父代/母本基因做等长改写（交叉）/ 单字段轮转（变异）
"""

import json
from pathlib import Path

import httpx
import pytest

from app import executor
from app.config import Config
from app.dataset_adapter import FileDataset
from app.export import export_run, verify
from app.llm import LLMClient
from app.store import SnapshotStore
from tests.test_llm_async import _ok_body

from tests.test_a_e2e import _answer_text, _task_pool


def _config(**changes):
    raw = json.loads(Path("config.dev.json").read_text(encoding="utf-8"))
    raw.update(model="test-model", base_url="https://api.example.com/v1",
               n_gen=3, challenge_gens=[2], challenge_window=1,
               budget_amount=None, wall_limit_s=600)
    raw.update(changes)
    return Config.from_dict(raw)


def _rotate(text: str) -> str:
    """等长轮转：长度不变、标点数不变，满足「只改写不加句」的机器判据。"""
    return text[1:] + text[0] if len(text) > 1 else text + "甲"


class _MockBrain:
    """按请求类型分派的假模型，同时记录所有调用，供断言使用。"""

    def __init__(self, cfg, dataset):
        self.cfg = cfg
        self.lookup = {}
        for gen in range(1, 21):
            for task in dataset.common_dev(gen):
                self.lookup[task["statement"]] = task["reference"]
        for task in dataset.challenge_pool():
            self.lookup[task["statement"]] = task["reference"]
        self.pool = iter(_task_pool(dataset.signatures()))
        self.issued = []
        self.answered = []
        self.operator_calls = 0

    def _genes_for_crossover(self, payload):
        a, b = payload["parent_a"], payload["parent_b"]
        child = dict(a)
        for _ in range(len(a["solve_prompt"]) + 1):
            child["solve_prompt"] = _rotate(child["solve_prompt"])
            if child != a and child != b:
                return child
        raise AssertionError("交叉造不出与双亲都不同的子代")

    def _genes_for_mutation(self, payload):
        mother, field = payload["mother"], payload["field_to_mutate"]
        child = dict(mother)
        child[field] = (not mother[field]) if field == "self_check" else _rotate(mother[field])
        return child

    def handle(self, request):
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        budget = body["max_tokens"]

        if budget == self.cfg.max_tokens_operator:
            payload = json.loads(user)
            self.operator_calls += 1
            genes = (self._genes_for_crossover(payload) if "parent_a" in payload
                     else self._genes_for_mutation(payload))
            return httpx.Response(200, json=_ok_body(json.dumps(genes, ensure_ascii=False)))

        if budget == self.cfg.max_tokens_setter:
            task = next(self.pool)
            statement = executor.statement(task["template"], task["params"])
            self.lookup[statement] = executor.solve_exact(task)
            self.issued.append(task["template"])
            return httpx.Response(200, json=_ok_body(json.dumps(task)))

        statement = user.split("\n答案格式：")[0]
        reference = self.lookup.get(statement)
        assert reference is not None, "解题请求的题面既不在出题记录里也不在数据集中"
        self.answered.append(statement)
        return httpx.Response(200, json=_ok_body(json.dumps({"value": _answer_text(reference)})))


def _patch_client(monkeypatch, brain, log_root):
    class MockClient(LLMClient):
        def __init__(self, cfg_, **kwargs):
            super().__init__(cfg_, **kwargs, log_root=log_root,
                             _transport=httpx.MockTransport(brain.handle))

    monkeypatch.setattr("app.llm.LLMClient", MockClient)


def _write_config(tmp_path, cfg):
    path = tmp_path / "pilot-config.json"
    path.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")
    return path


def test_three_generations_with_operator_and_challenge_then_export(tmp_path, monkeypatch):
    from app.run import main

    cfg = _config()
    monkeypatch.setenv(cfg.api_key_env, "test-secret")
    dataset = FileDataset()
    brain = _MockBrain(cfg, dataset)
    _patch_client(monkeypatch, brain, tmp_path / "logs")

    code = main(["--config", str(_write_config(tmp_path, cfg)), "--pilot",
                 "--run-id", "mock-3gen", "--runs-root", str(tmp_path / "runs")])
    assert code == 0, "多代 mock pilot 未跑完"

    store = SnapshotStore(tmp_path / "runs", "mock-3gen")
    summary = store.load_summary()
    assert summary["status"] == "completed"
    assert summary["completed"] == 3
    assert summary["aborted"] == []

    # 3 代 = 6 人出题 + 60 互答 + 24 共同卷 + 54 祖先的固定结构，再加上挑战与算子
    gens = [store.load_generation(g)["record"] for g in (1, 2, 3)]
    for rec in gens:
        assert rec["status"] == "completed"
        assert rec["slots"]["peer"] == 30
        assert rec["slots"]["common"] == 24
        assert rec["slots"]["ancestor"] == 18
        assert len(rec["individuals"]) == cfg.n_pop
        assert len(rec["ranking"]) == cfg.n_pop

    # 算子确实被调用过：2 次跨代繁殖 × (交叉 2 + 变异 1)
    assert brain.operator_calls == 2 * (cfg.n_crossover + cfg.n_mutate)

    # 挑战在第 2 代出生，窗口 1 代 -> 只有第 2 代有挑战槽位
    assert gens[0]["slots"]["challenge"] == 0
    assert gens[1]["slots"]["challenge"] >= 1
    assert gens[2]["slots"]["challenge"] == 0

    # 每一代的每个人都有名次，且没有重复 ID
    for rec in gens:
        assert len(set(rec["ranking"])) == cfg.n_pop
        assert {row["id"] for row in rec["individuals"]} == set(rec["ranking"])

    # 导出 + 敏感内容自检：能导出、且不含 reference / holdout / raw_response / api_key
    # 第 0 代（初始群体）不进公开历史，所以 3 代就是 3 条
    out = tmp_path / "export.json"
    payload = export_run(store.run_dir, out)
    assert payload["n_gen"] == 3
    assert [g["gen"] for g in payload["generations"]] == [1, 2, 3]
    assert payload["run_id"] == "mock-3gen"
    assert payload["problems"] == []
    assert len(payload["lineage"]) >= cfg.n_survive
    assert verify(out) == 0
    text = out.read_text(encoding="utf-8")
    for banned in ('"reference"', "holdout", '"raw_response"', "api_key", "REPLACE_WITH"):
        assert banned not in text, banned

    # 文档里写的是 `--run runs/<run_id>`，引擎把快照放在 runs/snapshots/<run_id>：
    # 两种写法都必须能导出，否则按文档敲命令的人会当场卡住。
    from app.export import main as export_main
    assert export_main(["--run", str(tmp_path / "runs" / "mock-3gen"),
                        "--out", str(tmp_path / "export2.json")]) == 0


def test_resume_into_demo_branch_keeps_history(tmp_path, monkeypatch):
    from app.run import main

    cfg = _config()
    monkeypatch.setenv(cfg.api_key_env, "test-secret")
    dataset = FileDataset()
    brain = _MockBrain(cfg, dataset)
    _patch_client(monkeypatch, brain, tmp_path / "logs")
    config_path = _write_config(tmp_path, cfg)
    runs = tmp_path / "runs"

    # 先只跑 1 代，模拟「跑到一半被打断」
    assert main(["--config", str(config_path), "--pilot", "--run-id", "mock-part",
                 "--to-gen", "1", "--runs-root", str(runs)]) == 0
    store = SnapshotStore(runs, "mock-part")
    # 第 0 代是初始群体，也落快照（g00.json），所以 completed_gens 从 0 起
    assert store.completed_gens() == [0, 1]
    calls_before = store.load_summary()["call_total"]

    # 续跑必须落到新分支目录，源快照不可改写
    assert main(["--config", str(config_path), "--resume", "mock-part",
                 "--branch", "mock-part-b1", "--to-gen", "3",
                 "--runs-root", str(runs)]) == 0

    src = SnapshotStore(runs, "mock-part")
    dst = SnapshotStore(runs, "mock-part-b1")
    assert src.completed_gens() == [0, 1], "源运行被续跑改写了"
    assert dst.completed_gens() == [0, 1, 2, 3]

    manifest = dst.load_manifest()
    assert manifest["parent_run_id"] == "mock-part"
    assert manifest["from_gen"] == 1

    # 第 1 代是继承来的，记录里标了 inherited_from；第 2、3 代是新跑的
    gen1 = dst.load_generation(1)["record"]
    assert gen1["inherited_from"] == "mock-part"
    gen2 = dst.load_generation(2)["record"]
    assert "inherited_from" not in gen2
    assert dst.load_summary()["call_total"] > calls_before


def test_config_without_hard_limit_is_rejected(tmp_path, monkeypatch):
    """没设 wall_limit_s / max_calls 时，真实运行必须在装配阶段就退出。"""
    from app.run import main

    cfg = _config(wall_limit_s=None, max_calls=None)
    monkeypatch.setenv(cfg.api_key_env, "test-secret")
    _patch_client(monkeypatch, _MockBrain(cfg, FileDataset()), tmp_path / "logs")
    code = main(["--config", str(_write_config(tmp_path, cfg)), "--pilot",
                 "--run-id", "no-limit", "--runs-root", str(tmp_path / "runs")])
    assert code == 2
    assert not (tmp_path / "runs" / "no-limit" / "summary.json").exists()
