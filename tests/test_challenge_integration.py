"""挑战题接入主循环的契约测试。

状态机本身由 test_challenge.py 覆盖；这里测的是**接入**，也就是最容易
接错、且错了不会报错只会产出废数的四个地方：

1. 注入时点。g8 与 g15 是 challenge_gens 定的，早一代晚一代都会让
   「第 8 代注入」这句对外说法变成假话。
2. 赏金必须进本代 total。只写进记录不进 total，赏金就不产生选择压力，
   与大纲「挑战赏金产生选择压力」相违 —— 这种错跑完全程也看不出来。
3. 退役后必须停止注入。否则同一道题答了 20 代，p_t 和 attempt 全废。
4. manifest 只能写 ID/模板/签名，**不能写 statement 与 reference**。
   manifest 进仓库，题面进去等于把挑战题泄给了解题方。

全部打桩，不碰网络。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import evolve_run as ev  # noqa: E402


def _fake_batch(jobs, solver, workers):
    """全对且答案可用。分数固定，才能把「赏金有没有进 total」单独看出来。"""
    return [{"correct": True, "reason": None, "usable": True, "infra": False,
             "tokens_in": 10, "tokens_out": 5, "latency_s": 0.1} for _ in jobs]


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RUNS", tmp_path)
    monkeypatch.setattr(ev, "run_batch", _fake_batch)
    monkeypatch.setattr(ev.L, "preflight", lambda: {"model": "fake-model"})
    monkeypatch.setattr(ev.L, "budget", lambda role, default: 1000)
    return tmp_path


def _hist(run_id: str) -> dict[int, dict]:
    p = Path(ev.RUNS) / run_id / "history.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {r["gen"]: r for r in rows}


def _manifest(run_id: str) -> dict:
    return json.loads((Path(ev.RUNS) / run_id / "manifest.json").read_text(encoding="utf-8"))


# ────────────────────────── 注入时点 ──────────────────────────

def test_challenge_injected_exactly_at_born_gens(sandbox):
    ev.run("T_chal", gens=20)
    by_gen = _hist("T_chal")

    # g0–g7 与 g9–g14 一律没有挑战槽位
    for g in list(range(8)) + list(range(9, 15)):
        assert by_gen[g]["slots"]["challenge"] == 0, f"g{g} 不该有挑战槽位"
    # 两个 born_gen 各注入一次，固定六槽（全员必答）
    assert by_gen[8]["slots"]["challenge"] == 6
    assert by_gen[15]["slots"]["challenge"] == 6

    assert [c["born_gen"] for c in by_gen[8]["challenges"]] == [8]
    assert len(by_gen[15]["challenges"]) == 2      # 第一道退役后仍留在记录里


def test_challenge_retires_and_stops_injection(sandbox):
    """全员答对 = 6 人 >= 阈值 3，第一代就退役；之后不许再问同一道题。"""
    ev.run("T_retire", gens=20)
    by_gen = _hist("T_retire")

    c8 = by_gen[8]["challenges"][0]
    assert c8["state"] == "retired"
    assert c8["retired_reason"] == "threshold_reached"
    assert c8["threshold_attempt"] == 1
    assert len(c8["observations"]) == 1

    # g9 起不再注入，观测也不该再增长
    assert by_gen[9]["slots"]["challenge"] == 0
    assert len(by_gen[14]["challenges"][0]["observations"]) == 1


def test_bounty_enters_total_but_not_before_challenge(sandbox):
    """赏金进本代 total，且只在挑战代出现。

    全对时 solve=5.0、set=2.0；挑战代再加 bounty=2.0 → 9.0。
    如果赏金只在记录里没进 total，g8 的 total 会是 7.0 —— 这个差别
    不会让程序报错，只会让整个淘汰过程失去挑战带来的选择压力。
    """
    ev.run("T_bounty", gens=20)
    by_gen = _hist("T_bounty")

    assert by_gen[7]["individuals"][0]["total"] == 7.0
    assert by_gen[7]["individuals"][0]["bounty"] == 0.0

    assert by_gen[8]["individuals"][0]["bounty"] == 2.0
    assert by_gen[8]["individuals"][0]["total"] == 9.0

    # 挑战已退役，g9 回落
    assert by_gen[9]["individuals"][0]["bounty"] == 0.0
    assert by_gen[9]["individuals"][0]["total"] == 7.0


# ────────────────────────── manifest 与题面隔离 ──────────────────────────

def test_manifest_records_challenge_but_never_leaks_statement(sandbox):
    """manifest 记 ID/模板/签名，绝不写题面与参考答案。"""
    ev.run("T_man", gens=20)
    m = _manifest("T_man")

    assert m["challenge_gens"] == [8, 15]
    assert m["challenge_window"] == 6
    assert m["threshold_correct"] == 3
    assert [t["born_gen"] for t in m["challenge_tasks"]] == [8, 15]
    assert all(t["task_id"] for t in m["challenge_tasks"])

    blob = json.dumps(m, ensure_ascii=False)
    assert '"statement"' not in blob, "manifest 里出现题面字段，等于把挑战题泄了"
    assert '"reference"' not in blob, "manifest 里出现参考答案，这一轮挑战作废"

    # 汇总口径：正常退役与截断分开
    s = m["challenge_summary"]
    assert s["total"] == 2
    assert s["retired_normal"] == 2
    assert s["truncated"] == 0


def test_challenge_state_survives_resume(tmp_path, monkeypatch):
    """断在挑战代之后续跑，不许把同一道题再注入一次。

    重跑 g4 时 plan() 依据的是 state.json 里的 challenges，不是内存标记 ——
    内存标记在 --resume 时已经不存在了。判据错了的表现是「同一道题被答了
    两次、attempt 多推一格」，而程序不会报错，只会让窗口提前关闭。
    """
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["challenge_gens"] = [4]
    cfg["challenge_window"] = 4          # 4 + 4 - 1 = 7 <= n_gen=8，窗口完整
    monkeypatch.setattr(ev, "RUNS", tmp_path)
    monkeypatch.setattr(ev, "load_cfg", lambda: cfg)
    monkeypatch.setattr(ev.L, "preflight", lambda: {"model": "fake-model"})
    monkeypatch.setattr(ev.L, "budget", lambda role, default: 1000)

    # 每代 24 共同 + 10 祖先 = 34；挑战代再加 6 → 40。
    # 卡在 176 次：g0–g3(136) + g4(40) 刚好跑完，g5 第一批就崩。
    meter = {"n": 0, "limit": 176}

    def flaky(jobs, solver, workers):
        meter["n"] += len(jobs)
        if meter["n"] > meter["limit"]:
            raise RuntimeError("模拟中断")
        return _fake_batch(jobs, solver, workers)

    monkeypatch.setattr(ev, "run_batch", flaky)
    with pytest.raises(RuntimeError):
        ev.run("T_res", gens=8)

    done = _hist("T_res")
    assert 4 in done, "g4 应已完整落盘"
    assert done[4]["challenges"][0]["state"] == "retired"
    assert 5 not in done

    # 恢复后跑完
    monkeypatch.setattr(ev, "run_batch", _fake_batch)
    ev.run("T_res", gens=8, resume=True)
    rows = _hist("T_res")

    assert sorted(rows) == list(range(9)), "续跑应补齐 g5–g8，且不重写已完成代"
    assert len(rows[4]["challenges"][0]["observations"]) == 1, \
        "续跑把 g4 的挑战又答了一遍 —— 幂等判据失效"
    assert rows[5]["slots"]["challenge"] == 0


# ────────────────────────── 窗口越界 ──────────────────────────

def test_short_run_declares_no_challenge_instead_of_truncating(sandbox):
    """5 代跑不下 [8,15]+window 6，必须整轮不挑战，而不是把窗口截短。

    截短后最后一个窗口观察不满 6 次，退役原因永远走不到 window_closed，
    那批题的统计口径就废了；而废了的数据比没有数据更糟 —— 它看起来是有的。
    """
    ev.run("T_short", gens=5)
    m = _manifest("T_short")
    by_gen = _hist("T_short")

    assert m["challenge_gens"] == []
    assert m["challenge_tasks"] == []
    assert all(r["slots"]["challenge"] == 0 for r in by_gen.values())
    assert any("未注入" in n for n in m["notes"])
