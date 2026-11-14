"""结果页（`web/result.html` + `web/result.json`）的契约测试。

依据
----
技术规格 §1  L26        export.py 负责「公开历史与结果页」
技术规格 §2.2           holdout 20 道
技术规格 §10 L596       「所有数字追溯到 run_id」
技术规格 §10 L598       「出现负结果仍可形成完整交付」
任务书 C-3              每趟 run_id / 末代排名 / 三条留出数字 / 结论一句话

设计
----
这里有三类测试，故意分开：

1. **契约**：`result.json` 通过 `validate()`。字段缺一个就不许上页面。
2. **防吹牛**：`check_conclusion_fit()` 核对结论方向与数据方向。留出集 20 题
   的分辨率只有 5pp，所以 1 趟升 1 趟降时写「未观察到改善」是对的，写「显著提升」
   是错的——机器要能分辨这两件事，不能靠人自觉。
3. **页面自重**：`result.html` 不许引 CDN、不许写死数据数字、不许在缺数据时
   假装有数据（必须走 fail 分支）。

第 3 类用「篡改后必须报错」的写法，而不是「读一遍看着对」。只断言「有 data-run
属性」证明不了溯源真的能用；断言「把 run_id 改坏后 validate 会拒绝」才能。
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.result_schema import (  # noqa: E402
    LEVELS,
    check_conclusion_fit,
    delta_pp,
    validate,
)
from tests.make_fake_result import build  # noqa: E402

RESULT_JSON = ROOT / "web" / "result.json"
RESULT_HTML = ROOT / "web" / "result.html"


@pytest.fixture(scope="module")
def payload() -> dict:
    return build()


@pytest.fixture(scope="module")
def html() -> str:
    return RESULT_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def live_html(html) -> str:
    """去掉 HTML 注释后的版本。

    注释是给人看的，里面允许出现「20 题」「6 人」这类说明；能被浏览器执行的
    部分才是约束对象。不做这个区分，测试就是在管注释的措辞。
    """
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


# ── 1. 契约 ────────────────────────────────────────────────────


def test_schema_passes(payload):
    assert validate(payload) == []


def test_on_disk_json_is_valid_and_matches_builder(payload):
    """页面读的是磁盘上那份，不是内存里那份。

    干净克隆里没有 `runs/`（被 .gitignore 排除），只能校验契约；
    B 的开发机上有 `runs/`，再跟真实构建器逐行对账一次，防止手改 result.json。
    """
    disk = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
    assert validate(disk) == []
    assert check_conclusion_fit(disk) == []

    if (ROOT / "runs" / "protocol_v5.json").exists() and \
       (ROOT / "runs" / "ancestor_holdout.json").exists():
        from tools.build_result_board import build as build_real, load as load_json
        real = build_real(
            load_json(ROOT / "runs" / "ancestor_holdout.json"),
            load_json(ROOT / "runs" / "ancestors.json")["ancestors"],
            load_json(ROOT / "runs" / "protocol_v5.json"),
        )
        assert [r["run_id"] for r in disk["runs"]] == [r["run_id"] for r in real["runs"]]
        for a, b in zip(disk["runs"], real["runs"]):
            assert [l["holdout"]["correct"] for l in a["lineages"]] == \
                   [l["holdout"]["correct"] for l in b["lineages"]], a["run_id"]


def test_every_run_has_one_champion(payload):
    for run in payload["runs"]:
        champs = [l for l in run["lineages"] if l["kind"] == "champion"]
        assert len(champs) == 1, f"{run['run_id']} 冠军数不对"


def test_holdout_total_matches_metric(payload):
    n = payload["metric"]["n_tasks"]
    for run in payload["runs"]:
        for l in run["lineages"]:
            assert l["holdout"]["total"] == n


def test_run_ids_unique(payload):
    ids = [r["run_id"] for r in payload["runs"]]
    assert len(ids) == len(set(ids))


def test_mock_is_declared():
    """真假数据都必须自我声明：假的带 mock 徽章，真的必须有模型与来源。"""
    disk = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
    if disk.get("is_mock"):
        assert "mock" in str(disk.get("model_id", "")).lower()
    else:
        assert disk.get("model_id") and "mock" not in str(disk["model_id"]).lower()
        assert (disk.get("provenance") or {}).get("source")


def test_conclusion_text_not_empty(payload):
    assert payload["conclusion"]["text"].strip()


def test_conclusion_level_in_enum(payload):
    assert payload["conclusion"]["level"] in LEVELS


def test_every_evidence_points_to_real_run(payload):
    ids = {r["run_id"] for r in payload["runs"]}
    for ev in payload["conclusion"]["evidence"]:
        assert ev["run_id"] in ids


def test_result_json_carries_no_reference_values():
    """结果页进静态目录，必须过 export 白名单（任务书 §1 唯一硬约束）。

    这一条比看起来重要：结果页讲的是「答对几道」，只需要计数。一旦有人图省事把
    逐题期望值或原始响应塞进来，holdout 就等于公开了——`holdout` 字段本身是
    统计结果，允许存在；`reference` / `statement` / `raw_response` 是题面与答案，
    一律不许出现。
    """
    text = RESULT_JSON.read_text(encoding="utf-8")
    for banned in ("reference", "raw_response", "statement", "signature", "template", "params"):
        assert banned not in text, f"result.json 出现禁用字段：{banned}"


# ── 2. 防吹牛：结论必须与数据同向 ───────────────────────────────


def test_conclusion_matches_data(payload):
    assert check_conclusion_fit(payload) == []


def test_negative_result_is_representable(payload):
    """§10 L598：负结果算完整交付。所以假数据里必须真的有一趟跑输。"""
    deltas = [delta_pp(r) for r in payload["runs"]]
    assert any(d is not None and d < 0 for d in deltas), "假数据没有任何一趟跑输，页面就没测过负结果路径"


def test_at_least_one_tie_exists(payload):
    """再留一趟平局：显示层要能同时处理升、降、平三种，缺一种就是没覆盖。"""
    deltas = [delta_pp(r) for r in payload["runs"]]
    assert any(d is not None and abs(d) < 1e-9 for d in deltas)


def test_reject_false_improvement(payload):
    """全趟跑输却写 improved —— 必须拦下。这是最重要的一条。"""
    bad = copy.deepcopy(payload)
    for run in bad["runs"]:
        champ = next(l for l in run["lineages"] if l["kind"] == "champion")
        champ["holdout"]["correct"] = 1
    bad["conclusion"]["level"] = "positive"
    assert check_conclusion_fit(bad) != []


def test_reject_false_none_when_all_up(payload):
    """全趟都升却写「未观察到改善」—— 同样是失真，只是方向相反。"""
    bad = copy.deepcopy(payload)
    for run in bad["runs"]:
        champ = next(l for l in run["lineages"] if l["kind"] == "champion")
        champ["holdout"]["correct"] = run["lineages"][0]["holdout"]["total"]
    bad["conclusion"]["level"] = "none"
    assert check_conclusion_fit(bad) != []


def test_reject_evidence_with_unknown_run(payload):
    bad = copy.deepcopy(payload)
    bad["conclusion"]["evidence"][0]["run_id"] = "r_does_not_exist"
    assert any("不存在的 run_id" in e for e in validate(bad))


# ── 3. 契约的破坏性测试：改坏了必须被拦住 ───────────────────────


def test_reject_missing_is_mock(payload):
    bad = copy.deepcopy(payload)
    del bad["is_mock"]
    assert any("is_mock" in e for e in validate(bad))


def test_reject_two_champions(payload):
    bad = copy.deepcopy(payload)
    bad["runs"][0]["lineages"][1]["kind"] = "champion"
    assert any("champion" in e for e in validate(bad))


def test_reject_rank_out_of_range(payload):
    bad = copy.deepcopy(payload)
    bad["runs"][0]["lineages"][0]["final_rank"] = 99
    assert any("final_rank" in e for e in validate(bad))


def test_reject_holdout_overflow(payload):
    bad = copy.deepcopy(payload)
    l = bad["runs"][0]["lineages"][0]
    l["holdout"]["correct"] = l["holdout"]["total"] + 1
    assert any("越界" in e for e in validate(bad))


def test_reject_inconsistent_ancestor_across_runs(payload):
    """同一套祖先换个名字，跨趟对照就失效了（§6.2 冻结组要求）。"""
    bad = copy.deepcopy(payload)
    for l in bad["runs"][1]["lineages"]:
        if l["kind"] == "ancestor":
            l["label"] = l["label"] + "（改名）"
            break
    assert any("不一致" in e for e in validate(bad))


def test_reject_empty_runs(payload):
    bad = copy.deepcopy(payload)
    bad["runs"] = []
    assert any("runs" in e for e in validate(bad))


def test_reject_bad_level(payload):
    bad = copy.deepcopy(payload)
    bad["conclusion"]["level"] = "huge_win"
    assert any("level" in e for e in validate(bad))


# ── 4. 页面自重 ────────────────────────────────────────────────


def test_no_external_resources(live_html):
    """断网可播是硬要求：不许有 http(s) 外链、CDN、外部字体。

    `http://www.w3.org/2000/svg` 是 SVG 命名空间常量，不发请求，不算外链。
    """
    body = live_html.replace("http://www.w3.org/2000/svg", "").replace("https://www.w3.org/2000/svg", "")
    assert "http://" not in body and "https://" not in body, "页面出现外部 URL"
    assert not re.search(r"<script[^>]+src=", body), "页面引入外部脚本"
    assert not re.search(r"<link[^>]+href=", body), "页面引入外部样式/字体"


def test_only_fetches_result_json(html):
    fetches = re.findall(r"fetch\(([^)]*)\)", html)
    assert fetches, "页面没有任何 fetch，数据从哪来？"
    assert all('"result.json"' in f or "'result.json'" in f for f in fetches)


def test_no_hardcoded_scores(live_html):
    """形如 15/20 的分数不许写进页面——分母只能来自数据。"""
    assert not re.search(r">\s*\d{1,3}\s*/\s*\d{1,3}\s*<", live_html)


def test_no_hardcoded_metric_numbers(live_html):
    """每趟题数、趟数都不许写死。出现具体趟数就是版式绑死数据。"""
    for literal in ("20 题", "3 趟", "三趟独立运行"):
        assert literal not in live_html, f"页面写死了数据数字：{literal}"


def test_traceability_wired(html):
    """溯源不是文案，是交互：数字带 data-run，点击走 trace()。"""
    assert 'class="num" data-run=' in html
    assert "function trace(" in html
    assert "run_id" in html


def test_negative_result_branch_exists(html):
    """三种 level 都要有渲染分支，页面不能只写了「赢了」的样式。"""
    for lv in LEVELS:
        assert lv in html, f"页面缺少 {lv} 分支"


def test_fail_branch_present(html):
    """读不到数据必须显式报错，不许静默显示空表。"""
    assert "function fail(" in html
    assert "catch(" in html


def test_rank_rail_reads_n_individuals(html):
    """排名条的人数来自 n_individuals，不写死 6。"""
    assert "n_individuals" in html
    assert "final_rank" in html


def test_verdict_text_from_data(html):
    """结论句必须取自 conclusion.text，页面不许自己造句。"""
    assert "c.text" in html
    assert "conclusion" in html
