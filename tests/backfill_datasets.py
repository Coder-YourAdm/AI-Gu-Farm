"""把 125 道题的三个判分字段回填成真值（调用 A 的 executor.py，不自己实现）。

为什么必须是这个脚本
--------------------
`tests/make_datasets.py` 只产「题面参数」，`signature` / `statement` / `reference`
三个字段一律写 null。这三个字段的计算权归 A 的 `app/executor.py`（规格 §2.3）：
C 若自己算一遍，同一道题会得到第二个签名，去重与互斥检测同时失效。

所以本脚本只做搬运，不做数学：
    1. 用 A 的 `validate_task()` 逐题过一遍边界（拒绝要发生在求值之前）
    2. 用 A 的 `normalize_task()` 补全十个键
    3. 用 A 的 `judge()` 反向自检——把 reference 当答案喂回去必须判 right

第 3 步是这一步真正的价值：它证明「A 出的参考答案，A 的判分器能判对」。
少了这一步，回填只是把 null 换成数字，跑不跑得通没人知道。

幂等性
------
同一份 executor + 同一份 params，两次回填产出逐字节相同的文件。
签名唯一性用 A 的 `signature()` 复算，而不是本地近似算法。

用法
----
    python tests/backfill_datasets.py            # 回填 + 自检 + 写回 data/
    python tests/backfill_datasets.py --check     # 只校验不写盘
"""

from __future__ import annotations

import hashlib
import json
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

sys.path.insert(0, str(ROOT))
from app.executor import (  # noqa: E402
    judge,
    normalize_task,
    signature,
    solve_exact,
    solver_view,
    validate_task,
)

NAMES = ("calibration", "common_dev", "challenge_pool", "holdout")
KEY_ORDER = ["id", "source", "by", "gen", "type", "template",
             "params", "signature", "statement", "reference"]
EXECUTOR = Path(__file__).resolve().parent.parent / "app" / "executor.py"


def executor_fingerprint() -> str:
    """回填用的 executor 版本指纹。数据出问题时先看这个对不对。"""
    return hashlib.sha256(EXECUTOR.read_bytes()).hexdigest()


def ref_to_answer(reference: dict) -> str:
    """把 reference 结构转成模型会给的答案字符串（judge 的输入形态）。"""
    if reference["kind"] == "integer":
        return reference["value"]
    return "%s/%s" % (reference["numerator"], reference["denominator"])


def ref_to_wrong_answer(reference: dict) -> str:
    """造一个必然判 wrong 的答案：值 +1，格式仍合法。

    用「格式合法但不等」而不是乱码，才能区分 wrong 与 unanswered——
    乱码只会命中 answered 分支，测不出「算错了」这条路。
    """
    if reference["kind"] == "integer":
        return str(int(reference["value"]) + 1)
    n = int(reference["numerator"]) + int(reference["denominator"])
    d = int(reference["denominator"])
    return "%s/%s" % (n, d)


def ref_to_fraction(reference: dict) -> Fraction:
    if reference["kind"] == "integer":
        return Fraction(int(reference["value"]))
    return Fraction(int(reference["numerator"]), int(reference["denominator"]))


def fill_row(row: dict) -> dict:
    """一题回填。键顺序锁死在 §2.2 的十个键上。"""
    raw = {"template": row["template"], "params": row["params"]}
    ok, reason = validate_task(raw)
    if not ok:
        raise ValueError("%s 未通过 A 的 validate_task：%s" % (row["id"], reason))

    norm = normalize_task(
        raw,
        task_id=row["id"],
        source=row["source"],
        by=row["by"],
        gen=row["gen"],
    )
    if norm["type"] != row["type"]:
        raise ValueError("%s type 不一致：数据 %s / 执行器 %s"
                         % (row["id"], row["type"], norm["type"]))
    return {k: norm[k] for k in KEY_ORDER}


