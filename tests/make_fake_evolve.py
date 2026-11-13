"""造一对 pass1/pass2 假数据，供 web/evolve.html 联调。

**这是假数据，一个数字都不许进比赛结论。**
真数据由 app/demo.py 现场跑出来；字段契约见 app/evolve_schema.py。

    用法： python tests/make_fake_evolve.py
    产出： web/evolve.json
"""

import json
import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evolve_schema import SCHEMA, SCHEMA_VERSION, validate, verdict_of  # noqa: E402

OUT = ROOT / "web" / "evolve.json"

# 固定验证题库：4 族 10 题（8 道共同卷 + 2 道挑战）
BANK = [
    ("V01", "integral"), ("V02", "integral"), ("V03", "integral"),
    ("V04", "det"), ("V05", "det"), ("V06", "det"),
    ("V07", "balanced"), ("V08", "balanced"),
    ("V09", "mixed"), ("V10", "mixed"),
]

PASS1_OK = {"V04", "V05", "V07", "V08", "V09"}                     # 5/10
PASS2_OK = {"V01", "V02", "V04", "V05", "V06", "V07", "V08", "V09"}  # 8/10


def _per_task(ok_ids: set[str], wrong_reason: dict[str, str]) -> list[dict]:
    rows = []
    for tid, fam in BANK:
        ok = tid in ok_ids
        rows.append({
            "id": tid,
            "family": fam,
            "correct": ok,
            "answer_chars": 17 + (hash(tid) % 23),
            "reason": None if ok else wrong_reason.get(tid, "答案与期望值不符"),
        })
    return rows


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build() -> dict:
    r1 = {"V01": "分子漏乘 2，得 1/3（应为 1/6）",
          "V02": "端点 x=0 为奇点，直接代入得发散值",
          "V03": "分部积分符号错，末项写成 +1/4",
          "V06": "Bareiss 主元为 0 未换行，直接中断返回 None",
          "V10": "把定积分与不定积分混用，漏了代入上下限"}
    r2 = {"V03": "符号仍错（规则未覆盖符号校验）",
          "V10": "答案格式正确但数值差 1/12"}

    p1 = {"pass": 1, "duration_s": 24.8, "correct": 5, "total": len(BANK),
          "accuracy": 5 / len(BANK), "per_task": _per_task(PASS1_OK, r1),
          "state_version": "v0_seed", "tokens_in": 8120, "tokens_out": 2410}
    p2 = {"pass": 2, "duration_s": 23.1, "correct": 8, "total": len(BANK),
          "accuracy": 8 / len(BANK), "per_task": _per_task(PASS2_OK, r2),
          "state_version": "v1_learned", "tokens_in": 8940, "tokens_out": 2260}

    delta_pp = round((p2["accuracy"] - p1["accuracy"]) * 100, 1)

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "pair_id": "demo_pair_fake",
        "frozen": {
            "config_sha256": _sha('{"seed":42,"fitness_window":5,"set_weight":0.15}'),
            "prompts_sha256": _sha("setter|solver|crossover|mutate|seed"),
            "identical_across_passes": True,
            "note": "两次运行之间 config.json 与 prompts/ 的 SHA-256 完全一致，改动只落在 state.json",
        },
        "metric": {
            "name": "固定题库正确率",
            "short": "correct rate",
            "unit": "%",
            "n_tasks": len(BANK),
            "higher_is_better": True,
            "definition": f"{len(BANK)} 道冻结验证题（4 族）中判分完全正确的题数占比；判分由 executor 精确计算，不经 LLM 裁判",
        },
        "passes": [p1, p2],
        "learned": {
            "rules_added": [
                {"id": "R1", "from_pass": 1, "scope": "integral",
                 "text": "积分中间结果一律写成 Fraction(分子,分母) 并保留完整分子分母，禁止中途约分为小数",
                 "evidence": "V01 漏乘 2、V03 末项符号错，两条都是中途约分导致"},
                {"id": "R2", "from_pass": 1, "scope": "integral",
                 "text": "定积分代入前先检查上下限是否为被积函数的奇点；是则改写为极限形式再求值",
                 "evidence": "V02 在 x=0 处直接代入"},
                {"id": "R3", "from_pass": 1, "scope": "det",
                 "text": "Bareiss 消元每步检查主元；主元为 0 时交换行、结果乘 -1，再继续消元",
                 "evidence": "V06 主元为 0 未换行，进程返回 None"},
            ],
            "memories_added": [
                {"id": "M1", "from_pass": 1, "kind": "case",
                 "text": "V01 正确值 1/6；本题最易错点是漏除 2", "used_in_pass": 2},
                {"id": "M2", "from_pass": 1, "kind": "case",
                 "text": "V02 在 x=0 处为奇点，正确值以极限求得", "used_in_pass": 2},
            ],
            "params_changed": [
                {"key": "rule_weight[integral]", "scope": "state", "before": 0.0, "after": 0.35},
                {"key": "verify_depth", "scope": "state", "before": 1, "after": 2},
                {"key": "fitness_window", "scope": "config", "before": 5, "after": 5},
            ],
            "genes_changed": {"solve_prompt_diff_lines": 2, "set_prompt_diff_lines": 0},
        },
        "rollback": {
            "available": True,
            "snapshot_id": "v0_seed",
            "snapshot_path": "runs/demo_pair_fake/state/v0_seed.json",
            "command": "python -m app.demo rollback --pair demo_pair_fake --to v0_seed",
            "verified": True,
            "note": "回退后 state.json 逐字节恢复为 pass1 之前的版本，rules/memories 全部清空",
        },
        "conclusion": {
            "delta_accuracy_pp": delta_pp,
            "verdict": verdict_of(delta_pp),
            "one_line": f"固定题库正确率 {p1['correct']}/{len(BANK)} → {p2['correct']}/{len(BANK)}"
                        f"（+{delta_pp:.1f} 个百分点），新增 3 条规则、2 条案例记忆，全部来自 pass1 的失败模式",
        },
    }


def main() -> int:
    payload = build()
    errs = validate(payload)
    if errs:
        print("✗ 契约校验失败：")
        for e in errs:
            print("   -", e)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已写 {OUT.relative_to(ROOT)}  ({OUT.stat().st_size} 字节)")
    print(f"  pass1 {payload['passes'][0]['correct']}/{payload['passes'][0]['total']}"
          f" → pass2 {payload['passes'][1]['correct']}/{payload['passes'][1]['total']}"
          f"  判定 {payload['conclusion']['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
