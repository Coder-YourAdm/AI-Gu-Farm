#!/usr/bin/env python
"""打包分发物：把当前工作区的文件打进 分发/ 下的两个 zip。

**为什么需要这个脚本**

第一次打包是手敲 `zip` 命令，20:25 打的；21:35 才把 125 题的 reference 回填。
结果 `分发/给A_题集数据.zip` 里躺着的还是 `reference: null` 的旧数据 ——
一边在微信上说「已回填」，一边发出去的是空答案。

打包这件事只要靠手敲，就一定会在数据更新后忘记重跑。所以清单写在这，
`tests/test_dispatch.py` 逐字节比对 zip 内容与磁盘内容，过期立刻红。

**可复现**：固定时间戳 + 固定压缩级别 + 条目按清单顺序，同样的内容重跑
得到逐字节相同的 zip。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "分发"

# 条目名 -> 源文件（相对项目根）。
# 用"包内路径"作 key，能看出收件人打开 zip 看到的结构。
PACKS: dict[str, dict[str, str]] = {
    "给A_题集数据.zip": {
        "给A_README.md": "分发/给A_README.md",
        "data/calibration.json": "data/calibration.json",
        "data/common_dev.json": "data/common_dev.json",
        "data/challenge_pool.json": "data/challenge_pool.json",
        "data/holdout.json": "data/holdout.json",
        "data/manifest.json": "data/manifest.json",
        "data/verify_bank.json": "data/verify_bank.json",
        "十道验证题_给A核对.md": "十道验证题_给A核对.md",
        "给A_边界冲突说明.md": "给A_边界冲突说明.md",
        "tests/test_datasets.py": "tests/test_datasets.py",
        "tests/test_contract_compat.py": "tests/test_contract_compat.py",
        "tests/make_datasets.py": "tests/make_datasets.py",
        "tests/__init__.py": "tests/__init__.py",
        "pytest.ini": "pytest.ini",
    },
    "给B_prompts.zip": {
        "给B_README.md": "分发/给B_README.md",
        "prompts/setter.md": "prompts/setter.md",
        "prompts/solver.md": "prompts/solver.md",
        "prompts/seed_genotypes.json": "prompts/seed_genotypes.json",
        "prompts/crossover.md": "prompts/crossover.md",
        "prompts/mutate.md": "prompts/mutate.md",
        "一页纸_对接.md": "一页纸_对接.md",
        "对接说明_分工边界.md": "对接说明_分工边界.md",
        "tests/test_prompts.py": "tests/test_prompts.py",
        "pytest.ini": "pytest.ini",
    },
}

# 固定时间戳，让重跑产出逐字节相同的 zip（zip 默认写当前时间）。
FIXED_TIME = (2026, 11, 13, 0, 0, 0)


def build_one(zip_path: Path, entries: dict[str, str]) -> list[str]:
    missing = [src for src in entries.values() if not (ROOT / src).is_file()]
    if missing:
        raise FileNotFoundError("源文件不存在：" + "、".join(missing))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arcname, src in entries.items():
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (ROOT / src).read_bytes())
    return list(entries)


def build_all() -> dict[str, list[str]]:
    OUT.mkdir(parents=True, exist_ok=True)
    return {name: build_one(OUT / name, entries) for name, entries in PACKS.items()}


def main() -> int:
    result = build_all()
    for name, names in result.items():
        size = (OUT / name).stat().st_size
        print(f"✓ {name}  {len(names)} 项  {size:,} 字节")
        for n in names[:3]:
            print(f"    {n}")
        if len(names) > 3:
            print(f"    … 共 {len(names)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
