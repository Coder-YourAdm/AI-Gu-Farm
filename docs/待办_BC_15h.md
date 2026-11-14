# B / C 未完成清单（15:05 仓库实测）

> 检查时间：2026-11-14 15:05 ｜ `main = fe45b13c` ｜ 方法：GitHub tree API 递归扫
> **main / feat/tutor-c / c-final-20261114 三条分支**的全部 blob 路径，逐条比对分工表。
>
> ⚠️ **判读口径**：这里只说「仓库里看不看得见」。队友可能正在本地写、还没推；
> 别据此判定谁没干活，先问一句「本地写了吗，能不能先推一批」。
> 按 T+0 = 11:53 折算，现在约 **T+3:12**。

---

## 1. B（毛尚胜）— 唯一真风险

三条分支里**都找不到** `skills/`、`app/student/{events,profile}.py`、
`app/tutor/{memory,hint,dialog}.py`、`tools/tutor_demo.py`、
`app/serve.py` 的 `/api/ask`、`demo_offline.zip`，以及相关测试。
`app/tutor/` 目前只有 A 的 `__init__.py` + `policy.py`。

按分工表，现在（T+3:12）本应完成 B1–B3、正在做 B4；**仓库可见进度为 0，
落后约 2.5–3 小时**。而且 B7 的 `/api/ask` 是集成点 2 的前置，越晚推风险越大。

| 步骤 | 排期(北京时间) | 欠什么 | 状态 |
|---|---|---|---|
| B1 | 12:23–13:03 | `skills/prompt-evolver/`、`skills/kaoyan-tutor/` 两套 SKILL.md | ⬜ 仓库不可见 · **已逾期** |
| B2 | 13:03–13:43 | `app/student/events.py`（11 字段 append-only）+ `test_events.py` | ⬜ · **已逾期** |
| B3 | 13:43–14:23 | `app/student/profile.py`（upsert / supersedes）+ `test_profile.py` | ⬜ · **已逾期** |
| B4 | 14:23–15:13 | `app/tutor/memory.py`（0.5/0.3/0.2 打分、top-4、≤200 字）+ 测试 | ⬜ · 正在到期 |
| B5 | 15:13–15:53 | `app/tutor/hint.py`（五级梯度、纯模板）+ 测试 | ⬜ |
| B6 | 15:53–16:53 | `app/tutor/dialog.py`（Tutor 六动作）、`tools/tutor_demo.py --offline` | ⬜ |
| **B7** | 16:53–17:38 | **`app/serve.py` 加 `POST /api/ask` + `--allow-tutor`** | ⬜ **集成点 2 前置** |
| B8 | 17:38–18:18 | `skills/kaoyan-tutor/scripts/{ask,report}.py` | ⬜ |
| B9 | 18:18–18:58 | `export_best.py` 幂等 + `evolve.py --dry-run` | ⬜ |
| B10 | 18:58–19:53 | 断网演练 + `demo_offline.zip` | ⬜ |
| B12 | 20:03–21:03 | 答辩 3 问（6/8/11），每问 2 句 | ⬜ |
| B13–B15 | 21:03–23:53 | 实时层验收配合 / 彩排控台 / 重打包冻结 | 需真人，未到期 |

**C 已经替 B 把前端挡在前面了**：`tutor.html` 内置本地假 Tutor，实时输入栏现在就能演，
`var LIVE = { endpoint: null }` 改成 `"/api/ask"` 一处即接 B。所以 B 晚一点不阻塞主线演示，
但**会让「实时走真后端」这个加分项消失**。

---

## 2. C（徐潇）— 大幅超前，只剩晚上的活

C1–C10 **全部完成并合入 main**，还额外修了两处（复习计划空态文案 `e36a4b55`、
compare 负值条形按 min~max 映射 `4934e1de`）。三页 + `docs/demo/` 11 份脚本与文档齐备，
四个自检脚本复跑全 PASS。

| 步骤 | 排期(北京时间) | 欠什么 | 状态 |
|---|---|---|---|
| C11 | 21:03–21:53 | 截图 ×3（1920/1280 各一套，共 6 张，放 `docs/demo/shots/`）+ 90 秒录屏 `demo_90s.mp4` | ⬜ 仓库里 **0 张图 / 0 个视频**；按排期**未到期** |
| C12 | 21:53–22:53 | 彩排时操作页面 + 计时 + 记卡点（`演示操作单_C11_C12.md` 已写好） | 需真人，未到期 |
| C13 | 22:53–23:53 | 1280 / 1920 两档 × 三页布局真人自检（`check_layout.js` 是控制台粘贴脚本） | 需真人，未到期 |
| C9 | 19:53 起 | 集成点 2 的「无白屏 / 无 404」真人走查 | 脚本侧已 PASS，真人部分未做 |

**C11 的前置条件现已满足**（两份 JSON 都在、自检 PASS），所以他随时可以提前截图。
注意 `file://` 双击会挂黄条，截图必须走 `http://127.0.0.1:8123/`。

---

## 3. 三个人共同欠的（都按排期未到期）

| 项 | 时间 | 说明 |
|---|---|---|
| 集成点 2 | 19:53 | 封面 → 诊断 → 理由 → 实时连答 5 题 → 对照页，端到端无白屏 |
| 彩排 ×2 | 21:53 / 23:00 | A 讲、B 控台、C 计时，5 分钟不超时 |
| 冻结 | 23:30 | `git tag demo-freeze`、U 盘备份、`docs/演示须知.md`（**目前无此文件**） |
| 答辩 7 问 | 随时 | A 的 1/2/3/4/5/7/10 问；B 的 6/8/11、C 的 9/10/12 里 C 已写 |

---

## 4. 建议现在发给 B 的一句话（可直接粘）

```text
毛哥，刚扫了下三条分支，B1–B10 的文件仓库里还都看不到
（skills/、events/profile、memory/hint/dialog、/api/ask、demo_offline.zip）。
按排期现在该做到 B4/B5 了，是不是都在本地还没推？
麻烦先把 B1–B4 推一批，我们先对接口形状，不用等全部写完。
前端那边徐潇已经内置了本地假 Tutor，实时输入栏现在就能演，
所以你不赶也没关系；但 B7 的 /api/ask 是集成点 2 的前置，
最晚 19:53 前通一下比较稳。接你只要改 tutor.html 一处 LIVE.endpoint。
```

---

## 5. 复验记录（C 最新修复之后，15:05 亲跑）

```
check_pages.py                RESULT: PASS
check_compare.js              RESULT: PASS（0 项失败）
check_live.js                 RESULT: PASS（0 项失败）
check_web_contract.py --require-compare   PASS
pytest -q                     608 tests / 0 failures（14:50 那轮）
```
