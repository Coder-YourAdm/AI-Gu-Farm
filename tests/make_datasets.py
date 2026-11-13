"""四类题集的数据构造器（只出「题面参数」，不出判分三字段）。

依据
----
技术规格 §2.1 L113–L131  参数边界（实现时照抄，不要各自解释）
技术规格 §2.2 L137–L149  后端 Task 的十个键
技术规格 §5   L330       预生成 calibration 20 / common_dev 80（按 gen 分 20 组）/
                         challenge_pool 5 / holdout 20；各题验真并签名互斥
技术规格 §5   L330       共同卷每组 integral=2、det=2；calibration 与 holdout 各
                         integral=10、det=10；三类积分在全卷中尽量均衡

本文件为什么留空三个字段
----------------------
`statement()` / `signature()` / `solve_exact()` 在规格 §2.3 里都由 `executor.py`
提供。C 若自己实现一遍，同一道题会算出第二个签名 —— 去重失效、互斥检测失效、
`history` 里的 `task_id` 对不上，20 代数据全部作废。

所以本文件只产出 A 无法省略的那部分「数据」，三个判分字段全部写 `null`，
由 A 跑一遍回填：

    for t in tasks:
        t["statement"] = statement(t)
        t["signature"] = signature(t)
        t["reference"] = solve_exact(t)

本文件里的 `_local_signature()` 只用于**本地查重**，核验四类题集签名互斥。
它照规格 §2.2 的示例格式（`gamma|c=1|n=4|rate=2`，键按字母序）实现，
**不写进数据文件**，正式签名一律以 A 的 `signature()` 为准。

用法
----
    python tests/make_datasets.py            # 生成并校验，写 data/*.json
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SEED = 20261113          # 固定种子，保证题集可复现
N_CAL = 20
N_COMMON_GROUPS = 20     # 每组 4 题 = integral 2 + det 2，共 80
N_CHAL = 5
N_HOLD = 20

INT_TEMPLATES = ("poly", "gamma", "gaussian_odd")


# ────────────────────────────── 参数生成 ──────────────────────────────
def gen_poly(rng: random.Random) -> dict:
    """§2.1：coeffs 长度 2..5、元素 -5..5、末项非零、至少一个 i>=1 的系数非零。"""
    while True:
        d = rng.choice([1, 2, 3, 4])                 # 次数 1..4 → 长度 2..5
        coeffs = [rng.randint(-5, 5) for _ in range(d + 1)]
        if coeffs[-1] == 0:
            coeffs[-1] = rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])
        if all(c == 0 for c in coeffs[1:]):
            continue                                  # 必须含非常数项
        lower = rng.randint(-3, 3)
        upper = lower + rng.randint(1, 6)
        return {"coeffs": coeffs, "lower": lower, "upper": upper}


def gen_gamma(rng: random.Random) -> dict:
    """§2.1：n 1..9、rate 1..8、c ∈ ±{1..5}（不收 0，0 使答案恒为 0）。"""
    return {
        "n": rng.randint(1, 9),
        "rate": rng.randint(1, 8),
        "c": rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5]),
    }


def gen_gaussian_odd(rng: random.Random) -> dict:
    """§2.1：k 0..5，其余同 gamma。"""
    return {
        "k": rng.randint(0, 5),
        "rate": rng.randint(1, 8),
        "c": rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5]),
    }


def gen_det(rng: random.Random) -> dict:
    """§2.1：阶数 2..5、元素 -9..9 的整数方阵。

    额外避开奇异矩阵：行列式为 0 的题答案恒为 0，无区分度。
    规格未强制，但题集质量需要 —— 出题偏好里也写着「近奇异但非奇异」。
    """
    while True:
        n = rng.choice([2, 3, 3, 4, 4, 5])            # 偏重 3–4 阶，2 阶太容易
        m = [[rng.randint(-9, 9) for _ in range(n)] for _ in range(n)]
        if _det_fraction(m) != 0:
            return {"matrix": m}


def _det_fraction(m: list[list[int]]) -> Fraction:
    """Bareiss 消元算行列式，全程 Fraction，不碰浮点。"""
    n = len(m)
    a = [[Fraction(x) for x in row] for row in m]
    sign = 1
    prev = Fraction(1)
    for k in range(n - 1):
        if a[k][k] == 0:
            for i in range(k + 1, n):
                if a[i][k] != 0:
                    a[k], a[i] = a[i], a[k]
                    sign = -sign
                    break
            else:
                return Fraction(0)
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                a[i][j] = (a[i][j] * a[k][k] - a[i][k] * a[k][j]) / prev
        prev = a[k][k]
        for i in range(k + 1, n):
            a[i][k] = Fraction(0)
    return sign * a[n - 1][n - 1]


def gen_params(template: str, rng: random.Random) -> dict:
    return {
        "poly": gen_poly,
        "gamma": gen_gamma,
        "gaussian_odd": gen_gaussian_odd,
        "det": gen_det,
    }[template](rng)


# ──────────────────────── 本地查重用的签名 ────────────────────────
def _local_signature(template: str, params: dict) -> str:
    """照 §2.2 示例格式构造，仅用于本地去重。**不写入数据文件。**

    示例：`gamma|c=1|n=4|rate=2` —— 键按字母序，值原样。
    """
    parts = [template]
    for k in sorted(params):
        v = params[k]
        if isinstance(v, list):
            if v and isinstance(v[0], list):
                v = "[" + ";".join(",".join(str(x) for x in row) for row in v) + "]"
            else:
                v = ",".join(str(x) for x in v)
        parts.append(f"{k}={v}")
    return "|".join(parts)


# ────────────────────────────── 边界校验 ──────────────────────────────
def validate_params(template: str, p: dict) -> None:
    """§2.1 的精确边界，逐条断言。A 的 validate_task 会再做一次，这里先自己挡。"""
    assert isinstance(p, dict)
    if template == "poly":
        assert set(p) == {"coeffs", "lower", "upper"}, f"poly 键不对: {set(p)}"
        c, lo, up = p["coeffs"], p["lower"], p["upper"]
        assert isinstance(c, list) and 2 <= len(c) <= 5, f"coeffs 长度 {len(c)}"
        assert all(type(x) is int and -5 <= x <= 5 for x in c), f"coeffs 越界 {c}"
        assert c[-1] != 0, "末项为 0"
        assert any(x != 0 for x in c[1:]), "缺非常数项"
        assert type(lo) is int and -3 <= lo <= 3, f"lower {lo}"
        assert type(up) is int and lo + 1 <= up <= lo + 6, f"upper {up} / lower {lo}"
        assert up > lo
    elif template in ("gamma", "gaussian_odd"):
        nk = "n" if template == "gamma" else "k"
        assert set(p) == {nk, "rate", "c"}, f"{template} 键不对: {set(p)}"
        assert type(p["rate"]) is int and 1 <= p["rate"] <= 8, f"rate {p['rate']}"
        assert type(p["c"]) is int and abs(p["c"]) in (1, 2, 3, 4, 5), f"c {p['c']}"
        if template == "gamma":
            assert type(p["n"]) is int and 1 <= p["n"] <= 9, f"n {p['n']}"
        else:
            assert type(p["k"]) is int and 0 <= p["k"] <= 5, f"k {p['k']}"
    elif template == "det":
        assert set(p) == {"matrix"}, f"det 键不对: {set(p)}"
        m = p["matrix"]
        n = len(m)
        assert 2 <= n <= 5, f"阶数 {n}"
        assert all(isinstance(r, list) and len(r) == n for r in m), "不是方阵"
        assert all(type(x) is int and -9 <= x <= 9 for r in m for x in r), "元素越界"
        assert _det_fraction(m) != 0, "奇异矩阵，答案恒为 0，无区分度"
    else:
        raise AssertionError(f"未知模板 {template}")


# ────────────────────────────── 组题 ──────────────────────────────
def _task(tid: str, source: str, gen: int | None,
          template: str, params: dict) -> dict:
    """后端 Task 十个键，顺序照 §2.2。三个判分字段留 null 由 A 回填。"""
    return {
        "id": tid,
        "source": source,
        "by": None,
        "gen": gen,
        "type": "det" if template == "det" else "integral",
        "template": template,
        "params": params,
        "signature": None,
        "statement": None,
        "reference": None,
    }


def build_all() -> dict[str, list[dict]]:
    rng = random.Random(SEED)
    seen: set[str] = set()
    out: dict[str, list[dict]] = {
        "calibration": [], "common_dev": [], "challenge_pool": [], "holdout": [],
    }

    def make(tid: str, source: str, gen: int | None, template: str) -> dict:
        """生成一道题并保证签名全局互斥（四类题集共用一个 seen 集合）。"""
        for _ in range(500):
            params = gen_params(template, rng)
            try:
                validate_params(template, params)
            except AssertionError:
                continue
            sig = _local_signature(template, params)
            if sig in seen:
                continue
            seen.add(sig)
            return _task(tid, source, gen, template, params)
        raise RuntimeError(f"{tid} 生成 500 次仍重复，扩大参数空间或换种子")

    # 全局连续轮转：不能每个数据集重置，否则 common_dev 每组 2 题永远取 poly+gamma，
    # gaussian_odd 会被挤到只剩个位数（实测失衡到 29/27/7）。
    _rot = {"i": 0}

    def next_integral() -> str:
        tpl = INT_TEMPLATES[_rot["i"] % 3]
        _rot["i"] += 1
        return tpl

    # ── calibration：integral 10 + det 10 ──
    for i in range(10):
        out["calibration"].append(
            make(f"K{i + 1:02d}", "calibration", None, next_integral()))
    for i in range(10):
        out["calibration"].append(make(f"K{i + 11:02d}", "calibration", None, "det"))

    # ── common_dev：20 组 × (integral 2 + det 2) ──
    for gen in range(N_COMMON_GROUPS):
        for j in range(2):
            out["common_dev"].append(
                make(f"T{gen:02d}_{j + 1:02d}", "common_dev", gen, next_integral()))
        for j in range(2):
            out["common_dev"].append(
                make(f"T{gen:02d}_{j + 3:02d}", "common_dev", gen, "det"))

    # ── challenge_pool：integral 3 + det 2 ──
    for i in range(3):
        out["challenge_pool"].append(
            make(f"X{i + 1:02d}", "challenge_pool", None, next_integral()))
    for i in range(2):
        out["challenge_pool"].append(make(f"X{i + 4:02d}", "challenge_pool", None, "det"))

    # ── holdout：integral 10 + det 10 ──
    for i in range(10):
        out["holdout"].append(make(f"Y{i + 1:02d}", "holdout", None, next_integral()))
    for i in range(10):
        out["holdout"].append(make(f"Y{i + 11:02d}", "holdout", None, "det"))

    return out


# ────────────────────────────── 校验与落盘 ──────────────────────────────
KEY_ORDER = ["id", "source", "by", "gen", "type", "template",
             "params", "signature", "statement", "reference"]


def check(datasets: dict[str, list[dict]]) -> dict:
    """全部校验，任一不过直接抛。返回统计信息供打印。"""
    assert len(datasets["calibration"]) == N_CAL, "calibration 应为 20"
    assert len(datasets["common_dev"]) == N_COMMON_GROUPS * 4, "common_dev 应为 80"
    assert len(datasets["challenge_pool"]) == N_CHAL, "challenge_pool 应为 5"
    assert len(datasets["holdout"]) == N_HOLD, "holdout 应为 20"

    # 每组的 integral/det 组成
    for name in ("calibration", "holdout"):
        rows = datasets[name]
        n_int = sum(1 for r in rows if r["type"] == "integral")
        n_det = sum(1 for r in rows if r["type"] == "det")
        assert (n_int, n_det) == (10, 10), f"{name} 应为 10/10，实际 {n_int}/{n_det}"

    groups: dict[int, list[dict]] = {}
    for r in datasets["common_dev"]:
        groups.setdefault(r["gen"], []).append(r)
    assert len(groups) == N_COMMON_GROUPS, f"common_dev 应有 20 组，实际 {len(groups)}"
    for gen, rows in groups.items():
        assert len(rows) == 4, f"第 {gen} 组应 4 题，实际 {len(rows)}"
        assert sum(1 for r in rows if r["type"] == "integral") == 2, f"第 {gen} 组 integral≠2"
        assert sum(1 for r in rows if r["type"] == "det") == 2, f"第 {gen} 组 det≠2"

    # 结构、键顺序、边界
    all_ids: set[str] = set()
    sigs: dict[str, str] = {}
    for name, rows in datasets.items():
        for r in rows:
            assert list(r) == KEY_ORDER, f"{r['id']} 键顺序/名称不对：{list(r)}"
            assert r["id"] not in all_ids, f"id 重复 {r['id']}"
            all_ids.add(r["id"])
            assert r["type"] == ("det" if r["template"] == "det" else "integral")
            assert r["signature"] is None and r["statement"] is None
            assert r["reference"] is None
            validate_params(r["template"], r["params"])
            sig = _local_signature(r["template"], r["params"])
            assert sig not in sigs, f"签名重复：{sig}（{sigs[sig]} 与 {r['id']}）"
            sigs[sig] = r["id"]

    # 三类积分的均衡度
    tpl_count: dict[str, int] = {}
    for rows in datasets.values():
        for r in rows:
            tpl_count[r["template"]] = tpl_count.get(r["template"], 0) + 1

    int_counts = {t: tpl_count.get(t, 0) for t in INT_TEMPLATES}
    assert max(int_counts.values()) - min(int_counts.values()) <= 2, (
        f"三类积分失衡：{int_counts}（规格 §5 要求全卷尽量均衡）")

    return {
        "total": sum(len(v) for v in datasets.values()),
        "unique_signatures": len(sigs),
        "template_counts": tpl_count,
    }


def write(datasets: dict[str, list[dict]], stats: dict) -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rows in datasets.items():
        p = DATA / f"{name}.json"
        body = json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
        p.write_text(body, encoding="utf-8", newline="\n")
        files[name] = {
            "path": str(p.relative_to(ROOT)).replace("\\", "/"),
            "count": len(rows),
            "bytes": len(body.encode("utf-8")),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }

    manifest = {
        "schema": "datasets-manifest",
        "schema_version": "v5.1",
        "generated_by": "tests/make_datasets.py",
        "seed": SEED,
        "note": (
            "signature / statement / reference 三字段恒为 null，"
            "须由 A 的 executor.py（§2.3）回填后再用于运行与校验"
        ),
        "field_status": {
            "id": "filled", "source": "filled", "by": "filled(null)",
            "gen": "filled", "type": "filled", "template": "filled",
            "params": "filled",
            "signature": "null — 由 signature() 回填",
            "statement": "null — 由 statement() 回填",
            "reference": "null — 由 solve_exact() 回填",
        },
        "composition": {
            "calibration": {"integral": 10, "det": 10, "total": 20},
            "common_dev": {"groups": 20, "per_group": 4,
                           "integral": 40, "det": 40, "total": 80},
            "challenge_pool": {"integral": 3, "det": 2, "total": 5},
            "holdout": {"integral": 10, "det": 10, "total": 20},
        },
        "totals": stats,
        "files": files,
    }
    mp = DATA / "manifest.json"
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                  encoding="utf-8", newline="\n")
    return manifest


def main() -> int:
    datasets = build_all()
    stats = check(datasets)
    manifest = write(datasets, stats)

    print(f"✓ 生成 {stats['total']} 道题，签名全局唯一 {stats['unique_signatures']} 条")
    print(f"  模板分布: {stats['template_counts']}")
    for name, info in manifest["files"].items():
        print(f"  {info['path']:<28} {info['count']:>3} 题  "
              f"{info['bytes']:>6} 字节  {info['sha256'][:16]}")

    # 生成完立刻回填，不把 null 留在盘上。
    # 否则重跑一次本脚本就会把 125 题的 reference 全清空，
    # 而下游（B 的 orchestrator、大屏、结果页）会在运行时才发现题判不了。
    # 脚本模式下 sys.path[0] 是 tests/，必须手动补项目根，否则 import 不到。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from tests.backfill_datasets import backfill
    except ImportError:
        print("\n⚠ app/executor.py 不可用，三个判分字段仍为 null；"
              "拿到 A 的 executor 后跑 python tests/backfill_datasets.py")
        return 1
    print("\n回填判分字段：")
    backfill(write=True, verbose=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
