"""file:// 双击打开必须有数据 —— 任务书 C-2 的验收原文。

这条从来没达标过：以前三页都是 `fetch("xxx.json")`，双击打开被浏览器当跨域拦掉，
页面只剩一行报错。修法是把数据打成 `web/data.js`，用 `<script src>` 同步加载。

这几条测试锁的是"别再退回去"：
1. data.js 必须存在，且内容与 web/*.json 同源（不能各写一份、慢慢飘）
2. 三页都必须先引 data.js，再跑内联脚本
3. 三页的加载器必须先看内联数据，fetch 只能是兜底
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT))

PAGES = ("index.html", "evolve.html", "result.html")
BUNDLED = ("history.json", "evolve.json", "result.json")


def test_bundle_exists_and_covers_all_pages():
    p = WEB / "data.js"
    assert p.exists(), "web/data.js 不存在 —— 双击页面会白屏"
    text = p.read_text(encoding="utf-8")
    assert text.lstrip().startswith("/*"), "data.js 应带生成说明头"
    for name in BUNDLED:
        if (WEB / name).exists():
            assert f'"{name}"' in text, f"data.js 里缺 {name}"


def test_bundle_matches_json_on_disk():
    """data.js 与 json 必须是同一份数据。飘了就是两套事实。"""
    text = (WEB / "data.js").read_text(encoding="utf-8")
    body = text.split("window.WB_DATA = ", 1)[1].rstrip().rstrip(";")
    bundled = json.loads(body)
    for name, val in bundled.items():
        on_disk = json.loads((WEB / name).read_text(encoding="utf-8"))
        assert val == on_disk, f"{name} 的 data.js 副本与磁盘上的 json 不一致"


def test_all_pages_load_data_js_before_inline_script():
    for name in PAGES:
        html = (WEB / name).read_text(encoding="utf-8")
        src = html.find('<script src="data.js">')
        assert src > 0, f"{name} 没引 data.js"
        inline = re.search(r"<script>(?!.*src=)", html[src:], re.S)
        assert inline, f"{name} 找不到内联脚本"
        assert src < src + inline.start(), f"{name} 的 data.js 必须在内联脚本之前"


def test_pages_prefer_inline_data_over_fetch():
    """加载顺序写反了（先 fetch 再兜底）在 localhost 下看不出来，双击才炸。"""
    for name in PAGES:
        html = (WEB / name).read_text(encoding="utf-8")
        assert "window.WB_DATA" in html, f"{name} 没有读 window.WB_DATA"
        m = re.search(r"function loadData\([^)]*\)\{(.*?)\n\s{0,2}\}", html, re.S)
        assert m, f"{name} 缺 loadData 加载器"
        body = m.group(1)
        i_inline = body.find("BUNDLE")
        i_fetch = body.find("fetch(")
        assert i_inline >= 0 and i_fetch > i_inline, \
            f"{name} 的加载器必须先看内联数据，fetch 只能兜底"


def test_write_json_rebuilds_bundle(tmp_path):
    """生产者统一出口：写 json 就自动刷新 data.js，不靠人记得敲命令。"""
    from app.webdata import write_json

    web = tmp_path / "web"
    web.mkdir()
    write_json("evolve.json", {"hello": "世界"}, web=web)
    js = (web / "data.js").read_text(encoding="utf-8")
    assert "window.WB_DATA" in js
    assert '"hello"' in js and "世界" in js
    # 第二份追加进去时，第一份不能丢
    write_json("history.json", {"gen": 3}, web=web)
    js = (web / "data.js").read_text(encoding="utf-8")
    assert '"hello"' in js and '"gen": 3' in js


def test_bad_json_is_skipped_not_fatal(tmp_path, capsys):
    """半截 JSON 宁可跳过（页面报错），也不能把坏对象塞进包。"""
    from app.webdata import bundle

    web = tmp_path / "web"
    web.mkdir()
    (web / "history.json").write_text('{"ok": 1}', encoding="utf-8")
    (web / "evolve.json").write_text('{"broken": ', encoding="utf-8")
    bundle(web=web)
    js = (web / "data.js").read_text(encoding="utf-8")
    assert '"ok": 1' in js
    assert "broken" not in js
    assert "已跳过" in capsys.readouterr().err


@pytest.mark.parametrize("name", PAGES)
def test_page_script_syntax(name):
    """内联脚本语法错误在浏览器里是静默白屏，必须机器查。"""
    import shutil
    import subprocess
    import tempfile

    if not shutil.which("node"):
        pytest.skip("没有 node，跳过语法检查")
    html = (WEB / name).read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, f"{name} 没有内联脚本"
    tmp = Path(tempfile.mkdtemp())
    for i, b in enumerate(blocks):
        f = tmp / f"{name}.{i}.js"
        f.write_text(b, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name} 第 {i} 块语法错误：{r.stderr[:400]}"
