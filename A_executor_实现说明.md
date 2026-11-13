# app/executor.py 实现说明

> **读者**：B，以及 B 用来写 `orchestrator` 的模型。
> **目的**：让你知道每个函数**内部怎么走**，从而正确调用、正确处理返回、不把关键行为"优化"掉。
> 接口形状（JSON 长什么样）看 `A_to_B_接口契约.md`；本文讲的是**内部逻辑与约束**。

---

## 0 三十秒速览

- 位置：`app/executor.py`，只用标准库（`json` / `re` / `decimal` / `fractions` / `math`），**零第三方依赖**。
- 对外 6 个函数，内部 9 个辅助函数。
- 三条铁律：**入口先拒绝再计算**、**全程 `Fraction` 不碰 float**、**`reference` 只能由本模块算出**。
- 一行调用顺序：

```text
validate_task -> normalize_task(内含 signature/statement/solve_exact)
             -> [dedup 用 signature]
             -> solver_view 下发 -> 收回答 -> judge
```

---

## 1 模块分层（自底向上）

```text
常量            MAX_DIGITS=64 / MAX_ANSWER_CHARS=100 / MAX_RESPONSE_CHARS=8000 / MAX_EXPONENT=30
解析器          parse_integer / parse_fraction / parse_decimal / _split_exponent
题面            _SUP / _sup / _poly_expr / statement
校验            _as_int / _check_params / validate_task
归一            normalize_task
精确解          bareiss / solve_exact / _DECIMAL_TOL / _ref_fraction
视图与判分      solver_view / signature / judge
```

下层不依赖上层，所以任何一层都能单独测。

---

## 2 六个函数逐个说明

### 2.1 `validate_task(raw: dict) -> tuple[bool, str]`

**做什么**：只做 schema 与边界校验，**不做任何求值**。返回 `(True, "")` 或 `(False, 原因)`。

**内部步骤**

1. `raw` 不是 `dict` → `(False, "出题对象必须是 JSON 对象")`
2. `set(raw) != {"template", "params"}` → 拒。**多一个键也拒**（例如模型自作主张加了 `"level": 3`）
3. `template` 不在 `poly` / `gamma` / `gaussian_odd` / `det` → 拒
4. `params` 不是 `dict` → 拒
5. 调 `_check_params(template, params)`，捕获 `ValueError` 转成 `(False, str(exc))`
6. 全过 → `(True, "")`

**`_check_params` 的边界表**

| 模板 | 校验内容 |
|---|---|
| `poly` | 键集合恰为 `{coeffs, lower, upper}`；`coeffs` 是 list 且长度 2..5；每个元素 ∈ [-5,5]；`coeffs[-1] != 0`；至少一个 `coeffs[i] != 0`（i≥1）；`lower` ∈ [-3,3]；`upper` ∈ [lower+1, lower+6] |
| `gamma` | 键集合恰为 `{c, n, rate}`；`n` ∈ [1,9]；`rate` ∈ [1,8]；`c` ∈ [-5,5] 且 `c != 0` |
| `gaussian_odd` | 键集合恰为 `{c, k, rate}`；`k` ∈ [0,5]；`rate` ∈ [1,8]；`c` ∈ [-5,5] 且 `c != 0` |
| `det` | 键集合恰为 `{matrix}`；2..5 行；每行长度等于行数（方阵）；每个元素 ∈ [-9,9] |

**所有整数都过 `_as_int`**：

```python
def _as_int(value, field, lo, hi):
    if type(value) is not int:          # 不是 isinstance！
        raise ValueError("%s 必须是 int，收到 %s" % (field, type(value).__name__))
    if not (lo <= value <= hi):
        raise ValueError("%s 越界：%r 不在 [%d, %d]" % (field, value, lo, hi))
    return value
```

`type(True) is int` 为 **False**，所以这一行同时挡住 `true`、`2.0`、`"2"`。写成 `isinstance(value, int)` 会放过 `True`。

**返回示例**

```python
validate_task({"template":"gamma","params":{"n":3,"rate":0,"c":1}})
# (False, 'rate 越界：0 不在 [1, 8]')
validate_task({"template":"poly","params":{"coeffs":[1,1],"lower":0,"upper":1}, "level":3})
# (False, '出题对象只能有 template 与 params，收到 [\'level\', \'params\', \'template\']')
```

**B 必须遵守**

- 它**不抛异常**，只返回 `False`。不要包 try/except。
- `ok=False` 表示"本次出题尝试失败"，计入该个体的尝试次数，**不要擅自修正参数后重试**（规格禁止"前端偷偷修正越界值"）。

---

### 2.2 `normalize_task(raw, *, task_id, source, by, gen) -> dict`

