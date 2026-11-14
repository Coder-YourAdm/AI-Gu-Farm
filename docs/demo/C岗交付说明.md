# C 岗交付说明（给 A / B 对接用）

> 分支：`feat/tutor-c`　HEAD：`95ca417`（`88fd4de` 原 C 交付 + 8 个新提交 `0a52aac` … `95ca417`）
> **已推送远端**：`origin/feat/tutor-c = 95ca417`（2026-11-14 13:32 确认）。
> **唯一权威工作区**：`D:\WorkBuddy-temp\repo-real\AI-Gu-Farm`（`feat/tutor-c`）。
> 桌面 `C:\Users\ASUS\Desktop\黑客松` 是 B 的克隆，其中 `web/` 三页仅为同步副本，**不要在那里改 C 的代码**；
> `桌面\黑客松\.c-work` 是早期临时副本，已废弃，不再使用。
> 只改 `web/` 与 `docs/demo/`，**没碰 `app/`、`data/`、`tests/`**。

## 一、交付物清单

| 文件 | 是什么 |
|---|---|
| `web/tutor.html` | 诊断页：六块 + 三处交互 + 实时输入栏（五控件）+ 双模式 |
| `web/compare.html` | 对照页：3×3 条形、9 行明细、三指标切换、口径自检 9/9 |
| `web/cover.html` | 封面：105 / 3 / 9 三个数字 |
| `docs/demo/check_pages.py` | 静态自检：顶栏一致、九个 id、`reference`、外链、互跳、真数据形状 |
| `docs/demo/check_live.js` | 实时层自检：契约 4.5 形状、判分、提示、重置、失败回退 |
| `docs/demo/check_compare.js` | 对照页冒烟：9 行、口径 9/9、指标切换 |
| `docs/demo/check_layout.js` | C13 布局自检（浏览器控制台粘贴） |
| `docs/demo/check_all.ps1` | 一键跑前三套 |
| `docs/demo/自检_C9.md` / `自检_C13.md` / `演示操作单_C11_C12.md` / `答辩_C组_3问.md` | 验收记录、布局清单、截图录屏单、答辩稿 |

## 二、**打开方式（很重要，别双击就以为接了真数据）**

| 方式 | 结果 |
|---|---|
| 双击 `web/tutor.html`（`file://`） | 能打开、能交互，但浏览器禁止 `file://` 读本地 JSON，页面**只能用内联示例数据**并挂黄条 |
| `python app/serve.py --port 8123`（或 `python -m http.server 8123 --directory web`）后访问 `http://127.0.0.1:8123/tutor.html` | 才会 `fetch` 到 `web/tutor.json` / `web/compare.json`，黄条消失 |

**所以：正式演示、截图、录屏一律走 HTTP；`file://` 只当"断网兜底"。**
截图时如果还挂着黄条，先检查是不是双击打开的。

## 三、A 要接的：`web/tutor.json`（契约 4.3）

把文件放进 `web/` 即生效，**前端不用改一行**。形状硬要求（自检脚本会校验）：

- `profile_version`、`generated_at`、`student.sid/label`
- `kcs` **恰好 3 条**，每条 `id/name/mastery/items/hint_avg`，`id` 必须是
  `integral_definite` / `integral_improper` / `det`
- `turns`：**每条 `role="tutor"` 的轮次必须有 `why`**，且 `why` 里 `mastery/difficulty/last_error`
  是真实计算出来的三个数（页面上点题面就显示这三个数，编不得）
- `review_plan`、`memory_timeline`（`memory_timeline` 每条带 `src` 数组）
- **JSON 里不能出现 `reference` 字样**（自检会拦）

> 提醒：契约 4.3 的示例把第 4 题答案写成 `7/6`，那是错的。
> `∫₀¹(3x²−2x+1)dx = 1`。前端 fixture 已经改成 1，A 的数据别照抄示例。

**A 交付后的 C4 核对（3 步）**：
1. `python docs/demo/check_pages.py` → 第 7 项应显示 `kcs=3 tutor_turns=N why_ok=True reference=0`
2. `python -m http.server 8123 --directory web` 打开 tutor 页 → 黄条消失
3. 挑 3 个数字（任一 KC 的 `mastery`、任一题面的 `why.mastery`、`review_plan[0].due`）与 JSON 逐字比对

`web/compare.json` 同理：`rows` 必须 9 行，`gain/cost/fitness` 满足
`cost = 0.3·(items/20) + 0.7·(hints/20)`、`fitness = gain − 0.25·cost`（页面会自检，差 >0.002 标红）。

## 四、B 要接的：`POST /api/ask`（契约 4.5）

前端已经留好**唯一开关**：`web/tutor.html` 里

```js
var LIVE = { endpoint: null, ... };   // B 的通了就把 null 改成 "/api/ask"
```

请求：`{"sid":"live01","action":"start|submit|skip|hint|why|reset","answer":"..."}`
响应字段（页面逐项读取，缺一个就有一块不显示）：

| 字段 | 页面用途 | 缺了会怎样 |
|---|---|---|
| `ok` | 判成败；`false` 走回退 | 视为失败，自动回放 |
| `verdict` / `err` | 对错气泡与错因 | 气泡判成"错" |
| `statement` / `answer_format` | 题面与作答格式 | 新题面不显示 |
| `hint.level` / `hint.text` | 提示气泡 | 答错后没有提示 |
| `mastery`（3 个 KC） | 左侧掌握度条 | 掌握度不更新 |
| `why`（mastery/difficulty/last_error/memory） | 理由面板 | 点题面没反应 |
| `review` | 复习计划 | 右下面板不更新 |
| `turn` / `kc` / `elapsed_ms` | 轮次、知识点 | 影响不大 |

**任何异常请返回 `{"ok":false,"error":"..."}`（HTTP 500）**，页面会挂黄条并自动切回放，
不会白屏、不会卡死——这条 C8 已经验证过（含 501 的 HTML 响应）。

## 五、自检命令（谁都能跑）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File docs/demo/check_all.ps1
```

三套全过会打印 `ALL CHECKS PASS`。改完 `web/*.html` 或换了数据，跑一次再提交。

## 六、当前已知缺口

1. `web/tutor.json` / `web/compare.json` 还没有 → 页面用内联示例 + 黄条（**截图前必须补**）。
   缺的是**两份**，不是一份：`tutor.html` fetch `tutor.json`，`compare.html` fetch `compare.json`。
2. `app/serve.py` 没有 `/api/ask` 分支（实测 501）→ 实时模式走本地假 Tutor；B 交付后改一处 `endpoint`。
3. 推送已完成，`origin/feat/tutor-c` = `95ca417`；A/B 直接 `git fetch` 后 checkout 该分支即可拿到全部 C 交付。
4. 真人验收尚未做（三页互跳、实时连答 5 题、断接口自动回放、C13 六格 1280/1920）——
   脚本自检全绿 ≠ 演示验收通过，这部分需要人在浏览器里过一遍。
5. **打开方式**：`file://` 双击读不到本地 JSON，永远显示示例数据。正式演示/截图必须
   `python app/serve.py --port 8123`（或 `python -m http.server 8123 --directory web`）后走 HTTP。
