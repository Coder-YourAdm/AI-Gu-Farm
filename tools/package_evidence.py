"""把证据打成 zip（给 C / A 交接）。只收正式运行与冻结文件，不收开发期尝试的原始响应。

内容：
  protocol_v5 / calibration_v5 / ancestors / result_brief
  summaries（全部，含中止记录，便于解释哪些不是正式数据）
  plans / evaluations（两对留出，逐题）
  snapshots（只收 4 趟正式运行）
  docs：三份 B 文档 + charts 三张 CSV
  calls.jsonl（调用审计，含密钥脱敏后的索引）
  MANIFEST.txt：每个文件的 SHA-256
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "B_证据包_20261114.zip"
FORMAL = ["E_s42_7f878e7097", "F_s42_7f878e7097",
          "E_s43_7f878e7097", "F_s43_7f878e7097"]

SINGLE = [
    "runs/protocol_v5.json", "runs/calibration_v5.json", "runs/ancestors.json",
    "runs/result_brief.json", "docs/result_brief.json",
    "docs/B_交付简报.md", "docs/B_运行清单.md", "docs/B_协议偏离说明.md",
    "logs/calls.jsonl",
]
GLOB = ["runs/summaries/*.json", "runs/plans/*.json", "runs/evaluations/*.json",
        "docs/charts/*.csv"]
DIRS = [f"runs/snapshots/{r}" for r in FORMAL]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    files: list[Path] = []
    for rel in SINGLE:
        p = ROOT / rel
        if not p.exists():
            raise SystemExit(f"缺少 {rel}")
        files.append(p)
    for pattern in GLOB:
        files.extend(sorted(ROOT.glob(pattern)))
    for d in DIRS:
        files.extend(sorted((ROOT / d).glob("g*.json")))
    files = sorted(set(files))

    manifest = ["# B 证据包 · 2026-11-14 · 代码 commit d771ecb / tag b-formal-20261114",
                "# 格式：sha256 相对路径", ""]
    manifest += [f"{sha256(p)}  {p.relative_to(ROOT).as_posix()}" for p in files]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, p.relative_to(ROOT).as_posix())
        z.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")

    size = OUT.stat().st_size / 1024 / 1024
    print(f"{OUT}  files={len(files) + 1}  {size:.1f} MB")
    with zipfile.ZipFile(OUT) as z:
        names = z.namelist()
        print("entries:", len(names))
        bad = z.testzip()
        if bad:
            raise SystemExit(f"zip 校验失败: {bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
