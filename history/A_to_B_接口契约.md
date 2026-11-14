# A → B 接口契约（executor v1）

> 依据 `方案/技术规格_v5.md` §2；实现文件 `app/executor.py`（A1 已完成，pytest 39 passed）。
> **六个函数签名与字段名已冻结，逐字照抄，不要改名。** 下面的 JSON 都是真实运行输出，不是手写示意。

---

## 1 六个函数

```python
validate_task(raw: dict) -> tuple[bool, str]
normalize_task(raw: dict, *, task_id: str, source: str, by: str | None, gen: int | None) -> dict
solve_exact(task: dict) -> dict
solver_view(task: dict) -> dict
signature(task: dict) -> str
judge(task: dict, raw_answer: str | None) -> str
```

| 函数 | 谁调 | 干什么 |
|---|---|---|
| `validate_task` | B（出题回包后第一站） | 只做 schema + 边界校验，**不做求值**；返回 `(ok, reason)`，不抛异常 |
| `normalize_task` | B（校验通过后） | 补全成后端 `Task`，其中 `reference` 由 `solve_exact` 算出 |
| `solve_exact` | 只被 `normalize_task` 调 | 精确解，返回可 JSON 化的 `reference` |
| `solver_view` | B（发请求前） | 白名单构造 `SolverTask`，**唯一允许发给模型的结构** |
| `signature` | B（去重） | 规范化签名字符串 |
| `judge` | B（收回答后） | 三态判定；纯函数，不读任何外部状态 |

---

## 2 出题对象（setter 模型输出的解析结果）

```json
{"template": "gamma", "params": {"n": 4, "rate": 2, "c": 1}}
```

顶层**只能有 `template` 和 `params` 两个键**，多一个就拒。

| template | params 键 | 取值范围 |
|---|---|---|
| `poly` | `coeffs` / `lower` / `upper` | `coeffs` 长度 2..5、元素 -5..5、末项非零、至少一个 `i>=1` 非零；`lower` -3..3；`upper` = lower+1..lower+6 |
| `gamma` | `n` / `rate` / `c` | `n` 1..9；`rate` 1..8（**0 直接拒**）；`c` ±{1..5}（**0 拒绝**） |
| `gaussian_odd` | `k` / `rate` / `c` | `k` 0..5；`rate` 1..8；`c` ±{1..5} |
| `det` | `matrix` | 方阵 2..5 阶；元素 -9..9，**严格 int** |

整数必须是 `type(v) is int`：`true` / `2.0` / `"2"` 全部拒。

---

## 3 后端 Task（B 落盘、传给 `judge` 的对象）

真实输出：

```json
{
  "id": "T08_02",
  "source": "evolved",
  "by": "G06_C1",
  "gen": 8,
  "type": "integral",
  "template": "gamma",
  "params": {"n": 4, "rate": 2, "c": 1},
  "signature": "gamma|c=1|n=4|rate=2",
  "statement": "计算定积分 ∫₀^∞ x⁴ e^{-2x} dx。",
  "reference": {"kind": "rational", "numerator": "3", "denominator": "4"}
}
```

行列式版本：

```json
{
  "id": "D1",
  "source": "seed",
  "by": null,
  "gen": 0,
  "type": "det",
  "template": "det",
  "params": {"matrix": [[2, 1], [1, 3]]},
  "signature": "det|matrix=2,1;1,3",
  "statement": "计算下列 2 阶矩阵的行列式：[[2, 1], [1, 3]]。",
  "reference": {"kind": "integer", "value": "5"}
}
```

| 字段 | 说明 |
|---|---|
| `id` | 唯一 ID，格式 `G{出生代:02d}_{来源}{序号}` |
| `source` | `seed` / `evolved` / `challenge` |
| `by` | 出题个体的 ID，数据集的题为 `null` |
| `gen` | 出生代 |
| `type` | `integral` 或 `det`（只这两种） |
| `reference` | **只在后端缓存**，绝不下发给模型 |

---

## 4 SolverTask（唯一能发给模型的 JSON）

```json
{
  "type": "integral",
  "statement": "计算定积分 ∫₀^∞ x⁴ e^{-2x} dx。",
  "answer_format": "integer, fraction or finite decimal"
}
```

- 键集合**恰为** `{type, statement, answer_format}`。多一个键，S6 断言就挂。
- `answer_format` 只有两句：积分 `integer, fraction or finite decimal`，行列式 `integer only`。
- **禁止** `json.dumps(task)` 直接把后端 Task 发出去——`reference` 和 `template` 会一起泄漏。

---

## 5 `reference` 的两种编码

```json
{"kind": "integer",  "value": "9"}
{"kind": "rational", "numerator": "3", "denominator": "4"}
```

