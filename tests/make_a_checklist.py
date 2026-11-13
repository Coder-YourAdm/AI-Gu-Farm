"""从 data/verify_bank.json 生成给 A 的核对清单（十道验证题_给A核对.md）。

**为什么要脚本生成而不是手写**：手写会抄错题面或期望值，而这张表的作用
正是让 A 逐条核对——表本身错了，核对就变成两个人一起错。

用法： python tests/make_a_checklist.py
产出： 十道验证题_给A核对.md
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BANK = ROOT / "data" / "verify_bank.json"
OUT = ROOT / "十道验证题_给A核对.md"


def ref_str(r: dict) -> str:
    if r.get("kind") == "rational":
        return (f'`{{"kind":"rational","numerator":"{r["numerator"]}",'
                f'"denominator":"{r["denominator"]}"}}`')
    return f'`{{"kind":"integer","value":"{r["value"]}"}}`'


def params_str(t: dict) -> str:
    """按 A 的 params 键名渲染（lower/upper，不是 l/u；det 不是 matrix）。

    2026-11-13 21:20 修正：原实现写死 `p["l"]` / `p["u"]`，而 A 的 validate_task
    要求键集合恰为 {coeffs, lower, upper}，照原样发给 A 会让 10 道题全被拒。
    """
    p = t["params"]
    tpl = t["template"]
    if tpl == "det":
        return "matrix = " + json.dumps(p["matrix"], separators=(",", ":"))
    if tpl == "poly":
        return (f'coeffs=[{", ".join(str(c) for c in p["coeffs"])}], '
                f'lower={p["lower"]}, upper={p["upper"]}')
    if tpl == "gamma":
        return f'n={p["n"]}, rate={p["rate"]}, c={p["c"]}'
    return f'k={p["k"]}, rate={p["rate"]}, c={p["c"]}'


def main() -> int:
    rows = json.loads(BANK.read_text(encoding="utf-8"))["tasks"]
    L: list[str] = []
    A = L.append

    A("# 十道验证题 · 给 A 核对")
    A("")
    A("> 2026-11-13 · C 出")
    A("> 来源：`方案/技术规格_v5.md` 附录 A（A.1 积分 L610–L617 / A.2 行列式 L625–L631）。")
    A("> **题面与期望值逐字抄录，未改写。** 题库文件 `data/verify_bank.json`，现场不生成、不修改。")
    A("")
    A("## 怎么用这张表")
    A("")
    A("1. 你的 `judge(task, raw_answer) -> str` 要能吃下面每一条的 `task` 对象")
    A("2. 把「期望 reference」当正确答案喂进去，**必须返回 `right`**")
    A("3. 随便填个错答案（如 `0`）喂进去，**必须返回 `wrong`，不能抛异常**")
    A("4. 有任一条不通过，最后一列写原因——我改题库，**不改你的实现**")
    A("")
    A("## 十条")
    A("")
    A("| # | 附录A源 | 类型 / 模板 / 参数 | 题面（应逐字相等） | 期望 `reference` | 你的 judge 能判吗 |")
    A("|---|---|---|---|---|---|")
    for row in rows:
        t = row["task"]
        A(f'| {t["id"]} | {row["src"]} | `{t["type"]}` / `{t["template"]}`<br>{params_str(t)} '
          f'| `{t["statement"]}` | {ref_str(t["reference"])} | ☐ 通过　☐ 不通过 |')
    A("")
    A("## 三个必须你确认的点")
    A("")
    A("| # | 问题 | 我现在的填法 | 你回一句 |")
    A("|---|---|---|---|")
    A('| 1 | `by` / `gen` 填 `null`，`validate_task()` 接受吗？ | `"by": null, "gen": null` '
      '| 接受 / 不接受，应填 ___ |')
    A('| 2 | `source` 的合法值有哪些？`"verify_bank"` 在白名单里吗？ | `"source": "verify_bank"` '
      '| 认 / 不认，应填 ___ |')
    A('| 3 | det 的 `reference` 用字符串存数字，和你的实现一致吗？ | `{"kind":"integer","value":"5"}` '
      '| 一致 / 不一致，应为 ___ |')
    A("")
    A("## 自测（复制即跑）")
    A("")
    A("```bash")
    A("python -c \"")
    A("import json,sys; sys.path.insert(0,'.')")
    A("from app.executor import judge")
    A("bank=json.load(open('data/verify_bank.json',encoding='utf-8'))['tasks']")
    A("bad=0")
    A("for r in bank:")
    A("    t=r['task']; ref=t['reference']")
    A("    good=(ref['numerator']+'/'+ref['denominator']) if ref['kind']=='rational' else ref['value']")
    A("    v1=judge(t, good); v2=judge(t, '0')")
    A("    ok=(v1=='right' and v2=='wrong'); bad += 0 if ok else 1")
    A("    print(t['id'], '正解->'+v1, '错解->'+v2, 'OK' if ok else 'FAIL')")
    A("print('失败条数', bad)")
    A('"')
    A("```")
    A("")
    A("**通过标准：`失败条数 0`。**")
    A("")
    A("## 判分口径三条提醒（都来自规格，不是我的偏好）")
    A("")
    A("1. **`unanswered` 不是 `wrong`。** 规格 §2.4 / S2 / S3：`sqrt(4)`、`1; import os`、`1/0`、"
      "`NaN`、`Infinity`、`1e31`、65 位整数，全部判 `unanswered`。混成一个 bool，"
      "「拒答」会看起来像「算错」，答辩时说不清")
    A("2. **不许浮点。** 全程 `Fraction`，`14175/8388608` 这种分值过 `float` 就丢精度")
    A("3. **`reference` 只来自执行器。** 题库里的期望值全部手算/独立推导，禁止跑 `solve()` 回填")
    A("")
    A("## 与你的 `validate_task` 的接口约定（2026-11-13 21:20 修正）")
    A("")
    A("读了 `A_to_B_接口契约(1).md` 之后，本表有**三处已改正**——原来那份发给你的版本对不上你的实现：")
    A("")
    A("| 项 | 原来（错） | 现在（对齐你的契约） |")
    A("|---|---|---|")
    A("| `poly` 参数名 | `l` / `u` | `lower` / `upper` |")
    A("| 行列式模板名 | `matrix` | `det` |")
    A("| 签名格式 | `poly:{json}` | `poly\\|coeffs=1,1\\|lower=0\\|upper=1` |")
    A("")
    A("前两处会让你 `validate_task` 直接拒掉这 10 条（键集合不匹配 / 模板不在白名单），"
      "第三处会让两套去重算出不同 key。**如果你之前拿旧版跑过，请用这一版重跑。**")
    A("")
    A("### 一处例外：V07 过不了你自己的 `validate_task`")
    A("")
    A("V07 抄自附录 A 的 D2 `[[1,2,3],[4,5,6],[7,8,10]]`，**元素 10 超出你定的 `[-9,9]`**。")
    A("这不是笔误，两个事实同时成立：")
    A("")
    A("- 作为**纯函数用例**它是对的——测 `bareiss` 的「3 阶含负结果」，答案是 `-3`，不受取值域约束")
    A("- 作为**出题流程输入**它不合法——`validate_task` 应当拒它")
    A("")
    A("所以：**测 `judge` / `bareiss` 请照用 V07；测出题全流程请跳过它。**")
    A("你的 `test_executor.py` 里 D2 能过 36 条，说明那里绕过了 `validate_task`——没错，但别在"
      "`test_contracts.py` 里也当它合法。")
    A("")
    A("另外 **125 道正式题集（`data/*.json`）已全部通过你的边界表预检**，"
      "`det` 元素都在 `[-9,9]` 内，可以直接交给 `normalize_task` 回填。")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"✓ 已写 {OUT.name}（{len(rows)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
