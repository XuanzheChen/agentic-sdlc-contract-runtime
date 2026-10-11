"""PSC pending_only Contract-version Executor continuity.

One authoritative atomic ledger transaction: inherited vN Task retry state and
its provenance are published together in executor_attempts.json schema v2.
Source entries are never overwritten. Reads/dispatch fail closed if the
continuity record is absent, drifted, incomplete or incompatible.
"""
from __future__ import annotations
import hashlib
import json
import re
import tempfile
import os
from pathlib import Path
from typing import Any
import psc_runtime
import workflow_registry

FIELDS = ('execution_round','initial_attempted','quality_retries_used','abnormal_retries_used')
C_RE = re.compile(r'T-\d{3,}\Z')
FILE_NAMES = ('metadata.json','requirements.md','acceptance.md','implementation.md','constraints.md','tasks.md')
LOCK_NAME = 'executor-dispatch.lock'
MAX_QUALITY_RETRIES = 3
MAX_ABNORMAL_RETRIES = 3

class ContinuityError(ValueError):
    """No Executor admission without validated continuity."""

def canonical(value: Any) -> str:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)

def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()

def file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def state_path(project: Path) -> Path:
    return project/'runtime'/'workflow_state.json'

def ledger_path(project: Path) -> Path:
    return project/'runtime'/'executor_attempts.json'

def empty_state() -> dict[str, Any]:
    return dict(execution_round=1,initial_attempted=False,quality_retries_used=0,abnormal_retries_used=0)

def normalize(value: Any) -> dict[str, Any]:
    if not isinstance(value,dict):
        raise ContinuityError('retry state must be a JSON object')
    if set(FIELDS)-set(value):
        raise ContinuityError('retry state is missing required fields')
    if type(value['execution_round']) is not int or value['execution_round']<1:
        raise ContinuityError('invalid execution_round')
    if type(value['initial_attempted']) is not bool:
        raise ContinuityError('invalid initial_attempted')
    for name in FIELDS[2:]:
        if type(value[name]) is not int or value[name]<0:
            raise ContinuityError('invalid retry count: '+name)
    if (not value['initial_attempted'] and
            (value['quality_retries_used'] or value['abnormal_retries_used'])):
        raise ContinuityError('retry budget charged without initial attempt')
    return {name:value[name] for name in FIELDS}

def read_ledger(project: Path) -> dict[str, Any]:
    path=ledger_path(project)
    if not path.is_file():
        return {'schema_version':2,'tasks':{},'legacy_unclassified_attempts':{},'continuity':{}}
    try:
        obj=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError) as exc:
        raise ContinuityError('Executor attempt ledger unreadable') from exc
    if not isinstance(obj,dict) or obj.get('schema_version')!=2:
        raise ContinuityError('Executor attempt ledger schema is not v2')
    if not isinstance(obj.get('tasks'),dict) or not isinstance(obj.get('legacy_unclassified_attempts',{}),dict):
        raise ContinuityError('Executor attempt ledger structure invalid')
    obj.setdefault('legacy_unclassified_attempts',{})
    if not isinstance(obj.get('continuity',{}),dict):
        raise ContinuityError('Executor continuity metadata invalid')
    return obj

