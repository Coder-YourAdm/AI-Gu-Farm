"""prompts/ 五份文件的契约测试。

依据
----
技术规格 §3.2 `setter.md`（system）
技术规格 §3.3 `solver.md`（system）
技术规格 §3.4 算子 prompts 的固定要求
技术规格 §3.5 六份初代基因，三点约束「实现时必须写成测试」

为什么前两份要跟规格逐字比对
--------------------------
这两份是**从规格代码块自动抽取**的，不是手抄。测试重做一次抽取再比对，
是为了防止有人后来「顺手改一句措辞」——改一个字就可能改变模型行为，
而 pilot 的数字会因此不可复现。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
SPEC = ROOT / "方案" / "技术规格_v5.md"


def _grab_from_spec(anchor: str, lang: str) -> str:
    """从规格里取出 anchor 之后第一个 ```lang 代码块，逐字返回。"""
    text = SPEC.read_text(encoding="utf-8")
    i = text.index(anchor)
    m = re.search(r"```" + lang + r"\n(.*?)\n```", text[i:], re.S)
    assert m is not None, f"规格里找不到 {anchor} 的代码块"
    return m.group(1)


@pytest.fixture(scope="module")
def genotypes() -> list[dict]:
    return json.loads((PROMPTS / "seed_genotypes.json").read_text(encoding="utf-8"))


# ── 1. 五份文件齐全 ──────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "setter.md", "solver.md", "seed_genotypes.json", "crossover.md", "mutate.md",
])
def test_file_exists_and_nonempty(name: str) -> None:
    p = PROMPTS / name
    assert p.exists(), f"缺文件 {name}"
    assert p.stat().st_size > 0, f"{name} 是空的"


# ── 2. 两份与规格逐字一致（防手抄/防后续误改）────────────────────
@pytest.mark.parametrize("name,anchor", [
    ("setter.md", "### 3.2 `setter.md`（system）"),
    ("solver.md", "### 3.3 `solver.md`（system）"),
])
def test_matches_spec_verbatim(name: str, anchor: str) -> None:
    want = _grab_from_spec(anchor, "text")
    got = (PROMPTS / name).read_text(encoding="utf-8").rstrip("\n")
    assert got == want, f"{name} 与规格 §3 的代码块不一致，被改过"


def test_genotypes_match_spec(genotypes: list[dict]) -> None:
    want = json.loads(_grab_from_spec("### 3.5 六份初代基因", "json"))
    assert genotypes == want, "seed_genotypes.json 与规格 §3.5 不一致"


# ── 3. §3.5 的三点约束（规格要求必须写成测试）────────────────────
def test_len_within_200(genotypes: list[dict]) -> None:
    for g in genotypes:
        genes = g["genes"]
        n = len(genes["solve_prompt"]) + len(genes["set_prompt"])
        assert n <= 200, f"{g['meta']['id']} 合计 {n} 字，超过 200"


def test_prompts_pairwise_distinct(genotypes: list[dict]) -> None:
    sp = [g["genes"]["solve_prompt"] for g in genotypes]
    tp = [g["genes"]["set_prompt"] for g in genotypes]
    assert len(set(sp)) == 6, "solve_prompt 有重复"
    assert len(set(tp)) == 6, "set_prompt 有重复"


def test_self_check_distribution(genotypes: list[dict]) -> None:
    flags = [g["genes"]["self_check"] for g in genotypes]
    assert flags.count(True) == 3, f"self_check=True 应为 3 份，实际 {flags.count(True)}"
    assert flags.count(False) == 3, f"self_check=False 应为 3 份，实际 {flags.count(False)}"
    assert all(isinstance(f, bool) for f in flags), "self_check 必须是真布尔"


# ── 4. 基因结构完整（下游 orch/export 依赖这些键）────────────────
def test_meta_keys(genotypes: list[dict]) -> None:
    for g in genotypes:
        assert set(g) == {"genes", "meta"}, f"顶层键应为 genes/meta，实际 {set(g)}"
        assert set(g["genes"]) == {"solve_prompt", "set_prompt", "self_check"}
        for k in ("id", "parents", "origin", "born_gen"):
            assert k in g["meta"], f"{g['meta'].get('id')} 缺 meta.{k}"


def test_seed_origin_and_unique_id(genotypes: list[dict]) -> None:
    ids = [g["meta"]["id"] for g in genotypes]
    assert len(set(ids)) == 6, "meta.id 有重复"
    for g in genotypes:
        assert g["meta"]["origin"] == "seed", "初代 origin 必须为 seed"
        assert g["meta"]["parents"] == [], "初代 parents 必须为空"
        assert g["meta"]["born_gen"] == 0, "初代 born_gen 必须为 0"


# ── 5. 占位符齐全（缺一个就是运行时 KeyError）────────────────────
@pytest.mark.parametrize("name,placeholders", [
    ("setter.md", ["{set_prompt}"]),
    ("solver.md", ["{solve_prompt}", "{check_instruction}"]),
    ("crossover.md", ["{parent_a}", "{parent_b}"]),
    ("mutate.md", ["{parent}", "{field_to_mutate}"]),
])
def test_placeholders_present(name: str, placeholders: list[str]) -> None:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for ph in placeholders:
        assert ph in text, f"{name} 缺占位符 {ph}"


def test_must_render_with_replace_not_format() -> None:
    """【重要】这四份 prompt 只能用 str.replace() 渲染，不能用 str.format()。

    原因：`setter.md` 里有题面模板语法 `params={coeffs:[a0,...,ad],lower:l,upper:u}`，
    花括号会被 `str.format()` 当成占位符，直接 `KeyError: 'coeffs'`。
    规格原文就是这样，**不能改**（改了就和规格 §3.2 不一致）。

    这条测试是给 B 的护栏：谁把渲染改成 `.format()` 或 f-string，这里会红。
    """
    setter = (PROMPTS / "setter.md").read_text(encoding="utf-8")

    # format() 必须失败——这不是 bug，是这几份 prompt 的固有限制
    with pytest.raises((KeyError, ValueError, IndexError)):
        setter.format(set_prompt="x")

    # replace() 必须成功，且不留残留
    out = setter.replace("{set_prompt}", "优先 poly，系数不为零")
    assert "{set_prompt}" not in out
    assert "params={coeffs:" in out, "题面模板语法必须原样保留，不能被替换掉"


@pytest.mark.parametrize("name,fields", [
    ("setter.md", {"set_prompt": "PREFS"}),
    ("solver.md", {"solve_prompt": "STRATEGY", "check_instruction": ""}),
    ("crossover.md", {"parent_a": "A", "parent_b": "B"}),
    ("mutate.md", {"parent": "P", "field_to_mutate": "self_check"}),
])
def test_replace_rendering_leaves_no_placeholder(name: str, fields: dict) -> None:
    """用 replace 逐字段替换后，不应残留任何花括号包裹的字段名。"""
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    for k in fields:
        assert "{" + k + "}" not in text, f"{name} 残留占位符 {{{k}}}"


# ── 6. solver.md 的 check_instruction 规则（§3.3）─────────────────
def test_solver_has_check_instruction_placeholder() -> None:
    """§3.3：self_check=true 时替换为固定句，false 时为空串。占位符必须保留。"""
    text = (PROMPTS / "solver.md").read_text(encoding="utf-8")
    assert "{check_instruction}" in text
    assert text.count("{check_instruction}") == 1, "占位符只应出现一次"
