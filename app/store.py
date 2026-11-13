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

def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """写同目录临时文件再 `os.replace`。失败不会污染已有快照。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p


@dataclass
class SnapshotStore:
    """`root/snapshots/{run_id}/g{gen:02d}.json` + `root/history.jsonl`。"""

    root: str | Path = "runs"
    run_id: str = "run"

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / "snapshots" / self.run_id

    @property
    def history_path(self) -> Path:
        return Path(self.root) / "history.jsonl"

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
        return atomic_write_json(self.snapshot_path(int(record["gen"])), payload)

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
