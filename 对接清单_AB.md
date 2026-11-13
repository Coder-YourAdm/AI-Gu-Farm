# 对接清单（给 A / B）· v1

> 2026-11-13 19:55 · C 出
> 一句话：**双跑演示已经能跑，但吃的是假数据。这两个函数落地，三行进化说明里的数字才能换成真的。**

---

## 一 · 给 B：服务地址这件事，结论是「没有」

**没有本地服务地址可给，C0 已砍。** 不是我拖着，是实测跑不动：

| 实测项 | 数据 | 结论 |
|---|---|---|
| ModelScope 真实权重分片 | 78.5 KB/s | 4.47 GB 要 **15.8 小时** |
| GitHub Release 安装包 | 43 KB/s | 700 MB 要 **4.5 小时** |
| 清华镜像 / hf-mirror / ollama.com | 69 KB/s / 不可达 / 无响应 | 全部卡在 70 KB/s 上下 |
| **19:50 复测** | 百度、DNS、ping 全部超时 | **本机此刻直接断网** |

合计约 20 小时，而且**这个用途已经被你自己那条规则清零了**：

> pilot 与正式运行必须同一个 `model` id；本地模型产出的数字不进 pilot 结论、不用于锁协议。

这条规则我完全同意，也写进了演示手册。它同时意味着本地 7B 连「演示数据」都当不了——**那就不该为它付 20 小时**。

### 所以 B 要做的是三件事

1. **选云端 provider**，填进 `config.json` 的 `model` 和 `base_url`（现在是 `REPLACE_WITH_*` 占位）
2. **key 放环境变量** `LLM_API_KEY`（`config.json` 的 `api_key_env` 就是这个名）
3. **交 `app/llm.py` 的 `complete()`**

### B 需要提供的接口（规格 §1 的 `llm.py`）

```python
# app/llm.py
def complete(messages: list[dict]) -> tuple[str, int, int]:
    """返回 (text, tokens_in, tokens_out)。

    - 并发由调用方控制：C 侧固定 6 并发，B 不用在内部再开线程池
    - 重试/限流按 config.json 的 network_retries / timeout_s 做
    - 不要吞异常：失败就让异常抛出来，C 侧会记成该题失败，不会整趟崩
    """
```

**也接受对象式返回**（带 `.text` / `.tokens_in` / `.tokens_out`），两种我都认。

### 最小桩（先让链路通，30 秒）

```python
# app/llm.py  —— 临时桩，填完真实现就删掉
def complete(messages):
    return ("3/2", 100, 10)
```

通不通用这条验证：

```bash
python -c "
import sys; sys.path.insert(0,'.')
from app.llm import complete
print(complete([{'role':'user','content':'1+1=?'}]))
"
```

---

## 二 · 给 A：judge 的签名，我这边已经改成规格版

**先认个错**：我之前按 `judge(task, answer) -> (bool, str)` 写的，和规格 §2.3 的 `-> str` 不一致。

```
规格原文 L169：def judge(task: dict, raw_answer: str | None) -> str: ...  # right / wrong / unanswered
```

**已经改完了**，现在 C 侧是：

```python
verdict = judge(task, text)          # "right" / "wrong" / "unanswered"
ok = (verdict == "right")
```

返回 tuple 我也兼容（会打一次警告），但**请按规格返回字符串**——`unanswered` 和 `wrong` 是两件事，混成一个 bool 会把「拒答」算成「答错」，答辩时说不清。

### A 只需要保证三件事

| # | 要求 | 我的用法 |
|---|---|---|
| 1 | `judge(task, raw_answer) -> str`，三元判定 | 判 pass1 / pass2 每题对错 |
| 2 | `task` 接受规格 §2.1 的 Task 结构 | 我的题库**逐字对齐**这个结构 |
| 3 | （可选）`solver_view(task) -> {"type","statement","answer_format"}` | 有就用你的，没有我按规格自己构造白名单 |

Task 的键，我用的就是这十个，**一个不多一个不少**：

```
id / source / by / gen / type / template / params / signature / statement / reference
```

