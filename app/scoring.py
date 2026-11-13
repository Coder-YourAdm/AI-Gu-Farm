"""评分与排名。

权威契约：`方案/技术规格_v5.md` §6.1（v5.1）。
规格给出的四个片段（scores / gene_key / est_score / ranking_key）在这里实现，
另加 rank_population（同分打散）与 gene_history 的存取。

三条铁律：
1. 只做算术：不读文件、不调用 judge、不知道题目内容。
2. `gene_history` 是**测量记录**，不是继承：gene_key 一变就从空列表开始，
   子代永远拿不到父代的样本。
3. 同分打散只能用调用方传入的 `random.Random`，禁止全局 `random`。
"""

import hashlib
import json

N_COMMON = 4      # 每人每代共同卷题数
N_PEER = 5        # 每人答其余 5 人的题
N_SURVIVE = 3     # 留 3 淘 3

# gene_key -> [历次当代 total]；只在 status == completed 的代追加
GENE_HISTORY: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# 取值助手：同时支持 dict 与对象（B 的 genotype 可能是 dataclass）
# ---------------------------------------------------------------------------

def _field(obj, name):
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def _cfg(cfg, name, default=None):
    """config 既可能是 dict 也可能是对象，统一取值。"""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def _genes_dict(genes) -> dict:
    """把基因规约成只含三个字段的 dict（多余元数据不参与 gene_key）。"""
    if isinstance(genes, dict):
        return {
            "solve_prompt": genes["solve_prompt"],
            "set_prompt": genes["set_prompt"],
            "self_check": genes["self_check"],
        }
    return {
        "solve_prompt": _field(genes, "solve_prompt"),
        "set_prompt": _field(genes, "set_prompt"),
        "self_check": _field(genes, "self_check"),
    }


# ---------------------------------------------------------------------------
# 分数
# ---------------------------------------------------------------------------

def count_right(slots) -> int:
    """数一数这批槽位里有几个 right。wrong / unanswered 都不计。"""
    return sum(1 for s in slots if _field(s, "status") == "right")


def scores(common_slots, peer_slots_for_own_task, challenge_slot, cfg):
    """返回 (solve, set_score, bounty, total)。

    solve     共同卷答对数 0..4
    set_score 出题分 min(c, 5-c)/2，c 是别人答对「我出的题」的人数 0..5
    bounty    本次挑战答对 +bounty，否则 0
    total     solve + set_weight*set_score + bounty
    """
    if len(common_slots) != N_COMMON:
        raise ValueError("common 槽位必须是 %d 个，收到 %d" % (N_COMMON, len(common_slots)))
    if len(peer_slots_for_own_task) != N_PEER:
        raise ValueError("peer 槽位必须是 %d 个，收到 %d" % (N_PEER, len(peer_slots_for_own_task)))

    solve = count_right(common_slots)
    c = count_right(peer_slots_for_own_task)
    set_score = min(c, N_PEER - c) / 2

    bounty = 0.0
    if challenge_slot is not None and _field(challenge_slot, "status") == "right":
        bounty = float(_cfg(cfg, "bounty", 2.0))

    set_weight = float(_cfg(cfg, "set_weight", 2))
    total = solve + set_weight * set_score + bounty
    return solve, set_score, bounty, total


# ---------------------------------------------------------------------------
# 基因指纹与降噪
# ---------------------------------------------------------------------------

def gene_key(genes) -> str:
    """三字段基因的指纹：固定键序、无空白、UTF-8 编码后的 SHA-256。"""
    canonical = json.dumps(
        _genes_dict(genes), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_gene_history(ind, history=None, total=None) -> None:
    """把当代 total 记到该基因名下。

    调用时机：该代 status == completed 之后，**且只调一次**。
    aborted 代不许调——那会把失败样本混进测量记录。
    """
    store = GENE_HISTORY if history is None else history
    if total is None:
        total = _field(ind, "current_total")
    store.setdefault(gene_key(_field(ind, "genes")), []).append(float(total))


def est_score(ind, cfg, history=None) -> float:
    """同基因最近 fitness_window 次 measurement 的均值。

    必须先 append_gene_history 再调用；没有样本时抛 ValueError 而不是
    返回 0——静默兜底会把「调用顺序错了」伪装成「这个基因很弱」。
    """
    store = GENE_HISTORY if history is None else history
    hist = store.get(gene_key(_field(ind, "genes")))
    if not hist:
        raise ValueError("gene_history 里没有该基因的样本，先调用 append_gene_history")
    window = int(_cfg(cfg, "fitness_window", 1))
    if window <= 0:
        raise ValueError("fitness_window 必须 >= 1，收到 %r" % (window,))
    recent = hist[-window:]
    return sum(recent) / len(recent)


def ranking_key(ind, cfg, history=None):
    """排序键：先按降噪估计，再按当代 total（都取负，方便升序排）。"""
    return (
        -est_score(ind, cfg, history),
        -float(_field(ind, "current_total")),
    )


def rank_population(individuals, cfg, rng, history=None):
    """按 ranking_key 升序排；键完全相同的组内先按 ID 排好再 shuffle。

    先按 ID 排再 shuffle 是规格 §6.1 的硬要求：否则并发回包的顺序
    会影响抽签结果，同一趟跑两次得到不同排名。
    """
    keyed = []
    for ind in individuals:
        keyed.append((ranking_key(ind, cfg, history), str(_field(ind, "id")), ind))
    keyed.sort(key=lambda item: (item[0], item[1]))

    result = []
    i = 0
    while i < len(keyed):
        j = i
        while j + 1 < len(keyed) and keyed[j + 1][0] == keyed[i][0]:
            j += 1
        group = [item[2] for item in keyed[i:j + 1]]
        if len(group) > 1:
            rng.shuffle(group)
        result.extend(group)
        i = j + 1
    return result


def survivors(individuals, cfg, rng, history=None, n=N_SURVIVE):
    """取前 n 名（默认留 3）。"""
    return rank_population(individuals, cfg, rng, history)[:n]


# ---------------------------------------------------------------------------
# gene_history 的存取（快照恢复用）
# ---------------------------------------------------------------------------

def reset_gene_history(history=None) -> None:
    (GENE_HISTORY if history is None else history).clear()


def export_gene_history(history=None) -> dict:
    """导出可 JSON 化的副本（list 复制，避免外部改到内部状态）。"""
    store = GENE_HISTORY if history is None else history
    return {k: list(v) for k, v in store.items()}


def import_gene_history(data: dict, history=None) -> None:
    store = GENE_HISTORY if history is None else history
    store.clear()
    for key, values in data.items():
        store[key] = [float(v) for v in values]
