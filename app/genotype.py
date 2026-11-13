"""基因、基因指纹、三算子。

对应分工方案 v7.3 B3。这一层只做四件事：

1. **基因身份**：`Genes` 三字段 + `gene_key`（规范 JSON 的 SHA-256）。
   A3 的 `est_score` 直接 import 这里的 `gene_key` 与 `GeneHistory`，
   **不要另写一份**——两份实现漂移会让「同一基因」判成两个。
2. **初代加载与断言**：六份种子必须满足长度、两两不同、开关 3:3。
3. **三算子**：精英原样保留、交叉 2、变异 1。只有 `Genes` 会变，`Meta` 不交给算子。
4. **self_check 生效**：拼 solver prompt 时按布尔追加固定句，写死在常量里。

一条容易踩的设计约束：**所有随机抽样在发起网络请求之前一次抽完**。
否则 RNG 的消费顺序会取决于网络回包先后，同一 seed 两次跑出的种群不同。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .config import Config

# --------------------------------------------------------------------------
# 常量：与 prompts/ 里的固定文本保持一致
# --------------------------------------------------------------------------

# 技术规格 §3.3：self_check=true 时的固定追加句，不能让模型自己选。
CHECK_INSTRUCTION = "在内部检查符号、数量级和算术，再给最终答案。"

SOLVER_TEMPLATE = (
    "求解给定数学题。只输出 {{\"value\":\"...\"}}，不要解释或代码块。\n"
    "定积分答案写整数或 p/q；若用小数，保留至少十位有效数字。\n"
    "行列式只写整数。禁止函数表达式、多个候选、单位或等号。\n"
    "在内部执行下列解题策略：{solve_prompt}\n"
    "{check_instruction}"
)

VALID_ORIGINS = ("seed", "crossover", "mutate")
MUTABLE_FIELDS = ("solve_prompt", "set_prompt", "self_check")
_GENE_KEYS = frozenset(MUTABLE_FIELDS)


class GenotypeError(ValueError):
    """基因非法：长度、类型、算子输出不合规。"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Genes:
    """基因只有三个字段。任何成绩、历史、祖先标记都不在这里。"""

    solve_prompt: str
    set_prompt: str
    self_check: bool


@dataclass(frozen=True)
class Meta:
    """身份与出身。算子只能读，不能改。"""

    id: str
    parents: tuple[str, ...]
    origin: str
    born_gen: int


@dataclass
class Individual:
    """一个被评测的个体。`current_total`/`est`/`n_samples` 由代事务填写。"""

    genes: Genes
    meta: Meta
    current_total: float = 0.0
    est: float | None = None
    n_samples: int = 0

    @property
    def id(self) -> str:
        return self.meta.id


class ChatLike(Protocol):
    async def chat(self, role: str, messages: Sequence[Mapping[str, str]],
                   *, slot_id: str | None = ..., attempt: int = ...) -> Any: ...


# --------------------------------------------------------------------------
# 基因指纹
# --------------------------------------------------------------------------

