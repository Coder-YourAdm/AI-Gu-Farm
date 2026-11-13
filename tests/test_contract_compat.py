"""与 A 的 executor 契约兼容性预检。

依据 `A_to_B_接口契约(1).md`（A1 已冻结）与 `A_executor_实现说明(1).md` §2.1。

为什么需要这个文件
------------------
C 出的 125 题要交给 A 的 `normalize_task` 回填 `signature` / `statement` / `reference`。
A 的 `validate_task` 有一条很硬的规则：**params 的键集合必须恰等于约定**，
多一个键、少一个键、名字差一个字母，整道题都会被拒。

而"被拒"不会报错——它只是一条 `(False, reason)`。125 道题里如果有 20 道被拒，
现象是：A 回填完只有 105 道有 reference，剩下 20 道 reference 为 null，
跑到第 8 代才因为凑不齐题而 aborted。那时已经没时间重生成。

所以要在交出去之前，用 A 的规则把 125 道**先拒一遍**。

2026-11-13 21:20 首跑即发现两处真实冲突
----------------------------------------
`data/verify_bank.json`（给 A 的 10 道验证题）有三处与 A 的契约不符：
  1. `poly` 参数用 `l`/`u`，A 要 `lower`/`upper` → 全部被拒
  2. 行列式 `template` 写成 `matrix`，A 的白名单只有 `det` → 全部被拒
  3. 签名格式 `poly:{json}` 与 A 的 `poly|coeffs=..|lower=..|upper=..` 不通 → 去重失效
已修正。125 题主体（`data/*.json`）当时就是对的。

待办：A 的 `app/executor.py` 到位后，把本文件的 `_check_params` 换成**直接调用**
`executor.validate_task`，否则这里就是在维护第二套边界规则。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

DATASETS = ("calibration", "common_dev", "challenge_pool", "holdout")

# A 的模板白名单（executor.py 第 3 条校验）
TEMPLATES = {"poly", "gamma", "gaussian_odd", "det"}

# A 的 params 键集合，必须**恰等于**
PARAM_KEYS = {
    "poly": {"coeffs", "lower", "upper"},
    "gamma": {"c", "n", "rate"},
    "gaussian_odd": {"c", "k", "rate"},
    "det": {"matrix"},
}


def _load(name: str) -> list[dict]:
    d = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    return d["tasks"] if isinstance(d, dict) else d


def _all_tasks() -> list[dict]:
    out = []
    for name in DATASETS:
        out.extend(_load(name))
    return out


def _as_int(v) -> bool:
    """A 用的是 `type(v) is int`，不是 isinstance —— 后者会放过 True。"""
    return type(v) is int


def _check_params(tpl: str, p: dict) -> str | None:
    """按 A 的边界表检查，返回 None 表示合法，否则返回原因。"""
    if set(p) != PARAM_KEYS[tpl]:
        return f"params 键集合应为 {sorted(PARAM_KEYS[tpl])}，实际 {sorted(p)}"

    if tpl == "poly":
        coeffs = p["coeffs"]
        if not isinstance(coeffs, list) or not (2 <= len(coeffs) <= 5):
            return f"coeffs 长度应为 2..5，实际 {coeffs}"
        if not all(_as_int(c) and -5 <= c <= 5 for c in coeffs):
            return f"coeffs 元素应为 [-5,5] 内的 int，实际 {coeffs}"
        if coeffs[-1] == 0:
            return "coeffs 末项为 0（等于降次，签名会撞）"
        if not any(c != 0 for c in coeffs[1:]):
            return "coeffs 除常数项外全为 0（积分恒 0，无区分度）"
        lo, hi = p["lower"], p["upper"]
        if not (_as_int(lo) and -3 <= lo <= 3):
            return f"lower 越界：{lo}"
        if not (_as_int(hi) and lo + 1 <= hi <= lo + 6):
            return f"upper 越界：{hi}（应为 lower+1 .. lower+6）"

    elif tpl == "gamma":
        if not (_as_int(p["n"]) and 1 <= p["n"] <= 9):
            return f"n 越界：{p['n']}"
        if not (_as_int(p["rate"]) and 1 <= p["rate"] <= 8):
            return f"rate 越界：{p['rate']}"
        if not (_as_int(p["c"]) and -5 <= p["c"] <= 5 and p["c"] != 0):
            return f"c 越界或为 0：{p['c']}"

    elif tpl == "gaussian_odd":
        if not (_as_int(p["k"]) and 0 <= p["k"] <= 5):
            return f"k 越界：{p['k']}"
        if not (_as_int(p["rate"]) and 1 <= p["rate"] <= 8):
            return f"rate 越界：{p['rate']}"
        if not (_as_int(p["c"]) and -5 <= p["c"] <= 5 and p["c"] != 0):
            return f"c 越界或为 0：{p['c']}"

    elif tpl == "det":
        m = p["matrix"]
        if not isinstance(m, list) or not (2 <= len(m) <= 5):
            return f"矩阵阶数应为 2..5，实际 {m}"
        if any(not isinstance(row, list) or len(row) != len(m) for row in m):
            return "非方阵"
        if any(not _as_int(v) or not (-9 <= v <= 9) for row in m for v in row):
            return "矩阵元素应为 [-9,9] 内的 int"

    return None


def _expected_signature(tpl: str, p: dict) -> str:
    """A 的 signature 格式（契约 §7）。"""
    if tpl == "poly":
        return "poly|coeffs=%s|lower=%s|upper=%s" % (
            ",".join(str(v) for v in p["coeffs"]), p["lower"], p["upper"])
    if tpl == "det":
        return "det|matrix=" + ";".join(",".join(str(v) for v in row) for row in p["matrix"])
    return tpl + "|" + "|".join("%s=%s" % (k, p[k]) for k in sorted(p))


# ── 1. 125 题：模板名与参数键集合 ──────────────────────────────


@pytest.mark.parametrize("name", DATASETS)
def test_template_in_whitelist(name):
    for t in _load(name):
        assert t["template"] in TEMPLATES, f"{t['id']} 模板名 {t['template']!r} 不在 A 的白名单"


@pytest.mark.parametrize("name", DATASETS)
def test_param_keys_exact(name):
    """键集合必须**恰等于**，多一个少一个 A 都拒。"""
    for t in _load(name):
        assert set(t["params"]) == PARAM_KEYS[t["template"]], (
            f"{t['id']} ({t['template']}) 键集合 {sorted(t['params'])} "
            f"!= {sorted(PARAM_KEYS[t['template']])}")


@pytest.mark.parametrize("name", DATASETS)
def test_param_bounds_pass_a_validation(name):
    """这是核心：把 125 题按 A 的边界逐条拒一遍。"""
    bad = []
    for t in _load(name):
        reason = _check_params(t["template"], t["params"])
        if reason:
            bad.append(f"{t['id']}: {reason}")
    assert not bad, f"{len(bad)} 道题会被 A 的 validate_task 拒掉：\n" + "\n".join(bad[:10])


# ── 2. type 字段与模板的一致性 ─────────────────────────────────


def test_type_matches_template():
    for t in _all_tasks():
        expect = "det" if t["template"] == "det" else "integral"
        assert t["type"] == expect, f"{t['id']} type={t['type']} 与模板 {t['template']} 不符"


# ── 3. 验证题库（给 A 的 10 道）必须与契约逐字一致 ─────────────


# 已知越界但有意保留的条目：它们只用于测「纯函数」而非「出题校验」
KNOWN_BYPASS = {
    "V07": "抄自附录 A 的 D2 [[1,2,3],[4,5,6],[7,8,10]]，元素 10 超出 validate_task 的 "
           "[-9,9]。此条只用来测 bareiss 的「3 阶含负结果」，不经过出题校验；"
           "A 的 24 条用例同样如此（否则他的 D2 过不了自己的 validate_task）。",
}


def test_verify_bank_signature_format():
    bank = json.loads((DATA / "verify_bank.json").read_text(encoding="utf-8"))["tasks"]
    for row in bank:
        t = row["task"]
        assert t["template"] in TEMPLATES, f"{t['id']} 模板名非法：{t['template']}"
        assert set(t["params"]) == PARAM_KEYS[t["template"]], f"{t['id']} 键集合不对"
        reason = _check_params(t["template"], t["params"])
        if reason:
            assert t["id"] in KNOWN_BYPASS, f"{t['id']} 参数越界且未登记：{reason}"
        assert t["signature"] == _expected_signature(t["template"], t["params"]), (
            f"{t['id']} 签名格式与 A 的契约不符：\n"
            f"  实际 {t['signature']}\n  应为 {_expected_signature(t['template'], t['params'])}")


def test_known_bypass_entries_still_bypass():
    """登记表必须跟着事实走：某条越界一旦修好，就该从表里删掉，否则会掩盖新问题。"""
    bank = {row["task"]["id"]: row["task"]
            for row in json.loads((DATA / "verify_bank.json").read_text(encoding="utf-8"))["tasks"]}
    for tid in KNOWN_BYPASS:
        assert tid in bank, f"登记表里的 {tid} 已不存在"
        assert _check_params(bank[tid]["template"], bank[tid]["params"]) is not None, (
            f"{tid} 已不越界，请从 KNOWN_BYPASS 删除")


def test_verify_bank_reference_encoding():
    """A 的编码规则：分母为 1 一律 integer，其余 rational，数字全是字符串。"""
    bank = json.loads((DATA / "verify_bank.json").read_text(encoding="utf-8"))["tasks"]
    for row in bank:
        t = row["task"]
        ref = t["reference"]
        if t["template"] == "det":
            assert ref["kind"] == "integer", f"{t['id']} 行列式只能用 integer"
            assert isinstance(ref["value"], str)
        else:
            assert ref["kind"] in ("integer", "rational"), f"{t['id']} kind 非法"
            for k in ("value", "numerator", "denominator"):
                if k in ref:
                    assert isinstance(ref[k], str), f"{t['id']} 的 {k} 必须是字符串"


# ── 4. 签名互斥（回填后才有值，回填前跳过） ───────────────────


def test_signatures_mutually_exclusive_across_datasets():
    """题集签名互斥（规格 §5）。回填前 signature 为 null，此测试自动跳过。"""
    sigs = []
    for t in _all_tasks():
        if t.get("signature"):
            sigs.append(t["signature"])
    if not sigs:
        pytest.skip("signature 尚未由 A 的 executor 回填")
    assert len(sigs) == len(set(sigs)), f"有 {len(sigs) - len(set(sigs))} 个重复签名"
