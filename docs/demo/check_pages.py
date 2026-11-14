"""C 岗三页静态自检（零依赖，纯标准库）。

用法：在仓库根目录执行  python docs/demo/check_pages.py

检查项：
  1. 三页顶栏 CSS 是否逐字节一致（C6 样式统一）
  2. tutor.html 九个契约 id 是否齐全（实施方案 §5.4）
  3. 三硬检查：不出现 reference 字样、零外链
  4. 三页互跳链接是否都能落到真实文件（无 404）
  5. 文件本身是否有 UTF-8 损坏（U+FFFD）
  6. 三页导航文案是否一致、当前页高亮是否正确
"""
import hashlib
import json
import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文会乱码；统一按 UTF-8 输出（配合 chcp 65001）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
    pass

WEB = Path(__file__).resolve().parents[2] / "web"
PAGES = ["cover.html", "tutor.html", "compare.html"]
IDS = ["mode", "kcs", "turns", "why-panel", "memory-timeline",
       "review-list", "version", "answer", "send"]
NAV_EXPECT = ["\u5c01\u9762", "\u8bca\u65ad", "\u5bf9\u7167"]  # 封面 / 诊断 / 对照


def header_block(text: str) -> str:
    i = text.index("header{display:flex")
    j = text.index(".ver{")
    k = text.index("}", j) + 1
    return text[i:k]


def main() -> int:
    failed = 0
    texts = {p: (WEB / p).read_text(encoding="utf-8") for p in PAGES}

    print("== 1. header block identity ==")
    hashes = {}
    for p in PAGES:
        b = header_block(texts[p])
        hashes[p] = hashlib.sha256(b.encode("utf-8")).hexdigest()[:16]
        print(f"  {p:14s} len={len(b):4d} sha={hashes[p]}")
    same_header = len(set(hashes.values())) == 1
    print("  identical:", same_header)
    failed += 0 if same_header else 1

    print("== 2. tutor.html ids ==")
    missing = [i for i in IDS if f'id="{i}"' not in texts["tutor.html"]]
    print("  missing:", missing or "none")
    failed += 0 if not missing else 1
    has_max = 'maxlength="100"' in texts["tutor.html"]
    print("  answer maxlength=100:", has_max)
    failed += 0 if has_max else 1

    print("== 3. three hard checks ==")
    for p in PAGES:
        t = texts[p]
        ref = t.count("reference")
        ext = len(re.findall(r"https?://", t))
        print(f"  {p:14s} reference={ref} http={ext}")
        failed += 0 if ref == 0 and ext == 0 else 1

    print("== 4. nav hrefs ==")
    # 只校验页面互跳的相对 .html 链接（C6 验收口径）。
    # data: URI（<link rel="icon" href="data:,">）、锚点、外链不是本地文件：
    # 前者无需 resolve，后两者由第 3 项三硬标准负责，别在这里误报 BROKEN。
    nav_bad = 0
    for p in PAGES:
        for h in re.findall(r'href="([^"#]+)"', texts[p]):
            if ":" in h or not h.endswith(".html"):
                continue
            if not (WEB / h).exists():
                print(f"  BROKEN {p} -> {h}")
                nav_bad += 1
    failed += nav_bad
    print("  all html hrefs resolve:", "yes" if nav_bad == 0 else "see above")

    print("== 5. encoding damage (U+FFFD) ==")
    for p in PAGES:
        bad = texts[p].count(chr(0xFFFD))
        print(f"  {p:14s} bad_chars={bad}")
        failed += 0 if bad == 0 else 1

    print("== 6. nav labels & active state ==")
    for p in PAGES:
        nav = re.search(r'<nav class="pages">(.*?)</nav>', texts[p], re.S).group(1)
        labels = re.findall(r">([^<>]+)</a>", nav)
        active = [h for h, body in re.findall(r'<a href="([^"]+)"([^>]*)>', nav)]
        on = re.findall(r'<a href="([^"]+)" class="on"', nav)
        self_ok = on == [p]
        print(f"  {p:14s} labels={ascii(labels)} active={self_ok}")
        failed += 0 if labels == NAV_EXPECT and self_ok else 1

    print("== 7. replay data source（真数据是否到位） ==")
    for name, kind in (("tutor.json", "tutor"), ("compare.json", "compare")):
        p = WEB / name
        if not p.exists():
            print(f"  {name:14s} MISSING -> 页面用内联示例并挂黄条"
                  f"（正式截图前必须补，否则等于 mock 徽章）")
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 - 报告里要看到具体原因
            print(f"  {name:14s} BAD JSON: {e}")
            failed += 1
            continue
        leak = json.dumps(j, ensure_ascii=False).count("reference")
        if kind == "tutor":
            kcs = j.get("kcs") or []
            tutors = [t for t in j.get("turns", []) if t.get("role") == "tutor"]
            why_ok = all("why" in t for t in tutors)
            good = len(kcs) == 3 and why_ok and leak == 0
            print(f"  {name:14s} kcs={len(kcs)} tutor_turns={len(tutors)} "
                  f"why_ok={why_ok} reference={leak}")
        else:
            rows = j.get("rows") or []
            good = len(rows) == 9 and leak == 0
            print(f"  {name:14s} rows={len(rows)} reference={leak}")
        failed += 0 if good else 1

    print()
    print("RESULT:", "PASS" if failed == 0 else f"FAIL ({failed})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
