"""快照、公开历史与 RNG 状态的落盘（技术规格 §8）。

只做三件事，每件都要求可复现：
1. 代快照原子写（同目录临时文件 + `os.replace`），崩溃不会留下半个 JSON；
2. `history.jsonl` 追加审计记录，快照才是恢复真源；
3. RNG state 按固定 schema 序列化/还原，禁止 `eval` / `pickle`。
"""

from __future__ import annotations

import json
import os
import random
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.llm import ProtocolError

RNG_SCHEMA = "rng_state/1"
_RNG_WORDS = 625


# --------------------------------------------------------------------------
# RNG
# --------------------------------------------------------------------------

def rng_state_to_dict(rng: random.Random) -> dict:
    version, words, gauss_next = rng.getstate()
    return {
        "schema": RNG_SCHEMA,
        "version": version,
        "words": [int(w) for w in words],
        "gauss_next": gauss_next,
    }


def rng_from_dict(obj: Mapping[str, Any]) -> random.Random:
    """白名单结构还原。任何字段不符直接拒绝，不做「尽力兼容」。"""
    if not isinstance(obj, Mapping):
        raise ProtocolError("rng_state 必须是对象")
    if obj.get("schema") != RNG_SCHEMA:
        raise ProtocolError("rng_state schema mismatch")
    if obj.get("version") != 3 or type(obj.get("version")) is bool:
        raise ProtocolError("rng_state version 必须为 3")
    words = obj.get("words")
    if not isinstance(words, list) or len(words) != _RNG_WORDS:
        raise ProtocolError("rng_state words 必须是 625 个整数")
    if not all(type(w) is int and 0 <= w < 2 ** 32 for w in words):
        raise ProtocolError("rng_state words 越界或类型不符")
    gauss_next = obj.get("gauss_next")
    if gauss_next is not None and type(gauss_next) is not float:
        raise ProtocolError("gauss_next 只能是 float 或 null")
    rng = random.Random()
    rng.setstate((3, tuple(words), gauss_next))
    return rng


# --------------------------------------------------------------------------
# 原子写
# --------------------------------------------------------------------------

def atomic_write_json(path: str | Path, payload: Any, *, overwrite: bool = True) -> Path:
    """写同目录临时文件再 `os.replace`。失败不会污染已有快照。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.' + uuid.uuid4().hex + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if overwrite:
        os.replace(tmp, p)
    else:
        try:
            os.link(tmp, p)  # Atomic create-if-absent; never replace evidence.
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                # 沙箱、杀软、部分企业策略会拦住 unlink。此时目标文件已经通过
                # 硬链接落定，残留的 .tmp 只是垃圾文件——不能因为删不掉就判定
                # 整趟运行失败（这是「写成功」和「清理成功」两件事）。
                pass
    return p


@dataclass
class SnapshotStore:
    """`root/snapshots/{run_id}/g{gen:02d}.json` + 同目录 `history.jsonl`。"""

    root: str | Path = "runs"
    run_id: str = "run"

    def __post_init__(self):
        if (not self.run_id or self.run_id.strip() != self.run_id or
                any(c in self.run_id for c in ('/', '\\', ':', '..'))):
            raise ValueError('Invalid run_id')

    @property
    def manifest_path(self):
        return Path(self.root) / 'manifests' / f'{self.run_id}.json'

    @property
    def summary_path(self):
        return Path(self.root) / 'summaries' / f'{self.run_id}.json'

    def write_manifest(self, manifest):
        return atomic_write_json(self.manifest_path, manifest, overwrite=False)

    def load_manifest(self):
        if not self.manifest_path.exists():
            raise ProtocolError(f'No frozen manifest for {self.run_id}')
        return json.loads(self.manifest_path.read_text(encoding='utf-8'))

    def load_summary(self):
        if not self.summary_path.exists():
            raise ProtocolError(f'Run {self.run_id} has no terminal state')
        return json.loads(self.summary_path.read_text(encoding='utf-8'))

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / "snapshots" / self.run_id

    @property
    def history_path(self) -> Path:
        # 每趟一份，与快照同目录。规格 §8 的位置是 `runs/<run_id>/history.jsonl`，
        # 而 `app.export --run runs/<run_id>` 正是按这个位置读的。
        # 早先放在 root 下，会让六趟（E/F/A × 3 seed）的审计记录混进同一个文件：
        # 导出时不分 run_id，大屏上就会看到别人的代。
        return self.run_dir / "history.jsonl"

    def snapshot_path(self, gen: int) -> Path:
        return self.run_dir / f"g{gen:02d}.json"

    def save_generation(self, record: Mapping[str, Any], *,
                        rng: random.Random,
                        state_payload: Mapping[str, Any] | None = None) -> Path:
        """一代一文件。record 之外必须带 RNG state，否则无法从检查点续跑。"""
        payload = {
            "schema_version": record.get("schema_version", "5.1"),
            "run_id": record.get("run_id"),
            "gen": record.get("gen"),
            "phase": record.get("status"),
            "record": dict(record),
            "state": dict(state_payload or {}),
            "rng": rng_state_to_dict(rng),
        }
        return atomic_write_json(self.snapshot_path(int(record["gen"])), payload, overwrite=False)

    def append_history(self, record: Mapping[str, Any]) -> None:
        p = self.history_path
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def load_generation(self, gen: int) -> dict:
        p = self.snapshot_path(gen)
        if not p.exists():
            raise ProtocolError(f"快照不存在: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("gen") != gen:
            raise ProtocolError(f"快照代数不符: {data.get('gen')} != {gen}")
        if data.get("phase") != "completed":
            raise ProtocolError(f"只从 completed 快照恢复，当前 {data.get('phase')}")
        return data

    # -- 续跑用：只看「存在哪些代」，不解析内容 --------------------------

    def gens(self) -> list[int]:
        """已落盘的代次（升序）。文件名不合 `gNN.json` 一律不算。"""
        out: list[int] = []
        for p in self.run_dir.glob("g*.json"):
            stem = p.stem
            if len(stem) >= 3 and stem[0] == "g" and stem[1:].isdigit():
                out.append(int(stem[1:]))
        return sorted(out)

    def completed_gens(self) -> list[int]:
        """`phase=completed` 的代次。读取失败的单个文件跳过，不打断扫描。"""
        out: list[int] = []
        for gen in self.gens():
            try:
                data = json.loads(self.snapshot_path(gen).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("phase") == "completed" and data.get("gen") == gen:
                out.append(gen)
        return out

    def has_snapshots(self) -> bool:
        return bool(self.gens())
