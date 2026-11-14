# 把养蛊机做成 skill：训练期与运行期分开

> 一句话结论：**赞成"线上只跑一个 AI"，但要守住"离线训练一次、线上冻结使用"。**
> 演化不能从叙事里消失，只能从"在线实时"挪到"开赛前离线跑一遍"。

---

## 0. 为什么赞成

三个很实际的理由：

| 理由 | 具体 |
|---|---|
| **成本可预测** | 线上每句话只调一个模型、一份固定 prompt，token 数是常数；演化跑一趟 2289 次调用，放线上就是灾难 |
| **演示可复现** | 冻结的 prompt 是文件，出问题能 diff；在线演化的现场表现取决于当次抽签，评委看到的可能是最差的那一代 |
| **产品讲得清** | "我们练了一套教法，现在用它教你"是一句人话；"我们的系统每句话都在做遗传算法"听着像没做完 |

## 0.1 但必须补这一条

你们的赛道是「日新 / 生生不息」。如果"演化"整个消失，故事会从"日日更新"退化成"一次性调参"——**这是唯一真正会掉分的地方。**

解法不是把演化塞回线上，而是**换一个东西来"日新"**：

| | 原来 | 现在 |
|---|---|---|
| 什么在变 | prompt 在变 | **学生模型在变**（每次答题都更新）+ **prompt 每周离线重训一次** |
| 变的频率 | 每代（分钟级） | 每句话（秒级）/ 每周（离线） |
| 谁能看见 | 只有日志 | 大屏上的掌握度曲线、复习计划、策略版本号 |

答辩可以这么说：

> **"我们不是让 AI 每天重新学一遍怎么教，而是让它在开赛前把教法练到最好，之后每天只更新对你这个人的认识。教法周更，认识秒更。"**

这句话既保住了"日新"，又守住了"单 AI 服务"的工程理性。

### 什么情况下我不赞成

如果评委明确要"现场看到系统自己进化"，那就必须留一个**最小在线演化**：3 个个体、1 代、只在新学生接入时跑。代价约 +3 小时，收益是能现场表演。这个要提前问清楚，别赌。

---

## 1. 两个 skill，一条产物链

```
[训练期 · 重 · 几天一次]
  prompt-evolver
    输入：题域配置 + 预算（max_calls）+ 代数
    输出：best_profile.json  ← 唯一交付物
            │
            ▼
[运行期 · 轻 · 每句话一次]
  kaoyan-tutor
    输入：best_profile.json + 学生事件流
    输出：下一道题 / 一句追问 / 一次复习提醒
```

**关键纪律：`kaoyan-tutor` 只读 `best_profile.json`，不读 `runs/`、不读 `history.jsonl`、不读 `config.json`。**

这条比什么都重要。它同时带来三件事：学生端不可能泄漏参考值、模型换代不影响产品、旧版本可回滚（存档 profile 即可）。

---

## 2. 产物契约（两个 skill 之间唯一的接口）

```json
{
  "schema": "tutor_profile/1",
  "created_at": "2026-11-14T11:30:00+08:00",
  "version": "E_s42-20261114",
  "frozen": true,
  "genes": {
    "solve_prompt": "……",
    "set_prompt": "……",
    "self_check": true,
    "teach_prompt": "先问学生打算怎么下手，再给最小提示。"
  },
  "params": {
    "explain_grain": "step",
    "probe_rounds": 2,
    "example_count": 1,
    "hint_ladder": ["hint", "scaffold", "step", "answer"],
    "review_k": 0.9
  },
  "evidence": {
    "source_run": "E_s42",
    "condition": "E",
    "n_gen": 20,
    "gain": 0.42,
    "cost": 0.31,
    "fitness": 0.34,
    "baseline_gain": 0.28,
    "baseline": "首版手写 prompt（未演化）"
  },
  "limits": {
    "kc_scope": ["integral_definite", "integral_improper", "det", "matrix_power"],
    "answer_domain": "integer | rational | finite decimal",
    "refuse": ["证明题", "英语作文", "政治大题"]
  }
}
```

`evidence` 和 `limits` 不是装饰：

- `evidence` 让学生端能回答"你这套教法凭什么"——评委一问就点开；
- `limits` 让系统**知道自己不会什么**，遇到超纲题直接说"这题我判不了，建议看答案册"，而不是硬编一个答案。这是教育产品最该有的诚实，也是防翻车的关键。

---

## 3. `prompt-evolver` 的 SKILL.md（可直接落地）

