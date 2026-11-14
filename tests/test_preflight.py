"""preflight 体检脚本自身的回归：它必须能拦住真实的翻车点。

这几条测的不是 preflight 的排版，而是「它会不会把没配好的环境报成可以开跑」——
一个永远报 OK 的体检脚本比没有还危险。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import preflight  # noqa: E402


def _write_config(tmp_path: Path, **overrides) -> Path:
    raw = json.loads((ROOT / "config.dev.json").read_text(encoding="utf-8"))
    raw.update(overrides)
    path = tmp_path / "config.test.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _ready_config(tmp_path: Path, **overrides) -> Path:
    """把所有「真实运行」门槛都填好：非占位 model/base_url + 硬限额。"""
    base = {"model": "test-model-20260101",
            "base_url": "https://example.invalid/v1",
            "max_calls": 20000}
    base.update(overrides)
    return _write_config(tmp_path, **base)


def _status(checks, key: str) -> str:
    for c in checks:
        if c.key == key:
            return c.status
    raise AssertionError(f"没有 {key} 这项检查")


def test_placeholder_config_is_blocking(tmp_path):
    """占位 model 的配置必须 FAIL——这是 cfg.require_runtime_ready 的真实行为。"""
    cfg = _write_config(tmp_path)
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs")
    assert _status(checks, "runtime_ready") == preflight.FAIL
    assert any(c.status == preflight.FAIL for c in checks)


def test_missing_hard_limit_is_blocking(tmp_path):
    """没设 wall_limit_s / max_calls 时，真实运行会被 require_runtime_ready 拒。"""
    cfg = _write_config(tmp_path, model="test-model", base_url="https://example.invalid/v1")
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs")
    assert _status(checks, "runtime_ready") == preflight.FAIL
    detail = next(c.detail for c in checks if c.key == "runtime_ready")
    assert "wall_limit_s" in detail and "max_calls" in detail


def test_ready_config_has_no_fail(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = _ready_config(tmp_path)
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs")
    assert [c.title for c in checks if c.status == preflight.FAIL] == []
    # 密钥未设置只是 WARN，不阻断离线体检
    assert _status(checks, "secret") == preflight.WARN
    # 数据集、判分往返、目录可写必须是 OK
    for key in ("data", "executor", "runs", "deps", "plan"):
        assert _status(checks, key) == preflight.OK, key


def test_require_key_turns_warn_into_fail(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = _ready_config(tmp_path)
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs",
                                  require_key=True)
    assert _status(checks, "secret") == preflight.FAIL


def test_key_presence_detected_without_leaking(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-not-a-real-key")
    cfg = _ready_config(tmp_path)
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs")
    secret = next(c for c in checks if c.key == "secret")
    assert secret.status == preflight.OK
    assert "sk-not-a-real-key" not in secret.detail


def test_probe_skipped_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = _ready_config(tmp_path)
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs",
                                  probe=True)
    assert _status(checks, "probe") == preflight.SKIP


def test_missing_data_file_is_blocking(tmp_path):
    """少一份 holdout 时不能报 OK。"""
    broken = tmp_path / "data"
    broken.mkdir()
    for name in ("calibration", "common_dev", "challenge_pool"):
        shutil.copy(ROOT / "data" / f"{name}.json", broken / f"{name}.json")
    cfg = _ready_config(tmp_path)
    checks = preflight.run_checks(cfg, data_dir=broken, runs_root=tmp_path / "runs")
    assert _status(checks, "data") == preflight.FAIL


def test_budget_ratio_warns_when_tight(tmp_path):
    """max_calls 刚好够一趟时必须 WARN，装不下时必须 FAIL。"""
    cfg = _ready_config(tmp_path, max_calls=2289)          # E 一趟正好 2289
    checks = preflight.run_checks(cfg, data_dir=ROOT / "data", runs_root=tmp_path / "runs")
    assert _status(checks, "plan") == preflight.WARN

    cfg2 = _ready_config(tmp_path, max_calls=100)
    checks2 = preflight.run_checks(cfg2, data_dir=ROOT / "data", runs_root=tmp_path / "runs")
    assert _status(checks2, "plan") == preflight.FAIL


def test_main_exit_code(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = _ready_config(tmp_path)
    assert preflight.main(["--config", str(cfg),
                           "--runs-root", str(tmp_path / "runs")]) == 0
    assert preflight.main(["--config", str(_write_config(tmp_path)),
                           "--runs-root", str(tmp_path / "runs")]) == 1


def test_dotenv_is_loaded_and_never_overrides(tmp_path, monkeypatch):
    """.env 写了 key 却被告知没设，是最容易浪费十分钟的一类误会。"""
    import os
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("# 注释\nOTHER=1\nLLM_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    assert preflight._load_dotenv(path) == 2
    assert os.environ["LLM_API_KEY"] == "sk-from-dotenv"
    assert os.environ["OTHER"] == "1"

    monkeypatch.setenv("LLM_API_KEY", "sk-from-shell")
    assert preflight._load_dotenv(path) == 0            # 已存在的不覆盖
    assert os.environ["LLM_API_KEY"] == "sk-from-shell"


def test_dotenv_missing_file_is_noop(tmp_path):
    assert preflight._load_dotenv(tmp_path / "nope.env") == 0