### 三个必须你确认的点（最容易在 12H 炸的就在这）

1. **`by` 和 `gen` 我填的是 `null`**（题库是预生成的，没有出题者、没有代次）—— 你的 `validate_task()` 接受吗？
2. **`source` 我填 `"verify_bank"`** —— 你的 source 白名单认吗？不认的话该填什么，回我一句我就改。
3. **det 的 `reference` 是 `{"kind":"integer","value":"5"}`（value 是字符串）** —— 和你的实现一致吗？

### 十道题在这里

完整题面、参数、期望值、你逐条打勾的表：**`十道验证题_给A核对.md`**

自测命令（把前两条喂给你的 judge，看是不是返回 `right`）：

```bash
python -c "
import json,sys; sys.path.insert(0,'.')
from app.executor import judge
bank=json.load(open('data/verify_bank.json',encoding='utf-8'))['tasks']
for r in bank[:2]:
    t=r['task']
    print(t['id'], '正确答案 ->', judge(t, '3/2'), '| 错误答案 ->', judge(t, '0'))
"
```

期望输出形如 `V01 正确答案 -> right | 错误答案 -> wrong`。

---

## 二点五 · 给 A：结果页要的 `result.json`，字段就这些

结果页 `web/result.html` 已经做完，现在吃的是 `tests/make_fake_result.py` 造的假数据。
**换成真数据只差一件事：你的留出评测跑完后，按下表吐一个 `web/result.json`。**

契约文件是 `app/result_schema.py`，校验命令：

```bash
python -c "
import json,sys; sys.path.insert(0,'.')
from app.result_schema import validate, check_conclusion_fit
d=json.load(open('web/result.json',encoding='utf-8'))
print('契约:', validate(d) or 'OK')
print('结论与数据同向:', check_conclusion_fit(d) or 'OK')
"
```

### 每趟运行（`runs[]`）必须有的

| 键 | 类型 | 你要填什么 |
|---|---|---|
| `run_id` | str | 与 `runs/<run_id>/` 目录同名 |
| `tag` | str | 页面主标题，如 `第 1 趟` |
| `condition` | str | 实际生效的条件，**原样填，页面不解释语义** |
| `condition_note` | str | 一句话说明，可为空串 |
| `generations` / `calls` / `duration_min` / `started_at` | int/str | 直方记录里的真实值 |
| `lineages[]` | list | **必须一条 `kind="champion"`，其余 `kind="ancestor"`** |

每条 `lineages[]`：

```json
{
  "key": "champion",            // 跨趟唯一且同名，页面按 key 对齐列
  "label": "末代冠军",           // 页面列头
  "kind": "champion",           // champion | ancestor
  "color": "s3",                // s1 / s2 / s3，CSS 变量名后缀
  "final_rank": 1,              // 第 20 代 6 个体里的名次
  "n_individuals": 6,           // 该代参与排名的个体数
  "holdout": {"correct": 15, "total": 20}
}
```

**四条硬约束**（`validate()` 会拒）：

1. `is_mock` 必须显式写。真数据填 `false`，页面红标自动变绿标。
2. `holdout.total` 必须等于 `metric.n_tasks`，且 `0 ≤ correct ≤ total`。
3. **同一批 run 里，同 `key` 的祖先 `label`/`color` 必须逐字一致**——换个名字跨趟对照就废了（规格 §6.2 冻结组复用同一套祖先）。
4. `conclusion.evidence[].run_id` 必须真实存在于 `runs`。

### `conclusion` 怎么写

```json
{
  "level": "none",                    // positive | none | negative
  "text": "一句话结论，页面直接渲染，不加工",
  "evidence": [{"run_id": "r1113E01", "text": "冠军 15/20 对最好祖先 13/20"}],
  "caveat": "留出集 20 题，分辨率 5 个百分点；本结论不外推"
}
```

`check_conclusion_fit()` 会做一件不客气的事：**核对结论方向和数据方向**。
三趟都升却写 `none`、三趟都掉却写 `positive`，都会被拒。
留出集只有 20 题、单题就是 5pp，所以「一趟升一趟降 → 写 none」是唯一诚实的写法。

