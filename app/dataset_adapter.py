"""B-side adapter for C's JSON files. Evolution receives no holdout API."""
import copy
import json
from collections import Counter
from pathlib import Path

from . import executor
from .integrity import digest, task_digest


def read_tasks(path):
    text = Path(path).read_text(encoding='utf-8')
    if Path(path).suffix == '.jsonl':
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows['tasks']
    if not isinstance(rows, list):
        raise ValueError('Dataset must contain a task array')
    out = []
    for row in rows:
        raw = {'template': row['template'], 'params': row['params']}
        task = executor.normalize_task(raw, task_id=row['id'], source=row.get('source', 'dataset'),
                                       by=None, gen=row.get('gen'))
        for key in ('signature', 'statement', 'reference'):
            if row.get(key) != task[key]:
                raise ValueError(f"{path}: {row['id']} has invalid {key}")
        out.append(task)
    if len({t['id'] for t in out}) != len(out):
        raise ValueError(f'{path}: duplicate task ID')
    return out


class FileDataset:
    def __init__(self, data_dir='data'):
        root = Path(data_dir)
        partitions = {name: read_tasks(root / f'{name}.json') for name in
                      ('calibration', 'common_dev', 'challenge_pool', 'holdout')}
        for name, count in [('calibration',20), ('common_dev',80), ('challenge_pool',5), ('holdout',20)]:
            if len(partitions[name]) != count:
                raise ValueError(f'{name} must contain {count} tasks')
        for name in ('calibration', 'holdout'):
            if Counter(t['type'] for t in partitions[name]) != {'integral':10, 'det':10}:
                raise ValueError(f'{name}: expected 10 integral / 10 det')
        all_tasks = [t for tasks in partitions.values() for t in tasks]
        if len({t['signature'] for t in all_tasks}) != len(all_tasks):
            raise ValueError('Dataset partitions must have disjoint signatures')
        self.manifest = {name: digest(tasks) for name, tasks in partitions.items()}
        self.holdout_digest = task_digest(partitions['holdout'])
        self._signatures = {t['signature'] for t in all_tasks}
        self._pool = partitions['challenge_pool']
        groups = {}
        for task in partitions['common_dev']:
            groups.setdefault(task['gen'], []).append(task)
        # C's committed file labels its first group gen=0; engine starts at 1.
        keys = sorted(groups)
        if keys not in (list(range(20)), list(range(1,21))):
            raise ValueError('common_dev groups must be contiguous')
        self._common = {i+1: groups[k] for i,k in enumerate(keys)}
        for tasks in self._common.values():
            if Counter(t['type'] for t in tasks) != {'integral':2, 'det':2}:
                raise ValueError('Each common_dev group must contain 2 integral / 2 det')
        # Do not retain holdout bodies on the object supplied to evolution.

    def common_dev(self, gen):
        return copy.deepcopy(self._common[gen])

    def challenge_pool(self):
        return copy.deepcopy(self._pool)

    def signatures(self):
        return set(self._signatures)


def load_dataset(*, data_dir='data'):
    return FileDataset(data_dir)
