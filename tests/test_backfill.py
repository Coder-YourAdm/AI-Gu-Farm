"""回填后的题集：必须能被 A 的 executor 端到端吃下。

为什么单独一个文件
------------------
`test_datasets.py` 管的是「题目本身合不合规格」（数量、边界、均衡、互斥）。
本文件管的是另一件事：**这些题接上 A 的判分内核之后，还能不能跑**。

具体验四条链路：
    题面参数 → validate_task → normalize_task → judge

任何一条断了，20 代演化跑出来的数都是脏的，而且不会当场报错——
所以宁可在联调前用测试把它钉死。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from app.executor import (  # noqa: E402
    judge,
    signature,
    solve_exact,
    solver_view,
    validate_task,
)
from tests.backfill_datasets import (  # noqa: E402
    NAMES,
    fill_row,
    ref_to_answer,
    ref_to_wrong_answer,
)

KEY_SET = {"id", "source", "by", "gen", "type", "template",
           "params", "signature", "statement", "reference"}


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    out: list[dict] = []
    for n in NAMES:
        out.extend(json.loads((DATA / f"{n}.json").read_text(encoding="utf-8")))
    return out


# ── 1. 回填完整性 ────────────────────────────────────────────
def test_all_three_fields_filled(rows) -> None:
    for r in rows:
        assert r["signature"], f"{r['id']} signature 仍为空"
        assert r["statement"] and r["statement"].strip(), f"{r['id']} statement 为空"
        assert isinstance(r["reference"], dict), f"{r['id']} reference 未回填"


def test_reference_shape(rows) -> None:
    """reference 只能是 integer 或 rational，数字一律字符串（不能变 float）。"""
    for r in rows:
        ref = r["reference"]
        assert ref["kind"] in ("integer", "rational"), f"{r['id']} kind 非法"
        if ref["kind"] == "integer":
            assert set(ref) == {"kind", "value"}
            int(ref["value"])                     # 必须可解析
        else:
            assert set(ref) == {"kind", "numerator", "denominator"}
            assert int(ref["denominator"]) > 0, f"{r['id']} 分母非正"
        assert r["template"] != "det" or ref["kind"] == "integer", (
            f"{r['id']} 行列式答案必须是整数")


def test_no_float_anywhere(rows) -> None:
    """禁 float 是硬规则：reference 里的数字必须是字符串。

    一旦有 float 混进来，大整数行列式会静默丢精度——数值上看不出来，
    但末代排名会莫名其妙地变。所以这里逐个字段查类型，不查文本。
    """
    for r in rows:
        for k, v in r["reference"].items():
            assert isinstance(v, str), f"{r['id']}.reference.{k} 不是字符串（{type(v).__name__}）"


# ── 2. 能被 A 的校验与求值复现 ──────────────────────────────
def test_validate_task_accepts_all(rows) -> None:
    """回填后的题必须仍能过 validate_task —— 否则 B 出题时会被自己拒掉。"""
    for r in rows:
        ok, reason = validate_task({"template": r["template"], "params": r["params"]})
        assert ok, f"{r['id']} 被 validate_task 拒绝：{reason}"


def test_signature_reproducible(rows) -> None:
    for r in rows:
        want = signature({"template": r["template"], "params": r["params"]})
        assert r["signature"] == want, f"{r['id']} 签名不可复现：{want}"


def test_reference_reproducible(rows) -> None:
    for r in rows:
        assert r["reference"] == solve_exact(r), f"{r['id']} reference 不可复现"


def test_key_set_exact(rows) -> None:
    for r in rows:
        assert set(r) == KEY_SET, f"{r['id']} 键集合不对：{sorted(r)}"


# ── 3. judge 判得动这些题 ───────────────────────────────────
def test_judge_accepts_own_reference(rows) -> None:
    """A 出的参考答案，A 的判分器必须判 right。这是回填唯一真正的验收标准。"""
    for r in rows:
        payload = json.dumps({"value": ref_to_answer(r["reference"])},
                             ensure_ascii=False)
        assert judge(r, payload) == "right", f"{r['id']} 参考答案被判错"


def test_judge_rejects_perturbed_answer(rows) -> None:
    for r in rows:
        payload = json.dumps({"value": ref_to_wrong_answer(r["reference"])},
                             ensure_ascii=False)
        assert judge(r, payload) == "wrong", f"{r['id']} 错答案未被判 wrong"


@pytest.mark.parametrize("bad", [None, "", "   ", "not json", "{}",
                                 '{"value": 3}', '{"value": "3", "x": 1}', "[]"])
def test_judge_unanswered_not_wrong(rows, bad) -> None:
    """拒答/格式错必须记 unanswered。混成 wrong 会把「没答」当成「算错」。"""
    for r in rows:
        assert judge(r, bad) == "unanswered", f"{r['id']} 对 {bad!r} 判错"


def test_judge_det_rejects_fraction(rows) -> None:
    """行列式只接整数：给 1/2 是 unanswered，不是 wrong。"""
    dets = [r for r in rows if r["type"] == "det"]
    assert dets
    for r in dets:
        assert judge(r, json.dumps({"value": "1/2"})) == "unanswered"
        assert judge(r, json.dumps({"value": "abc"})) == "unanswered"


# ── 4. solver_view 不许泄漏 ─────────────────────────────────
def test_solver_view_whitelist(rows) -> None:
    for r in rows:
        view = solver_view(r)
        assert set(view) == {"type", "statement", "answer_format"}
        assert r["signature"] not in json.dumps(view, ensure_ascii=False), (
            f"{r['id']} 签名泄漏进 solver_view")
        assert "reference" not in view and "params" not in view
        # 答案本身不许出现在题面里。短答案（"7"、"0"）会偶然撞上题面数字，
        # 只对长度 >=3 的答案做这条断言，否则是假警报。
        ans = ref_to_answer(r["reference"])
        if len(ans) >= 3:
            assert ans not in view["statement"], f"{r['id']} 答案疑似写进题面"


# ── 5. 幂等：再跑一次回填必须一字不改 ───────────────────────
def test_backfill_is_idempotent(rows) -> None:
    """回填是纯函数。若两次结果不同，说明依赖了随机数/时间/字典序。"""
    for r in rows:
        again = fill_row(r)
        assert again == r, f"{r['id']} 二次回填结果不同"
        assert json.dumps(again, ensure_ascii=False) == json.dumps(
            r, ensure_ascii=False), f"{r['id']} 序列化顺序不稳定"