def write_ledger(project: Path, obj: dict[str,Any]) -> None:
    path=ledger_path(project)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='executor-continuity-',suffix='.tmp',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:
            json.dump(obj,f,ensure_ascii=False,indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def _metadata(contract: Path) -> dict[str,Any]:
    try:
        meta=json.loads((contract/'metadata.json').read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError) as exc:
        raise ContinuityError('Contract metadata unavailable') from exc
    if not isinstance(meta,dict):
        raise ContinuityError('Invalid Contract metadata')
    return meta

def contract_digest(contract: Path) -> str:
    return sha({p:file_sha(contract/p) for p in FILE_NAMES})

def _blocks(contract: Path) -> dict[str,str]:
    txt=(contract/'tasks.md').read_text(encoding='utf-8')
    blocks=psc_runtime.task_blocks(txt)
    if len(blocks)!=len({k for k,_ in blocks}):
        raise ContinuityError('duplicate task identity')
    return dict(blocks)

def _value(block: str, label: str) -> str:
    match=re.search(r'(?im)^'+re.escape(label)+r':[ \t]*(.*)$',block)
    if not match:
        return ''
    inline=' '.join(match.group(1).split()).casefold()
    if inline:
        return inline
    # PSC Contract tasks also permit bullet-list forms of Requirements,
    # Acceptance and Dependencies; they are not a new task merely because
    # a multiline list follows the field label.
    entries=psc_runtime.task_field_values(block,label)
    return ', '.join(sorted(entry.casefold() for entry in entries))



def identity(block: str) -> dict[str,Any]:
    """Immutable Task identity, excluding explicitly revisable implementation plans.

    Changes to write permissions, Implementation Notes, or Required Verification
    don't establish a new task. Goal, title, REQ/AC, dependencies and Forbidden
    Scope must remain equal across pending_only revisions.
    """
    ident={
        'title':_value(block,'Title'), 'goal':_value(block,'Goal'),
        'requirements':_value(block,'Requirements'),
        'acceptance':_value(block,'Acceptance Criteria') or _value(block,'Acceptance'),
        'dependencies':sorted(psc_runtime.task_field_values(block,'Dependencies') or
                              set(re.findall(r'T-\d{3,}',_value(block,'Dependencies')))),
        'forbidden_scope':sorted(psc_runtime.task_field_values(block,'Forbidden Scope') or
                                  ({_value(block,'Forbidden Scope')} if _value(block,'Forbidden Scope') not in {'','none'} else set())),
    }
    if not all(ident[k] for k in ('title','goal','requirements','acceptance')):
        raise ContinuityError('Task lacks stable identity fields')
    return ident

def _context(project: Path, target_version: int, repository: Path | None = None) -> dict[str,Any] | None:
    target=project/'contract'/f'v{target_version}'
    meta=_metadata(target)
    if meta.get('workflow_policy')!={'restart':'pending_only'}:
        return None
    source_version=meta.get('supersedes')
    if type(source_version) is not int or not (1<=source_version<target_version):
        raise ContinuityError('pending_only requires approved, explicit source supersedes')
    source=project/'contract'/f'v{source_version}'
    sm=_metadata(source)
    if meta.get('status')!='approved' or sm.get('status') not in {'approved','superseded'}:
        raise ContinuityError('pending_only Contract lineage not approved')
    if sm.get('version')!=source_version or meta.get('version')!=target_version:
        raise ContinuityError('Contract version/lineage mismatch')
    if repository is not None:
        source_check=psc_runtime.validate_contract(source,repository)
        if not source_check.get('valid'):
            raise ContinuityError('source Contract failed validation: '+str(source_check.get('errors',[])))
        result=psc_runtime.validate_contract(target,repository)
        if not result.get('valid'):
            raise ContinuityError('target Contract failed validation: '+str(result.get('errors',[])))
    return {'source_version':source_version,'target_version':target_version,
            'source_contract':source,'target_contract':target,
            'source_digest':contract_digest(source),'target_digest':contract_digest(target),
            'source_blocks':_blocks(source),'target_blocks':_blocks(target)}

def _boundary(project: Path, context: dict[str,Any], state: dict[str,Any]) -> list[str]:
    current=list(context['target_blocks'])
    source=list(context['source_blocks'])
    completed=state.get('last_completed_task')
    if completed is not None and (completed not in current or completed not in source):
        raise ContinuityError('last completed Task missing in Contract lineage')
    if completed is not None:
        prefix=current[:current.index(completed)+1]
        if prefix!=source[:len(prefix)]:
            raise ContinuityError('completed Task order drift')
    else:
        prefix=[]
    # Preserve all previous tasks' semantic identities, not only T-### IDs.
    for task_id in set(source)&set(current):
        if sha(identity(context['source_blocks'][task_id]))!=sha(identity(context['target_blocks'][task_id])):
            raise ContinuityError('Task identity drift: '+task_id)
    return [task for task in current if task not in prefix]

def _record(context: dict[str,Any],task_id: str, source: dict[str,Any]|None, state: dict[str,Any]) -> dict[str,Any]:
    from_contract=context['source_blocks'].get(task_id)
    to_contract=context['target_blocks'][task_id]
    return {
       'source_version':context['source_version'],'target_version':context['target_version'],
       'task':task_id,'source_key':f"v{context['source_version']}:{task_id}",
       'target_key':f"v{context['target_version']}:{task_id}",
       'source_task_present':from_contract is not None,
       'source_contract_sha256':context['source_digest'],
       'target_contract_sha256':context['target_digest'],
       'source_task_sha256':sha(from_contract) if from_contract is not None else None,
       'target_task_sha256':sha(to_contract),
       'task_identity_sha256':sha(identity(to_contract)),
       'source_retry_state':dict(source) if source is not None else None,
       'inherited_state':dict(state),
       'source_retry_state_sha256':sha(source) if source is not None else None,
       'initial_state_sha256':sha(state),
       'last_state_sha256':sha(state),
       'migrated_at':psc_runtime.now(),
    }

def _validate(project: Path, context: dict[str,Any], ledger: dict[str,Any], tasks: list[str]) -> None:
    records=ledger.get('continuity',{})
    for task_id in tasks:
        key=f"v{context['target_version']}:{task_id}"
        rec=records.get(key)
        if not isinstance(rec,dict):
            raise ContinuityError('continuity_required: '+key)
        if rec.get('source_version')!=context['source_version'] or rec.get('target_version')!=context['target_version'] or rec.get('task')!=task_id:
            raise ContinuityError('continuity lineage mismatch: '+key)
        source_key=f"v{context['source_version']}:{task_id}"
        if rec.get('source_key')!=source_key or rec.get('target_key')!=key:
            raise ContinuityError('continuity key mismatch: '+key)
        source_raw=ledger['tasks'].get(source_key)
        source=normalize(source_raw) if source_raw is not None else None
        if (rec.get('source_retry_state_sha256')!=(sha(source) if source else None)
                or rec.get('source_retry_state')!=source):
            raise ContinuityError('source retry state drift: '+key)
        if rec.get('initial_state_sha256')!=sha(rec.get('inherited_state')):
            raise ContinuityError('inherited source evidence tampered: '+key)
        target_raw=ledger['tasks'].get(key)
        if target_raw is None:
            raise ContinuityError('migrated retry state missing: '+key)
        target=normalize(target_raw)
        if rec.get('last_state_sha256')!=sha(target):
            raise ContinuityError('migrated retry state modified outside native path: '+key)
        src=context['source_blocks'].get(task_id)
        tgt=context['target_blocks'][task_id]
        if (rec.get('source_task_present')!=(src is not None)
            or rec.get('source_contract_sha256')!=context['source_digest']
            or rec.get('target_contract_sha256')!=context['target_digest']
            or rec.get('source_task_sha256')!=(sha(src) if src is not None else None)
            or rec.get('target_task_sha256')!=sha(tgt)
            or rec.get('task_identity_sha256')!=sha(identity(tgt))):
            raise ContinuityError('Contract/task continuity provenance drift: '+key)

def check_task(project: Path, contract: Path, task_id: str, *, require_current: bool = True) -> dict[str,Any]:
    """Read-only hard admission gate used by Snapshot, Preflight and dispatch."""
    project=Path(project).resolve()
    contract=Path(contract).resolve()
    # The effective workflow Contract is authoritative, NOT the caller's
    # contract argument. Otherwise a caller could point to a second
    # restart:all directory and evade pending_only continuity entirely.
    if state_path(project).is_file():
        try:
            workflow=psc_runtime.load_json(state_path(project))
            version=workflow.get('contract_version') if isinstance(workflow,dict) else None
            active_contract=(project/'contract'/f'v{version}').resolve()
            if active_contract.is_dir() and active_contract != contract:
                policy=_metadata(active_contract).get('workflow_policy')
                if policy=={'restart':'pending_only'}:
                    return {'status':'blocked','task':task_id,
                            'reason':'caller Contract is not the active pending_only Contract',
                            'retryable':False}
        except (OSError,ValueError,json.JSONDecodeError):
            return {'status':'blocked','task':task_id,
                    'reason':'unable to verify authoritative Contract path','retryable':False}
    # Real PSC Contract paths cannot be routed through an older version once
    # the workflow has activated a newer Contract. Keep generic external-path
    # legacy test fixtures compatible; runtime-scope paths are strict.
    if contract.parent == (project/'contract').resolve() and state_path(project).is_file():
        try:
            workflow=psc_runtime.load_json(state_path(project))
            active=workflow.get('contract_version') if isinstance(workflow,dict) else None
            if contract.name != f'v{active}':
                return {'status':'blocked','task':task_id,
                        'reason':'non-active Contract cannot dispatch Executor','retryable':False}
        except (OSError,ValueError,json.JSONDecodeError):
            return {'status':'blocked','task':task_id,
                    'reason':'cannot verify active Contract','retryable':False}
    # Ordinary restart policies retain their historical semantics. Once any
    # lineage has an authoritative continuity record it can NEVER be silently
    # bypassed by editing metadata.policy to restart:all.
    try:
        ledger=read_ledger(project)
        records=ledger.get('continuity',{})
        version=int(Path(contract).name[1:])
        known=any(key.startswith(f'v{version}:') for key in records)
    except (OSError,ValueError,TypeError) as exc:
        return {'status':'blocked','task':task_id,'reason':str(exc),'retryable':False}
    try:
        meta=_metadata(Path(contract))
    except (OSError,ValueError) as exc:
        # Legacy version-only contract fixtures never declare policy. A missing
        # active approved metadata file, however, cannot waive a known lineage.
        if known:
            return {'status':'blocked','task':task_id,'reason':'continuity Contract metadata missing','retryable':False}
        return {'status':'not_required','task':task_id}
    if meta.get('workflow_policy')!={'restart':'pending_only'}:
        if known:
            return {'status':'blocked','task':task_id,
                    'reason':'continuity Contract policy drift','retryable':False}
        return {'status':'not_required','task':task_id}
    try:
        state=psc_runtime.load_json(state_path(project))
        if not isinstance(state,dict):
            raise ContinuityError('workflow state invalid')
        version=state.get('contract_version')
        if contract.resolve()!=(project/'contract'/f'v{version}').resolve():
            raise ContinuityError('non-active Contract cannot dispatch E')
        if not C_RE.fullmatch(task_id):
            raise ContinuityError('invalid task identity')
        context=_context(project,version)
        if context is None:
            return {'status':'not_required','task':task_id}
        pending=_boundary(project,context,state)
        if task_id not in pending:
            raise ContinuityError('completed or unknown task cannot be dispatched')
        if require_current and (not pending or task_id != pending[0]):
            raise ContinuityError('Task dispatch out of pending_only order: '+task_id)
        if require_current and state.get('current_task') not in (None,pending[0]):
            raise ContinuityError('workflow current_task conflicts with completed task boundary')
        ledger=read_ledger(project)
        # Existing completed tasks' previous Executor continuity records are
        # still auditable after PASS; don't stop checking T-007 provenance just
        # because the workflow advanced to T-008.
        archived=[
            record.get('task') for key,record in ledger.get('continuity',{}).items()
            if key.startswith(f'v{version}:') and isinstance(record,dict)
            and record.get('task') in context['target_blocks']
        ]
        checked_tasks=list(dict.fromkeys(pending+archived))
        _validate(project,context,ledger,checked_tasks)
        key=f'v{version}:{task_id}'
        return {'status':'verified','task':task_id,'state':normalize(ledger['tasks'][key]),
                'provenance':dict(ledger['continuity'][key]),'source_version':context['source_version']}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'status':'blocked','task':task_id,'reason':str(exc),'retryable':False}

