"""把 B 的单一 `runs/history.jsonl` 按 `run_id` 拆成每趟一份。

为什么必须有这一步：B 的 `SnapshotStore.history_path` 是 `root/history.jsonl`，
六趟共用一份（每行自带 `run_id`）；而 `app/export.py:load_history()` 与
`app/evaluate_holdout.py:pick_lineages()` 都是按「一趟一个 history.jsonl」读，
且只按 `gen` 去重、**不看 run_id**。

直接把根文件喂给它们，同代的六条记录会互相覆盖，最后出来的是六趟拼接成的
一条并不存在的曲线 —— 页面上一切正常，数字全错。所以拆分不是格式偏好，
是数据正确性问题。

用法：
    python tools/split_history.py                # 拆全部趟
    python tools/split_history.py --run-id EVO_E1  # 只拆一趟
    python tools/split_history.py --check        # 只报告，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "runs" / "history.jsonl"
DEST_ROOT = ROOT / "runs" / "snapshots"


def _stamp_manifest(rid: str, dest_root: Path, *, repo_root: Path) -> str:
    """给这一趟补一个 `manifest.json`，带 `backend` 字段。

    why：C 的「真假自证」只认 `runs/<run_id>/manifest.json` 里的 `backend == "real"`，
    否则页面挂 MOCK 红标、数字不算数。B 的引擎把 manifest 写在 `runs/manifests/<id>.json`，
    且**没有 backend 字段**，于是 B 跑出来的真数据会被一律标成假。

    判定不猜：只有同时满足
      ① config.model / base_url 不是 REPLACE_WITH 占位符（不是空跑配置）
      ② logs/raw/<run_id>/ 下存在真实 HTTP 记录（确实调过接口）
    才写 `backend: "real"`；否则写 `"unknown"`，让导出侧按假数据标记。
    「证明不了是真的，就标成假的」——这条不能因为换成 B 的引擎就松动。
    """
    # manifest 与 snapshots 同级（B 的 SnapshotStore 布局），不假设仓库根
    src = dest_root.parent / "manifests" / f"{rid}.json"
    out = dest_root / rid / "manifest.json"
    if not src.exists():
        return "（B 未写 manifest，保持未证明）"
    man = json.loads(src.read_text(encoding="utf-8"))
    cfg = man.get("config") or {}
    model = str(cfg.get("model") or "")
    base = str(cfg.get("base_url") or "")
    placeholder = ("REPLACE_WITH" in model or "REPLACE_WITH" in base
                   or not model or not base)
    raw_dir = repo_root / "logs" / "raw" / rid
    calls = list(raw_dir.glob("*.json")) if raw_dir.is_dir() else []
    ok_calls = 0
    for f in calls[:200]:                      # 抽样即可，不读全量
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if rec.get("status") == "ok":
            ok_calls += 1
    if placeholder:
        backend, why = "unknown", "config 的 model/base_url 仍是占位符"
    elif not ok_calls:
        backend, why = "unknown", "logs/raw 下没有成功调用记录"
    else:
        backend, why = "real", f"config 非占位符；logs/raw 有 {ok_calls} 条 ok 调用"

    man.update({"backend": backend, "backend_reason": why,
                "stamped_by": "tools/split_history.py"})
    out.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"backend={backend}（{why}）"


def split(src: Path, *, only: str | None = None, check: bool = False,
          dest_root: Path | None = None) -> dict[str, int]:
    if not src.exists():
        raise SystemExit(f"✗ 找不到 {src}（B 的引擎还没跑过任何一趟）")

    buckets: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"✗ {src}:{lineno} JSON 解析失败: {exc}") from exc
        rid = rec.get("run_id")
        if not rid:
            skipped += 1          # 没有 run_id 的行无法归属，宁可报数也不猜
            continue
        buckets[str(rid)].append(rec)

    if skipped:
        print(f"  ⚠ {skipped} 行没有 run_id，已跳过（不猜归属）", file=sys.stderr)

    written: dict[str, int] = {}
    for rid, recs in sorted(buckets.items()):
        if only and rid != only:
            continue
        recs.sort(key=lambda r: (r.get("gen") if isinstance(r.get("gen"), int) else -1))
        written[rid] = len(recs)
        if check:
            continue
        out = (dest_root or DEST_ROOT) / rid / "history.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="\n") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) + "\n")
        _stamp_manifest(rid, dest_root or DEST_ROOT, repo_root=ROOT)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按 run_id 拆分 B 的 history.jsonl")
    ap.add_argument("--run-id", dest="run_id", default=None)
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--dest-root", dest="dest_root", default=None,
                    help="写到哪（默认 runs/snapshots）")
    args = ap.parse_args(argv)

    written = split(Path(args.src), only=args.run_id, check=args.check,
                    dest_root=Path(args.dest_root) if args.dest_root else None)
    if not written:
        print(f"✗ 没有任何趟匹配 {args.run_id or '（全部）'}", file=sys.stderr)
        return 3
    verb = "可拆" if args.check else "已拆"
    for rid, n in written.items():
        print(f"  {verb} {rid}: {n} 代")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
