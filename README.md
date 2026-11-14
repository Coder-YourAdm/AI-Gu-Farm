# AI 养蛊机 · Prompt Evolver

> BoHack 2026 天津青年黑客松参赛作品 · 赛道主题「日新 / 生生不息」

6 个 prompt 个体互相出题、互相解题；按题目区分度淘汰并重组，20 代后测量模型在当前题域上的能力上限。

**不做模型训练，不用 LLM 当裁判**——判分走本地精确算术（`Fraction`，无浮点），
同一道题任何人任何时候算出的答案都一样。

## 现在能跑什么

| 能力 | 状态 |
|---|---|
| 判分内核 `app/executor.py` | ✅ 六函数齐全，48 条回归 |
| 题集 125 道（`data/`） | ✅ 已回填 `signature`/`statement`/`reference`，`validate_task` 125/125 |
| 双跑编排 `app/demo.py` | ✅ `pair` / `rollback` / `status`，`--backend mock\|real` |
| 快照回退 | ✅ `v0_seed` 一键回退，实测逐字节可逆 |
| 20 代演化 `app/evolve_run.py` | ✅ 真跑完成：`E_real_20261114`，21 代 / 724 次调用 |
| 留出评测 `app/evaluate_holdout.py` | ✅ 3 趟 × 3 血脉 × 20 题 = 180 次真调用 |
| 演化大屏 / 双跑对照 / 结果页 | ✅ 三个单文件页面，零依赖、`file://` 双击可开 |
| 真模型调用 `app/llm_standalone.py` | ✅ `deepseek-flash`，token 预算读 `config.json` |

**三个页面挂的都是真数据，不是演示数据**：`is_mock: false`、`backend: real`，
每个数字都能指回 `runs/` 下的 `run_id` 与逐题 `calls_*.jsonl`。

| 页面 | 数据来源 `run_id` |
|---|---|
| `web/index.html` 演化大屏 | `E_real_20261114`（21 代，724 次调用） |
| `web/evolve.html` 双跑对照 | `demo_pair_calib`（19/20 → 20/20 → 19/20，判 `no_change`） |
| `web/result.html` 留出结果 | `E_real_20261114_r1/r2/r3`（各血脉 19–20/20，结论 `none`） |

**假数据自证**：导出链路上带 `is_mock` / `mock_reason`。解除红标的唯一条件是
`runs/<run_id>/manifest.json` 里 `backend: "real"`——改页面改不掉，只能改证据。
原则是「证明不了是真的，就标成假的」。

## 跑起来

```bash
# 1. 静态服务（三个页面）
python app/serve.py --port 8123
#   → http://127.0.0.1:8123/index.html    演化大屏
#   → http://127.0.0.1:8123/evolve.html   双跑对照（赛道「日新」主场）
#   → http://127.0.0.1:8123/result.html   留出结果页
# 也可以直接双击 web/index.html —— data.js 走 <script src> 同步加载，不需要服务

# 2. 双跑（mock，约 8 秒，不花 token）
python -m app.demo pair --pair demo_pair_dev --backend mock

# 3. 回退到初始快照
python -m app.demo rollback --pair demo_pair_dev --to v0_seed

# 4. 真模型（export LLM_API_KEY 后；密钥不进仓库）
python -m app.demo pair --pair demo_pair_calib --backend real --bank calibration

# 5. 20 代演化（真跑，按 config.json 的 model；--export 跑完直接出大屏数据）
python -m app.evolve_run --run-id my_run --gens 20 --export

# 6. 留出评测（3 趟 × 3 血脉 × 20 题）
python -m app.evaluate_holdout --history runs/E_real_20261114/history.jsonl --replicates 3

# 7. 导出白名单（自动重新打包 web/data.js）
python -m app.export --run runs/E_real_20261114 --out web/history.json
```

## 从哪开始

| 目的 | 看这份 |
|---|---|
| 搞懂这个项目在做什么 | [方案/AI养蛊机_项目大纲_v5.md](方案/AI养蛊机_项目大纲_v5.md) |
| 准备写代码 | [方案/技术规格_v5.md](方案/技术规格_v5.md) |
| 排期和验收 | [执行清单_v5.md](执行清单_v5.md) |
| 谁干什么 | [分工方案_v7.3.md](分工方案_v7.3.md) |
| 做 A（判分内核） | [A_任务清单.md](A_任务清单.md) |
| 和 A 对接（JSON 与函数） | [A_to_B_接口契约.md](A_to_B_接口契约.md) |
| 看 C 的交付与实测结论 | [给AB_题域饱和实测结论.md](给AB_题域饱和实测结论.md)、[给AB_真实运行数据与三条硬约束.md](给AB_真实运行数据与三条硬约束.md) |
| 看全部文档与历史 | [方案/README.md](方案/README.md) |

