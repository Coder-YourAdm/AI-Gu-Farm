"""Offline regressions for the B audit: immutable evidence and valid experiments."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.config import Config
from app.budget import BudgetExceeded
from app.dataset_adapter import FileDataset
from app.evaluate import (population_at_checkpoint, prepare_evaluation, evaluate_run,
                          evaluate_group, pair_evaluations)
from app.genotype import Operators, gene_key
from app.integrity import task_digest
from app.llm import LLMClient, ProtocolError
from app.orchestrator import run_generation, _aggregate_usage
from app.runtime import new_run, restore_run, branch_run
from app.runner import run_loop
from app.store import SnapshotStore
from doubles import FakeEngine, FakeDataset, FakeOperators, ScriptedChat
from tests.test_evaluate import _holdout_tasks
from tests.test_llm_async import _ok_body


def config(**changes):
    raw = json.loads(Path('config.dev.json').read_text(encoding='utf-8'))
    raw.update(model='test-model', base_url='https://api.example.com/v1',
               n_gen=3, challenge_gens=[], budget_amount=None, wall_limit_s=30)
    raw.update(changes)
    return Config.from_dict(raw)


def assemble(tmp_path, **changes):
    cfg = config(**changes)
    engine = FakeEngine()
    ds = FakeDataset(engine, n_gens=cfg.n_gen)
    tasks = _holdout_tasks(engine)
    ds.holdout_digest = task_digest(tasks)
    state = new_run(cfg=cfg, dataset=ds, run_id='run')
    store = SnapshotStore(tmp_path, state.run_id)
    return cfg, engine, ds, tasks, state, store


async def drive(state, store, engine, ds, *, chat=None, until=None):
    return await run_loop(state=state, store=store, engine=engine, dataset=ds,
        chat=chat or ScriptedChat(state.cfg, engine, answer_policy=lambda *_:'ok'),
        operators=FakeOperators(state.cfg), until=until)


def test_c_json_adapter_maps_groups_and_hides_holdout():
    ds = FileDataset()
    assert len(ds.signatures()) == 125
    assert len(ds.common_dev(1)) == 4
    assert ds.common_dev(1)[0]['gen'] == 0
    assert ds.common_dev(20)[0]['gen'] == 19
    assert not hasattr(ds, 'holdout')
    first = ds.common_dev(1)
    first[0]['params'].clear()
    assert ds.common_dev(1)[0]['params']


async def test_intermediate_and_initial_checkpoints_are_frozen(tmp_path):
    cfg, engine, ds, tasks, state, store = assemble(tmp_path, n_gen=6)
    await drive(state, store, engine, ds)
    initial, _ = population_at_checkpoint(store_root=tmp_path, run_id='run', checkpoint=0,
                                         seed_path='does-not-exist.json')
    assert [gene_key(p.genes) for p in initial] == [
        gene_key(p.genes) for p in new_run(cfg=cfg, dataset=ds, run_id='other').next_population]
    people, _ = population_at_checkpoint(store_root=tmp_path, run_id='run', checkpoint=5)
    rec = store.load_generation(5)['record']
    assert [p.id for p in people] == [p['meta']['id'] for p in rec['evaluated_population']]
    assert [p.id for p in people] != [p['meta']['id'] for p in rec['next_population']]


class BrokenChat:
    async def chat(self, *args, **kwargs):
        raise RuntimeError('test infrastructure failure')


async def test_abort_is_immutable_and_resume_has_new_identity(tmp_path):
    cfg, engine, ds, tasks, state, store = assemble(tmp_path)
    await drive(state, store, engine, ds, until=1)
    await drive(state, store, engine, ds, chat=BrokenChat())
    assert store.load_summary()['status'] == 'aborted'
    with pytest.raises(ProtocolError, match='branch'):
        await drive(state, store, engine, ds)
    original = {p.name:p.read_bytes() for p in store.run_dir.glob('*.json')}
    restored = restore_run(cfg=cfg, store=store, dataset=ds)
    assert restored.last_gen == 1
    with pytest.raises(ProtocolError, match='branch'):
        await drive(restored, store, engine, ds)
    with pytest.raises(ProtocolError):
        branch_run(cfg=cfg, source_store=store, target_store=store, dataset=ds)
    branch_store = SnapshotStore(tmp_path, 'demo_branch')
    branch = branch_run(cfg=cfg, source_store=store, target_store=branch_store, dataset=ds)
    await drive(branch, branch_store, engine, ds)
    assert branch_store.load_summary()['completed'] == 3
    assert branch_store.load_manifest()['parent_run_id'] == 'run'
    assert original == {p.name:p.read_bytes() for p in store.run_dir.glob('*.json')}
    with pytest.raises(ProtocolError, match='Demo'):
        prepare_evaluation(run_id='demo_branch', cfg=cfg, tasks=tasks, store_root=tmp_path)


async def test_failure_in_first_generation_can_branch_from_frozen_g0(tmp_path):
    cfg, engine, ds, _, state, store = assemble(tmp_path)
    await drive(state, store, engine, ds, chat=BrokenChat())
    assert restore_run(cfg=cfg, store=store, dataset=ds).last_gen == 0
    assert store.gens() == [0, 1]


async def test_resume_rejects_order_and_source_changes(tmp_path, monkeypatch):
    cfg, engine, ds, _, state, store = assemble(tmp_path)
    await drive(state, store, engine, ds, until=1)
    changed = copy.deepcopy(ds)
    changed._common[2].reverse()
    assert changed.signatures() == ds.signatures()
    with pytest.raises(ProtocolError, match='order'):
        restore_run(cfg=cfg, store=store, dataset=changed)
    monkeypatch.setattr('app.runtime.source_manifest', lambda:{'changed':'code'})
    with pytest.raises(ProtocolError, match='Code'):
        restore_run(cfg=cfg, store=store, dataset=ds)


async def test_operator_transport_aborts_without_committing_state(tmp_path):
    cfg, engine, ds, _, state, store = assemble(tmp_path)
    base = ScriptedChat(cfg, engine, answer_policy=lambda *_:'ok')
    class Chat:
        async def chat(self, role, messages, **kw):
            if role == 'operator':
                return base._result(role, kw['slot_id'], 'transport_error')
            return await base.chat(role, messages, **kw)
    chat = Chat()
    before = state.rng.getstate()
    rec = await run_generation(gen=1, state=state, dataset=ds, engine=engine, chat=chat,
                               operators=Operators(cfg, state.rng, chat))
    assert rec['status'] == 'aborted' and rec['reason'] == 'transport_error'
    assert state.last_gen == 0 and state.gene_history.to_dict() == {}
    assert state.rng.getstate() == before
    assert rec['operator_calls'] == 1 and rec['call_total'] == 79


async def test_content_fallback_still_accounts_for_operator_calls(tmp_path):
    cfg, engine, ds, _, state, _ = assemble(tmp_path)
    base = ScriptedChat(cfg, engine, answer_policy=lambda *_:'ok',
                        setter_plan={'G00_S1':['bad_json']})
    class Chat:
        async def chat(self, role, messages, **kw):
            if role == 'operator':
                return base._result(role, kw['slot_id'], 'ok', parse_error='bad_schema')
            return await base.chat(role, messages, **kw)
    chat = Chat()
    rec = await run_generation(gen=1, state=state, dataset=ds, engine=engine, chat=chat,
                               operators=Operators(cfg, state.rng, chat))
    assert rec['status'] == 'completed'
    assert rec['setter_calls'] == 7 and rec['operator_calls'] >= 3
    assert rec['call_total'] == 72 + 7 + rec['operator_calls']
    assert rec['usage']['call_count'] == rec['call_total']
    assert len(rec['answers']) == 72
    assert all('raw_answer' in a for a in rec['answers'])


async def test_wall_deadline_archives_aborted_generation(tmp_path):
    cfg, engine, ds, _, state, store = assemble(tmp_path, wall_limit_s=0.03)
    class HangingChat:
        async def chat(self, *a, **kw):
            await asyncio.sleep(60)
    await drive(state, store, engine, ds, chat=HangingChat())
    record = json.loads(store.snapshot_path(1).read_text(encoding='utf-8'))['record']
    assert record['reason'] == 'budget_limit'
    assert store.load_summary()['status'] == 'aborted'
    assert record['usage']['input_tokens'] is None
    assert record['call_total'] == 6


async def test_http_attempt_limit_includes_retries_and_concurrency(tmp_path, monkeypatch):
    cfg = config(max_calls=3, concurrency=2, network_retries=2)
    monkeypatch.setenv(cfg.api_key_env, 'test-secret')
    seen = []
    async def handler(req):
        seen.append(req)
        await asyncio.sleep(0)
        return httpx.Response(503, text='temporary unavailable')
    async with LLMClient(cfg, run_id='cap', log_root=tmp_path, backoff=(0,0),
                          _transport=httpx.MockTransport(handler)) as chat:
        results = await asyncio.gather(*[chat.chat('solver', [], slot_id=f's{i}')
                                         for i in range(10)], return_exceptions=True)
        assert chat.budget.used == len(seen) == 3
    assert any(isinstance(r, BudgetExceeded) for r in results)
    lines = [json.loads(line) for line in (tmp_path/'calls.jsonl').read_text().splitlines()]
    assert len(lines) == 3
    assert len(list((tmp_path/'raw'/'cap').glob('*.json'))) == 3


async def test_client_deadline_stops_inflight_http(tmp_path, monkeypatch):
    cfg = config(wall_limit_s=0.03)
    monkeypatch.setenv(cfg.api_key_env, 'test-secret')
    async def handler(req):
        await asyncio.sleep(60)
    async with LLMClient(cfg, run_id='deadline', log_root=tmp_path,
                          _transport=httpx.MockTransport(handler)) as chat:
        with pytest.raises(BudgetExceeded):
            await chat.chat('solver', [])
        assert chat.budget.used == 1
    assert len(list((tmp_path/'raw'/'deadline').glob('*.json'))) == 1


async def test_early_or_mismatched_holdout_sends_no_calls(tmp_path):
    cfg, engine, ds, tasks, state, store = assemble(tmp_path)
    await drive(state, store, engine, ds, until=1)
    chat = ScriptedChat(cfg, engine)
    with pytest.raises(ProtocolError, match='terminated'):
        await evaluate_run(run_id='run', cfg=cfg, tasks=tasks, engine=engine, chat=chat,
                           store_root=tmp_path)
    await drive(state, store, engine, ds)
    with pytest.raises(ProtocolError, match='config'):
        prepare_evaluation(run_id='run', cfg=config(seed=999), tasks=tasks, store_root=tmp_path)
    with pytest.raises(ProtocolError, match='Holdout'):
        prepare_evaluation(run_id='run', cfg=cfg, tasks=list(reversed(tasks)), store_root=tmp_path)
    assert chat.count() == 0


async def test_overlap_is_checked_in_noncheckpoint_generations(tmp_path):
    cfg, engine, ds, tasks, state, store = assemble(tmp_path, n_gen=5)
    await drive(state, store, engine, ds)
    path = store.snapshot_path(2)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['record']['exposed_signatures'].append(tasks[0]['signature'])
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ProtocolError, match='contamination'):
        prepare_evaluation(run_id='run', cfg=cfg, tasks=tasks, store_root=tmp_path)


async def test_holdout_transport_failure_is_na_and_cannot_retry(tmp_path):
    cfg, engine, ds, tasks, state, store = assemble(tmp_path, n_gen=1)
    await drive(state, store, engine, ds)
    result = await evaluate_run(run_id='run', cfg=cfg, tasks=tasks, engine=engine,
                                 chat=BrokenChat(), store_root=tmp_path)
    assert result['status'] == 'incomplete'
    assert all(cp['mean'] is None for cp in result['checkpoints'])
    assert all(p['score'] is None for cp in result['checkpoints'] for p in cp['individuals'])
    with pytest.raises(FileExistsError):
        await evaluate_run(run_id='run', cfg=cfg, tasks=tasks, engine=engine,
                            chat=BrokenChat(), store_root=tmp_path)


async def test_registered_group_waits_and_interleaves_runs(tmp_path):
    cfg, engine, ds, tasks, state, store = assemble(tmp_path, n_gen=1)
    members = [{'run_id':'E', 'condition':'E', 'seed':42}, {'run_id':'F', 'condition':'F', 'seed':42}]
    protocol = {'id':'test-protocol', 'config':cfg.to_dict(), 'members':members,
        'ancestors':state.frozen_manifest['ancestors'],
        'initial_population':state.frozen_manifest['initial_population'],
        'dataset':state.frozen_manifest['dataset']}
    from app.protocol import member_config
    states = []
    for member in members:
        st = new_run(cfg=member_config(protocol, member), dataset=ds, run_id=member['run_id'])
        st.frozen_manifest.update(protocol_id=protocol['id'], group_runs=['E','F'])
        states.append(st)
    await drive(states[0], SnapshotStore(tmp_path,'E'), engine, ds)
    with pytest.raises(ProtocolError):
        await evaluate_group(protocol, tasks=tasks, engine=engine, chat=BrokenChat(),
                             store_root=tmp_path, plan_only=True)
    await drive(states[1], SnapshotStore(tmp_path,'F'), engine, ds)
    chat = ScriptedChat(cfg, engine, answer_policy=lambda *_:'ok')
    preview = await evaluate_group(protocol, tasks=tasks, engine=engine, chat=chat,
                                   store_root=tmp_path, plan_only=True)
    assert chat.count() == 0 and preview['n_slots'] == 480
    assert {s['run_id'] for s in preview['plan'][:20]} == {'E','F'}
    assert {s['checkpoint'] for s in preview['plan'][:20]} == {0,1}
    results = await evaluate_group(protocol, tasks=tasks, engine=engine, chat=chat, store_root=tmp_path)
    assert all(r['status']=='completed' for r in results)
    assert pair_evaluations(*results)['final']['diff'] == 0
    for key in ('seed','data_seed','n_gen','n_holdout','holdout_digest','comparison_fingerprint',
                'initial_fingerprint','ancestor_fingerprint','protocol_id'):
        bad = copy.deepcopy(results[1])
        bad[key] = 'different'
        with pytest.raises(ProtocolError):
            pair_evaluations(results[0], bad)
    with pytest.raises(ProtocolError, match='already'):
        await evaluate_group(protocol, tasks=tasks, engine=engine, chat=chat, store_root=tmp_path)


async def test_demo_real_requests_include_different_questions(monkeypatch):
    from app import demo, llm
    seen = []
    monkeypatch.setattr(llm, 'preflight', lambda:None)
    monkeypatch.setattr(llm, 'complete', lambda messages:(seen.append(messages) or ('{}',1,1)))
    solver, _ = demo.make_solver('real')
    for row in demo.load_bank()[:2]:
        solver(row, demo.seed_state())
    assert seen[0][-1]['role'] == 'user'
    assert seen[0][-1]['content'] != seen[1][-1]['content']
    assert '答案格式' in seen[0][-1]['content']


def test_demo_keeps_existing_evidence_and_rejects_transport_learning(tmp_path, monkeypatch):
    from app import demo
    monkeypatch.setattr(demo, 'RUNS', tmp_path)
    directory = tmp_path/'existing'
    directory.mkdir()
    (directory/'proof.txt').write_text('original')
    monkeypatch.setattr(demo, 'make_solver', lambda _: (None,'test'))
    with pytest.raises(SystemExit, match='已存在'):
        demo.run_pair('existing','mock')
    assert (directory/'proof.txt').read_text() == 'original'
    result = demo.run_pass(1,'failed',demo.seed_state(),demo.load_bank()[:2],
                           lambda *_: (_ for _ in ()).throw(RuntimeError('offline')))
    assert result['status'] == 'aborted' and result['accuracy'] is None
    with pytest.raises(ValueError, match='调用失败'):
        demo.learn_from(result,demo.seed_state())


async def test_calibration_and_protocol_round_trip(tmp_path):
    from app.protocol import calibrate, lock_protocol, load_protocol
    from app.dataset_adapter import read_tasks
    cfg = config(n_gen=1)
    engine = FakeEngine()
    for t in read_tasks('data/calibration.json'):
        ref = t['reference']
        engine.reference_by_statement[t['statement']] = (ref['value'] if ref['kind']=='integer'
            else f"{ref['numerator']}/{ref['denominator']}")
    chat = ScriptedChat(cfg, engine, answer_policy=lambda slot,st: 'ok' if 'S1' in slot or 'S2' in slot or 'S3' in slot else 'wrong')
    report = await calibrate(cfg,chat)
    assert len(report['answers']) == 360
    assert report['mean_pass_rate'] == 0.5 and len(report['ancestors']) == 3
    report_path = tmp_path/'calibration.json'
    report_path.write_text(json.dumps(report), encoding='utf-8')
    path = tmp_path/'protocol.json'
    locked = lock_protocol(cfg,report_path,path,seeds=[42,43,44])
    assert len(locked['members']) == 6
    assert load_protocol(path) == locked
    with pytest.raises(FileExistsError):
        lock_protocol(cfg,report_path,path,seeds=[42,43,44])
    changed = copy.deepcopy(locked)
    changed['config']['seed'] += 1
    path.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ProtocolError):
        load_protocol(path)


def test_pilot_cli_uses_committed_dataset_and_real_http_contract(tmp_path, monkeypatch):
    from app.run import main
    cfg = config(n_gen=1)
    monkeypatch.setenv(cfg.api_key_env, 'test-secret')
    path = tmp_path/'pilot-config.json'
    path.write_text(json.dumps(cfg.to_dict()), encoding='utf-8')
    ds = FileDataset()
    occupied = ds.signatures()
    from app import executor
    sources = [{'template':'gamma', 'params':{'c':c,'n':n,'rate':rate}}
               for c in (1,-1,2,-2) for n in range(1,10) for rate in range(1,9)]
    available = iter(t for t in sources if executor.signature(t) not in occupied)
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        text = (json.dumps(next(available)) if body['max_tokens']==cfg.max_tokens_setter
                else '{"value":"0"}')
        return httpx.Response(200, json=_ok_body(text))
    class MockClient(LLMClient):
        def __init__(self, cfg, **kw):
            super().__init__(cfg, **kw, log_root=tmp_path/'logs',
                             _transport=httpx.MockTransport(handler))
    monkeypatch.setattr('app.llm.LLMClient', MockClient)
    assert main(['--config',str(path),'--pilot','--run-id','pilot-test',
                 '--runs-root',str(tmp_path/'runs')]) == 0
    summary = SnapshotStore(tmp_path/'runs','pilot-test').load_summary()
    assert summary['status'] == 'completed' and summary['call_total'] == 78
    assert len(seen) == 78


async def test_failed_calibration_retains_rows_without_selecting_ancestors():
    from app.protocol import calibrate
    report = await calibrate(config(), BrokenChat())
    assert report['status'] == 'incomplete' and report['ancestors'] == []
    assert report['mean_pass_rate'] is None and len(report['answers']) == 360
