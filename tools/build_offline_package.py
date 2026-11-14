"""打一个能上台的离线整包：解压 → 双击 → 四个页面可点，断网也能用。

内容只收运行必需的文件，绝不含 runs/、logs/、config.local.json、.env。
实时助教走本地 /api/ask（纯本地判分，不调模型），所以断网也能演。

产物：dist/demo_offline.zip
用法：python tools/build_offline_package.py
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
STAGE = DIST / "demo_offline"
ZIP = DIST / "demo_offline.zip"

COPY_DIRS = ["app", "web", "data", "prompts"]
SKIP_NAMES = {"__pycache__", ".pytest_cache", "config.local.json", ".env"}
SKIP_SUFFIX = (".pyc",)

START_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   AI 养蛊机 / 考研助教 · 离线演示
echo   ------------------------------------------------------------
echo   1. 服务会在本机 8124 端口启动，浏览器自动打开
echo   2. 四个页面：cover -> tutor -> compare -> result
echo   3. 关掉这个窗口即停止服务
echo.
start "" cmd /c "timeout /t 2 >nul & start "" http://127.0.0.1:8124/cover.html"
py -3 app\serve.py --port 8124 --allow-tutor || python app\serve.py --port 8124 --allow-tutor
echo.
echo   服务已停止。若端口被占用，可手动改上面两处 8124。
pause
"""

GUIDE = """# 离线演示包 · 上台须知

> 生成于 {stamp}｜代码版本 `{commit}`

## 一、三步启动

1. 解压整个 `demo_offline` 文件夹（不要只解压一层）；
2. 双击 `START_DEMO.bat`；
3. 浏览器会自动打开 `http://127.0.0.1:8124/cover.html`。

窗口保持打开就是服务在跑；关掉窗口即停止。**全程不需要联网、不需要 API key。**

## 二、四个页面怎么走

| 顺序 | 页面 | 一句话 |
|---|---|---|
| 1 | `cover.html` | 三个数字：可教学题数 / 知识点 / 教法对照组 |
| 2 | `tutor.html` | 点题面看「为什么给我这道题」；切「实时」，输入 `0` 连交几次看提示升到第 4 级（完整解法，不报数值），输入 `1` 看掌握度变化 |
| 3 | `compare.html` | 三套教法 × 三档学生，切增益/成本/适应度 |
| 4 | `result.html` | 留出评测真实结果：四趟 + 三个冻结祖先；红标应已消失 |

## 三、出问题怎么办（按顺序）

| 现象 | 处理 |
|---|---|
| 双击 bat 一闪而过 | 右键「以管理员身份运行」，或在该目录开终端跑 `py -3 app\\serve.py --port 8124 --allow-tutor` |
| 8124 被占用 | 把 `START_DEMO.bat` 里两处 `8124` 改成 8125 再双击 |
| 页面白屏 | 关掉服务重开；仍白屏就改用 `docs/demo/shots/` 的截图或 90 秒录屏 |
| 实时模式报错 | 页面会自动切回放并提示「实时模式不可用」，**这是设计好的降级**，照常讲 |
| 完全没电/没机器 | 放 90 秒录屏，口播不变 |

## 四、B 负责讲的三个数字

1. **E − F = +0.33 题 / 20**（两对分别 +0.50、+0.17）——方向为正，但幅度在噪声内，所以结论是「未观察到可靠改善」；
2. **挑战 X04：5 阶行列式连续六代 0/6**；X05：4 阶最高 2/6——这是能力边界的硬证据；
3. **留出评测 2,640 次调用、4 趟、3 个冻结祖先**，每个数字都能指到 `run_id`。

## 五、证据在哪

- 结果页数据：`web/result.json`（`is_mock: false`）
- 运行证据：`runs/evaluations/*.json`、`runs/result_brief.json`（B 的机器上）
- 交付简报：`docs/B_交付简报.md`
- 代码版本：`{commit}`（tag `{tag}`）
"""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        if p.is_dir() or p.name in SKIP_NAMES or p.suffix in SKIP_SUFFIX:
            continue
        if any(part in SKIP_NAMES for part in p.parts):
            continue
        target = dst / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    for name in COPY_DIRS:
        copy_tree(ROOT / name, STAGE / name)

    (STAGE / "START_DEMO.bat").write_text(START_BAT, encoding="utf-8", newline="\r\n")
    commit = git("rev-parse", "--short", "HEAD")
    tag = git("describe", "--tags", "--always")
    GUIDE_TXT = GUIDE.format(stamp="2026-11-14 22:15", commit=commit, tag=tag)
    (STAGE / "演示须知.md").write_text(GUIDE_TXT, encoding="utf-8")

    files = sorted(p for p in STAGE.rglob("*") if p.is_file())
    manifest = [f"{sha256(p)}  {p.relative_to(STAGE).as_posix()}" for p in files]
    (STAGE / "MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")

    ZIP.parent.mkdir(parents=True, exist_ok=True)
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(STAGE.rglob("*")):
            if p.is_file():
                z.write(p, f"demo_offline/{p.relative_to(STAGE).as_posix()}")
    size = ZIP.stat().st_size / 1024 / 1024
    print(f"staged {len(files)} files -> {STAGE.relative_to(ROOT)}")
    print(f"zip    {len(files) + 1} entries, {size:.1f} MB -> {ZIP.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
