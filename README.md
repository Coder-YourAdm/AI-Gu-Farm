# AI 养蛊机 · Prompt Evolver → 日新 · 智慧考研助教

> BoHack 2026 天津青年黑客松参赛作品 · 赛道主题「日新 / 生生不息」

**一句话**：前半程让 prompt 互相残杀，练出一套最会教的；后半程把这套教法冻结下来，
配上精确判分、掌握度估计和带证据的记忆，变成一个会记住你的考研数学助教。

- **养蛊机（已完成）**：6 个 prompt 个体互相出题、互相解题；按题目区分度淘汰并重组，
  20 代后测量模型在当前题域上的能力上限。**不做模型训练，不用 LLM 当裁判**——
  判分走本地精确算术（`Fraction`，无浮点），同一道题任何人任何时候算出的答案都一样。
- **助教（今日施工中）**：`养蛊机 → best_profile.json → 学生端`。教法**周更**（离线重训一次），
  对学生的认识**秒更**（每答一题更新掌握度）；学生能点开「为什么给我这道题」，
  评委能自己上手打一个答案，系统当场判、当场讲。

> 边界写在这里，不藏在附录：**只做答案能精确复核的题型**（高数 + 线代，当前 3 个知识点）。
> 证明题、英语作文、政治大题明确拒答——这不是能力不足的遮掩，是判分可信的前提。

## 现在能跑什么

| 能力 | 状态 |
|---|---|
| 判分内核 `app/executor.py` | ✅ 六函数齐全，48 条单测 |
| 题集 125 道（`data/`） | ✅ 已回填 `signature`/`statement`/`reference`，`validate_task` 125/125 |
| 双跑编排 `app/demo.py` | ✅ `pair` / `rollback` / `status`，`--backend mock\|real` |
| 快照回退 | ✅ `v0_seed` 一键回退，实测逐字节可逆 |
| 演化大屏 / 双跑对照 / 结果页 | ✅ 三个单文件页面，零依赖、断网可演示 |
| B 演化引擎 `app/run.py` | ✅ 异步接口、校准、pilot、冻结 E/F 协议、失败归档与独立恢复分支；已通过离线回归 |
| B 留出评测 | ✅ 冻结 g0 / 实际参评检查点、全组终态门禁、跨运行交错计划与严格配对；尚未运行真实模型实验 |
| 端到端回归 | ✅ 439 条用例（含 3 代 mock pilot、导出白名单自检、断点续跑） |
| 真模型调用 `app/llm.py` | ⚠️ 代码就绪，`config.json` 的 `model`/`base_url` 未填、缺 API key |
| 留出评测 `web/result.json` | ⚠️ 页面已按契约实现，数据待评测产出（现为 mock，页面挂红标） |
| 学生模型 + 记忆（`app/student/`、`app/tutor/`） | 🚧 施工中：契约已冻结，BKT / 难度 / 记忆 / 提示梯度今日落地 |
| 学生端（回放 + 实时） | 🚧 施工中：`web/tutor.html` + `POST /api/ask`；**回放模式不依赖网络** |
| 三套教法对照 | 🚧 施工中：`loose / verbose / answer_only`，fitness = 学生增益 − 0.25 × 交互成本 |

**诚实标注**：仓库里 `web/evolve.json`、`web/result.json` 当前是 `is_mock: true` 的
演示数据，用于跑通链路与排练。页面上有 `mock` 徽章，不会把假数字当真结果展示。

## 两个阶段怎么接

| | 阶段一 · 练功（已完成） | 阶段二 · 服务（施工中） |
|---|---|---|
| 谁在变 | prompt 个体（6 个 × 20 代） | 学生对每个知识点的掌握度、题目难度、记忆强度 |
| fitness | 出题区分度 + 解题正确率 | 学生前后测增益 − 交互成本 |
| 产物 | 冠军个体 | `best_profile.json`（冻结，可 diff、可回滚） |
| 运行期依赖 | 模型（出题/解题） | **本地引擎**：判分 / BKT / SM-2，模型只负责说话 |
| 断电能不能演示 | 不能 | **能**（回放模式 + 本地接口） |

## 跑起来

```bash
# 1. 静态服务（现有三页）
python app/serve.py --port 8123
#   → /index.html    演化大屏
#   → /evolve.html   双跑对照（赛道「日新」主场）
#   → /result.html   留出结果页

# 2. 双跑（mock，约 8 秒，不花 token）
python -m app.demo pair --pair demo_pair_dev --backend mock

# 3. 回退到初始快照
python -m app.demo rollback --pair demo_pair_dev --to v0_seed

# 4. 真模型（需先填 config.local.json 的 model / base_url / max_calls，并 export LLM_API_KEY）
python tools/preflight.py --config config.local.json             # 先体检，不发请求
python tools/preflight.py --config config.local.json --probe      # 再验地址/密钥/model ID
python -m app.demo pair --pair demo_pair_real --backend real
```

