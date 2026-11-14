"""set_model 脚本的回归：它写出来的配置必须能直接过 require_runtime_ready。

这个脚本的价值全在「一次做对」——base_url 用预设、model 由人给、key 只进 .env、
max_calls 必须补上（漏了会被 require_runtime_ready 拒）。任何一条退化，
队友就会在原地卡 20 分钟。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import set_model  # noqa: E402


def test_list_returns_zero(capsys):
    assert set_model.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "deepseek" in out and "ollama" in out
    assert "https://api.deepseek.com/v1" in out


def test_missing_args(tmp_path):
    assert set_model.main(["--provider", "deepseek", "--out-dir", str(tmp_path)]) == 2
    assert set_model.main(["--model", "x", "--out-dir", str(tmp_path)]) == 2


def test_key_required_unless_ollama(tmp_path):
    assert set_model.main(["--provider", "deepseek", "--model", "m",
                           "--out-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "config.local.json").exists()


def test_dry_run_writes_nothing(tmp_path, capsys):
    code = set_model.main(["--provider", "deepseek", "--model", "deepseek-chat",
                           "--key", "sk-abcdefghijk", "--dry-run",
                           "--out-dir", str(tmp_path)])
    assert code == 0
    assert not (tmp_path / "config.local.json").exists()
    assert not (tmp_path / ".env").exists()
    assert "dry-run" in capsys.readouterr().out


def test_real_write_produces_runtime_ready_config(tmp_path, capsys):
    code = set_model.main(["--provider", "deepseek", "--model", "deepseek-chat",
                           "--key", "sk-abcdefghijk", "--max-calls", "4000",
                           "--out-dir", str(tmp_path)])
    assert code == 0

    raw = json.loads((tmp_path / "config.local.json").read_text(encoding="utf-8"))
    assert raw["model"] == "deepseek-chat"
    assert raw["base_url"] == "https://api.deepseek.com/v1"
    assert raw["max_calls"] == 4000
    assert raw["wall_limit_s"] is None          # 必填键不能删，只能置空
    assert "sk-abcdefghijk" not in json.dumps(raw)   # key 绝不进 JSON

    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env.strip() == "LLM_API_KEY=sk-abcdefghijk"

    # 关键断言：写出来的配置能直接过闸
    from app.config import Config
    cfg = Config.from_dict(raw)
    cfg.require_runtime_ready()

    # 输出里不许出现完整 key
    out = capsys.readouterr().out
    assert "sk-abcdefghijk" not in out


def test_existing_env_is_replaced_not_duplicated(tmp_path):
    (tmp_path / ".env").write_text("OTHER=1\nLLM_API_KEY=old\n", encoding="utf-8")
    assert set_model.main(["--provider", "moonshot", "--model", "kimi",
                           "--key", "sk-new", "--out-dir", str(tmp_path)]) == 0
    line = (tmp_path / ".env").read_text(encoding="utf-8")
    assert line.count("LLM_API_KEY=") == 1
    assert "LLM_API_KEY=sk-new" in line
    assert "OTHER=1" in line


def test_ollama_needs_no_key(tmp_path):
    assert set_model.main(["--provider", "ollama", "--model", "qwen2.5:14b",
                           "--out-dir", str(tmp_path)]) == 0
    raw = json.loads((tmp_path / "config.local.json").read_text(encoding="utf-8"))
    assert raw["base_url"] == "http://127.0.0.1:11434/v1"


def test_custom_base_url_wins(tmp_path):
    assert set_model.main(["--provider", "deepseek", "--model", "m", "--key", "sk-k",
                           "--base-url", "https://proxy.example.com/v1",
                           "--out-dir", str(tmp_path)]) == 0
    raw = json.loads((tmp_path / "config.local.json").read_text(encoding="utf-8"))
    assert raw["base_url"] == "https://proxy.example.com/v1"
