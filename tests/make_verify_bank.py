"""生成双跑演示的冻结验证题库 data/verify_bank.json。

**题面与期望值全部来自 `方案/技术规格_v5.md` 附录 A（L610–L631），不另编。**
挑 10 条：I1/I3/I4/I5/I7（积分族）+ D1/D2/D3/D6/D7（行列式族）。

结构（**关键**）
---------------
每一条都是 `{"family", "mock_wrong_reason", "task"}`，
其中 `task` 是**规格 §2.1 定义的 Task 结构**，键集合恰为：
    id / source / by / gen / type / template / params / signature / statement / reference

`family` 和 `mock_wrong_reason` 放在**外层**，不进 Task —— 因为 A 的 `validate_task()`
很可能拒绝未知键，塞进去会在联调时炸。判分只吃 `task`，学习只用 `family`。

`reference` 的格式也照规格：积分是 `{"kind":"rational","numerator":..,"denominator":..}`，
行列式是 `{"kind":"integer","value":".."}`，**不写成 "3/2" 这种字符串**。

用法： python tests/make_verify_bank.py
产出： data/verify_bank.json
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "verify_bank.json"

A_FORMAT = "integer, fraction or finite decimal"

# id, 源编号, 族, type, template, params, 题面（逐字抄附录 A）, answer_format, reference, 常见错法
BANK = [
    ("V01", "I1", "integral", "integral", "poly",
     {"coeffs": [1, 1], "lower": 0, "upper": 1},
     "计算定积分 ∫_{0}^{1} (x + 1) dx。", A_FORMAT,
     {"kind": "rational", "numerator": "3", "denominator": "2"},
     "分子漏乘或漏除以 2，得 5/4"),

    ("V02", "I3", "integral", "integral", "poly",
     {"coeffs": [0, 0, 5], "lower": 1, "upper": 2},
     "计算定积分 ∫_{1}^{2} (5x²) dx。", A_FORMAT,
     {"kind": "rational", "numerator": "35", "denominator": "3"},
     "区间代入时用 0 而非 1，得 40/3"),

    ("V03", "I4", "integral", "integral", "gamma",
     {"n": 1, "rate": 2, "c": 1},
     "计算定积分 ∫₀^∞ x e^{-2x} dx。", A_FORMAT,
     {"kind": "rational", "numerator": "1", "denominator": "4"},
     "把 n!/r^n 写成 n!/r^(n+1)，得 1/2"),

    ("V04", "I5", "integral", "integral", "gamma",
     {"n": 9, "rate": 8, "c": 5},
     "计算定积分 ∫₀^∞ 5x⁹ e^{-8x} dx。", A_FORMAT,
     {"kind": "rational", "numerator": "14175", "denominator": "8388608"},
     "阶乘与幂次错位，分母少乘一个 8"),

    ("V05", "I7", "integral", "integral", "gaussian_odd",
     {"k": 5, "rate": 1, "c": -2},
     "计算定积分 ∫₀^∞ -2x¹¹ e^{-1x²} dx。", A_FORMAT,
     {"kind": "integer", "value": "-120"},
     "丢掉负系数，得 120"),

    ("V06", "D1", "det", "determinant", "det",
     {"matrix": [[2, 1], [1, 3]]},
     "计算矩阵 [[2,1],[1,3]] 的行列式。", "integer",
     {"kind": "integer", "value": "5"},
     "交换了主次对角项顺序，得 -5"),

    ("V07", "D2", "det", "determinant", "det",
     {"matrix": [[1, 2, 3], [4, 5, 6], [7, 8, 10]]},
     "计算矩阵 [[1,2,3],[4,5,6],[7,8,10]] 的行列式。", "integer",
     {"kind": "integer", "value": "-3"},
     "展开式符号错，得 3"),

    ("V08", "D3", "det", "determinant", "det",
     {"matrix": [[1, 2], [2, 4]]},
     "计算矩阵 [[1,2],[2,4]] 的行列式。", "integer",
     {"kind": "integer", "value": "0"},
     "主元为 0 时未判定奇异，返回 None 或报错"),

    ("V09", "D6", "det", "determinant", "det",
     {"matrix": [[0, 1], [1, 0]]},
     "计算矩阵 [[0,1],[1,0]] 的行列式。", "integer",
     {"kind": "integer", "value": "-1"},
     "首元素为 0 未换行，符号未翻转，得 1"),

    ("V10", "D7", "det", "determinant", "det",
     {"matrix": [[0, 2, 1], [3, 0, 1], [4, 5, 0]]},
     "计算矩阵 [[0,2,1],[3,0,1],[4,5,0]] 的行列式。", "integer",
     {"kind": "integer", "value": "23"},
     "多轮换行后符号累计错，得 -23"),
]


def signature_of(tpl: str, params: dict) -> str:
    """按 A **已冻结**的格式生成签名（`A_to_B_接口契约.md` §7）。

        gamma|c=1|n=4|rate=2
        poly|coeffs=1,1|lower=0|upper=1
        gaussian_odd|c=-2|k=2|rate=3
        det|matrix=2,1;1,3

    这里必须与 A 的实现逐字一致。签名同时用于四套数据集的互斥校验——
    格式不同，同一道题在两套去重规则下会算出两个 key，互斥校验等于没做。

    2026-11-13 21:20 修正：原实现用 `tpl + ":" + json.dumps(params)`，
    与 A 的 `tpl|k=v|k=v` 格式不通，且 poly 缺少上下限的位置约定。
    """
    if tpl == "poly":
        return "poly|coeffs=%s|lower=%s|upper=%s" % (
            ",".join(str(v) for v in params["coeffs"]),
            params["lower"], params["upper"],
        )
    if tpl == "det":
        return "det|matrix=" + ";".join(
            ",".join(str(v) for v in row) for row in params["matrix"]
        )
    # gamma / gaussian_odd：参数名按字母序，保证同参数不同键序得到同一签名
    return tpl + "|" + "|".join("%s=%s" % (k, params[k]) for k in sorted(params))


def main() -> int:
    rows = []
    for tid, src, fam, typ, tpl, params, statement, fmt, ref, wrong in BANK:
        rows.append({
            "src": src,
            "family": fam,
            "mock_wrong_reason": wrong,
            "answer_format": fmt,
            "task": {
                "id": tid,
                "source": "verify_bank",
                "by": None,
                "gen": None,
                "type": typ,
                "template": tpl,
                "params": params,
                "signature": signature_of(tpl, params),
                "statement": statement,
                "reference": ref,
            },
        })

    sigs = [r["task"]["signature"] for r in rows]
    assert len(set(sigs)) == len(sigs), "题库签名重复，现场会触发去重"
    assert len(rows) == 10, f"应为 10 题，实际 {len(rows)}"

    # Task 键集合必须与规格 §2.1 一致，多一个键都有被 validate_task 拒绝的风险
    SPEC_KEYS = {"id", "source", "by", "gen", "type", "template",
                 "params", "signature", "statement", "reference"}
    for r in rows:
        extra = set(r["task"]) - SPEC_KEYS
        assert not extra, f"{r['task']['id']} 多了键 {extra}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "frozen": True,
        "source": "方案/技术规格_v5.md 附录 A（A.1 积分 / A.2 行列式）",
        "note": "task 是规格 §2.1 的 Task（可直接喂 judge）；answer_format 是 SolverTask 字段，由 solver_view 产出；family 仅供规则学习，三者都不混",
        "n_tasks": len(rows),
        "tasks": rows,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    fam = {}
    for r in rows:
        fam[r["family"]] = fam.get(r["family"], 0) + 1
    print(f"✓ 已写 {OUT.relative_to(ROOT)}（{len(rows)} 题：{fam}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