def canonical_json(genes: Genes) -> str:
    """固定键序、无空白的规范 JSON。键序用 sort_keys，不依赖字段声明顺序。"""
    payload = {
        "self_check": genes.self_check,
        "set_prompt": genes.set_prompt,
        "solve_prompt": genes.solve_prompt,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def gene_key(genes: Genes) -> str:
    """三字段规范 JSON 的 SHA-256。同基因必同键，改一个字必不同。"""
    return hashlib.sha256(canonical_json(genes).encode("utf-8")).hexdigest()


def genotype_chars(genes: Genes) -> int:
    return len(genes.solve_prompt) + len(genes.set_prompt)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def validate_genes(genes: Genes, *, max_chars: int) -> None:
    if type(genes.solve_prompt) is not str or type(genes.set_prompt) is not str:
        raise GenotypeError("solve_prompt/set_prompt 必须是 str")
    if type(genes.self_check) is not bool:
        raise GenotypeError("self_check 必须是 bool（不接受 0/1/\"true\"）")
    if not genes.solve_prompt.strip():
        raise GenotypeError("solve_prompt 不能为空")
    if not genes.set_prompt.strip():
        raise GenotypeError("set_prompt 不能为空")
    n = genotype_chars(genes)
    if n > max_chars:
        raise GenotypeError(f"基因合计 {n} 字，超过上限 {max_chars}")


def parse_operator_output(obj: Any, *, max_chars: int) -> Genes:
    """算子返回必须恰好三个 gene 字段，多一个少一个都拒绝。"""
    if type(obj) is not dict:
        raise GenotypeError(f"算子输出必须是 JSON 对象，当前 {type(obj).__name__}")
    keys = set(obj)
    if keys != _GENE_KEYS:
        extra = sorted(keys - _GENE_KEYS)
        missing = sorted(_GENE_KEYS - keys)
        raise GenotypeError(f"算子输出字段不符：多余 {extra} 缺少 {missing}")
    genes = Genes(
        solve_prompt=obj["solve_prompt"],
        set_prompt=obj["set_prompt"],
        self_check=obj["self_check"],
    )
    validate_genes(genes, max_chars=max_chars)
    return genes


def child_id(gen: int, origin: str, index: int) -> str:
    """`G{出生代:02d}_{S|C|M}{序号}`。种子用 S，交叉 C，变异 M。"""
    tag = {"seed": "S", "crossover": "C", "mutate": "M"}[origin]
    return f"G{gen:02d}_{tag}{index}"


# --------------------------------------------------------------------------
# solver prompt 拼装（self_check 在此生效）
# --------------------------------------------------------------------------

def build_solver_system(genes: Genes) -> str:
    """按 §3.3 模板拼 system prompt；self_check=true 时末尾追加固定句。"""
    check = CHECK_INSTRUCTION if genes.self_check else ""
    text = SOLVER_TEMPLATE.format(solve_prompt=genes.solve_prompt,
                                  check_instruction=check)
    return text.rstrip("\n")


# 算子 system prompt。C 若交付 prompts/crossover.md、prompts/mutate.md，
# 启动时优先读文件；文件不在就用下面这份临时文本（内容按 §3.4 的要求写）。
CROSSOVER_SYSTEM = (
    "你在做两个解题策略的交叉。只输出一个 JSON 对象，恰好三个字段："
    "solve_prompt、set_prompt、self_check。\n"
    "要求：融合两段已有的可执行方法；两段冲突时选定具体一条，不要写\"两者结合\"；"
    "保留合理的出题偏好；不得新增题目模板或工具权限；"
    "solve_prompt 与 set_prompt 合计不超过 200 字；不要输出解释、答案或代码块。"
)

MUTATE_SYSTEM = (
    "你要在保留其余字段不变的前提下，修改指定的一个字段。"
    "只输出一个 JSON 对象，恰好三个字段：solve_prompt、set_prompt、self_check。\n"
    "要求：只有 field_to_mutate 指定的字段与母本不同，另外两个字段必须逐字相同；"
    "改字符串时只改一句、不得比母本长 10% 以上；改 self_check 时必须翻转真假；"
    "solve_prompt 与 set_prompt 合计不超过 200 字；不要输出解释或代码块。"
)


def load_operator_prompts(prompts_dir: str | Path | None) -> tuple[str, str]:
    """有 C 交付的 md 就用，没有就用内置临时版。返回 (crossover, mutate)。"""
    cross, mut = CROSSOVER_SYSTEM, MUTATE_SYSTEM
    if prompts_dir is not None:
        d = Path(prompts_dir)
        for name in ("crossover.md", "mutate.md"):
            p = d / name
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8").strip()
            if not text:
                continue
            if name == "crossover.md":
                cross = text
            else:
                mut = text
    return cross, mut

# --------------------------------------------------------------------------
# 初代加载
# --------------------------------------------------------------------------

def load_seed_genotypes(path: str | Path, *, max_chars: int = 200) -> list[Individual]:
    """加载六份种子并断言。任一不满足直接失败，不做修补。"""
    p = Path(path)
    if not p.exists():
        raise GenotypeError(f"种子文件不存在: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise GenotypeError(f"{p} 不是合法 JSON: {e}") from e
    if type(raw) is not list:
        raise GenotypeError("种子文件顶层必须是数组")

    people: list[Individual] = []
    for i, item in enumerate(raw, start=1):
        if type(item) is not dict or set(item) != {"genes", "meta"}:
            raise GenotypeError(f"第 {i} 份缺少 genes/meta 或多出字段")
        g = item["genes"]
        m = item["meta"]
        if type(g) is not dict or set(g) != _GENE_KEYS:
            raise GenotypeError(f"第 {i} 份 genes 字段不符")
        genes = Genes(g["solve_prompt"], g["set_prompt"], g["self_check"])
        validate_genes(genes, max_chars=max_chars)
        if type(m) is not dict or set(m) != {"id", "parents", "origin", "born_gen"}:
            raise GenotypeError(f"第 {i} 份 meta 字段不符")
        if m["origin"] != "seed":
            raise GenotypeError(f"第 {i} 份 origin 必须是 seed，当前 {m['origin']!r}")
        if m["parents"]:
            raise GenotypeError(f"第 {i} 份是种子，parents 必须为空")
        if type(m["born_gen"]) is not int or m["born_gen"] != 0:
            raise GenotypeError(f"第 {i} 份 born_gen 必须是 0")
        people.append(Individual(
            genes=genes,
            meta=Meta(id=m["id"], parents=(), origin="seed", born_gen=0),
        ))

    _assert_seed_invariants(people)
    return people


def _assert_seed_invariants(people: list[Individual]) -> None:
    if len(people) != 6:
        raise GenotypeError(f"初代必须 6 人，当前 {len(people)}")

    expected_ids = [child_id(0, "seed", i) for i in range(1, 7)]
    actual_ids = [p.id for p in people]
    if actual_ids != expected_ids:
        raise GenotypeError(f"初代 ID 必须是 {expected_ids}，当前 {actual_ids}")

    solve = [p.genes.solve_prompt for p in people]
    setters = [p.genes.set_prompt for p in people]
    if len(set(solve)) != 6:
        raise GenotypeError("六份 solve_prompt 必须两两不同（疑似复制粘贴抄重）")
    if len(set(setters)) != 6:
        raise GenotypeError("六份 set_prompt 必须两两不同")

    trues = sum(p.genes.self_check for p in people)
    if trues != 3:
        raise GenotypeError(f"self_check 必须是 3:3，当前 {trues}:{6 - trues}")

    keys = [gene_key(p.genes) for p in people]
    if len(set(keys)) != 6:
        raise GenotypeError("六份基因指纹出现重复")


# --------------------------------------------------------------------------
# 基因历史：测量记录，不是继承
# --------------------------------------------------------------------------

class GeneHistory:
    """以 `gene_key` 为键的重复测量记录。

    只在整代 `status=completed` 后追加。子代 `gene_key` 一变就从空列表开始，
    所以子代永远拿不到父代的样本——这是与 v4 被判掉的「成绩继承」的分界线。
    """

    def __init__(self, data: Mapping[str, Sequence[float]] | None = None):
        self._data: dict[str, list[float]] = {
            str(k): [float(x) for x in v] for k, v in (data or {}).items()
        }

    def append(self, genes: Genes, total: float) -> None:
        self._data.setdefault(gene_key(genes), []).append(float(total))

    def samples(self, genes: Genes) -> list[float]:
        return list(self._data.get(gene_key(genes), []))

    def n_samples(self, genes: Genes) -> int:
        return len(self._data.get(gene_key(genes), []))

    def estimate(self, genes: Genes, window: int, *, current_total: float) -> float:
        """规格 §6.1 的字面公式，**前提是当代样本已追加进历史**。

        代事务里请用 `estimate_including()`：它在「还没追加」时给出同一个值。
        直接在这里调用而忘了追加，`window>1` 会少算一代，降噪静默失效。
        """
        if window < 1:
            raise GenotypeError(f"fitness_window 必须 >= 1，当前 {window}")
        h = self._data.get(gene_key(genes), [])
        if not h:
            return float(current_total)
        tail = h[-window:]
        return sum(tail) / len(tail)

    def estimate_including(self, genes: Genes, window: int,
                           current_total: float) -> float:
        """把当代分当作已在窗口内的估计值，但**不写入**历史。

        规格 §6.1 的 `mean(hist[-window:])` 隐含「当代已完成并追加」。而追加必须
        等整代提交成功（§7：算子失败同样中止，aborted 代不进历史）。两者用这个方法
        调和：排序用的是「假如现在追加」的值，写入发生在 `status=completed` 之后。

        由此得到两条可直接断言的边界：
        - `window=1` 时结果恒等于 `current_total`；
        - 同一基因连续两代受测，第 2 代的估计是两次 `total` 的均值。
        """
        if window < 1:
            raise GenotypeError(f"fitness_window 必须 >= 1，当前 {window}")
        h = self._data.get(gene_key(genes), [])
        window_values = (h + [float(current_total)])[-window:]
        return sum(window_values) / len(window_values)

    def to_dict(self) -> dict[str, list[float]]:
        return {k: list(v) for k, v in self._data.items()}

    @classmethod
    def from_dict(cls, obj: Mapping[str, Sequence[float]]) -> "GeneHistory":
        return cls(obj)


# --------------------------------------------------------------------------
# 三算子
# --------------------------------------------------------------------------

@dataclass
class BreedStats:
    elite_ids: list[str] = field(default_factory=list)
    crossover_parents: list[list[str]] = field(default_factory=list)
    crossover_repeat: int = 0
    fallback_clone: int = 0
    mutation_field: str | None = None
    mutation_rejected: int = 0
    children: int = 0
    children_equal_to_parent: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def repeat_policy_rate(self) -> float:
        """子代基因与任一父代相同的比例。"""
        if self.children == 0:
            return 0.0
        return self.children_equal_to_parent / self.children

    def to_dict(self) -> dict:
        return {
            "elite_ids": self.elite_ids,
            "crossover_parents": self.crossover_parents,
            "crossover_repeat": self.crossover_repeat,
            "fallback_clone": self.fallback_clone,
            "mutation_field": self.mutation_field,
            "mutation_rejected": self.mutation_rejected,
            "children": self.children,
            "repeat_policy_rate": round(self.repeat_policy_rate, 4),
            "failures": self.failures,
        }


@dataclass
class BreedResult:
    population: list[Individual]
    stats: BreedStats


class Operators:
    """精英 3 + 交叉 2 + 变异 1。

    `ranked` 必须已按 `ranking_key` 排好（最好的在前），本类不做排序。
    """

    def __init__(self, cfg: Config, rng, chat: ChatLike, *,
                 prompts_dir: str | Path | None = "prompts"):
        self.cfg = cfg
        self.rng = rng
        self.chat = chat
        self.cross_system, self.mutate_system = load_operator_prompts(prompts_dir)

    # ---------------- 对外 ----------------

    async def breed(self, *, gen: int, ranked: Sequence[Individual]) -> BreedResult:
        if len(ranked) < self.cfg.n_survive:
            raise GenotypeError(
                f"可选精英不足：需要 {self.cfg.n_survive}，当前 {len(ranked)}"
            )
        elites = list(ranked[: self.cfg.n_survive])
        stats = BreedStats(elite_ids=[e.id for e in elites])

        # 先把所有随机决定一次抽完，再发请求。
        # 否则 RNG 消费顺序会取决于网络回包先后，同 seed 两次跑出不同种群。
        cross_plans = [self.rng.sample(elites, 2) for _ in range(self.cfg.n_crossover)]
        mother = self.rng.choice(elites) if self.cfg.n_mutate else None
        field = self.rng.choice(MUTABLE_FIELDS) if self.cfg.n_mutate else None
        fallback_picks = [self.rng.randrange(len(elites))
                          for _ in range(self.cfg.n_crossover + self.cfg.n_mutate)]

        population: list[Individual] = [Individual(genes=e.genes, meta=e.meta)
                                        for e in elites]

        for i, parents in enumerate(cross_plans, start=1):
            child = await self._crossover(gen, i, parents, fallback_picks[i - 1],
                                          elites, stats)
            population.append(child)

        if mother is not None and field is not None:
            child = await self._mutate(gen, 1, mother, field,
                                       fallback_picks[-1], elites, stats)
            population.append(child)

        if len(population) != self.cfg.n_pop:
            raise GenotypeError(
                f"重组后人数错误：{len(population)} != {self.cfg.n_pop}"
            )

        stats.children = self.cfg.n_crossover + self.cfg.n_mutate
        parent_keys = {gene_key(e.genes) for e in elites}
        stats.children_equal_to_parent = sum(
            1 for ind in population[self.cfg.n_survive:]
            if gene_key(ind.genes) in parent_keys
        )
        return BreedResult(population=population, stats=stats)

    # ---------------- 交叉 ----------------

    async def _crossover(self, gen: int, index: int, parents: Sequence[Individual],
                         fallback_pick: int, elites: Sequence[Individual],
                         stats: BreedStats) -> Individual:
        a, b = parents
        stats.crossover_parents.append([a.id, b.id])
        cid = child_id(gen, "crossover", index)
        messages = [{"role": "system", "content": self.cross_system},
                    {"role": "user", "content": json.dumps({
                        "parent_a": json.loads(canonical_json(a.genes)),
                        "parent_b": json.loads(canonical_json(b.genes)),
                    }, ensure_ascii=False, indent=2)}]

        for attempt in range(1, self.cfg.operator_attempts + 1):
            res = await self.chat.chat("operator", messages,
                                       slot_id=f"{cid}_cross_a{attempt}")
            if not getattr(res, "content_ok", False):
                stats.failures.append(f"{cid}_a{attempt}:{getattr(res, 'parse_error', None) or getattr(res, 'status', 'unknown')}")
                continue
            try:
                genes = parse_operator_output(res.parsed,
                                              max_chars=self.cfg.max_genotype_chars)
            except GenotypeError as e:
                stats.failures.append(f"{cid}_a{attempt}:schema:{e}")
                continue

            if genes == a.genes or genes == b.genes:
                # 交叉未发生：子代与某个父代完全一样，不能伪称成功
                stats.crossover_repeat += 1
                stats.failures.append(f"{cid}_a{attempt}:identical_to_parent")
                continue

            return Individual(
                genes=genes,
                meta=Meta(id=cid, parents=(a.id, b.id),
                          origin="crossover", born_gen=gen),
            )

        # 预登记回退：克隆精英补位，并如实记录
        stats.fallback_clone += 1
        source = elites[fallback_pick % len(elites)]
        stats.failures.append(f"{cid}:fallback_clone_from_{source.id}")
        return Individual(
            genes=source.genes,
            meta=Meta(id=cid, parents=(a.id, b.id),
                      origin="crossover", born_gen=gen),
        )

    # ---------------- 变异 ----------------

    async def _mutate(self, gen: int, index: int, mother: Individual, field_name: str,
                      fallback_pick: int, elites: Sequence[Individual],
                      stats: BreedStats) -> Individual:
        stats.mutation_field = field_name
        cid = child_id(gen, "mutate", index)
        messages = [{"role": "system", "content": self.mutate_system},
                    {"role": "user", "content": json.dumps({
                        "mother": json.loads(canonical_json(mother.genes)),
                        "field_to_mutate": field_name,
                    }, ensure_ascii=False, indent=2)}]

        for attempt in range(1, self.cfg.operator_attempts + 1):
            res = await self.chat.chat("operator", messages,
                                       slot_id=f"{cid}_mut_a{attempt}")
            if not getattr(res, "content_ok", False):
                stats.failures.append(f"{cid}_a{attempt}:{getattr(res, 'parse_error', None) or getattr(res, 'status', 'unknown')}")
                continue
            try:
                genes = parse_operator_output(res.parsed,
                                              max_chars=self.cfg.max_genotype_chars)
                validate_mutation(mother.genes, genes, field_name,
                                 max_chars=self.cfg.max_genotype_chars)
            except GenotypeError as e:
                stats.mutation_rejected += 1
                stats.failures.append(f"{cid}_a{attempt}:mutation:{e}")
                continue

            return Individual(
                genes=genes,
                meta=Meta(id=cid, parents=(mother.id,),
                          origin="mutate", born_gen=gen),
            )

        stats.fallback_clone += 1
        source = elites[fallback_pick % len(elites)]
        stats.failures.append(f"{cid}:fallback_clone_from_{source.id}")
        return Individual(
            genes=source.genes,
            meta=Meta(id=cid, parents=(mother.id,),
                      origin="mutate", born_gen=gen),
        )


# --------------------------------------------------------------------------
# 变异合法性
# --------------------------------------------------------------------------

_TERMINATORS = "。！？；!?;"


def _sentence_count(text: str) -> int:
    return sum(text.count(ch) for ch in _TERMINATORS)


def validate_mutation(mother: Genes, child: Genes, field_name: str, *,
                      max_chars: int) -> None:
    """恰好一个字段改变，且必须是设计好的那个。

    字符串规则（§3.1 / §3.4）：只改一句、基因合计 ≤ 上限且 ≤ 母本 1.1 倍、不新增句子。
    「只改一句」用「终止符数量不增加」做机器可判的近似：允许重写，不允许加句。
    """
    if field_name not in _GENE_KEYS:
        raise GenotypeError(f"field_to_mutate 非法: {field_name!r}")

    # 指定字段没动，先单独报——比笼统的「只能改」更容易定位
    if getattr(mother, field_name) == getattr(child, field_name):
        if field_name == "self_check":
            raise GenotypeError("self_check 必须真翻转，当前未变")
        raise GenotypeError(f"{field_name} 未改变，变异未发生")

    changed = [f for f in MUTABLE_FIELDS if getattr(mother, f) != getattr(child, f)]
    if changed != [field_name]:
        raise GenotypeError(
            f"必须且只能改 {field_name}，实际改动了 {changed or '无'}"
        )

    if field_name == "self_check":
        return

    old = getattr(mother, field_name)
    new = getattr(child, field_name)
    if not new.strip():
        raise GenotypeError(f"{field_name} 不能改成空字符串")
    if "\n" in new or "\r" in new:
        raise GenotypeError(f"{field_name} 不能含换行")
    # 1.1 倍看的是「基因合计长度」。只看单个字段会把短字段锁死：
    # 33 字的 set_prompt 按字段算只允许加 3 字，等于禁止变异。
    before, after = genotype_chars(mother), genotype_chars(child)
    if after * 10 > before * 11:
        raise GenotypeError(
            f"基因合计从 {before} 字增到 {after} 字，超过母本 1.1 倍"
        )
    if _sentence_count(new) > _sentence_count(old):
        raise GenotypeError(
            f"{field_name} 句子数从 {_sentence_count(old)} 增到 {_sentence_count(new)}，"
            "变异只允许改写、不允许加句"
        )


# --------------------------------------------------------------------------
# F 组：原样复制
# --------------------------------------------------------------------------

def clone_population(population: Sequence[Individual]) -> list[Individual]:
    """F 条件用：基因与 ID 原样复制，不调用算子。

    新建 Individual 对象是为了避免代记录之间共享可变字段，
    基因与 meta 本身是不可变 dataclass，可以安全共享。
    """
    return [Individual(genes=i.genes, meta=i.meta) for i in population]
