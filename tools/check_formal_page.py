"""正式实验页一键自检：数据一致性 + 三硬标准 + 四要点 + 渲染路径 + 互跳。

用法
----
    python tools/check_formal_page.py           # 人看的报告
    python tools/check_formal_page.py --quiet  # 只给退出码

退出码 0 = 全过；1 = 有问题（会打印具体哪条）。
渲染部分交给 tools/check_formal_render.js（Node 轻量 DOM 桩，沙箱无浏览器）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BRIEF_FILE = ROOT / "docs" / "result_brief.json"
PAGE = WEB / "formal.html"


def main() -> int:
    quiet = "--quiet" in sys.argv[1:]
    out: list[str] = []
    bad: list[str] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            bad.append(name + ("：" + detail if detail else ""))
        if not quiet:
            out.append(("  ✓ " if ok else "  ✗ ") + name + (("  — " + detail) if detail and ok else ""))

    # ① 文件齐
    for p in (BRIEF_FILE, PAGE, ROOT / "tools" / "build_formal_page.py",
              ROOT / "tools" / "check_formal_render.js"):
        ck(f"存在 {p.relative_to(ROOT).as_posix()}", p.is_file())

    brief = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    html = PAGE.read_text(encoding="utf-8")

    # ② 内联数据与 brief 逐字段一致（防止有人手改页面数字）
    m = re.search(r'<script id="brief" type="application/json">([\s\S]*?)</script>', html)
    ck("页面内联 brief 可解析", bool(m))
    if m:
        inlined = json.loads(m.group(1))
        ck("内联数据与 docs/result_brief.json 深度相等", inlined == brief)
        ck("内联数据非占位", len(m.group(1)) > 1000)

    # ③ 三硬标准
    ck("页面无 reference 字样", "reference" not in html.lower())
    ext = re.findall(r'(?:src|href)="https?://[^"]*"', html)
    ck("零外链（断网 file:// 可开）", not ext, f"外链 {len(ext)} 条")
    ck("单文件内联脚本", len(re.findall(r"<script>", html)) == 1)
    ck("无入场动画（opacity 关键帧）", "@keyframes" not in html and "fadeIn" not in html)

    # ④ B 的四要点（静态文案 + 数据条件）
    ck("① 真模型绿标 / 无 mock 徽章", "badge live" in html and "● 真模型" in html
       and "badge mock" not in html)
    ck("② 挑战图读 runs[].stats.challenges", "stats.challenges" in html)
    ck("② 挑战题标未达标、不画成达标", "未达 3/6 门槛" in html and "不画成达标" in html)
    done = [k for k, v in brief["runs"].items() if v["status"] == "completed"]
    x04_zero = all(
        all(o[1] == 0 for o in brief["runs"][k]["stats"]["challenges"]["X04"]["observations"])
        for k in done
    )
    ck("② X04 四趟六次全 0（与数据一致）", x04_zero)
    x05_max = max(
        max((o[1] for o in brief["runs"][k]["stats"]["challenges"]["X05"]["observations"]), default=0)
        for k in done
    )
    ck("② X05 最高 2/6，未达 3/6 门槛", x05_max == 2, f"实际 {x05_max}")
    aborted = [k for k, v in brief["runs"].items() if v["status"] != "completed"]
    ck("③ 取消/未启动 2 趟且不参与配对", len(aborted) == 2 and all(
        all(p["E_run"] != k and p["F_run"] != k for p in brief["pairs"]) for k in aborted))
    ck("③ 页面有「不计 0 分」文案", "不计 0 分" in html)
    ck("④ limits 五条", len(brief["limits"]) == 5)
    ck("④ 页面渲染 limits[]", "id=\"limits\"" in html and "BRIEF.limits.map" in html)

    # ⑤ 数字自洽（与页面口径自检同规则）
    for i, p in enumerate(brief["pairs"]):
        f, cps = p["final"], p["checkpoints"]
        ck(f"pairs[{i}] E−F 自洽", abs((f["E"] - f["F"]) - f["E_minus_F"]) <= 0.002)
        ck(f"pairs[{i}] 末点=final", abs(cps[-1]["E"] - f["E"]) <= 0.002
           and abs(cps[-1]["F"] - f["F"]) <= 0.002)
        ck(f"pairs[{i}] 检查点 5 个", len(cps) == 5)
    mean = sum(brief["main_result"]["E_minus_F_by_pair"]) / len(brief["main_result"]["E_minus_F_by_pair"])
    ck("主结果均值自洽", abs(mean - brief["main_result"]["E_minus_F_mean"]) <= 0.002)

    # ⑥ 四页互跳目标存在
    for name in ("index", "evolve", "result", "formal"):
        t = (WEB / f"{name}.html").read_text(encoding="utf-8")
        links = re.findall(r'href="([^"]*\.html)"', t)
        ck(f"{name}.html 互跳目标存在", all((WEB / x).is_file() for x in links),
           ",".join(links))
    for name in ("index", "evolve", "result"):
        ck(f"{name}.html 有正式实验入口",
           "formal.html" in (WEB / f"{name}.html").read_text(encoding="utf-8"))

    # ⑦ 渲染路径（Node DOM 桩）
    node = ROOT / "tools" / "check_formal_render.js"
    try:
        r = subprocess.run(["node", str(node)], capture_output=True, text=True,
                           cwd=ROOT, timeout=120)
        ck("渲染自检脚本通过", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[:120])
        if not quiet and r.returncode == 0:
            out.append("  --- 渲染自检摘要 ---")
            for line in r.stdout.strip().splitlines():
                if "PASS" in line or "FAIL" in line or "RESULT" in line:
                    out.append("  " + line.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        ck("渲染自检脚本可执行", False, str(e))

    # ⑧ 现有交付数据未被改动（这一层是新增，不覆盖）
    rj = json.loads((WEB / "result.json").read_text(encoding="utf-8"))
    ck("result.json 仍是 E_real 交付数据（未被覆盖）",
       rj.get("is_mock") is False and rj.get("backend") == "real"
       and rj.get("schema") == "result-board")

    if not quiet:
        print("正式实验页自检 · " + str(PAGE.relative_to(ROOT).as_posix()))
        print("=" * 62)
        print("\n".join(out))
        print()
        if bad:
            print("RESULT: FAIL（" + str(len(bad)) + "）")
            for b in bad:
                print("  - " + b)
        else:
            print("RESULT: PASS（数据一致 / 三硬标准 / 四要点 / 渲染 / 互跳 / 旧数据未动）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
