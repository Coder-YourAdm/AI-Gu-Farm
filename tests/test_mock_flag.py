"""假数据自证：三张页面的 `is_mock` 链路。

为什么值得单独一个测试文件
--------------------------
`web/history.json` 的 20 代曲线、`web/evolve.json` 的 4/10 → 8/10、
`web/result.json` 的留出分数——这三处长得和真数据完全一样。
如果 `is_mock` 缺失或判断方向反了，页面会把假数据渲染成绿标，
而**没有人能靠肉眼看出来**（技术规格 §10 L598：不把 demo 分支计入重复实验）。

所以这条链路的验收不是「有 is_mock 字段」，是三个方向：
  1. 缺失时不能落到「真」那一侧（fail-safe）
  2. 类型必须是 bool，字符串 "false" 在 JS 里是真值
  3. 生产者（export.py / demo.py）必须自己算出来，不能靠页面猜
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import evolve_schema                      # noqa: E402
from app.export import resolve_is_mock, export_run  # noqa: E402

WEB = ROOT / "web"


# ─────────────────────────────────────────────────────────────
# 1. 导出器的判定方向（宁误标假，不误标真）
# ─────────────────────────────────────────────────────────────
def test_no_manifest_defaults_to_mock(tmp_path):
    """没有 manifest 能证明是真跑，就必须标假。"""
    is_mock, why = resolve_is_mock(tmp_path)
    assert is_mock is True
    assert "manifest" in why


def test_manifest_real_unlocks_green(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"backend": "real"}), encoding="utf-8")
    is_mock, why = resolve_is_mock(tmp_path)
    assert is_mock is False
    assert "real" in why


def test_manifest_mock_stays_mock(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"backend": "mock"}), encoding="utf-8")
    assert resolve_is_mock(tmp_path)[0] is True


def test_explicit_is_mock_wins(tmp_path):
    """manifest 显式写 is_mock 时以它为准（给 B 留一个直接覆盖的口子）。"""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"is_mock": False, "backend": "mock"}), encoding="utf-8")
    assert resolve_is_mock(tmp_path)[0] is False


def test_broken_manifest_is_not_trusted(tmp_path):
    (tmp_path / "manifest.json").write_text("{ not json", encoding="utf-8")
    is_mock, why = resolve_is_mock(tmp_path)
    assert is_mock is True
    assert "JSON" in why


def test_fake_dirname_is_mock(tmp_path):
    d = tmp_path / "fake_s42"
    d.mkdir()
    assert resolve_is_mock(d)[0] is True


# ─────────────────────────────────────────────────────────────
# 2. evolve 契约：缺字段 / 类型错都必须被拒
# ─────────────────────────────────────────────────────────────
def _minimal_evolve_payload():
    return {
        "schema": evolve_schema.SCHEMA,
        "schema_version": evolve_schema.SCHEMA_VERSION,
        "pair_id": "p",
        "is_mock": True,
        "frozen": {"config_sha256": "a", "prompts_sha256": "b",
                   "identical_across_passes": True},
        "metric": {"name": "n", "n_tasks": 10},
        "passes": [
            {"pass": 1, "duration_s": 1, "correct": 4, "total": 10,
             "accuracy": 0.4, "per_task": [], "state_version": "v0"},
            {"pass": 2, "duration_s": 1, "correct": 8, "total": 10,
             "accuracy": 0.8, "per_task": [], "state_version": "v1"},
        ],
        "learned": {"rules_added": [], "memories_added": [], "params_changed": []},
        "rollback": {"available": True, "snapshot_id": "v0", "command": "x"},
        "conclusion": {"verdict": "improved"},
    }


def test_evolve_requires_is_mock():
    p = _minimal_evolve_payload()
    del p["is_mock"]
    errs = evolve_schema.validate(p)
    assert any("is_mock" in e for e in errs)


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, None])
def test_evolve_rejects_non_bool_is_mock(bad):
    """字符串 "false" 是 JS 真值 —— 这类数据必须拒，不能留给页面去猜。"""
    p = _minimal_evolve_payload()
    p["is_mock"] = bad
    errs = evolve_schema.validate(p)
    assert any("is_mock" in e for e in errs), f"{bad!r} 应被拒绝"


def test_evolve_accepts_bool_is_mock():
    assert evolve_schema.validate(_minimal_evolve_payload()) == []


# ─────────────────────────────────────────────────────────────
# 3. 落盘的三份数据都带布尔标记（真实文件，不是内存对象）
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["history.json", "evolve.json", "result.json"])
def test_shipped_json_declares_is_mock(name):
    p = WEB / name
    assert p.exists(), f"{p} 不存在"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert "is_mock" in d, f"{name} 缺 is_mock"
    assert isinstance(d["is_mock"], bool), f"{name} 的 is_mock 不是布尔值"


def test_evolve_json_mock_consistent_with_backend():
    """evolve.json 的 backend 和 is_mock 不能互相矛盾。"""
    d = json.loads((WEB / "evolve.json").read_text(encoding="utf-8"))
    assert d["is_mock"] is (d.get("backend") != "real")


# ─────────────────────────────────────────────────────────────
# 4. 三张页面都必须真的渲染这个标记（防回退）
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["index.html", "evolve.html", "result.html"])
def test_page_renders_is_mock(name):
    html = (WEB / name).read_text(encoding="utf-8")
    assert "is_mock" in html, f"{name} 没有读 is_mock，假数据会被当成真的"


def test_result_page_has_no_truthy_check_on_is_mock():
    """`D.is_mock ? x : y` 在字段缺失时会落到绿标一侧 —— 方向反了，必须用 ===。"""
    html = (WEB / "result.html").read_text(encoding="utf-8")
    assert "D.is_mock ?" not in html, "result.html 又用回了真值判断"


# ─────────────────────────────────────────────────────────────
# 5. 导出端到端：漏了 is_mock 就不许通过自检
# ─────────────────────────────────────────────────────────────
def test_export_verify_rejects_missing_is_mock(tmp_path):
    """构造一份没有 is_mock 的 history.json，verify 必须返回非 0。"""
    from app.export import verify

    payload = {"run_id": "x", "generations": [], "lineage": {},
               "challenges": [], "summary": {}, "problems": []}
    bad = tmp_path / "history.json"
    bad.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert verify(bad) != 0


def test_export_end_to_end_marks_mock(tmp_path):
    """真跑一次导出：假 run 目录导出的结果必须带 is_mock=true。"""
    run_dir = ROOT / "runs" / "fake_s42"
    if not (run_dir / "history.jsonl").exists():
        pytest.skip("runs/fake_s42 不存在")
    out = tmp_path / "history.json"
    payload = export_run(run_dir, out, lenient=True)
    assert payload["is_mock"] is True
    assert isinstance(payload["mock_reason"], str) and payload["mock_reason"]