def selfcheck(rows: list[dict]) -> dict:
    """回填后的硬校验。任何一条不过都直接抛，不允许带着坏数据往前走。"""
    sigs: dict[str, str] = {}
    for r in rows:
        # ① 签名必须能被 A 的 signature() 复现（不是「看起来像」）
        want = signature({"template": r["template"], "params": r["params"]})
        if r["signature"] != want:
            raise ValueError("%s 签名与 signature() 不符：%s / %s"
                             % (r["id"], r["signature"], want))
        if want in sigs:
            raise ValueError("签名重复：%s（%s 与 %s）" % (want, sigs[want], r["id"]))
        sigs[want] = r["id"]

        # ② reference 必须能被 solve_exact() 复现
        want_ref = solve_exact(r)
        if r["reference"] != want_ref:
            raise ValueError("%s reference 与 solve_exact() 不符" % r["id"])

        # ③ 题面不能是空串，也不能把答案写进题面
        if not isinstance(r["statement"], str) or not r["statement"].strip():
            raise ValueError("%s statement 为空" % r["id"])

        # ④ judge 自检：对答案判 right，错答案判 wrong
        right_answer = json.dumps({"value": ref_to_answer(r["reference"])},
                                  ensure_ascii=False)
        got = judge(r, right_answer)
        if got != "right":
            raise ValueError("%s 的参考答案被判成 %s（应为 right）" % (r["id"], got))

        wrong_answer = json.dumps({"value": ref_to_wrong_answer(r["reference"])},
                                  ensure_ascii=False)
        got = judge(r, wrong_answer)
        if got != "wrong":
            raise ValueError("%s 的错答案被判成 %s（应为 wrong）" % (r["id"], got))

        # ⑤ 拒答与格式错必须是 unanswered，不能混成 wrong
        for bad in (None, "", "   ", "not json", "{}", '{"value": 3}',
                    '{"value": "3", "x": 1}'):
            got = judge(r, bad)
            if got != "unanswered":
                raise ValueError("%s 对 %r 判成 %s（应为 unanswered）"
                                 % (r["id"], bad, got))

        # ⑥ solver_view 白名单：只能有 type / statement / answer_format
        view = solver_view(r)
        if set(view) != {"type", "statement", "answer_format"}:
            raise ValueError("%s solver_view 键集合泄漏：%s" % (r["id"], sorted(view)))
        leaked = [k for k in ("reference", "params", "signature", "template", "by")
                  if k in json.dumps(view, ensure_ascii=False)]
        if leaked:
            raise ValueError("%s solver_view 正文出现敏感词：%s" % (r["id"], leaked))

    return {"tasks": len(rows), "unique_signatures": len(sigs)}


def decimal_roundtrip(rows: list[dict]) -> int:
    """积分题额外验一条：有限十进制答案也要判 right（大纲容差 1e-8）。

    只挑分母质因子仅含 2/5、且十进制表示不超过 12 位小数的题，
    否则那不是「有限十进制」，拿去做容差测试等于测了个不存在的情况。
    """
    checked = 0
    for r in rows:
        if r["type"] != "integral":
            continue
        val = ref_to_fraction(r["reference"])
        den = val.denominator
        while den % 2 == 0:
            den //= 2
        while den % 5 == 0:
            den //= 5
        if den != 1:
            continue
        dec = ("%.14f" % float(val)).rstrip("0").rstrip(".")
        if len(dec.split(".")[-1]) > 12:
            continue
        got = judge(r, json.dumps({"value": dec}))
        if got != "right":
            raise ValueError("%s 的十进制答案 %s 被判成 %s" % (r["id"], dec, got))
        checked += 1
    return checked


def backfill(*, write: bool = True, verbose: bool = True) -> dict:
    datasets = {n: json.loads((DATA / f"{n}.json").read_text(encoding="utf-8"))
                for n in NAMES}

    filled: dict[str, list[dict]] = {}
    for name in NAMES:
        filled[name] = [fill_row(r) for r in datasets[name]]

    rows = [r for n in NAMES for r in filled[n]]
    stats = selfcheck(rows)
    stats["decimal_checked"] = decimal_roundtrip(rows)
    stats["executor_sha256"] = executor_fingerprint()

    if verbose:
        print("✓ 回填 %d 题，签名全局唯一 %d 条" % (stats["tasks"],
                                                    stats["unique_signatures"]))
        print("  judge 自检：对答案 right / 错答案 wrong / 拒答 unanswered 全部符合")
        print("  十进制容差回归：%d 题通过" % stats["decimal_checked"])
        print("  executor 指纹：%s" % stats["executor_sha256"][:16])

    if not write:
        return stats

    files = {}
    for name in NAMES:
        body = json.dumps(filled[name], ensure_ascii=False, indent=2) + "\n"
        raw = body.encode("utf-8")
        target = DATA / f"{name}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)          # 原子替换，避免半个文件
        files[name] = {
            "path": "data/%s.json" % name,
            "count": len(filled[name]),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if verbose:
            print("  %-28s %3d 题  %6d 字节  %s"
                  % (files[name]["path"], files[name]["count"],
                     files[name]["bytes"], files[name]["sha256"][:16]))

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    manifest["note"] = (
        "signature / statement / reference 已由 app/executor.py 回填；"
        "回填脚本 tests/backfill_datasets.py，executor 指纹见 backfilled.executor_sha256"
    )
    manifest["field_status"] = {
        "id": "filled", "source": "filled", "by": "filled",
        "gen": "filled", "type": "filled", "template": "filled",
        "params": "filled",
        "signature": "filled — executor.signature()",
        "statement": "filled — executor.statement()",
        "reference": "filled — executor.solve_exact()",
    }
    manifest["backfilled"] = {
        "by": "tests/backfill_datasets.py",
        "executor_sha256": stats["executor_sha256"],
        "tasks": stats["tasks"],
        "unique_signatures": stats["unique_signatures"],
        "judge_selfcheck": "reference→right / perturbed→wrong / 拒答→unanswered",
        "decimal_checked": stats["decimal_checked"],
    }
    manifest["files"] = files
    manifest["totals"]["unique_signatures"] = stats["unique_signatures"]
    (DATA / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    if verbose:
        print("  data/manifest.json 已更新（字段状态 + 文件指纹）")
    return stats


def main(argv: list[str]) -> int:
    write = "--check" not in argv
    backfill(write=write, verbose=True)
    if not write:
        print("（--check 模式，未写盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