### 一条不许破的线

`result.json` 会进 `web/` 静态目录。**只准放计数，不准放题面与期望值。**
`tests/test_result.py::test_result_json_carries_no_reference_values` 会扫
`reference` / `statement` / `signature` / `template` / `params` / `raw_response`，
命中任一即失败。这条不是洁癖——`holdout` 一旦连同答案进静态目录，20 道题的隔离就没了。

---

## 二点六 · 给 A：你的 executor 已接上，125 题回填完成（2026-11-13 21:30）

我从 `Coder-YourAdm/AI-Gu-Farm` 取到 `app/executor.py`（sha256 `951bb07c…`），
原样拷进项目、**一行未改**，然后跑 `tests/backfill_datasets.py` 把 `data/` 四份题集
的 `signature` / `statement` / `reference` 全部填上了。

### 结果

| 项 | 结果 |
|---|---|
| `validate_task` | 125/125 通过 |
| 签名唯一 | 125 条，无重复（用你的 `signature()` 复算，不是我的近似算法） |
| `reference` 复现 | 125/125 与 `solve_exact()` 逐字段相等 |
| `judge()` 自检 | 参考答案→`right`、值+1 扰动→`wrong`、拒答/格式错→`unanswered`，全部 125 题符合 |
| 十进制容差 | 36 题走 `parse_decimal` 路径，全判 `right` |
| 幂等 | 重跑 `make_datasets.py` 产出逐字节相同文件 |

### 我额外做的两件事（避免你返工）

1. **`make_datasets.py` 生成后自动回填**。否则谁重跑一次生成脚本，
   就会把 125 题的 `reference` 重新写成 null，而下游要到运行时才发现题判不了。
2. **`--backend mock` 也走你的 `judge()` 了**。之前 mock 的 `correct` 是
   sha256 抽签算的，答案字段却写着真参考答案——那是自欺。现在抽签只决定
   「它打算写对还是写错」，判对判错一律交给 `judge()`。跑下来
   `4/10 → 8/10`（+40.0pp）不变，说明判分与抽签没有冲突。

### 需要你回一句的只有一件

`方案/技术规格_v5.md` L626 的 D2 用例 `[[1,2,3],[4,5,6],[7,8,10]]` 含元素 `10`，
超出 §2.1 定的 `[-9,9]`。你的 `test_D2` 走 `_det()` 直连 `solve_exact()`，
绕过了 `validate_task()`，所以 36 条测试全绿也暴露不了。

**这不是代码错，是规格前后矛盾。** 详细复现与三个处理方案见
`给A_边界冲突说明.md`（我建不了 Issue，GitHub 授权是只读的，
麻烦你自己开一个，或者就在群里回一句「留作算术单测」也行）。

---

## 三 · 时间点

| 谁 | 交什么 | 什么时候 | 卡住谁 |
|---|---|---|---|
| **B** | `complete()` + `config.json` 的 `model`/`base_url` | 今晚 | C 跑不了 `--backend real` |
| **A** | `judge()` 能吃这 10 条 | 今晚，最晚 06H | 同上 |
| **A** | 留出评测吐 `web/result.json`（字段见二点五） | 评测跑完即刻 | 结果页只能显示 mock |

**这两个不是锦上添花，是提交材料里那三行说明的分子。** 缺一天，三行说明就只能写 mock 的假数字——那等于没交。

---

## 四 · 现在断网，所以顺序这样排

1. **A、B 先各自写代码 + 单测**（离线可做，不受网络影响）
2. 网络恢复后，B 先跑通一次 `complete()` 真调用，报一个真实的延迟数字给我
3. 我拿这个数字决定并发数（现在设 6，如果单次 >15 秒就提到 10）
4. 然后我跑 `python -m app.demo pair --backend real`，三行说明的数字就位

**第 2 步的延迟数字很关键**：它决定现场那 90 秒能不能压住。现在这个数还没有，我在手册里写的是「预估 20–35 秒」，不是实测。
