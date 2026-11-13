"""B4 用的测试替身。**只允许出现在 tests/ 下，不得进 app/。**

为什么要写这份替身：A4 的 `executor.py` 目前六个函数还是 `NotImplementedError`，
C 的 `datasets.py`、`challenge.py` 也还没有。B4 要验证的是「一代事务的调度是否正确」，
不是「数学算得对不对」。所以这里按技术规格逐条实现一份**参照实现**：

- `FakeEngine`：§2.1 参数边界、§2.3 六个函数签名、§2.4 答案解析、§2.5 题面规则；
- `FakeDataset`：§5 的 common_dev 分组与 challenge_pool；
- `ScriptedChat`：可控的三态响应（合法题 / 非法格式 / 传输错误），并统计调用次数。

真模块到位后，`FakeEngine` 应被 `app.executor` 直接替换，`FakeDataset` 被
`app.datasets` 替换，调度代码一行不改。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from math import factorial
from typing import Any, Callable, Mapping, Sequence

from app.config import Config
from app.genotype import (GeneHistory, Individual, load_seed_genotypes)
from app.llm import CallResult
from app.orchestrator import Challenge

# --------------------------------------------------------------------------
# 题面（规格 §2.5）
# --------------------------------------------------------------------------

_SUPS = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def _sup(n: int) -> str:
    if 0 <= n <= 19:
        return "".join(_SUPS[int(d)] for d in str(n))
    return f"^{n}"


def _poly_text(coeffs: Sequence[int]) -> str:
    """降幂、省略系数 1、负号放项首、` + ` / ` - ` 连接。"""
    terms: list[str] = []
    for i in range(len(coeffs) - 1, -1, -1):
        a = int(coeffs[i])
        if a == 0:
            continue
        if i == 0:
            body = str(abs(a))
        else:
            var = "x" if i == 1 else "x" + _sup(i)
            body = var if abs(a) == 1 else f"{abs(a)}{var}"
        if not terms:
            terms.append(("-" if a < 0 else "") + body)
        else:
            terms.append((" - " if a < 0 else " + ") + body)
    return "".join(terms) if terms else "0"


def _c_prefix(c: int) -> str:
    if c == 1:
        return ""
    if c == -1:
        return "-"
    return str(c)


def _x_power(n: int) -> str:
    if n == 0:
        return ""
    if n == 1:
        return "x"
    return "x" + _sup(n)


def statement_of(template: str, params: Mapping[str, Any]) -> str:
    if template == "poly":
        coeffs = params["coeffs"]
        return (f"计算定积分 ∫_{{{params['lower']}}}^{{{params['upper']}}} "
                f"({_poly_text(coeffs)}) dx。")
    if template == "gamma":
        n, rate, c = params["n"], params["rate"], params["c"]
        exp = "-x" if rate == 1 else f"-{rate}x"
        return f"计算定积分 ∫₀^∞ {_c_prefix(c)}{_x_power(n)} e^{{{exp}}} dx。"
    if template == "gaussian_odd":
        k, rate, c = params["k"], params["rate"], params["c"]
        exp = "-x²" if rate == 1 else f"-{rate}x²"
        return (f"计算定积分 ∫₀^∞ {_c_prefix(c)}{_x_power(2 * k + 1)} "
                f"e^{{{exp}}} dx。")
    if template == "det":
        matrix = params["matrix"]
        body = "[" + ", ".join("[" + ", ".join(str(x) for x in row) + "]"
                               for row in matrix) + "]"
        return f"计算下列 {len(matrix)} 阶矩阵的行列式：{body}。"
    raise ValueError(f"未知模板 {template!r}")


# --------------------------------------------------------------------------
# 参数边界（规格 §2.1 的表，逐行照抄）
# --------------------------------------------------------------------------

_TEMPLATE_KEYS = {
    "poly": {"coeffs", "lower", "upper"},
    "gamma": {"n", "rate", "c"},
    "gaussian_odd": {"k", "rate", "c"},
    "det": {"matrix"},
}


def _is_int(v: Any) -> bool:
    return type(v) is int          # 拒绝 bool 与 5.0


def validate_task(raw: dict) -> tuple[bool, str]:
    if not isinstance(raw, Mapping):
        return False, "schema: 顶层必须是对象"
    if set(raw) != {"template", "params"}:
        return False, f"schema: 只允许 template 与 params，收到 {sorted(raw)}"
    template = raw["template"]
    if template not in _TEMPLATE_KEYS:
        return False, f"schema: 未知模板 {template!r}"
    params = raw["params"]
    if not isinstance(params, Mapping):
        return False, "schema: params 必须是对象"
    if set(params) != _TEMPLATE_KEYS[template]:
        return False, (f"schema: {template} 的 params 必须恰好是 "
                       f"{sorted(_TEMPLATE_KEYS[template])}")

    def bad_range(msg: str) -> tuple[bool, str]:
        return False, f"range: {msg}"

    if template == "poly":
        coeffs = params["coeffs"]
        if not isinstance(coeffs, list) or not all(_is_int(x) for x in coeffs):
            return False, "schema: coeffs 必须是 int 列表"
        if not (2 <= len(coeffs) <= 5):
            return bad_range("coeffs 长度必须 2..5")
        if any(not (-5 <= x <= 5) for x in coeffs):
            return bad_range("系数越界 -5..5")
        if coeffs[-1] == 0:
            return bad_range("末项不能为 0")
        if all(x == 0 for x in coeffs[1:]):
            return bad_range("至少一个 i>=1 的系数非零")
        lo, hi = params["lower"], params["upper"]
        if not (_is_int(lo) and _is_int(hi)):
            return False, "schema: lower/upper 必须是 int"
        if not (-3 <= lo <= 3):
            return bad_range("lower 越界 -3..3")
        if not (lo + 1 <= hi <= lo + 6):
            return bad_range("upper 必须落在 lower+1..lower+6")
        return True, ""

    if template in ("gamma", "gaussian_odd"):
        key = "n" if template == "gamma" else "k"
        hi_key = 9 if template == "gamma" else 5
        val, rate, c = params[key], params["rate"], params["c"]
        if not (_is_int(val) and _is_int(rate) and _is_int(c)):
            return False, "schema: n/k、rate、c 必须是 int"
        if not (0 <= val <= hi_key) or (template == "gamma" and val < 1):
            return bad_range(f"{key} 越界")
        if not (1 <= rate <= 8):
            return bad_range("rate 必须在 1..8（0 发散）")
        if c == 0 or abs(c) > 5:
            return bad_range("c 必须为 ±1..±5")
        return True, ""

    matrix = params["matrix"]
    if not isinstance(matrix, list) or not all(isinstance(r, list) for r in matrix):
        return False, "schema: matrix 必须是二维列表"
    d = len(matrix)
    if not (2 <= d <= 5):
        return bad_range("矩阵阶数必须 2..5")
    if any(len(r) != d for r in matrix):
        return False, "schema: 必须是方阵"
    if not all(_is_int(x) for r in matrix for x in r):
        return False, "schema: 元素必须是 int"
    if any(not (-9 <= x <= 9) for r in matrix for x in r):
        return bad_range("元素越界 -9..9")
    return True, ""


# --------------------------------------------------------------------------
# 精确求解（规格 §2.3 / 大纲 §4.1 的闭式）
# --------------------------------------------------------------------------

def _det_int(matrix: Sequence[Sequence[int]]) -> int:
    n = len(matrix)
    if n == 1:
        return matrix[0][0]
    total = 0
    for j in range(n):
        minor = [list(row[:j]) + list(row[j + 1:]) for row in matrix[1:]]
        total += (-1) ** j * matrix[0][j] * _det_int(minor)
    return total


def _rational(value: Fraction) -> dict:
    return {"kind": "rational", "numerator": str(value.numerator),
            "denominator": str(value.denominator)}


def _answer_text(task: Mapping[str, Any]) -> str:
    ref = task["reference"]
    if ref["kind"] == "integer":
        return ref["value"]
    return f"{ref['numerator']}/{ref['denominator']}"


class FakeEngine:
    """A 的 executor 的参照实现。签名与 §2.3 逐字一致。"""

    def __init__(self) -> None:
        self.reference_by_statement: dict[str, str] = {}

    # ---- §2.3 六个函数 ----

    def validate_task(self, raw: dict) -> tuple[bool, str]:
        return validate_task(raw)

    def normalize_task(self, raw: dict, *, task_id: str, source: str,
                       by: str | None, gen: int | None) -> dict:
        template = raw["template"]
        params = raw["params"]
        return {
            "id": task_id,
            "source": source,
            "by": by,
            "gen": gen,
            "type": "det" if template == "det" else "integral",
            "template": template,
            "params": params,
            "signature": self.signature({"template": template, "params": params}),
            "statement": statement_of(template, params),
        }

    def solve_exact(self, task: dict) -> dict:
        params = task["params"]
        template = task["template"]
        if template == "det":
            ref = {"kind": "integer", "value": str(_det_int(params["matrix"]))}
        elif template == "poly":
            coeffs, lo, hi = params["coeffs"], params["lower"], params["upper"]
            total = Fraction(0)
            for i, a in enumerate(coeffs):
                if a == 0:
                    continue
                total += Fraction(a) * (Fraction(hi) ** (i + 1)
                                        - Fraction(lo) ** (i + 1)) / (i + 1)
            ref = _rational(total)
        elif template == "gamma":
            n, rate, c = params["n"], params["rate"], params["c"]
            ref = _rational(Fraction(c * factorial(n), rate ** (n + 1)))
        elif template == "gaussian_odd":
            k, rate, c = params["k"], params["rate"], params["c"]
            ref = _rational(Fraction(c * factorial(k), 2 * rate ** (k + 1)))
        else:
            raise ValueError(f"未知模板 {template!r}")
        task["reference"] = ref
        self.reference_by_statement[task["statement"]] = _answer_text(task)
        return ref

    def solver_view(self, task: dict) -> dict:
        fmt = ("integer only" if task["type"] == "det"
               else "integer, fraction or finite decimal")
        return {"type": task["type"], "statement": task["statement"],
                "answer_format": fmt}

    def signature(self, task: dict) -> str:
        params = task["params"]
        def canon(v: Any) -> str:
            return json.dumps(v, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) if isinstance(v, list) else str(v)
        body = "|".join(f"{k}={canon(params[k])}" for k in sorted(params))
        return f"{task['template']}|{body}"

    def judge(self, task: dict, raw_answer: str | None) -> str:
        # 契约 §6：入参是「模型的整段原始返回」，judge 自己做 json.loads。
        # B4 最初写成接收裸值 "3/4"，与 A 的真实现口径不同——集成时发现，
        # 已按契约改正（裸值会让 json.loads 失败，全对的一代判成全 unanswered）。
        if not isinstance(raw_answer, str):
            return "unanswered"
        try:
            payload = json.loads(raw_answer)
        except ValueError:
            return "unanswered"
        if not isinstance(payload, dict) or set(payload) != {"value"}:
            return "unanswered"
        if not isinstance(payload["value"], str):
            return "unanswered"
        s = payload["value"].strip()
        if not s or len(s) > 100:
            return "unanswered"
        ref = task["reference"]

        if task["type"] == "det":
            if not re.fullmatch(r"[+-]?[0-9]+", s):
                return "unanswered"
            return "right" if int(s) == int(ref["value"]) else "wrong"

        target = Fraction(int(ref["numerator"]), int(ref["denominator"]))
        used_decimal = False
        if re.fullmatch(r"[+-]?[0-9]+", s):
            value = Fraction(int(s))
        elif re.fullmatch(r"[+-]?[0-9]+/[0-9]+", s):
            num, den = s.split("/")
            if int(den) <= 0:
                return "unanswered"
            value = Fraction(int(num), int(den))
        elif re.fullmatch(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?", s) \
                and ("." in s or "e" in s.lower()):
            used_decimal = True
            try:
                value = Fraction(Decimal(s))
            except Exception:
                return "unanswered"
        else:
            return "unanswered"

        if value == target:
            return "right"
        if used_decimal and abs(value - target) <= Fraction(Decimal("1e-8")):
            return "right"
        return "wrong"


# --------------------------------------------------------------------------
# 数据集（规格 §5）
# --------------------------------------------------------------------------

class FakeDataset:
    """common_dev 按代分 20 组、每组 integral=2 / det=2；挑战池 5 道。

    参数用「代 + 序号」的确定式推出，不消耗 RNG，保证同 seed 下两次构造完全一致。
    """

    def __init__(self, engine: FakeEngine, *, n_gens: int = 20,
                 n_pool: int = 5) -> None:
        self.engine = engine
        self._common: dict[int, list[dict]] = {}
        self._pool: list[dict] = []
        for g in range(1, n_gens + 1):
            self._common[g] = self._build_common(g)
        for i in range(n_pool):
            raw = {"template": "det",
                   "params": {"matrix": [[2 + i, 1], [1, 3 + i]]}}
            self._pool.append(self._make(raw, f"C{i + 1:02d}"))

    def _make(self, raw: dict, task_id: str) -> dict:
        task = self.engine.normalize_task(raw, task_id=task_id,
                                          source="dataset", by=None, gen=None)
        task["reference"] = self.engine.solve_exact(task)
        return task

    def _build_common(self, gen: int) -> list[dict]:
        out = []
        for i in range(2):
            n = 1 + (gen + i) % 9
            raw = {"template": "poly",
                   "params": {"coeffs": [gen % 5 + 1, -(i + 1), 2],
                              "lower": -2, "upper": 2 + i}}
            out.append(self._make(raw, f"D{gen:02d}_{i + 1}"))
        for i in range(2):
            d = (gen + i) % 3
            if d == 0:
                matrix = [[1 + gen % 4, 2], [3, 5]]
            elif d == 1:
                matrix = [[2, 1], [1, 3 + gen % 5]]
            else:
                matrix = [[gen % 5 + 2, 0], [1, 2]]
            raw = {"template": "det", "params": {"matrix": matrix}}
            out.append(self._make(raw, f"D{gen:02d}_{i + 3}"))
        return out

    def common_dev(self, gen: int) -> list[dict]:
        key = (gen - 1) % len(self._common) + 1
        return [dict(t) for t in self._common[key]]

    def challenge_pool(self) -> list[dict]:
        return [dict(t) for t in self._pool]

    def signatures(self) -> set[str]:
        sigs = {t["signature"] for ts in self._common.values() for t in ts}
        sigs |= {t["signature"] for t in self._pool}
        return sigs


# --------------------------------------------------------------------------
# 可控的模型响应
# --------------------------------------------------------------------------

# 出题响应的行为
SET_OK = "ok"
SET_BAD_JSON = "bad_json"
SET_BAD_SCHEMA = "bad_schema"         # JSON 合法但字段/参数非法
SET_TRANSPORT = "transport"

# 作答响应的行为
ANS_OK = "ok"
ANS_WRONG = "wrong"                   # 格式合法、值错
ANS_BAD_JSON = "bad_json"
ANS_WRONG_TYPE = "wrong_type"         # {"value": 7}，value 不是字符串
ANS_TRANSPORT = "transport"


def _stable_pick(slot_id: str, mod: int) -> int:
    return int(hashlib.sha256(slot_id.encode("utf-8")).hexdigest()[:8], 16) % mod


# 进程级计数器：跨 ScriptedChat 实例连续发号，保证同一趟里恒不撞签名。
# 若某个测试新建了 chat 对象而计数器归零，第 2 代就会与第 1 代撞重复，
# 触发整代重做，把「重做机制」误当成「调度出错」。
_ISSUED = itertools.count()
_GAMMA_SPACE = 720          # 10 种 c × 9 种 n × 8 种 rate，双射不可重复


class ScriptedChat:
    """按脚本返回三态响应，并记录每一次调用。

    `setter_plan` 键为个体 ID，值为按调用序号消费的行为列表；用尽后按
    `setter_default` 继续。`answer_policy` 接收 slot_id 返回是否答对。
    """

    def __init__(self, cfg: Config, engine: FakeEngine, *,
                 setter_plan: Mapping[str, list[str]] | None = None,
                 setter_default: str = SET_OK,
                 answer_policy: Callable[[str, str], str] | None = None,
                 usage: Mapping[str, int] | None = None,
                 latency_ms: int = 5) -> None:
        self.cfg = cfg
        self.engine = engine
        self.setter_plan = {k: list(v) for k, v in (setter_plan or {}).items()}
        self.setter_default = setter_default
        self.answer_policy = answer_policy
        self.usage = dict(usage or {"input_tokens": 100, "output_tokens": 20,
                                    "total_tokens": 120, "reasoning_tokens": None})
        self.latency_ms = latency_ms
        self.calls: list[dict] = []
        self._seq = 0
        self._per_id: dict[str, int] = {}

    # ---- 计数辅助（测试直接用） ----

    def count(self, role: str | None = None, individual_id: str | None = None) -> int:
        rows = self.calls
        if role is not None:
            rows = [c for c in rows if c["role"] == role]
        if individual_id is not None:
            rows = [c for c in rows if c["individual_id"] == individual_id]
        return len(rows)

    def _result(self, role: str, slot_id: str | None, status: str,
                text: str | None = None, parsed: dict | None = None,
                parse_error: str | None = None, attempt: int = 1) -> CallResult:
        self._seq += 1
        cfg = self.cfg
        return CallResult(
            call_id=f"{slot_id or role}_a{attempt}_{self._seq:06d}",
            role=role,
            model=cfg.model,
            base_url=cfg.base_url,
            temperature={"solver": cfg.solver_temperature,
                         "setter": cfg.setter_temperature,
                         "operator": cfg.operator_temperature}[role],
            max_tokens={"solver": cfg.max_tokens_solver,
                        "setter": cfg.max_tokens_setter,
                        "operator": cfg.max_tokens_operator}[role],
            status=status, text=text, parsed=parsed, parse_error=parse_error,
            usage=dict(self.usage) if status == "ok" else {},
            attempt=attempt, attempts=attempt, latency_ms=self.latency_ms,
            error=None if status == "ok" else "scripted",
            slot_id=slot_id,
        )

    # ---- 出题 ----

    def _next_task_params(self) -> dict:
        """10×9×8 = 720 组 gamma 参数，按序号双射取用，保证整趟不撞签名。"""
        m = next(_ISSUED)
        if m >= _GAMMA_SPACE:
            raise AssertionError(
                f"替身参数空间（{_GAMMA_SPACE} 组）已用尽；"
                "计数是进程级累加的，说明本进程发出的题数超过了设计上限"
            )
        cs = [1, -1, 2, -2, 3, -3, 4, -4, 5, -5]
        return {"template": "gamma",
                "params": {"c": cs[m % 10], "n": 1 + (m // 10) % 9,
                           "rate": 1 + (m // 90) % 8}}

    def _setter(self, messages: Sequence[Mapping[str, str]], slot_id: str,
                attempt: int) -> CallResult:
        ind_id = slot_id.split("_set_", 1)[1]
        idx = self._per_id.get(ind_id, 0)
        self._per_id[ind_id] = idx + 1
        plan = self.setter_plan.get(ind_id, [])
        behavior = plan[idx] if idx < len(plan) else self.setter_default

        self.calls[-1]["occupied_block"] = "\n{" in messages[-1]["content"]

        if isinstance(behavior, dict):        # 字面 raw，用于构造撞重复
            return self._result("setter", slot_id, "ok",
                                text=json.dumps(behavior), parsed=behavior,
                                attempt=attempt)
        if behavior == SET_TRANSPORT:
            return self._result("setter", slot_id, "transport_error", attempt=attempt)
        if behavior == SET_BAD_JSON:
            return self._result("setter", slot_id, "ok", text="这道题我出不了",
                                parse_error="no_object", attempt=attempt)
        if behavior == SET_BAD_SCHEMA:
            illegal = {"template": "gamma", "params": {"n": 0, "rate": 2, "c": 1}}
            return self._result("setter", slot_id, "ok", text=json.dumps(illegal),
                                parsed=illegal, attempt=attempt)
        raw = self._next_task_params()
        return self._result("setter", slot_id, "ok", text=json.dumps(raw),
                            parsed=raw, attempt=attempt)

    # ---- 作答 ----

    def _solver(self, messages: Sequence[Mapping[str, str]], slot_id: str,
                attempt: int) -> CallResult:
        user = messages[-1]["content"]
        statement = user.split("\n答案格式：")[0]
        answer = self.engine.reference_by_statement.get(statement)
        behavior = (self.answer_policy(slot_id, statement)
                    if self.answer_policy else (ANS_OK if _stable_pick(slot_id, 2) else ANS_BAD_JSON))
        if behavior == ANS_TRANSPORT:
            return self._result("solver", slot_id, "transport_error", attempt=attempt)
        if behavior == ANS_BAD_JSON:
            return self._result("solver", slot_id, "ok", text="我算出来是 3",
                                parse_error="no_object", attempt=attempt)
        if behavior == ANS_WRONG:
            payload = {"value": "999999"}
            return self._result("solver", slot_id, "ok", text=json.dumps(payload),
                                parsed=payload, attempt=attempt)
        if behavior == ANS_WRONG_TYPE:
            return self._result("solver", slot_id, "ok",
                                text='{"value": 3}', parsed={"value": 3},
                                attempt=attempt)
        if answer is None:
            raise AssertionError(f"题面未在引擎登记参考值: {statement!r}")
        payload = {"value": answer}
        return self._result("solver", slot_id, "ok", text=json.dumps(payload),
                            parsed=payload, attempt=attempt)

    async def chat(self, role: str, messages: Sequence[Mapping[str, str]], *,
                   slot_id: str | None = None, attempt: int = 1) -> CallResult:
        ind_id = None
        if slot_id and "_set_" in slot_id:
            ind_id = slot_id.split("_set_", 1)[1]
        elif slot_id:
            ind_id = slot_id.rsplit("_", 1)[-1]
        self.calls.append({"role": role, "slot_id": slot_id,
                           "individual_id": ind_id, "attempt": attempt})
        if role == "setter":
            return self._setter(messages, slot_id or "setter", attempt)
        if role == "solver":
            return self._solver(messages, slot_id or "solver", attempt)
        raise AssertionError(f"B4 替身未实现 {role} 角色")


# --------------------------------------------------------------------------
# 状态构造
# --------------------------------------------------------------------------

def build_state(cfg: Config, *, run_id: str = "test_run",
                seeds_path: str = "prompts/seed_genotypes.json",
                ancestors: Sequence[Individual] | None = None) -> Any:
    """按大纲 §8：初代六人，祖先取初代前三（B4 阶段无 calibration 数据）。"""
    import random

    from app.orchestrator import RunState

    people = load_seed_genotypes(seeds_path, max_chars=cfg.max_genotype_chars)
    rng = random.Random(cfg.seed)
    anc = list(ancestors) if ancestors is not None else [Individual(genes=p.genes,
                                                                    meta=p.meta)
                                                         for p in people[:cfg.n_ancestor]]
    return RunState(
        run_id=run_id,
        cfg=cfg,
        rng=rng,
        next_population=people,
        ancestors=anc,
        gene_history=GeneHistory(),
        dedup_set=set(),
        challenges={},
    )


def seed_dedup(state: Any, dataset: FakeDataset,
               challenge_ids: Sequence[str] = ("C01", "C02", "C03", "C04", "C05")) -> None:
    """把四类数据集的签名全部放进 dedup_set；五道挑战题全部占位（规格 §2.3）。"""
    state.dedup_set |= dataset.signatures()
    state.challenges = {
        cid: Challenge(task_id=cid, born_gen=born, window=state.cfg.challenge_window)
        for cid, born in zip(challenge_ids, state.cfg.challenge_gens)
    }


# --------------------------------------------------------------------------
# 重组器替身
# --------------------------------------------------------------------------

class FakeOperators:
    """只用于验证「E/A 走重组、F 走克隆」这条分派，不调用模型。

    真正的 `app.genotype.Operators`（精英 3 + 交叉 2 + 变异 1、两条重试路径、
    fallback_clone）已在 test_genotype.py 里单独测过，这里不重复。
    """

    def __init__(self, cfg: Config) -> None:
        from app.genotype import BreedResult, BreedStats, Genes, Meta, child_id

        self.cfg = cfg
        self._BreedResult = BreedResult
        self._BreedStats = BreedStats
        self._Genes = Genes
        self._Meta = Meta
        self._child_id = child_id
        self.calls: list[int] = []
        self._serial = 0

    def _novel_genes(self) -> "Any":
        self._serial += 1
        return self._Genes(f"子代策略 {self._serial}：先分类再精算。",
                           f"子代偏好 {self._serial}：优先 gamma。", True)

    async def breed(self, *, gen: int, ranked: Sequence[Individual]) -> "Any":
        cfg = self.cfg
        self.calls.append(gen)
        elites = list(ranked[:cfg.n_survive])
        population = [Individual(genes=e.genes, meta=e.meta) for e in elites]
        for i in range(cfg.n_crossover):
            population.append(Individual(
                genes=self._novel_genes(),
                meta=self._Meta(id=self._child_id(gen, "crossover", i + 1),
                                parents=(elites[0].id, elites[1 % len(elites)].id),
                                origin="crossover", born_gen=gen)))
        for i in range(cfg.n_mutate):
            population.append(Individual(
                genes=self._novel_genes(),
                meta=self._Meta(id=self._child_id(gen, "mutate", i + 1),
                                parents=(elites[-1].id,), origin="mutate",
                                born_gen=gen)))
        stats = self._BreedStats(elite_ids=[e.id for e in elites],
                                 children=cfg.n_crossover + cfg.n_mutate)
        return self._BreedResult(population=population, stats=stats)
