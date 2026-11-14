"""Calibration and immutable registration of paired E/F runs."""
import json
import random
from pathlib import Path

from .config import Config
from .dataset_adapter import FileDataset, read_tasks
from .genotype import load_seed_genotypes
from .integrity import digest, dataset_manifest, source_manifest, task_digest
from .llm import ProtocolError
from .runtime import _ind_full
from .store import atomic_write_json


def solver_fingerprint(cfg):
    return digest({k:getattr(cfg,k) for k in ('model','base_url','solver_temperature',
        'max_tokens_solver','max_response_chars','max_answer_chars','max_number_digits',
        'decimal_exp_abs_max','decimal_tolerance')})


async def calibrate(cfg, chat, *, data_dir='data', seeds_path='prompts/seed_genotypes.json'):
    from .evaluate import EvalSlot, run_plan
    from . import executor
    tasks = read_tasks(Path(data_dir) / 'calibration.json')
    people = load_seed_genotypes(seeds_path, max_chars=cfg.max_genotype_chars)
    plans = [EvalSlot('calibration', repeat, person.id, task['id'])
             for repeat in range(3) for person in people for task in tasks]
    rng = random.Random(cfg.seed)
    rng.shuffle(plans)
    rows = await run_plan(plan=plans,
        genes_by={(r,p.id):p.genes for r in range(3) for p in people},
        tasks_by_id={t['id']:t for t in tasks}, engine=executor, chat=chat,
        concurrency=cfg.concurrency)
    failed = any(r['status']=='transport_error' for r in rows)
    scores = {p.id:[sum(r['status']=='right' for r in rows
                  if r['individual_id']==p.id and r['checkpoint']==repeat) for repeat in range(3)]
              for p in people}
    rng.shuffle(people)
    people.sort(key=lambda p: -sum(scores[p.id]))
    mean = None if failed else sum(sum(v) for v in scores.values()) / len(rows)
    from .orchestrator import _aggregate_usage
    return {'schema':'calibration/1','status':'incomplete' if failed else 'completed',
            'solver_fingerprint':solver_fingerprint(cfg),
            'source':source_manifest(), 'calibration_digest':task_digest(tasks),
            'scores':None if failed else scores, 'mean_pass_rate':mean,
            'warning_band_ok':not failed and 0.2 <= mean <= 0.8,
            'initial_fingerprint':digest([_ind_full(p) for p in load_seed_genotypes(seeds_path)]),
            'ancestors':[] if failed else [_ind_full(p) for p in people[:cfg.n_ancestor]],
            'usage':_aggregate_usage(getattr(chat, 'records', [])), 'answers':rows}


def read_calibration(cfg, calibration_path, *, data_dir='data'):
    report = json.loads(Path(calibration_path).read_text(encoding='utf-8'))
    initial = [_ind_full(p) for p in load_seed_genotypes('prompts/seed_genotypes.json')]
    if (report.get('status')!='completed' or report.get('solver_fingerprint')!=solver_fingerprint(cfg)
        or report.get('source')!=source_manifest()
        or report.get('initial_fingerprint')!=digest(initial)
        or report.get('calibration_digest') != task_digest(read_tasks(Path(data_dir)/'calibration.json'))):
        raise ProtocolError('Calibration does not match model, data, seeds or source')
    if not report.get('warning_band_ok'):
        raise ProtocolError('Calibration outside 20%-80%; run a new development calibration before locking')
    return report


def lock_protocol(cfg, calibration_path, path, *, seeds, data_dir='data', conditions=('E','F')):
    cfg.require_runtime_ready()
    report = read_calibration(cfg, calibration_path, data_dir=data_dir)
    dataset = FileDataset(data_dir)
    initial = [_ind_full(p) for p in load_seed_genotypes('prompts/seed_genotypes.json')]
    if not seeds or len(set(seeds)) != len(seeds) or any(type(s) is not int for s in seeds):
        raise ProtocolError('Registration requires unique integer seeds')
    if set(conditions) != {'E','F'}:
        raise ProtocolError('Formal registration requires paired E and F')
    if cfg.condition not in ('E', 'F'):
        raise ProtocolError('Use an E/F config for formal registration')
    core = {'schema':'protocol/1','config':cfg.to_dict(),'seeds':list(seeds),
            'conditions':['E','F'],'initial_population':initial,'ancestors':report['ancestors'],
            'dataset':dataset_manifest(dataset,cfg.n_gen),'source':source_manifest(),
            'calibration_digest':digest(report)}
    core['id'] = digest(core)
    core['members'] = [{'run_id':f'{c}_s{s}_{core["id"][:10]}','condition':c,'seed':s}
                       for s in seeds for c in ['E','F']]
    atomic_write_json(path, core, overwrite=False)
    return core


def load_protocol(path, *, data_dir='data'):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    core = {k:v for k,v in value.items() if k not in ('id','members')}
    if value.get('schema') != 'protocol/1' or digest(core) != value.get('id'):
        raise ProtocolError('Protocol fingerprint mismatch')
    cfg = Config.from_dict(value['config'])
    dataset = FileDataset(data_dir)
    if value['source'] != source_manifest() or value['dataset'] != dataset_manifest(dataset,cfg.n_gen):
        raise ProtocolError('Frozen source or dataset changed')
    expected = [{'run_id':f'{c}_s{s}_{value["id"][:10]}','condition':c,'seed':s}
                for s in value['seeds'] for c in ['E','F']]
    if value['members'] != expected:
        raise ProtocolError('Protocol run registry changed')
    return value


def member_config(protocol, member):
    raw = dict(protocol['config'])
    raw.update(condition=member['condition'], seed=member['seed'])
    return Config.from_dict(raw)
