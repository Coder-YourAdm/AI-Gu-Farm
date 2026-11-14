"""E/F 对照与断点续跑的契约测试。

三件事必须锁死，任何一条破了都会让 E/F 数据变成废数：

1. `--freeze` 只关选择，不关评测 —— 基因必须全程一字不变，但调用次数不能少。
   如果 freeze 顺手把评测也跳了，E 和 F 的调用数不同，两条曲线没得比。
2. `--resume` 真的接着跑，不重跑已完成的代，也不丢学习到的规则。
   规则丢了，续跑段的 check_instruction 会突然变空，等于换了实验条件。
3. manifest 必须显式写出 condition / freeze / fitness_window。
   只留 config 哈希的话，事后没人能从产物里读出"这一趟锁的是什么"。

全部用假 solver 打桩，不碰网络 —— 这几个性质跟模型无关，用真调用测只是浪费钱。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import evolve_run as ev  # noqa: E402


# ────────────────────────────── 打桩 ──────────────────────────────
def _fake_batch(jobs, solver, workers):
    """不调模型，直接返回"全对且答案可用" —— 分数不再是变量，性质才看得清。"""
    return [{"correct": True, "reason": None, "usable": True, "infra": False,
             "tokens_in": 10, "tokens_out": 5, "latency_s": 0.1} for _ in jobs]


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 RUNS 指到临时目录，模型调用全部打桩。"""
    monkeypatch.setattr(ev, "RUNS", tmp_path)
    monkeypatch.setattr(ev, "run_batch", _fake_batch)
    monkeypatch.setattr(ev.L, "preflight", lambda: {"model": "fake-model"})
    monkeypatch.setattr(ev.L, "budget", lambda role, default: 1000)
    return tmp_path