- 分母为 1 的一律编码成 `integer`（I2 期望的就是 `9`，不是 `9/1`）。
- 数字全部是**字符串**，避免大整数在 JSON 里被变成浮点。
- `det` 只用 `integer`。

---

## 6 `judge(task, raw_answer)` 的输入输出

输入是**模型的原始返回字符串**（整段文本），不是解析后的对象。

| 输入 | 输出 |
|---|---|
| `{"value":"3/4"}` | `right` |
| `{"value":"0.75"}` | `right`（十进制走 1e-8 相对容差） |
| `{"value":"0.8"}` | `wrong` |
| `sqrt(4)`（不是 JSON） | `unanswered` |
| `None` / 空串 | `unanswered` |
| 9000 字符响应 | `unanswered`（在 `json.loads` 之前就拒） |

三态语义：`right` 计 1；`wrong` 与 `unanswered` 都计 0，**且都占分母**——模型不会因为格式失败而"少答一题"。

### ⚠️ 唯一会抛异常的地方

`judge` 在 `reference` 结构有问题时抛 `ValueError`（数据故障，不是模型答错）。**保证只抛 ValueError**，不会漏成 `KeyError` 或 `ZeroDivisionError`；覆盖：不是 dict、`kind` 不认识、缺 `value`/`numerator`/`denominator`、字段不是整数、分母为 0。B 必须捕获：

```python
try:
    status = judge(task, raw)
except ValueError:
    # 参考值异常 -> 中止该代，标记 invalid，不继续排名（规格 §15）
```

不要把它 try 成 `wrong`，那会把执行器 bug 记成模型能力问题。

---

## 7 `signature` 格式

```
gamma|c=1|n=4|rate=2
poly|coeffs=1,1|lower=0|upper=1
gaussian_odd|c=-2|k=2|rate=3
det|matrix=2,1;1,3
```

- 参数按字母序，值与值之间 `|`，键值之间 `=`。
- `poly` **必须含上下限**——`coeffs=[1,1]` 在 `[0,1]` 和 `[1,2]` 上是两道不同的题（S5）。
- 精确相等即视为重复；数学等价性不由签名承担。

---

## 8 拒绝示例（`validate_task` 的 reason）

```python
validate_task({"template": "gamma", "params": {"n": 3, "rate": 0, "c": 1}})
# -> (False, 'rate 越界：0 不在 [1, 8]')
```

`ok=False` 时按「本次尝试失败」计数，不抛异常、不修正取值。

---

## 9 B 侧流水线建议

```text
模型出题返回
  -> json.loads（失败：格式错，计一次尝试）
  -> validate_task  (ok, reason)
        False -> 计一次尝试，重出
        True  -> normalize_task 生成 Task
                 -> signature 查 dedup_set
                      命中 -> duplicate，题不入池
                      未命中 -> 入池，reference 缓存
  -> solver_view 下发
  -> 收回答 -> judge -> right / wrong / unanswered
```

---

## 10 B 不要做的事

1. 不要自己给 `Task` 加字段（尤其 `difficulty`）——`SolverTask` 白名单会挂。
2. 不要自己算 `reference`，也不要相信模型的任何 claim。
3. 不要在 `judge` 外层再包一层"格式错就重发一次"——规格只允许本地 JSON 提取修复。
4. 不要改 `signature` 的拼接格式——它同时用于四套数据集的互斥校验。

---

## 11 祖先指标（`app/ancestor.py`，C 的大屏用这套字段）

```python
from app.ancestor import ancestor_report

generations = [{"gen": 0, "slots": ancestor_slots, "task_types": {"T1": "integral"}}, ...]
rows = ancestor_report(generations)      # 每代一行，已带三代均线
```

| 字段 | 含义 | 空值规则 |
|---|---|---|
| `gen` | 代数 | — |
| `integral_failure` | 积分题的 `1-correct/3` 均值 | 本代无积分题 → `null` |
| `det_failure` | 行列式同上 | 本代无行列式题 → `null` |
| `balanced_failure` | `0.5*integral + 0.5*det` | 两型不全 → `null` |
| `mixed_failure` | 全部六题均值 | 本代无题 → `null` |
| `task_mix` | `{"integral": n, "det": m}` | — |
| `unanswered_count` | 祖先 `unanswered` 个数（算错，但单独统计） | — |
| `*_mean` | 四个指标的**三代均线** | 遇 `null` 断开 |

**画图时三条硬要求**：

1. `null` 当**断线**处理，不要画成 0——0 的意思是"三道题全答对了"，语义完全相反。
2. 均线不跨缺口连线，缺口之后重新起算，不吃缺口前的值。
3. 均线只帮助阅读，不证明显著性、单调性或进步方向，图上要注明。

`ancestors.json` 的格式见 `data/ancestors.example.json`；`load_ancestors()` 会校验恰 3 个、id 唯一、`genes` 三字段齐全。
