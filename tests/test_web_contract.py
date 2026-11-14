"""web/ 数据契约自检脚本自身的回归（徐潇 2026-11-14 的三条反馈）。

重点不是「脚本能不能跑」，而是它**会不会把错数据放行**：
一个永远报 PASS 的校验器比没有更危险。

所有临时文件都写 pytest 的 `tmp_path`（自动清理）——本机沙箱会拦
`unlink()`，手动删临时文件反而会让测试假失败。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_web_contract as cwc  # noqa: E402

STUDENTS = ["weak", "mid", "strong"]
POLICIES = ["loose", "verbose", "answer_only"]


def _row(student, policy, *, items=20, hints=14, gain=0.412,
         cost=None, fitness=None):
    exp_cost = cwc._expected_cost(items, hints)          # 0.79
    exp_fit = cwc._expected_fitness(gain, exp_cost)      # 0.2145
    return {
        "student": student, "policy": policy,
        "gain": gain,
        "cost": round(exp_cost, 3) if cost is None else cost,
        "fitness": round(exp_fit, 3) if fitness is None else fitness,
        "items": items, "hints": hints,
        "items_to_mastery": 17,
    }


def _compare(rows=None):
    if rows is None:
        rows = [_row(s, p) for s in STUDENTS for p in POLICIES]
    return {
        "seed": 42, "students": STUDENTS, "policies": POLICIES,
        "rows": rows,
        "winner": {"student": "mid", "policy": "loose",
                   "reason": "同增益下成本最低"},
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_real_tutor_json_passes_xuxiao_four_checks():
    """仓库里那份回放数据必须同时满足徐潇的四条验收。"""
    assert cwc.check_tutor(ROOT / "web" / "tutor.json") == []


def test_valid_compare_passes(tmp_path):
    assert cwc.check_compare(_write(tmp_path / "c.json", _compare())) == []


def test_old_example_cost_0280_is_rejected(tmp_path):
    """徐潇指出的 §4.4 原示例：cost=0.280 / fitness=0.342 必须被拦下。"""
    bad = _compare()
    bad["rows"][0]["cost"] = 0.280
    bad["rows"][0]["fitness"] = 0.342
    errs = cwc.check_compare(_write(tmp_path / "bad.json", bad))
    assert any("cost" in e for e in errs)
    assert any("fitness" in e for e in errs)


def test_all_five_top_fields_are_required(tmp_path):
    for key in cwc.COMPARE_TOP:
        payload = _compare()
        payload.pop(key)
        errs = cwc.check_compare(_write(tmp_path / (key + ".json"), payload))
        assert any(key in e for e in errs), key


def test_rows_must_be_nine_and_unique(tmp_path):
    short = _compare()
    short["rows"] = short["rows"][:-1]
    assert any("9 行" in e for e in
               cwc.check_compare(_write(tmp_path / "short.json", short)))

    dup = _compare()
    dup["rows"][1] = dict(dup["rows"][0])
    assert any("重复" in e for e in
               cwc.check_compare(_write(tmp_path / "dup.json", dup)))


def test_tolerance_002_boundary(tmp_path):
    """差 0.001 放行、差 0.01 标红，与页面口径一致。"""
    good, bad = _compare(), _compare()
    good["rows"][0]["fitness"] += 0.001
    bad["rows"][0]["fitness"] += 0.010
    assert cwc.check_compare(_write(tmp_path / "good.json", good)) == []
    assert cwc.check_compare(_write(tmp_path / "bad.json", bad))


def test_reference_word_in_tutor_is_rejected(tmp_path):
    payload = json.loads((ROOT / "web" / "tutor.json").read_text(encoding="utf-8"))
    payload["notes"].append("reference")
    assert any("reference" in e for e in
               cwc.check_tutor(_write(tmp_path / "t.json", payload)))


def test_kc_ids_and_why_gap_are_checked(tmp_path):
    wrong_id = json.loads((ROOT / "web" / "tutor.json").read_text(encoding="utf-8"))
    wrong_id["kcs"][0]["id"] = "poly"
    assert any("kcs id" in e for e in
               cwc.check_tutor(_write(tmp_path / "t1.json", wrong_id)))

    wrong_gap = json.loads((ROOT / "web" / "tutor.json").read_text(encoding="utf-8"))
    wrong_gap["turns"][0]["why"]["gap"] = 0.99
    assert any("why.gap" in e for e in
               cwc.check_tutor(_write(tmp_path / "t2.json", wrong_gap)))


def test_cli_skips_missing_compare_until_required(tmp_path):
    tutor = ROOT / "web" / "tutor.json"
    missing = tmp_path / "nope.json"
    assert cwc.main(["--tutor", str(tutor), "--compare", str(missing)]) == 0
    assert cwc.main(["--tutor", str(tutor), "--compare", str(missing),
                     "--require-compare"]) == 1


def test_cli_passes_with_valid_pair(tmp_path):
    compare = _write(tmp_path / "compare.json", _compare())
    assert cwc.main(["--tutor", str(ROOT / "web" / "tutor.json"),
                     "--compare", str(compare)]) == 0
