"""四类题集的契约测试。

依据
----
技术规格 §2.1 L113–L131  参数边界（精确表，实现时照抄）
技术规格 §2.2 L137–L149  后端 Task 十个键
技术规格 §5   L330       四类题集数量与组成、签名互斥、三类积分均衡

设计
----
本测试在「回填前」和「回填后」都必须通过：

- 回填前：`signature` / `statement` / `reference` 为 `null`，签名互斥用本地算法核验
- 回填后：三个字段非空，且 `signature` 两两不同

所以不要假设 `signature` 一定有值 —— A 填完直接跑，测试自然升级为更强的一档。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT))
from tests.make_datasets import (  # noqa: E402
    INT_TEMPLATES,
    _det_fraction,
    _local_signature,
    validate_params,
)

NAMES = ("calibration", "common_dev", "challenge_pool", "holdout")
EXPECT_COUNT = {"calibration": 20, "common_dev": 80,
                "challenge_pool": 5, "holdout": 20}
KEY_ORDER = ["id", "source", "by", "gen", "type", "template",
             "params", "signature", "statement", "reference"]


@pytest.fixture(scope="module")
def datasets() -> dict[str, list[dict]]:
    return {n: json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
            for n in NAMES}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


# ── 1. 数量与组成（§5 L330）──────────────────────────────────
@pytest.mark.parametrize("name,count", EXPECT_COUNT.items())
def test_count(datasets, name: str, count: int) -> None:
    assert len(datasets[name]) == count, f"{name} 应为 {count} 道"


@pytest.mark.parametrize("name", ["calibration", "holdout"])
def test_integral_det_10_10(datasets, name: str) -> None:
    rows = datasets[name]
    assert sum(r["type"] == "integral" for r in rows) == 10
    assert sum(r["type"] == "det" for r in rows) == 10


def test_common_dev_groups(datasets) -> None:
    """§5：common_dev 按 gen 分 20 组，每组 integral=2、det=2。"""
    groups: dict[int, list[dict]] = {}
    for r in datasets["common_dev"]:
        groups.setdefault(r["gen"], []).append(r)
    assert len(groups) == 20, f"应有 20 组，实际 {len(groups)}"
    for gen, rows in groups.items():
        assert len(rows) == 4, f"第 {gen} 组应 4 题"
        assert sum(r["type"] == "integral" for r in rows) == 2
        assert sum(r["type"] == "det" for r in rows) == 2


def test_integral_templates_balanced(datasets) -> None:
    """§5：三类积分在全卷中尽量均衡。"""
    counts = {t: 0 for t in INT_TEMPLATES}
    for name in NAMES:
        for r in datasets[name]:
            if r["template"] in counts:
                counts[r["template"]] += 1
    assert max(counts.values()) - min(counts.values()) <= 2, f"失衡：{counts}"
    assert sum(counts.values()) == 63, f"积分总题数应为 63，实际 {sum(counts.values())}"


# ── 2. 字段结构（§2.2）──────────────────────────────────────
def test_key_order_and_names(datasets) -> None:
    for name in NAMES:
        for r in datasets[name]:
            assert list(r) == KEY_ORDER, f"{r['id']} 键不对：{list(r)}"


def test_no_extra_keys(datasets) -> None:
    """§2.1：不接受未知键。"""
    for name in NAMES:
        for r in datasets[name]:
            extra = set(r) - set(KEY_ORDER)
            assert not extra, f"{r['id']} 有多余键 {extra}"


def test_type_matches_template(datasets) -> None:
    for name in NAMES:
        for r in datasets[name]:
            want = "det" if r["template"] == "det" else "integral"
            assert r["type"] == want, f"{r['id']} type 与 template 不符"


def test_source_matches_dataset(datasets) -> None:
    for name in NAMES:
        for r in datasets[name]:
            assert r["source"] == name, f"{r['id']} source 应为 {name}"


def test_ids_unique(datasets) -> None:
    ids = [r["id"] for n in NAMES for r in datasets[n]]
    dup = [i for i in set(ids) if ids.count(i) > 1]
    assert not dup, f"id 重复：{dup}"


# ── 3. 参数边界（§2.1 精确表）──────────────────────────────
def test_params_within_bounds(datasets) -> None:
    for name in NAMES:
        for r in datasets[name]:
            try:
                validate_params(r["template"], r["params"])
            except AssertionError as e:
                pytest.fail(f"{r['id']} ({r['template']}) 越界：{e}")


def test_det_nonsingular(datasets) -> None:
    """行列式非零 —— 奇异矩阵答案恒为 0，无区分度。"""
    for name in NAMES:
        for r in datasets[name]:
            if r["template"] == "det":
                assert _det_fraction(r["params"]["matrix"]) != 0, f"{r['id']} 奇异"


def test_ints_are_really_int(datasets) -> None:
    """§2.1：整数必须 type(v) is int，拒绝 bool/float/字符串替代。"""
    for name in NAMES:
        for r in datasets[name]:
            for k, v in r["params"].items():
                if isinstance(v, list):
                    flat = [x for row in v for x in row] if (
                        v and isinstance(v[0], list)) else v
                    for x in flat:
                        assert type(x) is int, f"{r['id']}.{k} 含非 int"
                else:
                    assert type(v) is int, f"{r['id']}.{k} 非 int"


# ── 4. 签名互斥（§5）──────────────────────────────────────
def test_signatures_mutually_exclusive(datasets) -> None:
    """四类题集签名互斥。回填前用本地算法核验，回填后优先用真签名。"""
    real = [r["signature"] for n in NAMES for r in datasets[n]]
    filled = all(s is not None for s in real)

    if filled:
        dup = [s for s in set(real) if real.count(s) > 1]
        assert not dup, f"真签名重复：{dup[:5]}"
    else:
        sigs = [_local_signature(r["template"], r["params"])
                for n in NAMES for r in datasets[n]]
        dup = [s for s in set(sigs) if sigs.count(s) > 1]
        assert not dup, f"本地签名重复：{dup[:5]}"


def test_three_fields_null_before_backfill(datasets) -> None:
    """回填前三个判分字段必须为 null —— 防止有人手工塞了个值进去。"""
    rows = [r for n in NAMES for r in datasets[n]]
    if all(r["signature"] is not None for r in rows):
        pytest.skip("已回填，本项不适用")
    for r in rows:
        assert r["signature"] is None, f"{r['id']}.signature 应留空给 A"
        assert r["statement"] is None, f"{r['id']}.statement 应留空给 A"
        assert r["reference"] is None, f"{r['id']}.reference 应留空给 A"


# ── 5. manifest 与实际文件一致 ──────────────────────────────
def test_manifest_matches_files(manifest, datasets) -> None:
    assert manifest["seed"] == 20261113
    for name in NAMES:
        info = manifest["files"][name]
        assert info["count"] == len(datasets[name])
        body = (DATA / f"{name}.json").read_bytes()
        assert len(body) == info["bytes"], f"{name} 字节数变了"
        assert hashlib.sha256(body).hexdigest() == info["sha256"], (
            f"{name} 内容变了但 manifest 没更新 —— 改完数据必须重跑 make_datasets.py")


def test_manifest_declares_field_status(manifest, datasets) -> None:
    """manifest 的字段状态必须与数据实际状态一致。

    回填前后都合法，但不许「数据填了、manifest 还说 null」——
    那会让下一个人再去回填一遍，签名重算、指纹全变。
    """
    fs = manifest["field_status"]
    rows = [r for n in NAMES for r in datasets[n]]
    filled = all(r["signature"] is not None and r["reference"] is not None
                 for r in rows)
    for k in ("signature", "statement", "reference"):
        if filled:
            assert "null" not in fs[k], f"数据已回填，manifest 仍称 {k} 为 null"
            assert "filled" in fs[k], f"manifest 未声明 {k} 已填"
        else:
            assert "null" in fs[k], f"manifest 未声明 {k} 待回填"


def test_manifest_records_backfill_provenance(manifest, datasets) -> None:
    """回填过的数据必须记下用的是哪个版本的 executor。"""
    rows = [r for n in NAMES for r in datasets[n]]
    if all(r["reference"] is None for r in rows):
        pytest.skip("尚未回填")
    assert "backfilled" in manifest, "已回填但 manifest 没有 backfilled 段"
    assert len(manifest["backfilled"]["executor_sha256"]) == 64, "executor 指纹缺失"
    assert manifest["backfilled"]["unique_signatures"] == len(rows)
