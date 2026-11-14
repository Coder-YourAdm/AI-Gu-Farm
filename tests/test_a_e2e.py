# -*- coding: utf-8 -*-
"""A 视角的端到端：真实数据集 + mock 模型，真跑一代，验证判分链路给出 right。

B 的 `test_pilot_cli_uses_committed_dataset_and_real_http_contract` 证明了
「链路能跑完、调用数 78 对」，但它让模型一律返回 `{"value":"0"}`，只走到
wrong / unanswered 分支。这里让 mock 总是答对，补上 **right 分支**的端到端证据：

- 出题请求：从合法池里取一道题，算出 reference 并记住题面
- 解题请求：按题面反查 reference，返回正确字面量
- 断言：78 次调用、status=completed、快照里每一题都被判为 right
"""

import json
from pathlib import Path

import httpx
import pytest

from app import executor
from app.config import Config
from app.dataset_adapter import FileDataset
from app.llm import LLMClient
from app.store import SnapshotStore
from tests.test_llm_async import _ok_body


def _config(**changes):
    raw = json.loads(Path("config.dev.json").read_text(encoding="utf-8"))
    raw.update(model="test-model", base_url="https://api.example.com/v1",
               n_gen=1, challenge_gens=[], budget_amount=None, wall_limit_s=30)
    raw.update(changes)
    return Config.from_dict(raw)


def _answer_text(reference):
    if reference["kind"] == "integer":
        return reference["value"]
    return "%s/%s" % (reference["numerator"], reference["denominator"])


def _task_pool(occupied):
    """合法出题源：gamma 覆盖 integral，2 阶矩阵覆盖 det，全部避开已占用签名。

    两族**交错**排列：一代只有 6 道演化题，如果 gamma 全排在前面，
    祖先尺里就没有行列式题，det_failure 会是 null 而不是 0.0。
    """
    gammas, dets = [], []
    for c in (1, -1, 2, -2, 3):
        for n in range(1, 10):
            for rate in range(1, 9):
                task = {"template": "gamma", "params": {"c": c, "n": n, "rate": rate}}
                if executor.signature(task) not in occupied:
                    gammas.append(task)
    for a in (-9, -5, -3, -1, 1, 3, 5, 9):
        for b in (-8, -4, -2, 2, 4, 8):
            for cc in (-7, -3, 1, 6):
                for d in (-6, 1, 7):
                    task = {"template": "det", "params": {"matrix": [[a, b], [cc, d]]}}
                    if executor.signature(task) not in occupied:
                        dets.append(task)

    pool = []
    for i in range(max(len(gammas), len(dets))):
        if i < len(gammas):
            pool.append(gammas[i])
        if i < len(dets):
            pool.append(dets[i])
    return pool


def test_pilot_grades_correct_answers_as_right(tmp_path, monkeypatch):
    from app.run import main

    cfg = _config()
    monkeypatch.setenv(cfg.api_key_env, "test-secret")

    config_path = tmp_path / "pilot-config.json"
    config_path.write_text(json.dumps(cfg.to_dict()), encoding="utf-8")

    dataset = FileDataset()
    lookup = {}
    for gen in range(1, 21):
        for task in dataset.common_dev(gen):
            lookup[task["statement"]] = task["reference"]
    for task in dataset.challenge_pool():
        lookup[task["statement"]] = task["reference"]

    pool = iter(_task_pool(dataset.signatures()))
    issued = []
    answered = []

    def handler(request):
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        if body["max_tokens"] == cfg.max_tokens_setter:
            task = next(pool)
            statement = executor.statement(task["template"], task["params"])
            lookup[statement] = executor.solve_exact(task)
            issued.append(task["template"])
            return httpx.Response(200, json=_ok_body(json.dumps(task)))

        statement = user.split("\n答案格式：")[0]
        reference = lookup.get(statement)
        assert reference is not None, "解题请求的题面既不在出题记录里也不在数据集中"
        answered.append(statement)
        return httpx.Response(
            200, json=_ok_body(json.dumps({"value": _answer_text(reference)}))
        )

    class MockClient(LLMClient):
        def __init__(self, cfg_, **kwargs):
            super().__init__(cfg_, **kwargs, log_root=tmp_path / "logs",
                             _transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.llm.LLMClient", MockClient)

    code = main([
        "--config", str(config_path), "--pilot", "--run-id", "pilot-a-e2e",
        "--runs-root", str(tmp_path / "runs"),
    ])
    assert code == 0

    store = SnapshotStore(tmp_path / "runs", "pilot-a-e2e")
    summary = store.load_summary()
    assert summary["status"] == "completed"
    assert summary["call_total"] == 78
    assert len(answered) == 78 - len(issued)          # 出题 6 次，其余都是解题/祖先

    # 两族题型都出过，避免只测到 integral
    assert "gamma" in issued

    # 关键：72 个解题槽位全部被判 right（mock 每道题都答对）
    record = store.load_generation(1)["record"]
    assert record["slots"] == {"peer": 30, "common": 24, "ancestor": 18, "challenge": 0}
    statuses = [a["status"] for a in record["answers"]]
    assert len(statuses) == 72
    assert set(statuses) == {"right"}, "答对的题必须全部判 right，实际分布：%r" % (
        {s: statuses.count(s) for s in set(statuses)},)

    # 祖先指标：本代 6 道题全对 -> 四个值都是 0，且不因为缺题型变成 null
    anc = record["ancestor"]
    assert anc["integral_failure"] == 0.0
    assert anc["det_failure"] == 0.0
    assert anc["balanced_failure"] == 0.0
    assert anc["mixed_failure"] == 0.0
    # 字段名以 A 的 ancestor.generation_failures 为准（task_mix，不是 task_counts）
    assert anc["task_mix"] == {"integral": 3, "det": 3}
    assert "unanswered_count" in anc