## 目录

```text
app/
  executor.py        判分内核：题面生成 / 边界校验 / Bareiss 行列式 / judge（A）
  llm_standalone.py  complete(messages) -> (text, tokens_in, tokens_out)，预算读 config.json
  demo.py            双跑编排：pair / rollback / status
  evolve_run.py      20 代演化：3 族冻结祖先题 + 6 个体 × 4 道共同题
  evaluate.py        留出评测：3 趟 × 3 血脉 × 20 题
  export.py          白名单导出，reference / holdout / raw_response 不进静态目录
  webdata.py         把 web/*.json 打包成 web/data.js（file:// 场景用）
  serve.py           标准库静态服务，禁目录列表，--allow-rollback 提供回退接口
  evolve_schema.py   双跑数据契约校验
  result_schema.py   结果页数据契约校验 + 防吹牛核对
data/                125 道题：calibration 20 / common_dev 80 / challenge_pool 5 / holdout 20
prompts/             setter / solver / 六份种子基因 / crossover / mutate
web/                 三个单文件页面 + data.js + 三份真数据
tests/               274 passed, 1 skipped
方案/                技术规格 v5.1、项目大纲 v5、分工方案 v7 / v7.3、复核与历史版本
```

## 判分为什么值得看

题域限定在两类可精确求解的题：多项式/指数积分、整数矩阵行列式。
答案一律用 `Fraction` 计算，禁止浮点——`0.1+0.2` 这类问题不会污染分数。

`judge()` 返回三值而非布尔：

| 返回 | 含义 |
|---|---|
| `right` | 答案正确 |
| `wrong` | 答案错误（含格式错、超长、非数值） |
| `unanswered` | 拒答 / 空响应 / 推理截断 |

**拒答与算错必须分开**——把两者混成一个 `False`，会让「模型拒答」看起来像
「模型算错」，于是针对错误类型学到的规则全是错的方向。

推理模型还有第四种状态：`reasoning_content` 吃光 `max_tokens` 时 `content` 为空，
这不是「不会」，是基础设施没给够预算。这类样本记 `infra`，**不进能力分、不进学习器、
不计入判定方向**——否则学习器会从截断里归纳出根本不存在的规则。

## 测试

```bash
pytest            # 274 passed, 1 skipped
```

几条值得单说的：

- `tests/test_hand_calc.py` —— 附录 A 的 16 条用例用三条独立路径（手算 / 闭式 / 数值积分）交叉验证
- `tests/test_file_protocol.py` —— `file://` 断网双击场景：页面不许外链、不许 `fetch`、`data.js` 必须存在
- `tests/test_result.py` —— 结果页不许写死分数、不许外链、不许把 `reference` 带进导出
- `tests/test_verdict_noise.py` —— 判定容差必须等于单题分辨率（20 题 = 5pp），不许拍脑袋定 2.0
- `tests/test_learn_attribution.py` —— 学习器不许从 `infra` 截断样本里归纳规则

## 已知边界（诚实写清楚，不粉饰）

1. **题域饱和是最大风险。** `deepseek-flash` 在 calibration 20/20、gamma_sum 变体 16/16、
   省流提示词 8/8。冠军多趟满分时，本项目的结论是「未观察到稳定改善」，而不是
   「养蛊有效」——区分度不够就没有可演化的信号。详见 `给AB_题域饱和实测结论.md`。
2. **留出评测的方向不一致。** 3 趟结果为 −5.0 / +0.0 / +0.0 个百分点，落在单题分辨率
   （5pp）之内，结论写 `none`，不包装成 positive。
3. **更难的题域当前不可用。** 6–7 阶行列式能拉开差距，但 0/6 全因 `finish=length`
   截断；硅基 Qwen3.5-9B 正确率 8/8 但中位 35s、4B 一半超时 180s。都不可用。
4. **附录 B 变体无效**——闭式公式太容易套，加难度加不上去。
5. 附录 A 的 D2 用例矩阵元素 `10` 超出 §2.1 的 `[-9,9]`，属规格前后不一致，
   见 `给A_边界冲突说明.md`。
