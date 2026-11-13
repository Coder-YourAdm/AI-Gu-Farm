"""分发包不许过期。

为什么专门写这个
----------------
第一次打包是手敲命令，20:25 打的；21:35 才把 125 题的 `reference` 回填。
于是 `分发/给A_题集数据.zip` 里躺着 `reference: null` 的旧数据 ——
微信上说「已回填」，发出去的是空答案，而两边都不会报错。

手打包这件事，只要数据一改就一定会忘。所以这里逐字节比对 zip 内容与磁盘内容：
数据回填了而 zip 没重打，测试立刻红。
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.make_dispatch import OUT, PACKS  # noqa: E402


@pytest.fixture(scope="module", params=sorted(PACKS), ids=lambda s: s.replace(".zip", ""))
def pack(request):
    name = request.param
    path = OUT / name
    if not path.is_file():
        pytest.skip(f"{name} 未打包，先跑 python tests/make_dispatch.py")
    return name, path, PACKS[name]


def test_zip_entries_match_manifest(pack):
    name, path, entries = pack
    with zipfile.ZipFile(path) as zf:
        got = zf.namelist()
    assert got == list(entries), f"{name} 条目与清单不一致"


def test_zip_content_is_current(pack):
    """核心断言：zip 里每个文件必须与磁盘上同名文件逐字节相同。"""
    name, path, entries = pack
    stale: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for arcname, src in entries.items():
            in_zip = hashlib.sha256(zf.read(arcname)).hexdigest()
            on_disk = hashlib.sha256((ROOT / src).read_bytes()).hexdigest()
            if in_zip != on_disk:
                stale.append(f"{arcname}（磁盘 {on_disk[:8]} / 包内 {in_zip[:8]}）")
    assert not stale, (
        f"{name} 已过期，以下文件与磁盘不一致，重跑 python tests/make_dispatch.py：\n  "
        + "\n  ".join(stale))


def test_no_external_backend_files(pack):
    """包内文件必须存在于磁盘，不许塞别处的东西。"""
    name, path, entries = pack
    for src in entries.values():
        assert (ROOT / src).is_file(), f"{name} 引用了不存在的文件：{src}"


def test_b_pack_carries_no_task_data():
    """给 B 的包不许带题集——他不需要 holdout，泄漏面越小越好。"""
    entries = PACKS["给B_prompts.zip"]
    assert not [n for n in entries if n.startswith("data/")], "给B 的包出现了 data/ 条目"


def test_a_pack_data_is_backfilled():
    """给 A 的题集必须是回填后的。这是那个旧包踩过的坑。"""
    path = OUT / "给A_题集数据.zip"
    if not path.is_file():
        pytest.skip("未打包")
    import json

    with zipfile.ZipFile(path) as zf:
        for arc in ("data/calibration.json", "data/common_dev.json",
                    "data/challenge_pool.json", "data/holdout.json"):
            rows = json.loads(zf.read(arc).decode("utf-8"))
            rows = rows["tasks"] if isinstance(rows, dict) else rows
            missing = [r["id"] for r in rows if r.get("reference") is None
                       or r.get("signature") is None or r.get("statement") is None]
            assert not missing, f"{arc} 里有 {len(missing)} 题未回填，例如 {missing[:3]}"
