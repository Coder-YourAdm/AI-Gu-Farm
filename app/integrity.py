"""Canonical fingerprints shared by runtime, protocol and evaluation."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def task_digest(tasks):
    return digest([{'id': t['id'], 'template': t['template'], 'params': t['params']}
                   for t in tasks])


def source_manifest():
    paths = sorted((ROOT / 'app').glob('*.py')) + sorted((ROOT / 'prompts').glob('*'))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(
        p.read_text(encoding='utf-8').replace('\r\n', '\n').encode()).hexdigest()
        for p in paths if p.is_file()}


def dataset_manifest(dataset, n_gen):
    # Include order, group assignment, full task contents and reference values.
    data = {'common': [dataset.common_dev(g) for g in range(1, n_gen + 1)],
            'challenge': dataset.challenge_pool(), 'signatures': sorted(dataset.signatures())}
    if hasattr(dataset, 'manifest'):
        data['files'] = dataset.manifest
    return {'digest': digest(data), 'holdout_digest': getattr(dataset, 'holdout_digest', None)}


def comparison_config(cfg):
    value = cfg.to_dict()
    for key in ('condition', 'seed'):
        value.pop(key)
    return digest(value)
