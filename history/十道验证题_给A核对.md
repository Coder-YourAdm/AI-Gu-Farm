# 十道验证题 · 给 A 核对

> 2026-11-13 · C 出
> 来源：`方案/技术规格_v5.md` 附录 A（A.1 积分 L610–L617 / A.2 行列式 L625–L631）。
> **题面与期望值逐字抄录，未改写。** 题库文件 `data/verify_bank.json`，现场不生成、不修改。

## 怎么用这张表

1. 你的 `judge(task, raw_answer) -> str` 要能吃下面每一条的 `task` 对象
2. 把「期望 reference」当正确答案喂进去，**必须返回 `right`**
3. 随便填个错答案（如 `0`）喂进去，**必须返回 `wrong`，不能抛异常**
4. 有任一条不通过，最后一列写原因——我改题库，**不改你的实现**

## 十条

| # | 附录A源 | 类型 / 模板 / 参数 | 题面（应逐字相等） | 期望 `reference` | 你的 judge 能判吗 |
|---|---|---|---|---|---|
| V01 | I1 | `integral` / `poly`<br>coeffs=[1, 1], lower=0, upper=1 | `计算定积分 ∫_{0}^{1} (x + 1) dx。` | `{"kind":"rational","numerator":"3","denominator":"2"}` | ☐ 通过　☐ 不通过 |
| V02 | I3 | `integral` / `poly`<br>coeffs=[0, 0, 5], lower=1, upper=2 | `计算定积分 ∫_{1}^{2} (5x²) dx。` | `{"kind":"rational","numerator":"35","denominator":"3"}` | ☐ 通过　☐ 不通过 |
| V03 | I4 | `integral` / `gamma`<br>n=1, rate=2, c=1 | `计算定积分 ∫₀^∞ x e^{-2x} dx。` | `{"kind":"rational","numerator":"1","denominator":"4"}` | ☐ 通过　☐ 不通过 |
| V04 | I5 | `integral` / `gamma`<br>n=9, rate=8, c=5 | `计算定积分 ∫₀^∞ 5x⁹ e^{-8x} dx。` | `{"kind":"rational","numerator":"14175","denominator":"8388608"}` | ☐ 通过　☐ 不通过 |
| V05 | I7 | `integral` / `gaussian_odd`<br>k=5, rate=1, c=-2 | `计算定积分 ∫₀^∞ -2x¹¹ e^{-1x²} dx。` | `{"kind":"integer","value":"-120"}` | ☐ 通过　☐ 不通过 |
| V06 | D1 | `determinant` / `det`<br>matrix = [[2,1],[1,3]] | `计算矩阵 [[2,1],[1,3]] 的行列式。` | `{"kind":"integer","value":"5"}` | ☐ 通过　☐ 不通过 |
| V07 | D2 | `determinant` / `det`<br>matrix = [[1,2,3],[4,5,6],[7,8,10]] | `计算矩阵 [[1,2,3],[4,5,6],[7,8,10]] 的行列式。` | `{"kind":"integer","value":"-3"}` | ☐ 通过　☐ 不通过 |
| V08 | D3 | `determinant` / `det`<br>matrix = [[1,2],[2,4]] | `计算矩阵 [[1,2],[2,4]] 的行列式。` | `{"kind":"integer","value":"0"}` | ☐ 通过　☐ 不通过 |
| V09 | D6 | `determinant` / `det`<br>matrix = [[0,1],[1,0]] | `计算矩阵 [[0,1],[1,0]] 的行列式。` | `{"kind":"integer","value":"-1"}` | ☐ 通过　☐ 不通过 |
| V10 | D7 | `determinant` / `det`<br>matrix = [[0,2,1],[3,0,1],[4,5,0]] | `计算矩阵 [[0,2,1],[3,0,1],[4,5,0]] 的行列式。` | `{"kind":"integer","value":"23"}` | ☐ 通过　☐ 不通过 |

## 三个必须你确认的点

| # | 问题 | 我现在的填法 | 你回一句 |
|---|---|---|---|
| 1 | `by` / `gen` 填 `null`，`validate_task()` 接受吗？ | `"by": null, "gen": null` | 接受 / 不接受，应填 ___ |
| 2 | `source` 的合法值有哪些？`"verify_bank"` 在白名单里吗？ | `"source": "verify_bank"` | 认 / 不认，应填 ___ |
| 3 | det 的 `reference` 用字符串存数字，和你的实现一致吗？ | `{"kind":"integer","value":"5"}` | 一致 / 不一致，应为 ___ |

## 自测（复制即跑）

```bash
python -c "
import json,sys; sys.path.insert(0,'.')
from app.executor import judge
bank=json.load(open('data/verify_bank.json',encoding='utf-8'))['tasks']
bad=0
for r in bank:
    t=r['task']; ref=t['reference']
    good=(ref['numerator']+'/'+ref['denominator']) if ref['kind']=='rational' else ref['value']
    v1=judge(t, good); v2=judge(t, '0')
    ok=(v1=='right' and v2=='wrong'); bad += 0 if ok else 1
    print(t['id'], '正解->'+v1, '错解->'+v2, 'OK' if ok else 'FAIL')
print('失败条数', bad)
"
```

**通过标准：`失败条数 0`。**

## 判分口径三条提醒（都来自规格，不是我的偏好）

1. **`unanswered` 不是 `wrong`。** 规格 §2.4 / S2 / S3：`sqrt(4)`、`1; import os`、`1/0`、`NaN`、`Infinity`、`1e31`、65 位整数，全部判 `unanswered`。混成一个 bool，「拒答」会看起来像「算错」，答辩时说不清
2. **不许浮点。** 全程 `Fraction`，`14175/8388608` 这种分值过 `float` 就丢精度
3. **`reference` 只来自执行器。** 题库里的期望值全部手算/独立推导，禁止跑 `solve()` 回填

## 与你的 `validate_task` 的接口约定（2026-11-13 21:20 修正）

读了 `A_to_B_接口契约(1).md` 之后，本表有**三处已改正**——原来那份发给你的版本对不上你的实现：

| 项 | 原来（错） | 现在（对齐你的契约） |
|---|---|---|
| `poly` 参数名 | `l` / `u` | `lower` / `upper` |
| 行列式模板名 | `matrix` | `det` |
| 签名格式 | `poly:{json}` | `poly\|coeffs=1,1\|lower=0\|upper=1` |

前两处会让你 `validate_task` 直接拒掉这 10 条（键集合不匹配 / 模板不在白名单），第三处会让两套去重算出不同 key。**如果你之前拿旧版跑过，请用这一版重跑。**

### 一处例外：V07 过不了你自己的 `validate_task`

V07 抄自附录 A 的 D2 `[[1,2,3],[4,5,6],[7,8,10]]`，**元素 10 超出你定的 `[-9,9]`**。
这不是笔误，两个事实同时成立：

- 作为**纯函数用例**它是对的——测 `bareiss` 的「3 阶含负结果」，答案是 `-3`，不受取值域约束
- 作为**出题流程输入**它不合法——`validate_task` 应当拒它

所以：**测 `judge` / `bareiss` 请照用 V07；测出题全流程请跳过它。**
你的 `test_executor.py` 里 D2 能过 36 条，说明那里绕过了 `validate_task`——没错，但别在`test_contracts.py` 里也当它合法。

另外 **125 道正式题集（`data/*.json`）已全部通过你的边界表预检**，`det` 元素都在 `[-9,9]` 内，可以直接交给 `normalize_task` 回填。
