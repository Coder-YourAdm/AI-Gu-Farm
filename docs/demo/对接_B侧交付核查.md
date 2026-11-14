# B 侧交付核查与对接（2026-11-14）

> 核查对象：https://github.com/Coder-YourAdm/AI-Gu-Farm
> 核查方式：GitHub 全树 API（`/git/trees/main?recursive=1`）+ 分支/标签 API，不是本地缓存
> 核查时 main：`4dc9827`；分支：`main` / `feat/tutor-c` / `c-final-20261114`；标签：`b-formal-20261114`

## 一、一句话结论

B 上传的是**养蛊机引擎与实验证据**；但 12 小时方案里分给 B 的 **B1–B7「助教后端」没有任何文件进仓库**。
所以前端"实时模式"目前接不上——不是 C 的页面问题，是后端整条链缺失。

## 二、已核实存在（B 的引擎/证据侧）

| 内容 | 路径/标识 |
|---|---|
| 交付简报 | `docs/B_交付简报.md` |
| 协议偏离说明 | `docs/B_协议偏离说明.md` |
| 运行清单 | `docs/B_运行清单.md` |
| 修复说明 | `docs/B修复说明.md` |
| 三张图数据 | `docs/charts/ancestor_curve.csv`、`challenge.csv`、`ef_pair.csv` |
| 结果简报机读版 | `docs/result_brief.json` |
| 正式实验标签 | tag `b-formal-20261114`（commit `d771ecb`） |
| 引擎侧代码 | `app/run.py`、`app/orchestrator.py`、`app/evaluate.py` 等（已在 main） |

## 三、未找到（B1–B7 助教侧，全树搜索 0 命中）

| 方案编号 | 期望路径 | GitHub 全树 |
|---|---|---|
| B1 | `skills/prompt-evolver/SKILL.md`、`skills/kaoyan-tutor/SKILL.md` | **连 `skills/` 目录都没有** |
| B2 | `app/student/events.py`（11 字段 append-only） | 无 |
| B3 | `app/student/profile.py`（`supersedes` 记忆） | 无 |
| B4 | `app/tutor/memory.py`（0.5/0.3/0.2 打分） | 无 |
| B5 | `app/tutor/hint.py`（五级梯度） | 无 |
| B6 | `app/tutor/dialog.py`（`class Tutor` 六动作） | 无 |
| B7 | `app/serve.py` 的 `/api/ask` + `--allow-tutor` | 无（只有 `/api/rollback`） |
| — | `tools/tutor_demo.py` | 无 |

`app/tutor/` 里只有 `__init__.py` + A 的 `policy.py`；`app/student/` 里只有 A 的 `bkt/diagnose/difficulty/kc/schedule`。

## 四、"都传了"最可能指什么

B 的主责是**引擎与教学法实验**，她已经完成并有 tag `b-formal-20261114`、简报与图表。
但 12h 方案把「记忆 / 提示 / 对话 / 实时接口」也划给了 B（B2–B7），这部分是**独立另一块**，
需要单独在 `feat/tutor-b`（或直接 `feat/tutor-c`）上补推。两件事都叫"B 的活"，但仓库里只有前者。

## 五、需要什么（二选一）

### 路线 A：B 补推最小集（约 40 分钟）

3 个文件 + 1 个分支：

1. `app/tutor/hint.py` —— `LADDER`、`next_level`、`render`
2. `app/tutor/dialog.py` —— `class Tutor` + `handle()` 六动作
3. `app/serve.py` —— `/api/ask` 分支 + `--allow-tutor`

完整字段、签名、`serve.py` 补丁：见 **`docs/demo/给B_B7最小集.md`**。
`events/profile/memory` 可以先返回空实现（`why.memory = []`），不阻塞联调。

推送与自检：

```bash
git checkout -b feat/tutor-b
git add app/tutor/hint.py app/tutor/dialog.py app/serve.py
git commit -m "feat(tutor): B7 实时接口最小集｜验收：probe_ask.py PASS"
git push -u origin feat/tutor-b
```

### 路线 B：C 写应急后端（30–40 分钟）

由 C 在 `feat/tutor-c` 上补 `dialog.py` + `hint.py` + serve 分支，标注"应急实现，B 到位后可替换"。
风险：与 B 的目录重叠；不建议两人同时改 `app/tutor/`。

## 六、后端到了之后，怎么验（3 步，C 侧执行）

```bash
# 1) B 起服务
python app/serve.py --port 8123 --allow-tutor

# 2) 契约探针（12 字段 + 六动作 + 提示封顶）
python docs/demo/probe_ask.py --url http://127.0.0.1:8123/api/ask
#    期望：RESULT: PASS

# 3) C 切前端 + 回归
python docs/demo/switch_live.py --on
node docs/demo/check_browser_live.js      # 真浏览器 + 真 HTTP
powershell -File docs/demo/check_all.ps1
```

探针 PASS 之前，`LIVE.endpoint` 保持 `null`（本地假 Tutor 兜底），演示不受影响。

## 七、可直接发给 B 的话

> 你在 main 上的 `docs/B_交付简报.md`、`docs/charts/*`、tag `b-formal-20261114` 我们都看到了，引擎侧没问题。
> 但 12h 方案里 B1–B7 的助教后端在仓库里搜不到：`skills/`、`app/tutor/dialog.py`、`hint.py`、`memory.py`、`app/student/events.py`、`profile.py`、`serve.py` 的 `/api/ask` 全部 0 命中（GitHub 全树 API 查的）。
> 如果你已经写了，麻烦推一个 `feat/tutor-b` 分支（或把文件发我），我这边 3 分钟能验完。
> 最小集只要 3 个文件：`hint.py` + `dialog.py` + serve 的 `/api/ask` 分支，字段照 `docs/demo/给B_B7最小集.md` 抄就行。
> 如果来不及，回一句"这次不交"，我们走本地状态机兜底，答辩口径我们统一。