**做什么**：把校验通过的出题对象补全成后端 `Task`（10 个字段）。

**内部步骤**

1. 先调 `validate_task`；不通过直接 `raise ValueError("出题对象未通过校验：<reason>")`
2. `type` 由模板推导：`det` → `"det"`，其余 → `"integral"`
3. 三次派生调用：`signature(...)`、`statement(...)`、`solve_exact(...)`
4. 返回固定 10 键 dict

**注意**

- `params` 是**原 dict 的引用**，不是深拷贝。B 拿到 Task 后不要就地改 `params`。
- 这里的 `raise ValueError` 是**开发者错误**（说明你跳过了 validate），不是模型错误。生产路径上不应触发。
- `reference` 只来自 `solve_exact`。任何"模型说答案是 X"的说法都不影响它。

---

### 2.3 `solve_exact(task: dict) -> dict`

**做什么**：按模板算精确答案，返回可 JSON 化的 `reference`。

**四个模板的实现**

```python
poly:            sum(Fraction(a * (u**(i+1) - l**(i+1)), i+1) for i, a in enumerate(coeffs))
gamma:           Fraction(c * factorial(n), rate ** (n + 1))
gaussian_odd:    Fraction(c * factorial(k), 2 * rate ** (k + 1))
det:             {"kind": "integer", "value": str(bareiss(matrix))}
```

**编码规则**

- 分母为 1 → `{"kind": "integer", "value": "9"}`
- 否则 → `{"kind": "rational", "numerator": "3", "denominator": "4"}`
- 数字一律转字符串（避免 JSON 把大整数变成浮点）
- 未知模板 → `raise ValueError`

**`bareiss(m)` 的三个关键点**

```python
if a[k][k] == 0:                 # 主元为 0
    ... 换行 + sign = -sign      # 找不到非零主元 -> return 0
a[i][j] = (a[i][j]*a[k][k] - a[i][k]*a[k][j]) // prev   # // 是整除，必然除尽
```

- 换行必须翻符号，否则 D6 会错成 `1`（正确是 `-1`）。
- 全零列直接判 0，不能继续消元。
- `// prev` 是 Bareiss 的精髓：整数矩阵全程整数，不产生分数，也就没有精度问题。

**调用约束**

- 它**不校验参数**（性能考虑）。必须先用 `validate_task`。
- 不要为了"试试答案"在别处重复实现这些公式——四族公式只有这一份。

---

### 2.4 `solver_view(task: dict) -> dict`

**做什么**：白名单构造给模型的 `SolverTask`。

```python
{"type": ..., "statement": ..., "answer_format": ...}
```

`answer_format` 只有两句：

- `det` → `"integer only"`
- 其它 → `"integer, fraction or finite decimal"`

**为什么必须走它**

直接 `json.dumps(task)` 会把 `reference`、`template`、`params`、`by`、`gen` 一起发出去——模型看到答案就没实验了。S6 断言键集合恰为这三键。

---

### 2.5 `signature(task: dict) -> str`

**做什么**：生成规范化签名字符串，供硬去重。

| 模板 | 格式 |
|---|---|
| `gamma` / `gaussian_odd` | `模板\|c=..\|k=..\|rate=..`（参数名按字母序） |
| `poly` | `poly\|coeffs=1,1\|lower=0\|upper=1` |
| `det` | `det\|matrix=2,1;1,3` |

**三条约束**

1. `poly` **必须带上下限**：`coeffs=[1,1]` 在 `[0,1]` 和 `[1,2]` 上是两道题（S5）。
2. 参数按字母序拼接，保证同参数不同键序得到同一签名。
3. 只保证"本项目定义的规范形式"的重复检测，**不承担数学等价判定**（`x+x` 和 `2x` 不算同一题）。

---

### 2.6 `judge(task, raw_answer) -> str`

**做什么**：把模型返回的原始字符串判成 `right` / `wrong` / `unanswered`。纯函数。

**严格顺序（顺序本身就是正确性）**

```text
1. 不是 str（含 None）              -> unanswered
2. len(raw_answer) > 8000           -> unanswered      ← 必须在 json.loads 之前
3. json.loads 失败（含非 JSON 文本） -> unanswered
4. 不是 dict，或键集合 != {"value"}  -> unanswered
5. value 不是 str                    -> unanswered
6. strip 后为空，或长度 > 100        -> unanswered
7. 按 type 解析                       -> 失败即 unanswered
     det:      只走 parse_integer
     integral: parse_integer -> parse_fraction -> parse_decimal（任一成功即停）
8. 取 reference（结构损坏会抛 ValueError）
9. 比较：
     det:      精确相等
     integral: 用了 decimal -> |ans-ref| <= 1e-8 * max(1, |ref|)
               否则         -> 精确相等
```

