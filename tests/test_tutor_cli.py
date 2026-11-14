"""回放数据生成器回归：结构、真实性、幂等、敏感词。

这些测试保护的是「集成点 1」——C 拿到 web/tutor.json 就能独立开工。
任何一条不过，前端拿到的就是一份看起来能用、其实讲不通的数据。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tutor_cli  # noqa: E402
from app import executor  # noqa: E402
from app.student import kc  # noqa: E402

BANNED = ('"reference"', "holdout", '"raw_response"', "api_key", "REPLACE_WITH")


def test_build_shape_matches_contract():
    d = tutor_cli.build(sid="demo", turns=8, at=tutor_cli.DEFAULT_AT)
    assert d["profile_version"] == "manual-v1"
    assert d["student"]["sid"] == "demo"
    assert len(d["kcs"]) == 3
    assert [r["id"] for r in d["kcs"]] == list(kc.KC_IDS)
    assert len(d["turns"]) >= 8
    assert d["review_plan"] and d["memory_timeline"]


def test_every_tutor_turn_has_real_why():
    d = tutor_cli.build(turns=8)
    for turn in d["turns"]:
        if turn["role"] != "tutor":
            continue
        w = turn["why"]
        assert 0.0 <= w["mastery"] <= 1.0
        assert 0.0 <= w["difficulty"] <= 1.0
        assert w["gap"] == pytest.approx(w["difficulty"] - w["mastery"],
                                        abs=1e-4)
        assert w["last_error"] and w["last_error"] != "本知识点暂无错题记录"
        assert isinstance(w["memory"], list)


def test_mastery_changes_between_rounds_is_the_rixin_evidence():
    """两个出题轮的 mastery 必须不同——否则「每次答题都在更新」是假的。"""
    d = tutor_cli.build(turns=8)
    values = [t["why"]["mastery"] for t in d["turns"]
              if t.get("statement")]
    assert len(values) >= 2
    assert len(set(values)) >= 2


def test_no_sensitive_content_leaks_into_replay():
    text = json.dumps(tutor_cli.build(turns=8), ensure_ascii=False)
    for word in BANNED:
        assert word not in text, word
    # 题面里不该出现模板名、参数或「参考答案」字样
    for turn in tutor_cli.build(turns=8)["turns"]:
        if "statement" in turn:
            for word in ("poly", "gamma", "gaussian", "reference", "params"):
                assert word not in turn["statement"]


def test_student_answers_are_legal_literals():
    """模拟答案必须是 judge 能解析的字面量，不能是表达式或未答。"""
    d = tutor_cli.build(turns=8)
    pattern = re.compile(r"^[+-]?[0-9]+(?:/[0-9]+)?$")
    for turn in d["turns"]:
        if turn["role"] != "student":
            continue
        assert pattern.fullmatch(turn["answer"]), turn["answer"]
        assert turn["verdict"] in ("right", "wrong")
        assert turn["err"] in ("sign", "calc", "concept", "misread",
                              "giveup", "none")


def test_every_answer_verdict_is_reproducible():
    """回放里每个判定都能用当前 executor 重算出来（不是手写标签）。"""
    d = tutor_cli.build(turns=8)
    pool = {t["statement"]: t for t in tutor_cli._pool()}
    current = None
    for turn in d["turns"]:
        if "statement" in turn:
            current = turn["statement"]
            continue
        if turn["role"] != "student":
            continue
        assert current is not None
        raw = json.dumps({"value": turn["answer"]}, ensure_ascii=False)
        assert executor.judge(pool[current], raw) == turn["verdict"]


def test_build_is_deterministic():
    a = tutor_cli.build(sid="demo", turns=8)
    b = tutor_cli.build(sid="demo", turns=8)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
           json.dumps(b, ensure_ascii=False, sort_keys=True)


def test_four_turn_version_also_passes():
    d = tutor_cli.build(turns=4)
    assert len(d["turns"]) >= 4
    assert len(d["kcs"]) == 3


def test_cli_writes_file(tmp_path):
    out = tmp_path / "tutor.json"
    assert tutor_cli.main(["--sid", "demo", "--turns", "4",
                           "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["turns"]) >= 4
    # 再跑一次逐字节相同（幂等）
    first = out.read_bytes()
    tutor_cli.main(["--sid", "demo", "--turns", "4", "--out", str(out)])
    assert out.read_bytes() == first


def test_cli_accepts_offline_flag(tmp_path):
    assert tutor_cli.main(["--offline", "--turns", "4",
                           "--out", str(tmp_path / "t.json")]) == 0


def test_self_check_rejects_bad_payload():
    d = tutor_cli.build(turns=4)
    d["kcs"] = d["kcs"][:2]
    with pytest.raises(ValueError, match="kcs"):
        tutor_cli._self_check(d, 4)
    d = tutor_cli.build(turns=4)
    d["turns"][0].pop("why")
    with pytest.raises(ValueError, match="why"):
        tutor_cli._self_check(d, 4)
    d = tutor_cli.build(turns=4)
    d["notes"].append("reference")
    with pytest.raises(ValueError, match="敏感"):
        tutor_cli._self_check(d, 4)


def test_build_rejects_bad_args():
    with pytest.raises(ValueError, match="sid"):
        tutor_cli.build(sid="", turns=4)
    with pytest.raises(ValueError, match="turns"):
        tutor_cli.build(turns=0)
    with pytest.raises(ValueError, match="turns"):
        tutor_cli.build(turns=True)
