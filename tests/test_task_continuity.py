"""P0: end-to-end pending_only budget continuity without touching live projects."""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import sys
from pathlib import Path
import pytest
from conftest import SKILL_ROOT, default_sections

sys.path.insert(0,str(SKILL_ROOT/'scripts'))
import task_continuity as C
import psc_runtime as R
import psc_mcp_server as MCP
import supervisor_runtime as S

def write(path:Path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')

def fixture(tmp_path,*,already_active=True,quality=1,abnormal=0,round_number=1,
            source_version=2,target_version=3):
    repo=tmp_path/'repo'
    repo.mkdir()
    project=tmp_path/'project'
    runtime=project/'runtime'
    runtime.mkdir(parents=True)
    def task_block(n,*,updated=False):
        tid=f'T-{n:03d}'
        scope=('scripts/fix.py\n- tests/fix.py\n- docs/**' if updated and n==7 else 'docs/**')
        return (f'## {tid}\n\nTitle: Task {n}\n\nGoal: complete task {n}.\n\n'
                f'Requirements: REQ-001\nAcceptance Criteria: AC-001\n'
                f'Dependencies:\n- '+ (f'T-{n-1:03d}' if n>1 else 'None') +'\n'
                f'Allowed Scope:\n- {scope}\n'
                'Forbidden Scope:\n- private/**\n'
                + ('Implementation Notes:\n- updated implementation details\n' if updated and n==7 else
                   'Implementation Notes:\n- original details\n')
                +'Required Verification:\n- pytest\n\n')
    tasks1='# Tasks\n\n'+''.join(task_block(n) for n in range(1,9))
    tasks2='# Tasks\n\n'+''.join(task_block(n,updated=True) for n in range(1,9))
    for version,tasks in ((source_version,tasks1),(target_version,tasks2)):
        contract=project/'contract'/f'v{version}'
        contract.mkdir(parents=True)
        sections=default_sections(version=version)
        meta=json.loads(sections['metadata.json'])
        meta['supersedes']=source_version if version==target_version else source_version-1
        meta['workflow_policy']={'restart':'pending_only'} if version==target_version else {'restart':'all'}
        sections['metadata.json']=json.dumps(meta)
        sections['requirements.md']='# Requirements\\n\\n## REQ-001\\nRequirement one.\\n'.replace('\\n',chr(10))
        sections['acceptance.md']='# Acceptance\\n\\n## AC-001\\nCovers REQ-001.\\n'.replace('\\n',chr(10))
        sections['tasks.md']=tasks
        for name,content in sections.items():
            (contract/name).write_text(content,encoding='utf-8')
    write(runtime/'workflow_state.json',{
      'schema_version':1,'contract_version':target_version if already_active else source_version,
      'status':'ready','current_task':'T-007','last_completed_task':'T-006',
      'attempt':0,'last_stage':'preflight_denied','updated_at':'2026-10-11T00:00:00+00:00',
      'execution_owner':'executor'})
    record=dict(execution_round=round_number,initial_attempted=True,
                quality_retries_used=quality,abnormal_retries_used=abnormal)
    write(runtime/'executor_attempts.json',{
      'schema_version':2,'tasks':{f'v{source_version}:T-007':record},
      'legacy_unclassified_attempts':{}})
    # Existing accepted artifacts stay byte-for-byte intact.
    passed=[]
    for n in range(1,7):
        f=project/'developing'/'artifacts'/f'T-{n:03d}'/'result.md'
        f.parent.mkdir(parents=True,exist_ok=True)
        f.write_text(f'native PASS T-{n:03d}\n',encoding='utf-8')
        passed.append((f,f.read_bytes()))
    (project/'developing'/'tasks').mkdir(parents=True,exist_ok=True)
    for n in range(1,9):
        (project/'developing'/'tasks'/f'T-{n:03d}.md').write_text(task_block(n,updated=already_active),encoding='utf-8')
    return project,repo,passed

def source_unchanged(project, expected):
    for file,payload in expected:
        assert file.read_bytes()==payload

def test_v2_v3_pending_task_recovers_full_budget_without_consumption(tmp_path):
    project,repo,passed=fixture(tmp_path)
    before=json.loads(C.ledger_path(project).read_text(encoding='utf-8'))
    snap=S.supervisor_snapshot(project)
    assert snap['retry']['continuity_status']=='blocked'
    config=project/'runtime'/'runtime.json'
    config.write_text('{}',encoding='utf-8')
    assert MCP.preflight_check_tool(str(repo),str(config),str(project),
        str(project/'developing'/'tasks'/'T-007.md'),str(project/'contract'/'v3'))['status']=='continuity_blocked'
    assert C.check_task(project,project/'contract'/'v3','T-007')['status']=='blocked'
    old_hash=C.file_sha(C.state_path(project))
    result=MCP.recover_task_continuity_tool(str(project),str(repo),2,3,old_hash)
    assert result['status']=='migrated'
    ledger=C.read_ledger(project)
    assert ledger['tasks']['v2:T-007']==before['tasks']['v2:T-007']
    assert ledger['tasks']['v3:T-007']==ledger['tasks']['v2:T-007']
    assert ledger['tasks']['v3:T-008']==C.empty_state()
    snap=S.supervisor_snapshot(project)
    assert snap['retry']['initial_attempted'] is True
    assert snap['retry']['quality_retries_used']==1
    assert snap['retry']['abnormal_retries_used']==0
    assert snap['retry']['execution_round']==1
    assert snap['retry']['quality_retries_remaining']==2
    assert snap['retry']['abnormal_retries_remaining']==3
    assert snap['last_completed_task']=='T-006'
    assert snap['current_task']=='T-007'
    assert snap['task_order'][-2:]==['T-007','T-008']
    assert C.file_sha(C.state_path(project))==old_hash
    source_unchanged(project,passed)
    blocked=MCP._invoke_executor_impl(str(repo),str(repo/'runtime.json'),str(project),
        str(project/'developing'/'tasks'/'T-007.md'),str(project/'contract'/'v3'),retry_kind='initial')
    assert blocked['reason']=='retry_kind_required'
    assert C.read_ledger(project)==ledger

def test_remaining_quality_abnormal_budget_charges_on_real_executor_attempt(tmp_path,monkeypatch):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    contract=project/'contract'/'v3'
    task=project/'developing'/'tasks'/'T-007.md'
    prior=C.check_task(project,contract,'T-007')['state']
    completed={'log_path':'mock-real-executor.log','status':'completed','reason':None}
    with MCP.workflow_registry._locked(project/'runtime'/C.LOCK_NAME):
        updated,which=MCP._charge_actual_attempt(project,contract,task,
            dispatch_kind='quality_rework',result=completed,prior_state=prior,
            legacy_unclassified_attempts={})
    assert which=='quality_rework'
    assert updated['quality_retries_used']==2
    assert C.check_task(project,contract,'T-007')['status']=='verified'
    with MCP.workflow_registry._locked(project/'runtime'/C.LOCK_NAME):
        state,which=MCP._charge_actual_attempt(project,contract,task,
            dispatch_kind='abnormal_retry',result=completed,prior_state=updated,
            legacy_unclassified_attempts={})
    assert which=='abnormal_retry'
    assert state['abnormal_retries_used']==1
    assert C.read_ledger(project)['tasks']['v2:T-007']['quality_retries_used']==1

@pytest.mark.parametrize('quality,abnormal',[(3,0),(1,3),(3,3)])
def test_exhausted_retries_do_not_restart_after_upgrade(tmp_path,quality,abnormal):
    project,repo,_=fixture(tmp_path,quality=quality,abnormal=abnormal)
    C.recover(project,repo,2,3)
    task=project/'developing'/'tasks'/'T-007.md'
    state=C.check_task(project,project/'contract'/'v3','T-007')['state']
    blocked=MCP._budget_block(project=project,contract_path=project/'contract'/'v3',
        task_path=task,state=state,dispatch_kind='quality_rework',legacy_unclassified_attempts=0)
    assert blocked['status']=='retry_limit_reached'

def test_double_recovery_idempotent_and_does_not_refresh(tmp_path):
    project,repo,_=fixture(tmp_path)
    first=C.recover(project,repo,2,3)
    content=C.ledger_path(project).read_bytes()
    second=C.recover(project,repo,2,3)
    assert second['status']=='already_migrated'
    assert C.ledger_path(project).read_bytes()==content
    assert first['ledger_sha256']==C.file_sha(C.ledger_path(project))

def test_source_retry_state_drift_blocks_all_dispatch(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    ledger=C.read_ledger(project)
    ledger['tasks']['v2:T-007']['quality_retries_used']=0
    C.write_ledger(project,ledger)
    check=C.check_task(project,project/'contract'/'v3','T-007')
    assert check['status']=='blocked' and 'source retry state drift' in check['reason']

@pytest.mark.parametrize('which',('title','goal','requirements','forbidden_scope'))
def test_task_identity_mismatch_cannot_inherit(tmp_path,which):
    project,repo,_=fixture(tmp_path)
    path=project/'contract'/'v3'/'tasks.md'
    text=path.read_text(encoding='utf-8')
    if which=='title':text=text.replace('Title: Task 7','Title: Completely different')
    elif which=='goal':text=text.replace('Goal: complete task 7.','Goal: replace the bus fleet.')
    elif which=='requirements':
        text=text.replace('Requirements: REQ-001','Requirements: REQ-001, REQ-002',1)
        req=project/'contract'/'v3'/'requirements.md'
        req.write_text(req.read_text(encoding='utf-8')+'\\n## REQ-002\\nAdded.\\n'.replace('\\n',chr(10)),encoding='utf-8')
        acceptance=project/'contract'/'v3'/'acceptance.md'
        acceptance.write_text(acceptance.read_text(encoding='utf-8').replace(
            'Covers REQ-001.','Covers REQ-001 and REQ-002.'),encoding='utf-8')
    else:text=text.replace('Forbidden Scope:\n- private/**','Forbidden Scope:\n- busoperation/**',1)
    path.write_text(text,encoding='utf-8')
    with pytest.raises(C.ContinuityError,match='Task identity drift'):
        C.recover(project,repo,2,3)
    assert 'v3:T-007' not in C.read_ledger(project)['tasks']

def test_supersedes_mismatch_and_sha_conflict_fail_without_write(tmp_path):
    project,repo,_=fixture(tmp_path)
    path=C.ledger_path(project)
    before=path.read_bytes()
    with pytest.raises(C.ContinuityError,match='workflow_state_sha_conflict'):
        C.recover(project,repo,2,3,expected_state_sha256='deadbeef'*8)
    meta=project/'contract'/'v3'/'metadata.json'
    obj=json.loads(meta.read_text(encoding='utf-8'))
    obj['supersedes']=1
    meta.write_text(json.dumps(obj),encoding='utf-8')
    with pytest.raises(C.ContinuityError,match='direct lineage|metadata unavailable'):
        C.recover(project,repo,2,3)
    assert path.read_bytes()==before

def test_atomic_ledger_write_failure_preserves_original(tmp_path,monkeypatch):
    project,repo,_=fixture(tmp_path)
    original=C.ledger_path(project).read_bytes()
    def fail(*args,**kwargs):raise PermissionError('injected rename denial')
    monkeypatch.setattr(C.os,'replace',fail)
    with pytest.raises(PermissionError,match='injected'):
        C.recover(project,repo,2,3)
    assert C.ledger_path(project).read_bytes()==original
    assert not list((project/'runtime').glob('executor-continuity-*.tmp'))

def test_parallel_recovery_exactly_once(tmp_path):
    project,repo,_=fixture(tmp_path)
    def action(_):
        return C.recover(project,repo,2,3)['status']
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(action,range(4)))
    assert results.count('migrated')==1
    assert results.count('already_migrated')==3
    assert C.check_task(project,project/'contract'/'v3','T-007')['status']=='verified'

def test_multi_step_v2_v3_v4_does_not_refresh_budget(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    v3=project/'contract'/'v3'
    v4=project/'contract'/'v4'
    import shutil
    shutil.copytree(v3,v4)
    meta=json.loads((v4/'metadata.json').read_text(encoding='utf-8'))
    meta['version']=4
    meta['supersedes']=3
    (v4/'metadata.json').write_text(json.dumps(meta),encoding='utf-8')
    state=R.load_json(C.state_path(project))
    state['contract_version']=4
    R.dump_json(C.state_path(project),state)
    C.recover(project,repo,3,4)
    current=C.check_task(project,v4,'T-007')
    assert current['status']=='verified'
    assert current['state']['quality_retries_used']==1
    assert len(C.read_ledger(project)['continuity'])==4
    assert C.read_ledger(project)['tasks']['v2:T-007']['quality_retries_used']==1

def test_restart_all_leaves_original_budget_semantics(tmp_path):
    project,repo,_=fixture(tmp_path)
    meta=project/'contract'/'v3'/'metadata.json'
    value=json.loads(meta.read_text(encoding='utf-8'))
    value['workflow_policy']={'restart':'all'}
    meta.write_text(json.dumps(value),encoding='utf-8')
    assert C.check_task(project,project/'contract'/'v3','T-007')['status']=='not_required'
    snap=S.supervisor_snapshot(project)
    assert snap['retry']['initial_attempted'] is False
    assert snap['retry']['quality_retries_used']==0


def test_activation_native_migrates_before_switching_effective_contract(tmp_path):
    project,repo,passed=fixture(tmp_path,already_active=False)
    original=C.read_ledger(project)['tasks']['v2:T-007'].copy()
    outcome=R.activate_contract(project,repo)
    assert outcome['status']=='activated'
    assert outcome['task_continuity']['status']=='migrated'
    assert outcome['workflow_policy']=={'restart':'pending_only'}
    snap=S.supervisor_snapshot(project)
    assert snap['contract_version']==3 and snap['last_completed_task']=='T-006'
    assert snap['retry']['quality_retries_used']==original['quality_retries_used']
    assert snap['retry']['initial_attempted'] is True
    source_unchanged(project,passed)
    assert R.activate_contract(project,repo)['status']=='no_activation'


def test_activation_state_publication_failure_retries_without_double_migration(tmp_path,monkeypatch):
    project,repo,_=fixture(tmp_path,already_active=False)
    original=R.dump_json
    def fail_state(path,value):
        if Path(path).name=='workflow_state.json':
            raise PermissionError('injected state update failure')
        return original(path,value)
    monkeypatch.setattr(R,'dump_json',fail_state)
    with pytest.raises(PermissionError,match='state update failure'):
        R.activate_contract(project,repo)
    # Authoritative v2 remains active; v3 transition is a fully journaled
    # one-file ledger commit, not a silently free retry budget.
    assert R.load_json(C.state_path(project))['contract_version']==2
    assert 'v3:T-007' in C.read_ledger(project)['continuity']
    before=C.ledger_path(project).read_bytes()
    monkeypatch.setattr(R,'dump_json',original)
    recovered=R.activate_contract(project,repo)
    assert recovered['status']=='activated'
    assert recovered['task_continuity']['status']=='already_migrated'
    assert C.ledger_path(project).read_bytes()==before


def test_target_retry_tamper_and_target_contract_drift_block(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    ledger=C.read_ledger(project)
    ledger['tasks']['v3:T-007']['initial_attempted']=False
    C.write_ledger(project,ledger)
    check=C.check_task(project,project/'contract'/'v3','T-007')
    assert check['status']=='blocked'
    assert ('modified outside native path' in check['reason'] or 'retry budget charged without initial attempt' in check['reason'])
    # Restore a valid ledger and prove contract digest pinning.
    ledger['tasks']['v3:T-007']['initial_attempted']=True
    C.write_ledger(project,ledger)
    path=project/'contract'/'v3'/'constraints.md'
    path.write_text(path.read_text(encoding='utf-8')+'\\\\nC-999: new constraint.\\\\n'.replace('\\\\n',chr(10)),encoding='utf-8')
    check=C.check_task(project,project/'contract'/'v3','T-007')
    assert check['status']=='blocked'
    assert 'provenance drift' in check['reason']


def test_missing_source_counter_but_provider_usage_blocks_free_initial(tmp_path):
    project,repo,_=fixture(tmp_path)
    ledger=C.read_ledger(project)
    del ledger['tasks']['v2:T-007']
    C.write_ledger(project,ledger)
    with (project/'runtime'/'executor_token_usage.jsonl').open('w',encoding='utf-8') as f:
        f.write(json.dumps({'contract_version':2,'task':'T-007','total_tokens':1500})+chr(10))
    with pytest.raises(C.ContinuityError,match='usage exists without source'):
        C.recover(project,repo,2,3)
    assert 'v3:T-007' not in C.read_ledger(project)['tasks']


def test_legacy_unclassified_source_attempts_fail_closed(tmp_path):
    project,repo,_=fixture(tmp_path)
    ledger=C.read_ledger(project)
    del ledger['tasks']['v2:T-007']
    ledger['legacy_unclassified_attempts']['v2:T-007']=2
    C.write_ledger(project,ledger)
    with pytest.raises(C.ContinuityError,match='legacy source attempts'):
        C.recover(project,repo,2,3)


def test_preflight_manual_check_does_not_run_model_for_wrong_retry_kind(tmp_path,monkeypatch):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    config=project/'runtime'/'runtime.json'
    config.write_text('{}',encoding='utf-8')
    def unexpected(**kw):raise AssertionError('PC should not execute')
    monkeypatch.setattr(MCP,'_preflight_gate',unexpected)
    result=MCP.preflight_check_tool(str(repo),str(config),str(project),
       str(project/'developing'/'tasks'/'T-007.md'),
       str(project/'contract'/'v3'),retry_kind='initial')
    assert result['status']=='retry_classification_required'
    assert result['blocks_executor'] is True


def test_new_virgin_task_may_initial_but_not_retry(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    contract=project/'contract'/'v3'
    state=C.check_task(project,contract,'T-008',require_current=False)['state']
    assert state['initial_attempted'] is False
    task=project/'developing'/'tasks'/'T-008.md'
    assert MCP._budget_block(project=project,contract_path=contract,task_path=task,
         state=state,dispatch_kind='initial',legacy_unclassified_attempts=0) is None
    # Existing budget policy remains untouched; native pending_only dispatcher
    # enforces this stronger inherited-round rule before calling PC or E.
    flow=R.load_json(C.state_path(project))
    flow['last_completed_task']='T-007'
    flow['current_task']='T-008'
    R.dump_json(C.state_path(project),flow)
    blocked=MCP._invoke_executor_impl(str(repo),str(repo/'runtime.json'),
        str(project),str(task),str(contract),retry_kind='quality_rework')
    assert blocked['reason']=='initial_attempt_required'


def test_source_contract_changed_after_migration_must_fail_closed(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    file=project/'contract'/'v2'/'implementation.md'
    file.write_text(file.read_text(encoding='utf-8')+'additional note',encoding='utf-8')
    blocked=C.check_task(project,project/'contract'/'v3','T-007')
    assert blocked['status']=='blocked'
    assert 'provenance drift' in blocked['reason']


def test_unauthorized_restart_policy_change_cannot_disable_continuity(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    metadata=project/'contract'/'v3'/'metadata.json'
    raw=json.loads(metadata.read_text(encoding='utf-8'))
    raw['workflow_policy']={'restart':'all'}
    metadata.write_text(json.dumps(raw),encoding='utf-8')
    blocked=C.check_task(project,project/'contract'/'v3','T-007')
    assert blocked['status']=='blocked'
    assert 'policy drift' in blocked['reason']


def test_approved_explicit_reset_preserves_provenance_and_increments_round(tmp_path):
    project,repo,_=fixture(tmp_path,quality=3)
    C.recover(project,repo,2,3)
    state=R.load_json(C.state_path(project))
    state['status']='blocked'
    state['retry_exhaustion']={'contract_version':3,'task':'T-007',
                               'budget':'quality_rework','used':3,'limit':3}
    R.dump_json(C.state_path(project),state)
    R.resolve_retry_exhaustion(project,'reset-and-continue-executor')
    snap=S.supervisor_snapshot(project)
    assert snap['retry']['execution_round']==2
    assert snap['retry']['initial_attempted'] is False
    assert snap['retry']['quality_retries_used']==0
    assert C.check_task(project,project/'contract'/'v3','T-007')['status']=='verified'


def test_future_pending_task_cannot_be_dispatched_before_native_pass(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    contract=project/'contract'/'v3'
    blocked=C.check_task(project,contract,'T-008')
    assert blocked['status']=='blocked'
    assert 'out of pending_only order' in blocked['reason']
    attempt=MCP._invoke_executor_impl(str(repo),str(repo/'missing.json'),str(project),
       str(project/'developing'/'tasks'/'T-008.md'),str(contract),retry_kind='initial')
    assert attempt['status']=='continuity_blocked'
    assert C.read_ledger(project)['tasks']['v3:T-008']==C.empty_state()


def test_active_v3_cannot_route_attempt_via_old_v2_contract(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    blocked=C.check_task(project,project/'contract'/'v2','T-007')
    assert blocked['status']=='blocked'
    assert 'active pending_only Contract' in blocked['reason'] or 'non-active Contract' in blocked['reason']
    result=MCP._invoke_executor_impl(str(repo),str(repo/'runtime.json'),str(project),
       str(project/'developing'/'tasks'/'T-007.md'),
       str(project/'contract'/'v2'),retry_kind='quality_rework')
    assert result['status']=='continuity_blocked'


def test_recovery_does_not_ignore_unproven_completed_target_attempt(tmp_path):
    project,repo,_=fixture(tmp_path)
    ledger=C.read_ledger(project)
    ledger['tasks']['v3:T-006']=dict(execution_round=1,initial_attempted=True,
        quality_retries_used=0,abnormal_retries_used=0)
    C.write_ledger(project,ledger)
    with pytest.raises(C.ContinuityError,match='unaudited earlier attempts'):
        C.recover(project,repo,2,3)
    assert 'v3:T-007' not in C.read_ledger(project)['tasks']


def test_next_task_keeps_prior_pending_task_provenance_under_audit(tmp_path):
    project,repo,_=fixture(tmp_path)
    C.recover(project,repo,2,3)
    workflow=R.load_json(C.state_path(project))
    workflow['last_completed_task']='T-007'
    workflow['current_task']='T-008'
    R.dump_json(C.state_path(project),workflow)
    assert C.check_task(project,project/'contract'/'v3','T-008')['status']=='verified'
    ledger=C.read_ledger(project)
    ledger['continuity']['v3:T-007']['source_retry_state_sha256']='0'*64
    C.write_ledger(project,ledger)
    check=C.check_task(project,project/'contract'/'v3','T-008')
    assert check['status']=='blocked'
    assert 'source retry state drift' in check['reason']


def test_multiline_contract_task_identity_is_first_class():
    base=("## T-007\n\nTitle: Task seven\n\nGoal: Complete seven.\n"
          "Requirements:\n- REQ-001\n- REQ-002\n"
          "Acceptance:\n- AC-001\n- AC-002\n"
          "Dependencies:\n- T-006\nForbidden Scope:\n- private/**\n"
          "Implementation Notes:\n- original\nRequired Verification:\n- pytest\n")
    upgraded=base.replace("Implementation Notes:\n- original",
        "Allowed Scope:\n- precise.py\nImplementation Notes:\n- new notes")
    assert C.identity(base)==C.identity(upgraded)
    assert C.identity(base)['requirements']
    assert C.identity(base)['acceptance']
    assert C.identity(base)['dependencies']==['T-006']
    assert C.identity(base.replace('Goal: Complete seven.','Goal: Different objective.'))!=C.identity(upgraded)


def test_registered_native_recovery_tool_uses_real_mcp_sdk(tmp_path):
    import asyncio
    project,repo,_=fixture(tmp_path)
    server=MCP.build_server()
    args={'project':str(project),'repository':str(repo),
          'source_version':2,'target_version':3,
          'expected_state_sha256':C.file_sha(C.state_path(project)),
          'expected_ledger_sha256':C.file_sha(C.ledger_path(project))}
    async def invoke():
        return await server.call_tool('psc_recover_task_continuity',args)
    result=asyncio.run(invoke())
    assert result.structured_content['status']=='migrated'
    assert C.check_task(project,project/'contract'/'v3','T-007')['status']=='verified'


def test_external_restart_all_contract_cannot_bypass_active_pending_only(tmp_path):
    project,repo,_=fixture(tmp_path)
    outside=tmp_path/'outside'/'v3'
    outside.mkdir(parents=True)
    (outside/'metadata.json').write_text(json.dumps({
        'version':3,'workflow_policy':{'restart':'all'},'status':'approved'
    }),encoding='utf-8')
    assert C.check_task(project,outside,'T-007')['status']=='blocked'
    result=MCP._invoke_executor_impl(str(repo),str(repo/'runtime.json'),str(project),
         str(project/'developing'/'tasks'/'T-007.md'),str(outside),
         retry_kind='initial')
    assert result['status']=='continuity_blocked'
