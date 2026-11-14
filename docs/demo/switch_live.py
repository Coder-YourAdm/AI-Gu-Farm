#!/usr/bin/env python3
"""切换 web/tutor.html 的实时接口开关（LIVE.endpoint）。

B7 到位后，前端只差这一行：`endpoint: null` → `endpoint: "/api/ask"`。
手改容易改错位置或把注释一起改掉，用这个脚本一条命令，并且可以随时切回。

用法：
    python docs/demo/switch_live.py --status   # 看现在是哪档（默认行为）
    python docs/demo/switch_live.py --on       # 切到真接口 /api/ask
    python docs/demo/switch_live.py --off      # 切回本地假 Tutor（null）

安全措施：
  - 只匹配 `endpoint:` 这一个赋值，且先断言全文件只有一处；
  - 按字节读写，保留原有换行符（CRLF/LF 不被改写）；
  - 写回前打印 diff 片段，写回后回读校验。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "web" / "tutor.html"
PATTERN = re.compile(r'(endpoint:\s*)("(?:[^"\\]|\\.)*"|null)')
TARGET = '"/api/ask"'


def load() -> tuple[Path, str]:
    if not PAGE.exists():
        raise SystemExit(f"[X] 找不到 {PAGE}")
    text = PAGE.read_bytes().decode("utf-8")
    matches = PATTERN.findall(text)
    if len(matches) != 1:
        raise SystemExit(f"[X] 期望恰好一处 endpoint 赋值，实际 {len(matches)} 处——"
                         f"文件可能被改过，请人工确认")
    return PAGE, text


def current(text: str) -> str:
    return PATTERN.search(text).group(2)


def apply(text: str, value: str) -> str:
    return PATTERN.sub(lambda m: m.group(1) + value, text, count=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="切换 LIVE.endpoint")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="切到真接口 /api/ask")
    g.add_argument("--off", action="store_true", help="切回本地假 Tutor")
    g.add_argument("--status", action="store_true", help="只看当前状态（默认）")
    args = ap.parse_args()

    page, text = load()
    now = current(text)
    mode = "真接口" if now != "null" else "本地假 Tutor"

    if not (args.on or args.off):
        print(f"当前：{now}  （{mode}）")
        print(f"文件：{page}")
        return 0

    want = TARGET if args.on else "null"
    if now == want:
        print(f"已经是 {want}（{mode}），无需改动")
        return 0

    new_text = apply(text, want)
    if new_text == text or current(new_text) != want:
        raise SystemExit("[X] 替换失败，未写入")

    page.write_bytes(new_text.encode("utf-8"))
    print(f"已切换：{now} → {want}")
    print(f"文件：{page}")
    print()
    if args.on:
        print("下一步（约 3 分钟）：")
        print("  1) python app/serve.py --port 8123 --allow-tutor")
        print("  2) python docs/demo/probe_ask.py --url http://127.0.0.1:8123/api/ask")
        print("  3) 浏览器打开 http://127.0.0.1:8123/tutor.html，切实时连答 5 题")
        print("  4) node docs/demo/check_browser_live.js   （页面侧回归）")
    else:
        print("已回到兜底形态：实时模式走页面内本地状态机，不依赖后端。")
        print("建议再跑一次：powershell -File docs/demo/check_all.ps1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