`tools/preflight.py` 会逐项检查：配置结构与语义、`require_runtime_ready`
（占位符 / 硬限额）、密钥环境变量、数据 125 题自校验、判分链路往返、运行目录可写、
调用量是否装得下 `max_calls`。**任何一项 FAIL 都不要开跑**——`--probe` 只花几个 token。

### 配真模型：一条命令

```bash
python tools/set_model.py --list                                    # 看预设服务商
python tools/set_model.py --provider deepseek --model deepseek-chat --key sk-xxxx
python tools/preflight.py --config config.local.json --require-key --probe
```

`set_model.py` 从 `config.dev.json` 复制一份 `config.local.json`，按服务商填 `base_url`、
补 `max_calls`（默认 3000，E 一趟 2289），并**把 key 只写进 `.env`**。
`config.local.json` 与 `.env` 都在 `.gitignore` 里（第 19、20 行），**永远不会进仓库**。

> `preflight` 会自动读 `.env`，不需要每次 `export`。
> `--probe` 会显示**服务端回显的 model**：如果它和你填的不一样，说明走的是别名或中转，
> 实验报告里要如实写「我们传 X，服务端回显 Y」。

> 助教侧命令随今日施工落地：`python tools/tutor_cli.py --sid demo --turns 8 --out web/tutor.json`、
> `python app/serve.py --port 8123 --allow-tutor`。契约见 [AI 可读实施方案](docs/AI可读_实施方案.md)。
>
> 生成对照数据：`python tools/compare.py --seed 42 --out web/compare.json`
> （离线、幂等；三套教法 × 三档模拟学生，结论见 [评测报告](docs/评测报告_12h.md)）。
> 生成对照数据：`python tools/compare.py --seed 42 --out web/compare.json`
> （离线、幂等；三套教法 × 三档模拟学生，结论见 [评测报告](docs/评测报告_12h.md)）。
> **放进 `web/` 之前先跑数据契约自检**：`python tools/check_web_contract.py`
> （`tutor.json` 的四条验收 + `compare.json` 的 9 行 rows / 顶层 5 字段 / 逐行
> `cost=0.3·items/20+0.7·hints/20`、`fitness=gain−0.25·cost`，容差 0.002）。
> `compare.json` 尚未生成时跳过，最终冻结加 `--require-compare`。

## 目录

```text
app/
  executor.py        判分内核：题面生成 / 边界校验 / Bareiss 行列式 / judge（A）
  llm.py             同步 complete() 与异步 LLMClient 的兼容入口
  llm_async.py       异步 HTTP、角色参数、限流、重试、日志与硬限额
  run.py             校准 / pilot / 锁协议 / 正式 E/F / 组间评测 CLI
  runtime.py         初始状态冻结与独立恢复分支
  runner.py          代事务、不可覆盖快照、完整运行汇总
  protocol.py        校准祖先选择与预登记 E/F 运行
  dataset_adapter.py C 的 JSON 题库到 B Dataset 接口
  demo.py            双跑编排：pair / rollback / status
  export.py          白名单导出，reference / holdout / raw_response 不进静态目录
  serve.py           标准库静态服务；--allow-rollback 回退、--allow-tutor 实时判分
  student/           🚧 学生模型：KC 树 / BKT / 难度 / 复习排期 / 事件 / 画像（A、B）
  tutor/             🚧 记忆检索 / 五级提示 / 对话状态机（B）
  evalsim/           🚧 模拟学生与 fitness（A）
data/                125 道题：calibration 20 / common_dev 80 / challenge_pool 5 / holdout 20
prompts/             setter / solver / 六份种子基因 / crossover / mutate
skills/              🚧 prompt-evolver（训练期）与 kaoyan-tutor（运行期）两套 skill
web/                 现有三个单文件页面 + 两份 mock 数据；🚧 tutor / compare / cover
tests/               A/B/C 单元测试、集成测试与 B 缺陷回归
docs/                完整方案 / 12 小时分工 / AI 实施方案 / skill 包装 / B 修复说明
方案/                技术规格 v5、项目大纲 v5、分工方案 v7.3（含 12 小时冲刺附记）
tools/               开发期工具：sympy 交叉验证 / 题面自检 / pilot 前置体检 /
                      助教回放生成 / web 数据契约自检，均不进判分链路
```

## 判分为什么值得看

