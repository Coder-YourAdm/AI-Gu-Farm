# AI 养蛊机 · Prompt Evolver

> BoHack 2026 天津青年黑客松参赛作品 · 赛道主题「日新 / 生生不息」

6 个 prompt 个体互相出题、互相解题；按题目区分度淘汰并重组，20 代后测量模型在当前题域上的能力上限。

**不做模型训练，不用 LLM 当裁判**——判分走本地精确算术（`Fraction`，无浮点），
同一道题任何人任何时候算出的答案都一样。

## 现在能跑什么

| 能力 | 状态 |
|---|---|
| 判分内核 `app/executor.py` | ✅ 六函数齐全，39 条单测 |
| 题集 125 道（`data/`） | ✅ 已回填 `signature`/`statement`/`reference`，`validate_task` 125/125 |
| 双跑编排 `app/demo.py` | ✅ `pair` / `rollback` / `status`，`--backend mock\|real` |
| 快照回退 | ✅ `v0_seed` 一键回退，实测逐字节可逆 |
| 演化大屏 / 双跑对照 / 结果页 | ✅ 三个单文件页面，零依赖、断网可演示 |
| 真模型调用 `app/llm.py` | ⚠️ 代码就绪，`config.json` 的 `model`/`base_url` 未填、缺 API key |
| 留出评测 `web/result.json` | ⚠️ 页面已按契约实现，数据待评测产出（现为 mock，页面挂红标） |

**诚实标注**：仓库里 `web/evolve.json`、`web/result.json` 当前是 `is_mock: true` 的
演示数据，用于跑通链路与排练。页面上有 `mock` 徽章，不会把假数字当真结果展示。

## 跑起来

```bash
# 1. 静态服务（三个页面）
python app/serve.py --port 8123
#   → http://127.0.0.1:8123/index.html    演化大屏
#   → http://127.0.0.1:8123/evolve.html   双跑对照（赛道「日新」主场）
#   → http://127.0.0.1:8123/result.html   留出结果页

# 2. 双跑（mock，约 8 秒，不花 token）
python -m app.demo pair --pair demo_pair_dev --backend mock

# 3. 回退到初始快照
python -m app.demo rollback --pair demo_pair_dev --to v0_seed

# 4. 真模型（需先填 config.json 的 model / base_url，并 export LLM_API_KEY）
python -m app.demo pair --pair demo_pair_dev --backend real
```

## 目录

```text
app/
  executor.py        判分内核：题面生成 / 边界校验 / Bareiss 行列式 / judge（A）
  llm.py             complete(messages) -> (text, tokens_in, tokens_out)（B 的契约）
  demo.py            双跑编排：pair / rollback / status
  export.py          白名单导出，reference / holdout / raw_response 不进静态目录
  serve.py           标准库静态服务，禁目录列表，--allow-rollback 提供回退接口
  evolve_schema.py   双跑数据契约校验
  result_schema.py   结果页数据契约校验 + 防吹牛核对
data/                125 道题：calibration 20 / common_dev 80 / challenge_pool 5 / holdout 20
prompts/             setter / solver / 六份种子基因 / crossover / mutate
web/                 三个单文件页面 + 两份 mock 数据
tests/               151 条测试
方案/                技术规格 v5、项目大纲 v5、分工方案 v7.3
tools/               开发期交叉验证（sympy 对照 / 题面自检），不进判分链路
```

## 判分为什么值得看

题域限定在两类可精确求解的题：多项式/指数积分、整数矩阵行列式。
答案一律用 `Fraction` 计算，禁止浮点——`0.1+0.2` 这类问题不会污染分数。

`judge()` 返回三值而非布尔：

| 返回 | 含义 |
|---|---|
| `right` | 答案正确 |
| `wrong` | 答案错误（含格式错、超长、非数值） |
| `unanswered` | 拒答 / 空响应 |

**拒答与算错必须分开**——把两者混成一个 `False`，会让「模型拒答」看起来像
「模型算错」，于是针对错误类型学到的规则全是错的方向。

## 测试

```bash
pytest            # 151 passed, 1 skipped
```

几条值得单说的：

- `tests/test_hand_calc.py` —— 附录 A 的 16 条用例用三条独立路径（手算 / 闭式 / 数值积分）交叉验证
- `tests/test_backfill.py` —— 回填后的题集必须能被 `judge()` 吃下，且重跑逐字节相同
- `tests/test_result.py` —— 结果页不许写死分数、不许外链、不许把 `reference` 带进导出
- `tests/test_prompts.py` —— 提示词必须用 `str.replace()` 渲染，`setter.md` 里的 `{coeffs}` 会击穿 `str.format()`

## 已知边界

- 留出评测（`web/result.json`）尚未由真模型跑出，结果页目前是 mock 数据
- 附录 A 的 D2 用例矩阵元素 `10` 超出 §2.1 的 `[-9,9]`，属规格前后不一致，见 `给A_边界冲突说明.md`
