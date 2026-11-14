"""web/ 下 JSON 的单文件兜底：把三份 JSON 打包成 web/data.js。

**为什么需要这个模块**

任务书 C-2 的验收原文是「浏览器直接 file:// 打开能出图」。这条从来没达标过 ——
以前所有测试都跑在 localhost 上，而三张页面用的是 `fetch("xxx.json")`，
file:// 双击时浏览器把读本地 JSON 当跨域，直接拦掉，页面只剩一行报错。
评委现场双击打开就是白屏，前面所有工夫归零。

`<script src="data.js">` 不受这条限制（脚本属于同源资源加载，不是 XHR），
所以把数据打成 JS 赋值语句是 file:// 下唯一稳的路子。本地服务下 fetch 仍作为
兜底保留 —— 两条路都通，断网、双击、localhost 三种场景一致。

**谁负责刷新它**

不靠人记得敲命令。任何一个生产者写 web/*.json 时都走 `write_json()`，
写完自动重建 data.js；漏掉的用 `python -m app.webdata` 兜一次。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BUNDLE = WEB / "data.js"

# 打包哪些文件。顺序固定，diff 才稳定。
FILES = ("history.json", "evolve.json", "result.json")

HEADER = ("/* 自动生成，请勿手改。源：web/*.json，产出：app/webdata.py\n"
          "   file:// 双击打开时页面读这个文件，不走 fetch。 */\n")


def bundle(files: tuple[str, ...] = FILES, web: Path = WEB) -> Path:
    """把 web/*.json 打成一个 data.js。缺文件不算错（还没生成而已）。"""
    payload: dict[str, object] = {}
    for name in files:
        p = web / name
        if not p.exists():
            continue
        try:
            payload[name] = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # 坏数据不进包：宁可在页面上报错，也不要把半个对象塞进去
            print(f"⚠ {name} 不是合法 JSON，已跳过：{exc}", file=sys.stderr)
    out = web / "data.js"
    text = HEADER + "window.WB_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    return out


def write_json(name: str, payload: object, web: Path = WEB, indent: int = 2) -> Path:
    """写一份 web/<name>，然后重建 data.js。生产者统一走这里，别自己 write_text。"""
    p = web / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n",
                 encoding="utf-8")
    bundle(web=web)
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重建 web/data.js（file:// 兜底包）")
    ap.add_argument("--web", default=str(WEB))
    args = ap.parse_args(argv)
    out = bundle(web=Path(args.web))
    data = out.read_text(encoding="utf-8")
    print(f"✓ 已重建 {out}（{out.stat().st_size} bytes，含 "
          f"{data.count('.json\":')} 份数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
