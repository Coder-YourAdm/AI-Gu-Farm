# A · 判分内核任务清单

> **权威顺序**：`方案/技术规格_v5.md`（v5.1）> `分工方案_v7.3.md` 的 A 段。两者冲突时以技术规格为准，它自己写了「字段名逐字照抄，一个都不改」。  
> **负责人**：A ｜ **交付**：今晚 A1 → 00–06H A2 → 06–12H A3/A4  
> **核心原则**：先拒绝，再计算；全程 `Fraction`，不碰 float；期望值手算，不许拿 `solve()` 的输出当答案。

---

## 0 开工前 15 分钟（三人一起，念完贴群里）

- [x] `Task` 键名：`id / source / by / gen / type / template / params / signature / statement / reference`
- [x] `SolverTask` 键名：`type / statement / answer_format` —— 多一个键就破坏 `S6`
- [x] 执行器 6 个函数：`validate_task` / `normalize_task` / `solve_exact` / `solver_view` / `signature` / `judge`
- [x] `config.json` 里 A 用到的键：`seed / fitness_window / set_weight / bounty / max_response_chars / max_answer_chars / max_number_digits`
- [x] 说清一句话：**24 条用例的期望值由 A 手算，C 独立再算一遍交叉核对，分歧不超过 2 条且要查清**

---

## A1 · executor 骨架（今晚，约 3 小时）

### 文件与导入

- [ ] 建 `app/executor.py`
- [ ] `from fractions import Fraction`、`from decimal import Decimal`、`from math import factorial`
- [ ] 不引入 SymPy / mpmath / numpy（规格 §1 明确首版不需要）

### 六个函数签名（照抄规格 §2.3，参数名、类型注解、返回值都不改）

- [ ] `validate_task(raw: dict) -> tuple[bool, str]`
- [ ] `normalize_task(raw: dict, *, task_id, source, by, gen) -> dict`
- [ ] `solve_exact(task: dict) -> dict`
- [ ] `solver_view(task: dict) -> dict`
- [ ] `signature(task: dict) -> str`
- [ ] `judge(task: dict, raw_answer: str | None) -> str`

> ⚠️ 规格里**没有** `validate()` / `solve()`。写错名字，B 的 import 和验收命令会全部失败。

### 三个解析器（入口先拒绝）

- [ ] `parse_integer(s)`：`^[+-]?\d+$`；**第一行就判 `isinstance(x, bool)` 并拒**；数字位数 >64 拒
- [ ] `parse_fraction(s)`：`[+-]?\d+/\d+`；分母 ≤0 拒；用 `Fraction` 自动约分、分母恒正
- [ ] `parse_decimal(s)`：必须**实际含 `.` 或 `e/E`**；指数绝对值 ≤30；尾数位数 ≤64；`Decimal` → `Fraction`，**不参与比较时用 float**
- [ ] 统一出口：格式错 → `unanswered`；数值合法但不等 → `wrong`；两者都计 0 且占分母

### 四个模板（全程 Fraction）

- [ ] `poly`：`Σ aᵢ·(u^(i+1) − l^(i+1))/(i+1)`；校验 `coeffs[-1] != 0`、至少一个 `i≥1` 的系数非零、`upper > lower`
- [ ] `gamma`：`Fraction(c * factorial(n), rate**(n+1))`
- [ ] `gaussian_odd`：`Fraction(c * factorial(k), 2 * rate**(k+1))`
- [ ] `det`：Bareiss 精确消元，零主元换行并记符号，找不到非零主元 → 0
- [ ] 参数边界照规格 §2.1 表逐条实现：`rate=0` 拒（发散）、`c=0` 拒（无区分度）、`n=0` 拒、矩阵元素严格 `type(v) is int`

> ⚠️ 这里**没有** `ln` / `sin` / `cos` / `e^(ax)`，也没有 `n=-1` 分支。三族全是定积分与反常积分，答案是数值。

### `statement()` 题面生成（规格 §2.5）

- [ ] Unicode 上标（`x⁴` 不是 `x^4`）；指数为 1 省略上标；指数为 0 并入常数项
- [ ] 系数 1 省略、-1 写 `-x⁴`、其他负数把负号放项首
- [ ] 降幂排列，项间 `+` / `-`，系数取绝对值
- [ ] `gamma` 上限写 `∞` 不写 `+∞`
- [ ] `gaussian_odd` 展开成具体奇数次（`k=2` → `x⁵`），不写 `2k+1`
- [ ] `det` 用 `[[2, 1], [1, 3]]` 行优先、逗号后一个空格
- [ ] 纯函数：同输入永远同输出