**三态语义**

| 状态 | 含义 | 计分 |
|---|---|---|
| `right` | 数值正确 | 1 |
| `wrong` | 数值合法但不等 | 0，**且占分母** |
| `unanswered` | 格式错/空/超限 | 0，**且占分母** |

模型不会因为格式失败而"少答一题"——这是为了让通过率口径一致。

**唯一抛异常的情况**

`reference` 结构损坏（`kind` 不认识、缺字段、整个不是 dict）时 `_ref_fraction` 抛 `ValueError`。

```python
try:
    status = judge(task, raw)
except ValueError:
    # 参考值异常 -> 中止该代，标 invalid，不继续排名
```

**不要**把它 catch 成 `wrong`。那会把"执行器/数据坏了"记成"模型不会做"，整份实验的结论就失真了。

---

## 3 辅助函数速查（B 不该直接调，但需要知道存在）

| 函数 | 作用 | 失败时 |
|---|---|---|
| `parse_integer(s)` | `[+-]?[0-9]+` → int，位数 ≤64 | `ValueError` |
| `parse_fraction(s)` | `a/b` → Fraction，分母 >0 | `ValueError` |
| `parse_decimal(s)` | 有限十进制 → Fraction，必须含 `.` 或 `e/E`，指数 ≤30 | `ValueError` |
| `_split_exponent(s)` | `1.25e-3` → `("1.25", -3)` | 不会失败 |
| `_sup(n)` | 指数 → Unicode 上标，>19 回退 `^n` | 不会失败 |
| `_poly_expr(coeffs)` | 多项式 → 降幂表达式字符串 | 系数全 0 时返回空串 |
| `statement(template, params)` | 题面生成（纯函数） | 未知模板 `ValueError` |
| `_as_int(v, field, lo, hi)` | 严格整数校验 | `ValueError` |
| `_check_params(t, params)` | 模板参数校验 | `ValueError` |
| `bareiss(m)` | 精确行列式 | 不会失败 |
| `_ref_fraction(ref)` | reference → Fraction | `ValueError` |

**`statement()` 虽然是公开的，但只应该通过 `normalize_task` 调用**——绕过它会让 `Task.statement` 和 `Task.signature` 不一致。

---

## 4 八个"不要优化"的点

这些地方看着可以简化，改了就错，或者会静默降低实验质量。

| # | 不要改成 | 为什么 |
|---|---|---|
| 1 | `isinstance(v, int)` | 会放过 `True`，D8 直接挂 |
| 2 | 把长度检查挪到 `json.loads` 之后 | 9000 字符的垃圾串会被解析，S1 的 mock 断言挂 |
| 3 | 给 `parse_fraction` 加"整数兜底" | 三条分支就不互斥了，`"5"` 会被两种规则同时接受 |
| 4 | 让 `det` 接受 `5/1` 或 `5.0` | 行列式答案域只有整数，放宽等于送分 |
| 5 | 用 `try: judge(...) except: return "wrong"` | 数据故障被记成模型能力问题 |
| 6 | 从 `signature` 里去掉 `poly` 的 `lower`/`upper` | 不同区间的题会被判成重复，M=6 凑不齐 |
| 7 | 在 `validate_task` 里"帮模型修正"越界值 | 规格明令禁止；会污染"格式失败率"这个指标 |
| 8 | 用 `float()` 替换任何 `Fraction` | `0.1+0.2 != 0.3`，答案最大分母是 10 位数 |

---

## 5 B 侧调用顺序（含错误分支）

```python
# 收到 setter 模型的原始文本 raw_text
ok, reason = validate_task(raw_obj)          # raw_obj 由 B 自己 json.loads 得到
if not ok:
    attempts += 1                             # 本次尝试失败，重出
else:
    task = normalize_task(raw_obj, task_id=..., source="evolved", by=ind_id, gen=gen)
    if task["signature"] in dedup_set:
        mark_duplicate(); attempts += 1
    else:
        dedup_set.add(task["signature"])
        pool.append(task)

# 下发
payload = solver_view(task)                   # 只发这个
raw_answer = chat(payload)                    # 模型原始返回

# 判分
try:
    status = judge(task, raw_answer)
except ValueError:
    abort_generation(reason="broken_reference")  # 中止该代
```

---

## 6 自检命令

```bash
python -m pytest -q tests/                  # 39 passed
python tools/check_statement.py             # 题面 8 条逐字
python tools/sympy_crosscheck.py 100        # 需要 sympy 环境
```

改完 `executor.py` 后这三条都跑一遍，再交下一个环节。