```markdown
---
name: prompt-evolver
description: 为指定题域离线演化一套教学 prompt。当用户说"跑一轮演化""更新教法""重训 prompt"
  "evolve the tutor prompt"，或需要生成/刷新 best_profile.json 时使用。全程不接触学生数据。
---

# prompt-evolver

## 何时用
- 题域变更（新增题型 / 调整难度范围）
- 每周定时重训（产出新版本 profile）
- 首版 profile 尚未生成

## 何时不用
- 只改一句讲解文案 → 直接改 profile 的 params，不需要重训
- 只是想看当前效果 → 用 kaoyan-tutor 的 `--report`

## 步骤
1. 读题域配置 `assets/domains/<domain>.json`，确认 KC 清单与模板族。
2. `python skills/prompt-evolver/scripts/evolve.py --domain kaoyan-math --gens 20 --max-calls 3000`
   —— 非交互，必须能在无人值守下跑完；中止就中止，不重试掩盖。
3. 跑完检查 `runs/<run_id>/summary.json`：`aborted` 必须为空，否则**不要**导出。
4. `python skills/prompt-evolver/scripts/export_best.py --run <run_id> --out assets/profile.json`
   —— 幂等：同一 run 连续导出两次必须字节一致。
5. 汇报三件事：冠军 fitness、对照基线 gain、用了多少次调用。

## 失败处理
- 缺 model / base_url / 硬限额 → 直接报错退出，**不许**用默认值兜底
- 演化中途有代 abort → 保留证据，不产出 profile
- 冠军与基线差距 < 0.03 → 明确说"这轮演化没有显著收益"，不要硬吹

## 验收
- `evolve.py --dry-run` 不联网、10 秒内打印调用计划
- `export_best.py` 两次输出 diff 为空
- profile 里 `limits.refuse` 非空（必须显式声明不会什么）
```

## 4. `kaoyan-tutor` 的 SKILL.md

```markdown
---
name: kaoyan-tutor
description: 用冻结的教学 profile 辅导考研数学（可精确判分题型）。当学生说"做题""讲一下这道"
  "我今天该复习什么"，或需要按掌握度出题、给提示、排复习计划时使用。
---

# kaoyan-tutor

## 铁律
1. 只读 `assets/profile.json`，不读 runs/、不读参考答案表。
2. 判分调 `app/executor.judge()`，**不用模型判断对错**。
3. 一次只问一个问题；答错先收窄，不收兵。
4. 超出 `limits.kc_scope` 的题，直接说不做。

## 每轮做什么
1. 读 L1 最近事件 + L3 画像 → 得到每个 KC 的掌握度与到期复习项
2. 选题：到期复习优先，其次 `|难度 − (θ+0.1)|` 最小
3. 出题：只发 `{type, statement, answer_format}` 三个字段
4. 学生答 → judge → 三态（right / wrong / unanswered）
5. 答错：按 `hint_ladder` 递进一级，最多 `probe_rounds` 轮
6. 写事件 → 更新 BKT → 若满足"无提示独立答对两次"则 KC 升级
7. 会话结束：写 L2 摘要 + 更新 L3 画像（带 src 证据）

## 失败处理
- profile 缺失 / schema 不符 → 停止，提示先跑 prompt-evolver
- 题目判分要求 reference 但 profile 里没有 → 报错，不猜
- 学生连续 3 题 unanswered → 降难度，不要继续加码

## 验收
- `ask.py --offline` 用固定 fixture 跑出的对话与快照一致
- 任意时刻都能回答"为什么给我这道题"（三个数字 + 一条记忆）
```

---

## 5. 目录布局

```text
skills/
  prompt-evolver/
    SKILL.md
    scripts/evolve.py           # 包装 python -m app.run --run，参数校验 + 非交互
    scripts/export_best.py      # runs/<id> → best_profile.json（幂等）
    assets/domains/kaoyan-math.json
  kaoyan-tutor/
    SKILL.md
    scripts/ask.py              # 一轮问答（--offline 走 fixture）
    scripts/report.py           # 当前掌握度 + 复习计划
    assets/profile.json         # 由 prompt-evolver 产出
```

三件事必须是**脚本**，不能是提示词：**判分、掌握度更新、复习排期**。
理由：提示词会漂、会随模型版本变化，而这三件事必须每次结果一致、可复核、可回归测试。

---

## 6. 节省下来的时间往哪放

在线演化砍掉，大约省 4–6 小时。建议这样分配：

| 用途 | 收益 |
|---|---|
| KC 从 6 个扩到 8 个（补"微分方程""矩阵相似"） | 覆盖面更像"考研数学"而不是"高数例题集" |
| 真人 n=1 案例（自己或同学跑 30 题） | 有一个真实前后测，比三个模拟学生都硬 |
| 校准图（预测正确率 vs 实际，报 Brier） | 回答"你怎么知道模型准" |
| 演示彩排 ×2 | 现场少翻车一次 |

---

## 7. 风险与止损

| 风险 | 表现 | 止损 |
|---|---|---|
| 冻结后不能随学生变教法 | 学生说"讲太细了" | `params` 可运行时改（讲解粒度属于参数，不属于重训），无需演化 |
| 别人装不上 skill | 缺 key、缺依赖 | 提供 `--dry-run` 与 `--offline` 两条零依赖路径 |
| skill 依赖仓库路径 | 换机器就崩 | 全部相对路径 + 启动时校验，缺什么报什么 |
| 评委要"现场进化" | 提问直冲赛道主题 | 备用：3 个体 1 代的小演化，演示 90 秒，平时不启用 |
| 叙事变成"一次性调参" | 显得不"日新" | 大屏主视觉换成**学生掌握度曲线**，右侧挂"教法版本 v20261114" |

---

## 8. 两句话记住

- **教法周更，认识秒更。**
- **训练期可以重、可以慢、可以联网；运行期必须轻、必须断网可跑、必须可复现。**