### A1 验收（自己跑，跑不出绿不许开 A2）

- [ ] `python -c "from app.executor import validate_task, normalize_task, solve_exact, solver_view, signature, judge"` 不报错
- [ ] I1–I8 返回值与附录 A 逐条相等，且 `type(v) is Fraction`（`isinstance(v, float)` 必须为 False）
- [ ] 附录 A 里 4 条题面字符串**逐字相等**
- [ ] 非法输入在**求值之前**被拒（用 mock 计数证明没进 `solve_exact`）

---

## A2 · 24 个核心用例（00–06H，约 4 小时）

### I 组 · 积分 8 条

- [ ] `I1` `poly` coeffs=[1,1], l=0, u=1 → `3/2`
- [ ] `I2` `poly` coeffs=[3,-2,1], l=-1, u=2 → `9`（分母为 1，编码为 integer）
- [ ] `I3` `poly` coeffs=[0,0,5], l=1, u=2 → `35/3`
- [ ] `I4` `gamma` n=1, rate=2, c=1 → `1/4`
- [ ] `I5` `gamma` n=9, rate=8, c=5 → `14175/8388608`（分母上限用例）
- [ ] `I6` `gaussian_odd` k=0, rate=2, c=3 → `3/4`
- [ ] `I7` `gaussian_odd` k=5, rate=1, c=-2 → `-120`（负号不能丢）
- [ ] `I8` 拒绝组 6 个输入：`rate=0`、`n=0`、`c=0`、`template="eigen"`、`coeffs=[1,0]`、`upper=l` → 全部 `(False, 原因)`

### D 组 · 行列式 8 条

- [ ] `D1` `[[2,1],[1,3]]` → `5`
- [ ] `D2` `[[1,2,3],[4,5,6],[7,8,10]]` → `-3`
- [ ] `D3` `[[1,2],[2,4]]` → `0`（奇异，主元全零分支）
- [ ] `D4` `[[2,1,0],[1,3,1],[0,1,2]]` → `8`
- [ ] `D5` `[[3,1,2],[0,-2,5],[0,0,7]]` → `-42`（上三角 = 对角积）
- [ ] `D6` `[[0,1],[1,0]]` → `-1`（首元素为 0 必须换行）
- [ ] `D7` `[[0,2,1],[3,0,1],[4,5,0]]` → `23`（多轮换行）
- [ ] `D8` 拒绝组 4 个输入：非方阵、元素 `2.0`（float）、元素 `true`（bool）、1 阶 `[[5]]` → 全部拒绝

### S 组 · 系统契约 8 条（v7.2 漏掉的就是这组）

- [ ] `S1` 原始响应 9000 字符（> `max_response_chars=8000`）→ 解析前即拒，**未调用 `json.loads`**
- [ ] `S2` `{"value":"sqrt(4)"}`、`{"value":"1; import os"}`、`{"value":"1/0"}` → 全部 `unanswered`，不抛异常
- [ ] `S3` `NaN` / `Infinity` / `1e31` / 65 位整数 → 全部 `unanswered`
- [ ] `S4` 同签名同代提交两次 → 第二次 `duplicate`，`dedup_set` 大小不增
- [ ] `S5` `poly`[1,1] 取 `l=0,u=1` 与 `l=1,u=2` → 两个不同签名，均入池
- [ ] `S6` `solver_view(task)` 键集合恰为 `{type, statement, answer_format}`，序列化后不含参考值数字串
- [ ] `S7` `refetch_rounds=2` 用尽 → aborted、`slots_created == 0`、**`solver_calls == 0`**
- [ ] `S8` `Fraction(35,3)` 与 RNG state 各存一次再读回 → 逐位相等，连抽 100 个数一致

### A2 验收

- [ ] `python -m pytest -q tests/test_executor.py` 24 条全绿
- [ ] 每条期望值都是字面量，全文搜不到 `== solve_exact(task)` 这种自证写法
- [ ] `I8` / `D8` 断言的是**抛错或 `(False, 原因)`**，不是「返回了一个错值」

---

## A3 · scoring 公平性（06–12H，约 3 小时）

- [ ] `judge()` 保持纯函数：不读文件、不读场景、不读条件标签，只比对 `task["reference"]` 与答案
- [ ] **不给 `Task` 加难度系数**（会破坏 `S6` 的白名单断言）
- [ ] 算分照规格 §6.1：`solve` 0–4、`c` 0–5、`set_score = min(c,5-c)/2`、`total = solve + set_weight*set_score + bounty`
- [ ] 同分打散用 `random.Random(seed)`，种子来自 `config.json`，**禁用全局 `random`**
- [ ] 排序先按 ID 排好再 shuffle（避免并发回包顺序影响抽签）
- [ ] 回归 1：`common` 槽位复制给六人 → 六个 `solve` 完全相同，`ranking_key` 差异只能来自 `set_score`
- [ ] 回归 2：父代 `gene_history` 有高分，子代 `est_score` 只用自己样本
- [ ] 回归 3：`fitness_window=1` 时 `est_score == current_total`

