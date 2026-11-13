"""config 的契约测试。

覆盖分工方案 B2 验收的三条数字，以及「不接受未知键 / int 必须真 int /
结构参数不许偷偷改」这些一旦破掉就会让公式失真的点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import (
    Config,
    ConfigError,
    call_plan,
    challenge_calls,
    holdout_calls,
    per_gen_calls,
    refetch_reserve,
)

DEV = Path("config.dev.json")


def _raw(**over) -> dict:
    raw = json.loads(DEV.read_text(encoding="utf-8"))
    raw.update(over)
    return raw


def _cfg(**over) -> Config:
    return Config.from_dict(_raw(**over), source_path="in-memory")


# ---------------------------------------------------------------- 调用数

def test_per_gen_is_78():
    cfg = _cfg()
    # 6 出题 + 30 互答 + 24 共同卷 + 18 祖先
    assert per_gen_calls(cfg) == 78


def test_default_twenty_gen_counts():
    cfg = _cfg()
    plan = call_plan(cfg)
    assert cfg.checkpoints == [0, 5, 10, 15, 20]
    assert plan["challenge_slots"] == 72          # (6+6)*6
    assert plan["holdout_slots"] == 600           # 5*6*20
    assert plan["E"] == 2289
    assert plan["F"] == 2232
    assert plan["refetch_reserve"] == 240
    # E-F 必须等于「F 没有重组」的量化差：19 代 × 3 次算子
    assert plan["E"] - plan["F"] == 19 * 3


def test_fifteen_gen_counts():
    cfg = _cfg(n_gen=15, challenge_gens=[4, 10])
    plan = call_plan(cfg)
    assert cfg.checkpoints == [0, 5, 10, 15]
    assert plan["holdout_slots"] == 480
    assert plan["E"] == 1764
    assert plan["F"] == 1722


def test_condition_a_same_as_e_with_zero_weight():
    cfg = _cfg(condition="A", set_weight=0)
    assert call_plan(cfg)["A"] == call_plan(cfg)["E"]
    assert cfg.set_weight == 0


def test_challenge_calls_equals_full_window_under_valid_config():
    # 合法配置下 born+window-1 <= n_gen 恒成立，所以每道挑战都跑满窗口
    cfg = _cfg(n_gen=20, challenge_gens=[8, 15], challenge_window=6)
    assert challenge_calls(cfg) == 72
    cfg2 = _cfg(n_gen=18, challenge_gens=[6, 12], challenge_window=6)
    assert challenge_calls(cfg2) == 72
    # 贴着末代出生也合法（born+window-1 == n_gen）
    cfg3 = _cfg(n_gen=17, challenge_gens=[6, 12], challenge_window=6)
    assert challenge_calls(cfg3) == 72


def test_challenge_window_overflow_is_rejected_not_truncated():
    # 截断不被允许：越界直接拒绝，避免「窗口被悄悄改短」后退役规则失真
    with pytest.raises(ConfigError):
        _cfg(n_gen=18, challenge_gens=[6, 16], challenge_window=6)


def test_holdout_scales_with_checkpoints():
    assert holdout_calls(_cfg()) == 600
    assert holdout_calls(_cfg(n_gen=15, challenge_gens=[4, 10])) == 480
    assert refetch_reserve(_cfg()) == 240


# ---------------------------------------------------------------- 严格校验

def test_unknown_key_rejected():
    with pytest.raises(ConfigError) as e:
        _cfg(gens=20)
    assert "未知键" in str(e.value)


def test_missing_key_rejected():
    raw = _raw()
    raw.pop("fitness_window")
    with pytest.raises(ConfigError) as e:
        Config.from_dict(raw)
    assert "缺少必填键" in str(e.value)


@pytest.mark.parametrize("bad", [True, False, 6.0, "6", None])
def test_int_field_rejects_non_int(bad):
    with pytest.raises(ConfigError):
        _cfg(n_pop=bad)


def test_bool_field_rejects_int():
    with pytest.raises(ConfigError):
        _cfg(attach_occupied_params=1)


def test_structural_params_locked():
    # n_pop 改 7 会让 n_t 与 scoring 的 assert 同时失效
    with pytest.raises(ConfigError) as e:
        _cfg(n_pop=7)
    assert "固定" in str(e.value)


def test_survive_crossover_mutate_must_sum_to_pop():
    with pytest.raises(ConfigError):
        _cfg(n_survive=2, n_crossover=2, n_mutate=1)


def test_condition_a_requires_zero_weight():
    with pytest.raises(ConfigError) as e:
        _cfg(condition="A", set_weight=2.0)
    assert "set_weight=0" in str(e.value)


def test_unknown_condition_rejected():
    with pytest.raises(ConfigError):
        _cfg(condition="G")


def test_challenge_window_must_fit_into_gens():
    # 15 代默认 [8,15] 会让第二道挑战窗口越界，必须显式改 [4,10]
    with pytest.raises(ConfigError) as e:
        _cfg(n_gen=15, challenge_gens=[8, 15])
    assert "n_gen >=" in str(e.value)


def test_challenge_windows_must_not_overlap():
    with pytest.raises(ConfigError) as e:
        _cfg(challenge_gens=[8, 10])   # 间隔 2 < window 6
    assert "重叠" in str(e.value)


def test_temperature_out_of_range_rejected():
    with pytest.raises(ConfigError):
        _cfg(solver_temperature=2.5)
    with pytest.raises(ConfigError):
        _cfg(setter_temperature=-0.1)


def test_tolerance_must_be_positive_fraction():
    assert _cfg().tolerance_fraction > 0
    with pytest.raises(ConfigError):
        _cfg(decimal_tolerance="0")
    with pytest.raises(ConfigError):
        _cfg(decimal_tolerance="abc")


def test_placeholder_blocks_real_run_but_not_dry_run():
    cfg = _cfg()                       # config.dev.json 就是占位符
    assert "REPLACE_WITH" in cfg.model
    with pytest.raises(ConfigError) as e:
        cfg.require_runtime_ready()
    assert "占位符" in str(e.value)


def test_budget_null_blocks_real_run():
    cfg = _cfg(model="some-model", base_url="https://example.com/v1")
    with pytest.raises(ConfigError) as e:
        cfg.require_runtime_ready()
    assert "budget_amount" in str(e.value)
    ok = _cfg(model="some-model", base_url="https://example.com/v1",
              budget_amount=15.0, budget_currency="CNY")
    ok.require_runtime_ready()


def test_fingerprint_stable_and_sensitive():
    a = _cfg()
    b = _cfg()
    assert a.semantic_fingerprint() == b.semantic_fingerprint()
    c = _cfg(fitness_window=1)
    assert a.semantic_fingerprint() != c.semantic_fingerprint()


def test_load_reports_missing_file():
    with pytest.raises(ConfigError):
        Config.load("no_such_config.json")
