# C 岗交接给 WorkBuddy（读这一份就够）

> 写于 2026-11-14T13:20+08:00　作者：DSH 会话（接替原 WorkBuddy 的 C 岗）
> 状态更新 2026-11-14 14:30（WorkBuddy）：C 侧成果已全部推送并合入 `main`；
> A 的 `tutor.json` / `compare.json` 均已到位。**字段口径与打包基线以 `../docs/demo/对接清单.md` 为准。**
> **你的仓库已经是最新的，不要再重做 C6–C13。**

## 0. 先确认你拿到的是哪一版

```bash
git -C D:\WorkBuddy-temp\repo-real\AI-Gu-Farm log --oneline -1
# 应为 4f99000（merge: 合入 A 的 A8–A11）；95ca417 / bd848c5 均为历史 HEAD
```

`web/` 下应该有：`tutor.html` 38 KB、`compare.html` 22 KB、`cover.html` 7.9 KB，
并且 `../docs/demo/` 下有 10 个文件（check_*.py/js/ps1、自检_C9/C13、演示操作单、答辩、C岗交付说明）。

如果还是 `88fd4de`：说明没进到新提交，别在这个状态下继续写。

## 1. 这 8 个提交做了什么（C6–C13，已完成，**别重做**）

| 提交 | 内容 |
|---|---|
| `0a52aac` | C6 三页顶栏/导航统一；tutor 布局改 flex 竖排，修掉提示条出现时的整页溢出 |
| `4ff616f` | C7 实时输入栏五控件 + 本地假 Tutor（严格按契约 4.5 形状、有理数判分、BKT、五级提示） |
| `d18e239` | C8 失败统一回退（断网 / 非 2xx / `ok:false` / 坏 JSON 都回放 + 黄条） |
| `0b4e212` | C9 集成自检记录 + `check_all.ps1` 一键复跑 |
| `86b7c1b` | C10 答辩 3 问（9/10/12）+ compare 页冒烟 |
| `8be2723` | C11/C12 截图录屏单 + 彩排卡；自检加"真数据是否到位"第 7 项 |
| `8dc1246` | C13 布局自检脚本（浏览器控制台粘贴，报溢出/字号/契约 id） |
| `95ca417` | C 岗交付说明（给 A/B 的接入口径） |

**验收**（在仓库根目录，三套全绿才是当前状态）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File docs/demo/check_all.ps1
# 期望结尾：ALL CHECKS PASS
```

## 2. 你现在该做的（按顺序）

1. **不要改 `web/*.html` 的结构**，C 侧逻辑已经自洽；要改先跑 `check_all.ps1`。
2. **C4 已完成**：`web/tutor.json` 与 `web/compare.json` 都已由 A 产出并合入 `main`，
   页面不再挂黄条。`python docs/demo/check_pages.py` 第 7 项实测 `kcs=3 why_ok=True reference=0 rows=9`，
   `python tools/check_web_contract.py --require-compare` 实测 PASS。
   字段形状、公式口径与两处契约数学错误见 `../docs/demo/对接清单.md` 第一节。
3. **推送与合并已完成**：`main` 与 `feat/tutor-c` 同在 `4f99000`，三页 + 两份数据都在主干上。
   桌面 bundle / `D://WorkBuddy-temp//outputs//c-to-main-f02caeb.bundle` 仅作网络中断时的兜底。
   打包必须包含的文件清单见 `../docs/demo/对接清单.md` 第二节。
4. **真人验收**（沙箱/无头浏览器做不了，需要人）：三页互跳、实时连答 5 题、断接口自动回放、
   C13 六格（1280/1920 × 三页）。
5. **截图/录屏前必须先有 `web/tutor.json`**，否则黄条 = mock 徽章，白截。

## 3. 三个坑（踩过，别再踩）

1. `file://` 双击打开**读不到** `web/tutor.json`（浏览器禁止本地页面 fetch 本地 JSON），
   永远显示内联示例。正式演示/截图必须 `python app/serve.py --port 8123` 后走 `http://127.0.0.1:8123/tutor.html`。
2. `web/tutor.html` 里 **只有一处**要动来接 B 的接口：`var LIVE = { endpoint: null }` → `"/api/ask"`。
   B 的响应必须按契约 4.5（`verdict/statement/hint/mastery/why/review` 一个都不能少）。
3. **不要提交 `data/verify_bank.json`**：这个克隆里它是 modified 状态，不是我改的，按铁律 `data/` 谁都不许动。

## 4. 分工边界（别越界）

- C 只改 `web/` 与 `../docs/demo/`；`app/`、`data/`、`tests/` 一行没碰。
- 若发现页面问题，先 `check_all.ps1` 复现，再改，改完再跑一遍，再提交。
- 提交信息沿用：`feat(tutor): <做了什么>｜验收：<命令>`。