**验收**：三条回归全绿；同一 `Task` 两次 `judge` 结果一致。

---

## A4 · ancestor 祖先指标（06–12H，约 3 小时）

> 「祖先」= **3 个冻结的参照个体**，不是血统来源。按 lineage 分层算失败率不在 v5 定义里。

- [ ] 从每代 18 个祖先槽位算 `per_task_failure = 1 - ancestor_correct/3`（`unanswered` 算错但单独计数）
- [ ] `integral_failure`：积分题均值，本代无积分题 → `null`
- [ ] `det_failure`：行列式题均值，本代无行列式题 → `null`
- [ ] `balanced_failure = 0.5*integral + 0.5*det`，**两型都有才算**
- [ ] `mixed_failure`：六题均值，另附题型数量
- [ ] `null` 不当 0、不跨缺口拼线；三代均线需连续三代有值，均线不参与排名
- [ ] 挑战题不进祖先尺；挑战个体单独打标
- [ ] 字段名与规格一致（不要同时出现 `failrate` 和 `failure_rate`）

**验收**：喂 20 代假 `history[]` 能输出四个字段；删掉某代某题型后该字段为 `null` 而不是 0。

---

## 交接物（做完就推，不等别人）

| 交给谁 | 交什么                | 对方怎么验                      |
| --- | ------------------ | -------------------------- |
| B   | `judge()` 可用       | B 的 mock 一代能评分跑通           |
| B   | `solve_exact()` 可用 | 参考值能进 `Task["reference"]`  |
| C   | 24 条期望值            | C 独立手算 16 条，与 A 分歧 ≤2 条且查清 |
| C   | 祖先指标字段名            | C 的大屏按这套字段画图，不改代码          |

---

## 五条红线（违反任一条，判分就不成立）

1. **不用 float 算积分或行列式**，也不用 `float()` 中途转换。
2. **不拿 `solve_exact()` 的输出当测试期望值**——那样只证明代码等于自己。
3. **不让 `solver_view()` 泄漏 `reference` / `template` / `params` / `by` / `gen`**。
4. **不把网络或格式失败记成 `wrong`**：格式错是 `unanswered`，基础设施错要中止该代。
5. **不在 judge 里读任何外部状态**（文件、配置里的场景标签、历史成绩）。

---

## 时间点自查

| 时点     | 应该已经完成                                         |
| ------ | ---------------------------------------------- |
| 今晚收工   | A1 六个函数 + 三族 + Bareiss + `statement()`，I1–I8 绿 |
| 00–06H | A2 24 条全绿                                      |
| 06–12H | A3 三条回归 + A4 四个字段                              |
| 12–18H | 协助 B 做 store 恢复测试与挑战校验                         |
| 22H 前  | `config.json` 冻结，A 不再改判分逻辑                     |

---

## 复审记录（2026-11-13 20:20，commit `1e2550d`）

A1 完成后自查出 4 处问题，已全部修复，pytest 从 36 涨到 **39 passed**：

| # | 问题 | 处理 |
| --- | --- | --- |
| 1 | `parse_fraction("5")` 返回 5，偏离规格「fraction 只接 `a/b`」 | 去掉兜底，裸整数必须走 `parse_integer`，三条分支互斥 |
| 2 | `judge` 遇损坏 `reference` 抛 `KeyError`，看不出是数据坏了 | 改 `_ref_fraction` 显式校验并抛 `ValueError`（§15：参考值异常要中止） |
| 3 | S1 只断言返回 `unanswered`，没做 mock 计数 | 补 `test_S1_oversized_never_reaches_json_loads`，断言 `calls == []` |
| 4 | S6 断言 `"9" not in ...` 太脆，换题面就误判 | 改为检查泄漏键名 + 题面一致性 |

**交给 B 的注意事项**：`judge()` 会在 `reference` 损坏时抛 `ValueError`，orchestrator 必须捕获并中止该代，**不能记成 `wrong`**。详见 `A_to_B_接口契约.md` §6。

**仍未覆盖的契约**：S4（需 `dedup_set`）、S7（需出题重做机制）、S8 的 RNG 部分（需快照）——等 B 的 orchestrator 起来后补进 `tests/test_contracts.py`，测试文件头部已注明。
