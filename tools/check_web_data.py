"""交付页面数据哨兵：一条命令确认 `web/` 里挂的还是不是交付数据。

为什么需要这个文件
------------------
2026-11-14 上午，`app/evaluate_holdout.py` 把结果写死到 `web/result.json`
（`write_json(out.name, ...)`，name 恒为 "result.json"），于是 E/F 六趟跑批
**每一趟都覆盖页面数据**，E_real 的交付结果被最后一趟 OFF_E1 静默顶掉。
表面症状是"页面突然变成 mock / 数字全变"，没有任何报错。

代码端的闸已经加了两道：
  1. `--out` 默认值不再指向 `web/`（默认落到 history 同目录）
  2. `--out` 指向 `web/` 直接被拒绝，发布只能显式 `--publish`

但代码闸只拦得住"用新代码的人"。如果有人从旧分支拉代码跑批，还是能覆盖。
所以再加这一道：**跑批前后各跑一次，两分钟就能确认交付数据还在**。

用法
----
    python tools/check_web_data.py            # 人看的报告
    python tools/check_web_data.py --quiet    # 只返回退出码，给脚本用

退出码：0 = 交付数据完好；1 = 被改过（会打印恢复命令）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# 交付基线：页面三份数据各自的身份。改这三行等于改交付口径，须同步 README。
EXPECT = {
    "history.json": {
        "label": "演化大屏（history）",
        "must": {"run_id": "E_real_20261114", "is_mock": False},
        "note": "21 代 / 724 次调用",
    },
    "evolve.json": {
        "label": "双跑对照（evolve）",
        "must": {"pair_id": "demo_pair_calib", "is_mock": False},
        "note": "20/20 → 19/19，判定 no_change",
    },
    "result.json": {
        "label": "留出结果（result）",
        "must": {"is_mock": False, "backend": "real"},
        "run_prefix": "E_real_20261114_",
        "note": "3 趟重测，level=none，infra_truncated=0",
    },
}


def load_bundle(path: Path) -> dict:
    """从 data.js 里取出 window.WB_DATA。解析失败 = 兜底包坏了，等于断网打不开。"""
    s = path.read_text(encoding="utf-8")
    i = s.find("window.WB_DATA")
    if i < 0:
        raise ValueError("data.js 里找不到 window.WB_DATA")
    s = s[i + len("window.WB_DATA"):].lstrip()
    if not s.startswith("="):
        raise ValueError("window.WB_DATA 后面不是赋值")
    s = s[1:].strip()
    if s.endswith(";"):
        s = s[:-1]
    return json.loads(s)


def check_one(name: str, spec: dict, bundle: dict, out: list[str],
              web: Path = WEB) -> bool:
    ok = True
    p = web / name
    if not p.exists():
        out.append(f"✗ {name} 不存在")
        return False

    try:
        disk = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        out.append(f"✗ {name} 读不出来：{e}")
        return False

    # ① 身份字段
    for k, want in spec["must"].items():
        got = disk.get(k)
        if got != want:
            out.append(f"✗ {name} 的 {k} 是 {got!r}，交付基线要求 {want!r}")
            ok = False

    # ② 趟次前缀（只有 result.json 有这个维度）
    if "run_prefix" in spec:
        ids = [r.get("run_id", "") for r in disk.get("runs", [])]
        bad = [i for i in ids if not i.startswith(spec["run_prefix"])]
        if not ids:
            out.append(f"✗ {name} 的 runs 是空的")
            ok = False
        elif bad:
            out.append(f"✗ {name} 的趟次不是交付基线：{bad}（应全部以 "
                       f"{spec['run_prefix']} 开头）")
            ok = False

    # ③ data.js 是否与 JSON 同步——不同步意味着双击页面看到的是旧数
    if name not in bundle:
        out.append(f"✗ data.js 里没有 {name}（断网双击会读不到）")
        ok = False
    elif bundle[name] != disk:
        out.append(f"✗ data.js 里的 {name} 与磁盘 JSON 不一致"
                   f"（双击页面看到的是旧数据，需要重跑 bundle）")
        ok = False

    if ok:
        out.append(f"✓ {name:<13} {spec['label']:<18} {spec['note']}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="确认 web/ 里挂的还是交付数据")
    ap.add_argument("--quiet", action="store_true", help="只返回退出码")
    ap.add_argument("--web", default=str(WEB), help="检查哪个 web/ 目录")
    a = ap.parse_args(argv)
    web = Path(a.web)

    lines: list[str] = []
    all_ok = True

    # 兜底包本身：断了的话 file:// 双击就是白屏/报错
    dj = web / "data.js"
    bundle: dict = {}
    if not dj.exists():
        lines.append("✗ web/data.js 不存在——断网双击三页都打不开")
        all_ok = False
    else:
        try:
            bundle = load_bundle(dj)
            lines.append(f"✓ data.js      {dj.stat().st_size // 1024} KB · "
                         f"含 {len(bundle)} 份数据")
        except Exception as e:
            lines.append(f"✗ data.js 坏了：{e}——断网双击三页都打不开")
            all_ok = False

    for name, spec in EXPECT.items():
        if not check_one(name, spec, bundle, lines, web):
            all_ok = False

    if not a.quiet:
        print("交付页面数据哨兵")
        print("─" * 62)
        for ln in lines:
            print(" " + ln)
        print("─" * 62)

    if not all_ok:
        if not a.quiet:
            print("\n交付数据已被改动。恢复（丢弃所有对 web/ 的改动）：")
            print("   git checkout -- web/")
            print("   若 git 里也没有，从归档恢复：")
            print("   copy D:\\WorkBuddy-temp\\restored_result.json web\\result.json")
            print("   然后 python -m app.webdata   # 重建 data.js")
            print("\n注意：先确认是谁改的。跑批覆盖 = 有人用了旧的 evaluate_holdout。")
        return 1

    if not a.quiet:
        print("\n三份数据与交付基线一致，双击页面看到的即交付数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