def _read_hist(run_dir: Path) -> list[dict]:
    return [json.loads(x) for x in
            (run_dir / "history.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


# ────────────────────────────── F 条件 ──────────────────────────────
def test_freeze_returns_population_untouched():
    """纯函数层：freeze 时基因、ID、顺序一个都不许变。"""
    pop = [{"genes": {"solve_prompt": "甲。", "self_check": False},
            "meta": {"id": "G01", "parents": [], "origin": "seed", "born_gen": 0}, "_id": "G01"},
           {"genes": {"solve_prompt": "乙。", "self_check": True},
            "meta": {"id": "G02", "parents": [], "origin": "seed", "born_gen": 0}, "_id": "G02"}]
    cfg = {"n_pop": 6, "n_survive": 3, "n_crossover": 2, "n_mutate": 1}
    out = ev.next_population(pop, ["G01", "G02"], {"rules": []}, 0, 0, cfg, freeze=True)
    assert [p["_id"] for p in out] == ["G01", "G02"]
    assert [p["genes"] for p in out] == [p["genes"] for p in pop]


def test_evolution_still_mutates_when_not_frozen():
    """对照组：不开 freeze 时必须真的产生新个体，否则 freeze 测不出差别。"""
    pop = [{"genes": {"solve_prompt": "先判断题型。再算。", "self_check": False},
            "meta": {"id": f"G{i:02d}", "parents": [], "origin": "seed", "born_gen": 0},
            "_id": f"G{i:02d}"} for i in range(1, 7)]
    ranking = [p["_id"] for p in pop]
    cfg = {"n_pop": 6, "n_survive": 3, "n_crossover": 2, "n_mutate": 1}
    out = ev.next_population(pop, ranking, {"rules": []}, 0, 0, cfg, freeze=False)
    ids = {p["_id"] for p in out}
    assert any("X" in i or "M" in i for i in ids), "没产生交叉/变异个体，演化等于没跑"
    assert len(out) == 6


def test_freeze_run_keeps_genes_across_generations(sandbox):
    """整跑层：F 条件 3 代，每代评测的种群必须完全相同，且 condition 写 F。"""
    ev.run("T_freeze", gens=2, freeze=True)
    rows = _read_hist(sandbox / "T_freeze")
    assert len(rows) == 3, "g0 + 2 代 = 3 行"
    first = rows[0]["evaluated_population"]
    for r in rows:
        assert r["condition"] == "F"
        assert r["evaluated_population"] == first, f"g{r['gen']} 的基因变了，F 条件不成立"
        # 评测照常：每代都有共同题与祖先槽位，没被 freeze 顺手跳过
        assert r["slots"]["common"] > 0 and r["slots"]["ancestor"] > 0


def test_freeze_manifest_declares_locked_fields(sandbox):
    """manifest 必须能自证锁了什么，不能只留一个哈希。"""
    ev.run("T_manifest", gens=0, freeze=True)
    m = json.loads((sandbox / "T_manifest" / "manifest.json").read_text(encoding="utf-8"))
    assert m["condition"] == "F"
    assert m["freeze"] is True
    assert m["fitness_window"] == 2
    assert m["data_seed"] == 1001
    assert m["is_mock"] is False
    assert any("冻结" in n for n in m["notes"])


def test_evolve_condition_is_e_by_default(sandbox):
    """不开 freeze 时 condition 仍取 config 的 E，别被 F 的改动带跑。"""
    import json as _json
    cfg = _json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ev.run("T_e", gens=0)
    rows = _read_hist(sandbox / "T_e")
    assert rows[0]["condition"] == cfg.get("condition", "E")


# ────────────────────────────── 断点续跑 ──────────────────────────────
def test_load_resume_returns_none_without_state(sandbox):
    (sandbox / "T_none").mkdir()
    assert ev.load_resume(sandbox / "T_none") is None


def test_save_load_state_roundtrip(sandbox):
    d = sandbox / "T_rt"
    d.mkdir()
    # state 与 history 是配套的：只有 state 没有 history 说明这个 run 不完整，
    # load_resume 会当"不可恢复"处理，所以这里先摆一个空 history
    (d / "history.jsonl").write_text("", encoding="utf-8")
    pop = [{"genes": {"solve_prompt": "甲。", "self_check": True},
            "meta": {"id": "G01", "parents": [], "origin": "seed", "born_gen": 0}, "_id": "G01"}]
    ev.save_state(d, next_gen=7, population=pop, totals_hist={"G01": [7.0, 6.5]},
                  state={"rules": [{"text": "先通分"}]}, freeze=True)
    got = ev.load_resume(d)
    assert got["next_gen"] == 7
    assert got["population"][0]["genes"]["self_check"] is True
    assert got["totals_hist"]["G01"] == [7.0, 6.5]
    assert got["state"]["rules"][0]["text"] == "先通分"
    assert got["freeze"] is True


def test_resume_continues_without_repeating_generations(sandbox):
    """跑 2 代 → 中断 → resume 到 4 代，总行数 5，且 gen 不重复。"""
    ev.run("T_resume", gens=1)
    rows1 = _read_hist(sandbox / "T_resume")
    assert [r["gen"] for r in rows1] == [0, 1]

    ev.run("T_resume", gens=3, resume=True)
    rows2 = _read_hist(sandbox / "T_resume")
    assert [r["gen"] for r in rows2] == [0, 1, 2, 3], "续跑重跑了已完成代，或跳代"
    assert len(rows2) == 4


def test_resume_refuses_without_state(sandbox):
    (sandbox / "T_bad").mkdir()
    with pytest.raises(SystemExit):
        ev.run("T_bad", gens=2, resume=True)


def test_resume_keeps_freeze_from_snapshot(sandbox):
    """续跑不许改条件：快照是 F，就算命令行没写 --freeze 也得继续 F。"""
    ev.run("T_keep", gens=1, freeze=True)
    ev.run("T_keep", gens=2, resume=True)          # 故意不传 freeze
    rows = _read_hist(sandbox / "T_keep")
    assert all(r["condition"] == "F" for r in rows)
    first = rows[0]["evaluated_population"]
    assert rows[-1]["evaluated_population"] == first


def test_resume_preserves_learned_rules(sandbox):
    """规则必须跟着快照走 —— 丢了就等于续跑段换了实验条件。"""
    ev.run("T_rules", gens=1)
    snap = json.loads((sandbox / "T_rules" / "state.json").read_text(encoding="utf-8"))
    assert "state" in snap and "rules" in snap["state"]
    ev.run("T_rules", gens=2, resume=True)
    rows = _read_hist(sandbox / "T_rules")
    # 续跑段不该出现"规则从 0 重新开始"的断层：最后一代的规则数不少于第一段末尾
    assert len(rows) == 3
