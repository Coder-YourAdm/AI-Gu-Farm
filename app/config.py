"""协议参数与预算。

唯一来源是锁定后的 `config.json`（技术规格 §1）。本模块只做三件事：
1. 严格校验字段（未知键报错、int 必须是真 int、不偷偷修正越界值）；
2. 提供结构不变量的断言，避免用改坏的参数继续套公式；
3. 由配置**推导**调用数，dry-run 不硬编码任何数字（规格 §9）。

B2 交付物之一。对应分工方案 v7.3 B2 第 1 条。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "5.1"

# 数据侧常量：技术规格 §5「预生成 ... holdout 20」。
# 不进 config.json（它不是可调参数），留出评测调用数需要它。
N_HOLDOUT = 20


class ConfigError(ValueError):
    """配置非法。永远不要吞掉它改成默认值。"""


class MissingConfigError(ConfigError):
    """字段合法但还不足以发起真实调用（占位符未替换 / 密钥未设置）。"""


# --------------------------------------------------------------------------
# 字段声明：名字 -> (类型标签, 是否必须)
# 名字与 config.json 逐字一致，顺序即落盘顺序。
# --------------------------------------------------------------------------

_FIELD_SPECS: list[tuple[str, str, bool]] = [
    ("schema_version", "str", True),
    ("condition", "str", True),
    ("seed", "int", True),
    ("data_seed", "int", True),
    ("model", "str", True),
    ("base_url", "str", True),
    ("api_key_env", "str", True),
    ("n_pop", "int", True),
    ("n_gen", "int", True),
    ("n_ancestor", "int", True),
    ("n_common", "int", True),
    ("n_survive", "int", True),
    ("n_crossover", "int", True),
    ("n_mutate", "int", True),
    ("set_weight", "num", True),
    ("bounty", "num", True),
    ("fitness_window", "int", True),
    ("challenge_gens", "int_list", True),
    ("challenge_window", "int", True),
    ("threshold_correct", "int", True),
    ("solver_temperature", "num", True),
    ("setter_temperature", "num", True),
    ("operator_temperature", "num", True),
    ("max_tokens_solver", "int", True),
    ("max_tokens_setter", "int", True),
    ("max_tokens_operator", "int", True),
    ("concurrency", "int", True),
    ("network_retries", "int", True),
    ("timeout_s", "num", True),
    ("task_attempts", "int", True),
    ("refetch_rounds", "int", True),
    ("attach_occupied_params", "bool", True),
    ("operator_attempts", "int", True),
    ("max_genotype_chars", "int", True),
    ("max_response_chars", "int", True),
    ("max_answer_chars", "int", True),
    ("max_number_digits", "int", True),
    ("decimal_exp_abs_max", "int", True),
    ("decimal_tolerance", "str", True),
    ("budget_currency", "opt_str", True),
    ("budget_amount", "opt_num", True),
    ("wall_limit_s", "opt_num", True),
]

FIELD_NAMES: tuple[str, ...] = tuple(name for name, _, _ in _FIELD_SPECS)

# v5 固定的结构参数。改了这些，槽位公式与 scoring 的 assert 会同时对不上，
# 所以直接拒绝，而不是"尽力兼容"（技术规格 §1 末段）。
_STRUCTURAL = {"n_pop": 6, "n_common": 4, "n_ancestor": 3}

_VALID_CONDITIONS = ("E", "F", "A")

# 占位符（大写 REPLACE_WITH 前缀）一旦留在配置里，禁止发起真实调用。
_PLACEHOLDER_MARKERS = ("REPLACE_WITH",)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_type(name: str, kind: str, value: Any) -> Any:
    """按声明校验单个值，返回规范化后的值。不做截断、不做四舍五入。"""
    if kind == "int":
        # type(v) is int 同时排除 bool 与 6.0（bool 是 int 的子类）
        if type(value) is not int:
            raise ConfigError(f"{name} 必须是 int（当前 {type(value).__name__}: {value!r}）")
        return value

    if kind == "num":
        if not _is_num(value):
            raise ConfigError(f"{name} 必须是 int 或 float（当前 {type(value).__name__}: {value!r}）")
        return float(value)

    if kind == "bool":
        if type(value) is not bool:
            raise ConfigError(f"{name} 必须是 bool（当前 {type(value).__name__}: {value!r}）")
        return value

    if kind == "str":
        if type(value) is not str:
            raise ConfigError(f"{name} 必须是 str（当前 {type(value).__name__}: {value!r}）")
        return value

    if kind == "opt_str":
        if value is None:
            return None
        if type(value) is not str:
            raise ConfigError(f"{name} 必须是 str 或 null（当前 {type(value).__name__}）")
        return value

    if kind == "opt_num":
        if value is None:
            return None
        if not _is_num(value):
            raise ConfigError(f"{name} 必须是数值或 null（当前 {type(value).__name__}）")
        return float(value)

    if kind == "int_list":
        if type(value) is not list:
            raise ConfigError(f"{name} 必须是 list[int]（当前 {type(value).__name__}）")
        out = []
        for i, item in enumerate(value):
            if type(item) is not int:
                raise ConfigError(f"{name}[{i}] 必须是 int（当前 {type(item).__name__}: {item!r}）")
            out.append(item)
        return out

    raise AssertionError(f"未声明的类型标签: {kind}")


@dataclass(frozen=True)
class Config:
    """已锁定的协议参数。字段顺序与 config.json 一致。"""

    schema_version: str
    condition: str
    seed: int
    data_seed: int
    model: str
    base_url: str
    api_key_env: str
    n_pop: int
    n_gen: int
    n_ancestor: int
    n_common: int
    n_survive: int
    n_crossover: int
    n_mutate: int
    set_weight: float
    bounty: float
    fitness_window: int
    challenge_gens: list[int]
    challenge_window: int
    threshold_correct: int
    solver_temperature: float
    setter_temperature: float
    operator_temperature: float
    max_tokens_solver: int
    max_tokens_setter: int
    max_tokens_operator: int
    concurrency: int
    network_retries: int
    timeout_s: float
    task_attempts: int
    refetch_rounds: int
    attach_occupied_params: bool
    operator_attempts: int
    max_genotype_chars: int
    max_response_chars: int
    max_answer_chars: int
    max_number_digits: int
    decimal_exp_abs_max: int
    decimal_tolerance: str
    budget_currency: str | None
    budget_amount: float | None
    wall_limit_s: float | None
    # 非 JSON 字段：只用于提示，不参与指纹
    source_path: str | None = field(default=None, compare=False)

    # ---------------- 构造 ----------------

    @classmethod
    def from_dict(cls, raw: dict, *, source_path: str | None = None) -> "Config":
        if type(raw) is not dict:
            raise ConfigError(f"config 顶层必须是 JSON 对象，当前 {type(raw).__name__}")

        known = set(FIELD_NAMES)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ConfigError(f"出现未知键: {unknown}；允许的键: {list(FIELD_NAMES)}")
        missing = [name for name, _, required in _FIELD_SPECS if required and name not in raw]
        if missing:
            raise ConfigError(f"缺少必填键: {missing}")

        values = {}
        for name, kind, _ in _FIELD_SPECS:
            values[name] = _check_type(name, kind, raw[name])

        cfg = cls(**values, source_path=source_path)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"配置文件不存在: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{p} 不是合法 JSON: {e}") from e
        return cls.from_dict(raw, source_path=str(p))

    # ---------------- 校验 ----------------

    def validate(self) -> None:
        """结构与语义不变量。任一不满足就直接失败，不降级。"""
        if self.schema_version != SCHEMA_VERSION:
            raise ConfigError(f"schema_version 必须是 {SCHEMA_VERSION}，当前 {self.schema_version!r}")

        if self.condition not in _VALID_CONDITIONS:
            raise ConfigError(f"condition 必须是 {_VALID_CONDITIONS} 之一，当前 {self.condition!r}")

        for name, expected in _STRUCTURAL.items():
            actual = getattr(self, name)
            if actual != expected:
                raise ConfigError(
                    f"{name} 在 v5 固定为 {expected}，当前 {actual}；"
                    "改它会同时破坏槽位公式与 scoring 的 assert"
                )

        if self.n_gen < 1:
            raise ConfigError(f"n_gen 必须 >= 1，当前 {self.n_gen}")
        if not (self.n_survive >= 1 and self.n_crossover >= 0 and self.n_mutate >= 0):
            raise ConfigError("n_survive/n_crossover/n_mutate 不能为负，n_survive 至少 1")
        if self.n_survive + self.n_crossover + self.n_mutate != self.n_pop:
            raise ConfigError(
                "n_survive+n_crossover+n_mutate 必须等于 n_pop"
                f"（{self.n_survive}+{self.n_crossover}+{self.n_mutate}"
                f" != {self.n_pop}）"
            )
        if self.fitness_window < 1:
            raise ConfigError(f"fitness_window 必须 >= 1，当前 {self.fitness_window}")

        if self.set_weight < 0:
            raise ConfigError(f"set_weight 不能为负，当前 {self.set_weight}")
        if self.condition == "A" and self.set_weight != 0:
            # 技术规格 §1：condition=A 必须 set_weight=0
            raise ConfigError(f"condition=A 要求 set_weight=0，当前 {self.set_weight}")
        if self.bounty < 0:
            raise ConfigError(f"bounty 不能为负，当前 {self.bounty}")

        for tname in ("solver_temperature", "setter_temperature", "operator_temperature"):
            t = getattr(self, tname)
            if not (0.0 <= t <= 2.0):
                raise ConfigError(f"{tname} 必须在 [0, 2]，当前 {t}")

        for tname in ("max_tokens_solver", "max_tokens_setter", "max_tokens_operator"):
            if getattr(self, tname) <= 0:
                raise ConfigError(f"{tname} 必须 > 0，当前 {getattr(self, tname)}")

        if self.concurrency < 1:
            raise ConfigError(f"concurrency 必须 >= 1，当前 {self.concurrency}")
        if self.network_retries < 0:
            raise ConfigError(f"network_retries 不能为负，当前 {self.network_retries}")
        if self.timeout_s <= 0:
            raise ConfigError(f"timeout_s 必须 > 0，当前 {self.timeout_s}")
        if self.task_attempts < 1:
            raise ConfigError(f"task_attempts 必须 >= 1，当前 {self.task_attempts}")
        if self.refetch_rounds < 0:
            raise ConfigError(f"refetch_rounds 不能为负，当前 {self.refetch_rounds}")
        if self.operator_attempts < 1:
            raise ConfigError(f"operator_attempts 必须 >= 1，当前 {self.operator_attempts}")

        if self.max_genotype_chars < 1:
            raise ConfigError(f"max_genotype_chars 必须 >= 1，当前 {self.max_genotype_chars}")
        for name in ("max_response_chars", "max_answer_chars", "max_number_digits"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} 必须 >= 1，当前 {getattr(self, name)}")
        if self.decimal_exp_abs_max < 0:
            raise ConfigError(f"decimal_exp_abs_max 不能为负，当前 {self.decimal_exp_abs_max}")

        # decimal_tolerance 必须是可转 Fraction 的正数（判分用 Fraction，不用 float）
        tol = self.tolerance_fraction
        if tol <= 0:
            raise ConfigError(f"decimal_tolerance 必须 > 0，当前 {self.decimal_tolerance!r}")

        if self.challenge_window < 1:
            raise ConfigError(f"challenge_window 必须 >= 1，当前 {self.challenge_window}")
        if self.threshold_correct < 1:
            raise ConfigError(f"threshold_correct 必须 >= 1，当前 {self.threshold_correct}")
        self._validate_challenge_gens()

        if not self.model.strip():
            raise ConfigError("model 不能为空")
        if not self.base_url.strip():
            raise ConfigError("base_url 不能为空")
        if not self.api_key_env.strip():
            raise ConfigError("api_key_env 不能为空（只写环境变量名，不写密钥本身）")
        if self.budget_amount is not None and self.budget_amount <= 0:
            raise ConfigError(f"budget_amount 必须 > 0 或为 null，当前 {self.budget_amount}")
        if self.wall_limit_s is not None and self.wall_limit_s <= 0:
            raise ConfigError(f"wall_limit_s 必须 > 0 或为 null，当前 {self.wall_limit_s}")

    def _validate_challenge_gens(self) -> None:
        gens = self.challenge_gens
        if gens != sorted(gens):
            raise ConfigError(f"challenge_gens 必须递增，当前 {gens}")
        if len(set(gens)) != len(gens):
            raise ConfigError(f"challenge_gens 不能重复，当前 {gens}")
        for born in gens:
            if not (1 <= born <= self.n_gen):
                raise ConfigError(f"challenge_gens 中的 {born} 超出 1..{self.n_gen}")
            # 技术规格 §6.3：CLI 创建协议时检查 born+window-1 <= n_gen
            if born + self.challenge_window - 1 > self.n_gen:
                raise ConfigError(
                    f"挑战在第 {born} 代出生、窗口 {self.challenge_window} 代，"
                    f"需要 n_gen >= {born + self.challenge_window - 1}，当前 {self.n_gen}"
                )
        for a, b in zip(gens, gens[1:]):
            if b - a < self.challenge_window:
                raise ConfigError(
                    f"相邻挑战出生间隔必须 >= window({self.challenge_window})，"
                    f"当前 {a} 与 {b} 间隔 {b - a}（两窗口不能重叠）"
                )

    # ---------------- 派生量 ----------------

    @property
    def tolerance_fraction(self) -> Fraction:
        try:
            return Fraction(Decimal(self.decimal_tolerance))
        except (InvalidOperation, ValueError) as e:
            raise ConfigError(f"decimal_tolerance 无法解析: {self.decimal_tolerance!r}") from e

    @property
    def checkpoints(self) -> list[int]:
        """留出评测检查点：{0} ∪ {5,10,...} <= G ∪ {G}（技术规格 §9）。"""
        pts = {0, self.n_gen}
        step = 5
        k = step
        while k < self.n_gen:
            pts.add(k)
            k += step
        return sorted(pts)

    @property
    def n_checkpoints(self) -> int:
        return len(self.checkpoints)

    def require_runtime_ready(self) -> None:
        """真正发请求前的最后一道闸。dry-run 不调用它。"""
        for name in ("model", "base_url"):
            value = getattr(self, name)
            if any(marker in value for marker in _PLACEHOLDER_MARKERS):
                raise MissingConfigError(
                    f"{name} 还是占位符 {value!r}；真实运行前必须替换为冻结值"
                )
        if self.budget_amount is None and self.wall_limit_s is None:
            raise MissingConfigError(
                "budget_amount 与 wall_limit_s 都是 null；"
                "正式运行要求填写费用上限，或明确选择仅按时间/调用上限控制"
            )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("source_path", None)
        return d

    def semantic_fingerprint(self) -> str:
        """锁协议用：全部语义字段的 SHA-256。B8 写入 protocol.json。"""
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 调用数推导（技术规格 §9 的两式，不硬编码任何数字）
# --------------------------------------------------------------------------

def per_gen_calls(cfg: Config) -> int:
    """单代常规调用：出题 + 互答 + 共同卷 + 祖先。"""
    return (
        cfg.n_pop                          # 出题
        + cfg.n_pop * (cfg.n_pop - 1)      # 互答，每人对其他 5 人各 1 次
        + cfg.n_pop * cfg.n_common         # 共同开发卷
        + cfg.n_ancestor * cfg.n_pop       # 祖先答演化题，M=n_pop
    )


def challenge_calls(cfg: Config) -> int:
    """挑战槽位总数；满窗口上界，按实际剩余代数封顶。"""
    total = 0
    for born in cfg.challenge_gens:
        span = min(cfg.challenge_window, cfg.n_gen - born + 1)
        total += span * cfg.n_pop
    return total


def holdout_calls(cfg: Config, n_holdout: int = N_HOLDOUT) -> int:
    return cfg.n_checkpoints * cfg.n_pop * n_holdout


def refetch_reserve(cfg: Config) -> int:
    """失败时才发生的出题重做成本，单列，不并入主数。"""
    return cfg.n_gen * cfg.refetch_rounds * cfg.n_pop


def call_plan(cfg: Config, n_holdout: int = N_HOLDOUT) -> dict:
    """按生效配置算四栏调用数。dry-run 与附录 A 核对表共用这一份实现。"""
    per = per_gen_calls(cfg)
    chal = challenge_calls(cfg)
    hold = holdout_calls(cfg, n_holdout)
    operators = (cfg.n_gen - 1) * (cfg.n_crossover + cfg.n_mutate)  # 末代不产生子代

    def _cond(c: str) -> int:
        base = cfg.n_gen * per + chal + hold
        return base + (operators if c in ("E", "A") else 0)

    return {
        "per_gen": per,
        "challenge_slots": chal,
        "holdout_slots": hold,
        "holdout_per_run": hold,
        "n_checkpoints": cfg.n_checkpoints,
        "n_holdout": n_holdout,
        "operator_calls": operators,
        "E": _cond("E"),
        "F": _cond("F"),
        "A": _cond("A"),
        "refetch_reserve": refetch_reserve(cfg),
    }