题域限定在两类可精确求解的题：多项式/指数积分、整数矩阵行列式。
答案一律用 `Fraction` 计算，禁止浮点——`0.1+0.2` 这类问题不会污染分数。

`judge()` 返回三值而非布尔：

| 返回 | 含义 |
|---|---|
| `right` | 答案正确 |
| `wrong` | 格式合法但数值错误 |
| `unanswered` | 拒答 / 空响应 / 格式错 / 超长 / 非数值 |

**拒答与算错必须分开**——把两者混成一个 `False`，会让「模型拒答」看起来像
「模型算错」，于是针对错误类型学到的规则全是错的方向。

助教侧复用了这条链路：学生端拿到的只有 `type / statement / answer_format` 三个字段，
`reference` 永不外发；掌握度与提示全部由本地引擎算，模型只负责把下一步说成人话。

## 测试

```bash
python -m pip install -r requirements.txt
python -m pytest
```

几条值得单说的：

- `tests/test_hand_calc.py` —— 附录 A 的 16 条用例用三条独立路径（手算 / 闭式 / 数值积分）交叉验证
- `tests/test_backfill.py` —— 回填后的题集必须能被 `judge()` 吃下，且重跑逐字节相同
- `tests/test_a_e2e.py` —— 真数据集 + mock 模型跑一代，72 个槽位必须全部判 `right`
- `tests/test_mock_pilot.py` —— 连续 3 代（含算子、含挑战代）+ 导出白名单自检 + 从断点续跑出 demo 分支
- `tests/test_preflight.py` —— 体检脚本必须能拦住「占位符未填 / 没设硬限额 / 数据缺份」，不许永远报 OK
- `tests/test_result.py` —— 结果页不许写死分数、不许外链、不许把 `reference` 带进导出
- `tests/test_prompts.py` —— 提示词必须用 `str.replace()` 渲染，`setter.md` 里的 `{coeffs}` 会击穿 `str.format()`

## 文档索引

| 想知道什么 | 看哪份 |
|---|---|
| 助教完整设计（8 个知识点 / 五级提示 / 四组对照 / 答辩 12 问） | [docs/完整方案_日新考研助手.md](docs/完整方案_日新考研助手.md) |
| 今天 12 小时谁做什么、每步怎么验收 | [docs/分工_12小时_详细版.md](docs/分工_12小时_详细版.md) |
| 3×3 教法对照的九行数字与三个诚实声明 | [docs/评测报告_12h.md](docs/评测报告_12h.md) |
| 3×3 教法对照的九行数字与三个诚实声明 | [docs/评测报告_12h.md](docs/评测报告_12h.md) |
| 给编码 AI 的执行说明（冻结契约 + 角色 prompt） | [docs/AI可读_实施方案.md](docs/AI可读_实施方案.md) |
| 养蛊机与助教怎么打包成两个 skill | [docs/skill包装_养蛊机与助教.md](docs/skill包装_养蛊机与助教.md) |
| B 的引擎修复清单与预算限制 | [docs/B修复说明.md](docs/B修复说明.md) |
| 真模型校准：12.8% 的根因与怎么落到 52.8% | [docs/校准结论_12h.md](docs/校准结论_12h.md) |
| 12 小时冲刺期与 v7.3 原分工的关系 | [分工方案_v7.3.md](分工方案_v7.3.md) 附录 C |

## 已知边界

- **真模型校准通过率随难度分档**：原卷（det 3–5 阶、积分 n 到 9）12.8%；把行列式降到 2–3 阶后
  **52.8%**（落在规格 20–80% 区间）；全简单档 92.8%。360 条回答零格式错误，所以是题难度而非解析问题。
  生成器 `tools/make_tier_dataset.py`，结论见 [校准结论](docs/校准结论_12h.md)
- 助教侧尚未落地：`app/student/`、`app/tutor/`、`web/tutor.html` 今日施工，契约已冻结
- 教法 profile 当前为手写基线（`manual-v1`），尚未由演化产出；冠军 profile 需真模型跑完一轮才有
- 题域 3 个知识点（定积分 / 反常积分 / 行列式），扩充路线见完整方案 §8
- 留出评测（`web/result.json`）尚未由真模型跑出，结果页目前是 mock 数据
- 真模型实验未开跑：`config.json` 的 `model` / `base_url` 仍是占位符，且需设 `max_calls` 或
  `wall_limit_s`（否则 `require_runtime_ready` 会拒绝），密钥走环境变量 `LLM_API_KEY`
- 附录 A 的 D2 用例矩阵元素 `10` 超出 §2.1 的 `[-9,9]`：测试已换成等值矩阵并补了
  `validate_task` 断言，规格原文未改，见 `给A_边界冲突说明.md`