def _migrate_locked(project:Path,repository:Path,source_version:int,target_version:int,
                    expected_state_sha256:str|None=None,
                    expected_ledger_sha256:str|None=None) -> dict[str,Any]:
    state_file=state_path(project)
    raw_state=state_file.read_bytes()
    current_hash=hashlib.sha256(raw_state).hexdigest()
    state=json.loads(raw_state)
    if not isinstance(state,dict) or state.get('contract_version') not in (source_version,target_version):
        raise ContinuityError('workflow active version does not match migration lineage')
    if expected_state_sha256 is not None and expected_state_sha256!=current_hash:
        raise ContinuityError('workflow_state_sha_conflict')
    old_ledger_hash=file_sha(ledger_path(project))
    if expected_ledger_sha256 is not None and expected_ledger_sha256!=old_ledger_hash:
        raise ContinuityError('executor_attempts_sha_conflict')
    context=_context(project,target_version,Path(repository).resolve())
    if context is None or context['source_version']!=source_version:
        raise ContinuityError('no approved pending_only direct lineage')
    pending=_boundary(project,context,state)
    ledger=read_ledger(project)
    entries=ledger['tasks']
    records=ledger.setdefault('continuity',{})
    keys=[f'v{target_version}:{task}' for task in pending]
    # A supposedly completed vN task with unproven vN E attempts is not
    # something a pending_only recovery may silently bless or ignore.
    stray=[k for k in entries if k.startswith(f'v{target_version}:') and
           k not in keys and k not in records]
    if stray:
        raise ContinuityError('target version has unaudited earlier attempts: '+', '.join(sorted(stray)))
    if all(k in records for k in keys):
        _validate(project,context,ledger,pending)
        return {'status':'already_migrated','source_version':source_version,
            'target_version':target_version,'tasks':pending,
            'ledger_sha256':old_ledger_hash,'workflow_state_sha256':current_hash}
    if any(k in records or k in entries for k in keys):
        raise ContinuityError('partial/ambiguous continuity exists; refuse overwrite')
    # Inspect E's independent historical usage ledger before declaring an
    # absent retry row to be truly virgin. A paid E call with a lost/missing
    # counter is *not* grounds for a new free initial.
    usage_file=project/'runtime'/'executor_token_usage.jsonl'
    usage_rows=[]
    if usage_file.is_file():
        try:
            usage_rows=[json.loads(line) for line in usage_file.read_text(
                encoding='utf-8').splitlines() if line.strip()]
        except (OSError,json.JSONDecodeError) as exc:
            raise ContinuityError('cannot audit Executor usage ledger') from exc
        if not all(isinstance(row,dict) for row in usage_rows):
            raise ContinuityError('malformed historical Executor usage')
    for task in pending:
        key=f'v{target_version}:{task}'
        source_key=f'v{source_version}:{task}'
        raw=entries.get(source_key)
        if raw is None:
            if any(str(row.get('task'))==task and
                   (row.get('contract_version')==source_version or
                    row.get('contract')==f'v{source_version}') for row in usage_rows):
                raise ContinuityError('Executor usage exists without source retry record: '+source_key)
            # Legacy/v1 ambiguous use must never become a "free initial".
            old=ledger.get('legacy_unclassified_attempts',{})
            if source_key in old and old[source_key]:
                raise ContinuityError('legacy source attempts cannot be classified: '+source_key)
            state_copy=empty_state()
        else:
            state_copy=normalize(raw)
        rec=_record(context,task,normalize(raw) if raw is not None else None,state_copy)
        entries[key]=state_copy
        records[key]=rec
    write_ledger(project,ledger)
    return {'status':'migrated','source_version':source_version,
        'target_version':target_version,'tasks':pending,
        'source_ledger_sha256':old_ledger_hash,
        'ledger_sha256':file_sha(ledger_path(project)),
        'workflow_state_sha256':current_hash,
        'fresh_preflight_required':True}

def recover(project:Path,repository:Path,source_version:int,target_version:int,
            expected_state_sha256:str|None=None,expected_ledger_sha256:str|None=None) -> dict[str,Any]:
    project=Path(project).resolve()
    with workflow_registry._locked(project/'runtime'/LOCK_NAME):
        return _migrate_locked(project,repository,source_version,target_version,
            expected_state_sha256,expected_ledger_sha256)

def charge_ledger(project:Path,contract:Path,task_id:str,new_state:dict[str,Any]) -> None:
    """Called only under the dispatch lock, updating state+chain SHA atomically."""
    project=Path(project).resolve()
    ledger=read_ledger(project)
    key=f'v{int(contract.name[1:])}:{task_id}'
    check=check_task(project,contract,task_id)
    if check['status']=='blocked':
        raise ContinuityError(check['reason'])
    if check['status']=='verified':
        rec=ledger['continuity'][key]
        if rec['last_state_sha256']!=sha(normalize(ledger['tasks'][key])):
            raise ContinuityError('continuity state drift before charge')
        rec['last_state_sha256']=sha(normalize(new_state))
    ledger['tasks'][key]=new_state
    write_ledger(project,ledger)
