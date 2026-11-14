#!/usr/bin/env python3
"""把三页打成"自包含"版本：数据内联，双击即看，不需要起服务、不需要联网。

为什么需要：`web/*.html` 用 fetch 读同目录 JSON，浏览器在 file:// 下会拦掉，
于是双击只能看到"内联示例数据"黄条。分享给别人看时这条路走不通。
本脚本把两份 JSON 直接注入页面，并劫持 fetch 返回内存数据，页面代码一行不改。

用法：
    python docs/demo/build_share.py                 # 输出到 dist/share/
    python docs/demo/build_share.py --zip           # 额外打一个 zip

产物：
    dist/share/cover.html  tutor.html  compare.html  README.txt
    dist/日新考研助手_前端预览.zip（加 --zip 时）
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
PAGES = ("cover.html", "tutor.html", "compare.html")

SHIM = """<script>
/* 自包含打包：数据已内联，file:// 双击也能看到真数据（不依赖任何服务） */
(function () {
  var DATA = %s;
  var orig = window.fetch;
  window.fetch = function (url) {
    var key = String(url);
    if (Object.prototype.hasOwnProperty.call(DATA, key)) {
      return Promise.resolve(new Response(JSON.stringify(DATA[key]), {
        status: 200, headers: { "Content-Type": "application/json" }
      }));
    }
    return orig.apply(this, arguments);
  };
})();
</script>
"""

README = """日新考研助手 · 前端预览（自包含版）

怎么打开
  双击 cover.html 即可，不需要装任何东西、也不需要联网。
  三页互相跳转：封面 → 诊断 → 对照。

能点什么
  1) 诊断页：点题面气泡 → 右侧出现"掌握度 / 难度 / 上次错因"
  2) 诊断页：点左侧知识点（定积分/反常积分/行列式）→ 对话流被过滤，再点取消
  3) 诊断页：点记忆条目 → 展开来源事件 id
  4) 诊断页：切右上「实时」→ 出现输入框，可以打答案（Enter 提交）；
     「我不会 / 再提示一点 / 为什么给我这道题 / 重置」四个按钮都能用
  5) 对照页：切顶部「增益 / 成本 / 适应度」→ 条形图按指标重画

说明
  - 这是前端预览包：数据是仓库里的 8 轮回放快照（tutor.json）与 9 组对照（compare.json），
    已内联进页面，所以不需要后端。
  - 实时模式在这个包里走的是页面内置的本地状态机（后端接口 /api/ask 尚未合入）。
  - 页面零外链，断网也能开。
"""


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {}
    for name in ("tutor.json", "compare.json"):
        path = WEB / name
        if not path.exists():
            raise SystemExit(f"[X] 缺少 {path}，先跑 A 的 tutor_cli / compare")
        data[name] = json.loads(path.read_text(encoding="utf-8"))

    written: list[Path] = []
    for page in PAGES:
        html = (WEB / page).read_text(encoding="utf-8")
        # 离线分享包没有后端：强制把实时接口关掉，避免对方点实时吃到 404/黄条。
        html = html.replace('endpoint: "/api/ask"', "endpoint: null")
        if "</head>" not in html:
            raise SystemExit(f"[X] {page} 里没有 </head>，无法注入")
        shim = SHIM % json.dumps(data, ensure_ascii=False, indent=1)
        html = html.replace("</head>", shim + "</head>", 1)
        dst = out_dir / page
        dst.write_text(html, encoding="utf-8", newline="\n")
        written.append(dst)

    (out_dir / "README.txt").write_text(README, encoding="utf-8", newline="\n")
    written.append(out_dir / "README.txt")
    return written


def make_zip(out_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.iterdir()):
            zf.write(p, arcname="AI助教_前端预览/" + p.name)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包自包含前端")
    ap.add_argument("--out", default=str(ROOT / "dist" / "share"))
    ap.add_argument("--zip", action="store_true", help="额外生成 zip")
    args = ap.parse_args()

    out_dir = Path(args.out)
    files = build(out_dir)
    for f in files:
        print(f"  {f.relative_to(ROOT)}  {f.stat().st_size / 1024:.1f} KB")

    if args.zip:
        zip_path = ROOT / "dist" / "日新考研助手_前端预览.zip"
        make_zip(out_dir, zip_path)
        print(f"ZIP: {zip_path}  {zip_path.stat().st_size / 1024:.1f} KB")
    print("OK：双击 cover.html 即可（不需要起服务）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
