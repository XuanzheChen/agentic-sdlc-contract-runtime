"""Proactive, audited Task-scoped Supervisor selection of PC evidence.

Supervisor chooses *which existing sources the read-only Checker sees*, never
the contents, runtime-computed SHA, final verdict, or any research authorization.
Every mutation publishes an immutable revision before an atomic latest pointer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import preflight_evidence as legacy
import task_continuity
import workflow_registry

MAX_SELECTED = 24
MAX_REASON = 1600


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _folder(project: Path, version: int, task_id: str) -> Path:
    return (Path(project).resolve() / 'runtime' / 'preflight' /
            'evidence_selection' / f'v{version}' / task_id)


def _validate_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_SELECTED:
        raise ValueError(f'{field} must be a list of at most {MAX_SELECTED} paths')
    if any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        raise ValueError(f'{field} must contain unique string paths')
    return value


def _read_latest(folder: Path) -> tuple[dict[str, Any] | None, str | None]:
    latest = folder / 'latest.json'
    if not latest.exists():
        return None, None
    if latest.is_symlink() or not latest.is_file() or folder.is_symlink():
        raise ValueError('PC evidence selection pointer is not a regular file')
    pointer = json.loads(latest.read_text(encoding='utf-8'))
    if not isinstance(pointer, dict) or set(pointer) != {'revision_file', 'revision_sha256'}:
        raise ValueError('PC evidence selection pointer is malformed')
    name, digest = pointer['revision_file'], pointer['revision_sha256']
    if (not isinstance(name, str) or
        not re.fullmatch(r'revision-[0-9a-f]{32}\\.json', name) or
        not isinstance(digest, str) or not legacy.SHA_RE.fullmatch(digest)):
        raise ValueError('PC evidence selection pointer is invalid')
    path = folder / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('PC evidence selection revision is missing')
    raw = path.read_bytes()
    if _hash(raw) != digest:
        raise ValueError('PC evidence selection revision SHA mismatch')
    record = json.loads(raw.decode('utf-8'))
    if not isinstance(record, dict) or record.get('schema_version') != 1:
        raise ValueError('PC evidence selection revision schema invalid')
    return record, digest


def load_selection(
    project: Path | None, repository: Path, version: int, task_id: str,
    task_sha256: str, contract_sha256: str,
) -> dict[str, Any]:
    """Load a revision and verify its *current* Task, Contract and source bytes.

    This runs on each PC gathering/revalidation, so an old ALLOW cannot survive
    a revision change or source drift.
    """
    empty = {'revision_sha256': None, 'include': [], 'exclude': [],
             'task_basis': None, 'reason': None}
    if project is None:
        return empty
    folder = _folder(project, version, task_id)
    record, digest = _read_latest(folder)
    if record is None:
        return empty
    if (record.get('contract_version') != version or record.get('task_id') != task_id
        or record.get('task_sha256') != task_sha256
        or record.get('contract_files_sha256') != contract_sha256):
        raise ValueError('PC evidence selection is bound to a different Task/Contract')
    include, exclude = record.get('include'), record.get('exclude')
    if not isinstance(include, list) or not isinstance(exclude, list):
        raise ValueError('PC evidence selection lists are malformed')
    if len(include) + len(exclude) > MAX_SELECTED:
        raise ValueError('PC evidence selection exceeds its file cap')
    if not isinstance(record.get('reason'), str) or not record['reason'].strip():
        raise ValueError('PC evidence selection is missing an audit reason')
    seen = set()
    for entry in [*include, *exclude]:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'bytes'}:
            raise ValueError('PC evidence selection source entry is malformed')
        path = entry['path']
        if not isinstance(path, str) or path in seen:
            raise ValueError('PC evidence selection contains repeated paths')
        seen.add(path)
        _source, normalized, data = legacy._safe_source(repository, path)
        if normalized != path or _hash(data) != entry['sha256'] or len(data) != entry['bytes']:
            raise ValueError('PC evidence selection source has changed: ' + path)
    return {'revision_sha256': digest, 'include': include, 'exclude': exclude,
            'task_basis': record.get('task_basis'), 'reason': record['reason']}


def get_selection(
    project: Path, repository: Path, task: Path, contract: Path,
) -> dict[str, Any]:
    import preflight_checker as pc
    task_text = Path(task).read_text(encoding='utf-8')
    task_id = pc._task_id_from_path(Path(task), task_text)
    version = pc._contract_version(Path(contract))
    return load_selection(project, repository, version, task_id,
                          pc._sha256_text(task_text), pc._contract_binding(contract))


def set_selection(
    project: Path, repository: Path, task: Path, contract: Path,
    include_paths: list[str], exclude_paths: list[str],
    task_basis: str, reason: str, expected_state_sha256: str,
    expected_selection_sha256: str | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Replace Task-scoped include/exclude lists without any prerequisite PC DENY."""
    import preflight_checker as pc  # Only local import; Checker also uses this module.

    project, repository = Path(project).resolve(), Path(repository).resolve()
    task, contract = Path(task).resolve(), Path(contract).resolve()
    include_paths = _validate_list(include_paths, 'include_paths')
    exclude_paths = _validate_list(exclude_paths, 'exclude_paths')
    if len(include_paths) + len(exclude_paths) > MAX_SELECTED:
        raise ValueError('combined selection exceeds the source cap')
    if set(include_paths) & set(exclude_paths):
        raise ValueError('include_paths and exclude_paths overlap')
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON
        or not isinstance(task_basis, str) or not task_basis.strip()
        or len(task_basis) > 500):
        raise ValueError('selection requires a bounded reason and exact Task/Contract basis')
    if not isinstance(expected_state_sha256, str) or not legacy.SHA_RE.fullmatch(expected_state_sha256):
        raise ValueError('expected_state_sha256 must come from a fresh Supervisor snapshot')
    if expected_selection_sha256 is not None and (
        not isinstance(expected_selection_sha256, str) or
        not legacy.SHA_RE.fullmatch(expected_selection_sha256)
    ):
        raise ValueError('expected_selection_sha256 must be null or a revision SHA')

    settings = pc.preflight_settings(config)
    if not settings['enabled']:
        raise ValueError('PC evidence selection requires Preflight enabled')
    if len(include_paths) > settings['max_files']:
        raise ValueError('selected inclusions exceed user-owned preflight.max_files')

    with workflow_registry._locked(project / 'runtime' / task_continuity.LOCK_NAME):
        state_path = project / 'runtime' / 'workflow_state.json'
        if _hash(state_path.read_bytes()) != expected_state_sha256:
            raise ValueError('workflow_state_conflict: refresh Supervisor snapshot')
        state = json.loads(state_path.read_text(encoding='utf-8'))
        task_text = task.read_text(encoding='utf-8')
        task_id = pc._task_id_from_path(task, task_text)
        version = pc._contract_version(contract)
        if (version is None or contract != project / 'contract' / f'v{version}'
            or task != project / 'developing' / 'tasks' / f'{task_id}.md'
            or state.get('contract_version') != version or
            state.get('current_task') != task_id or
            state.get('status') in {'failed', 'workflow_passed'}):
            raise ValueError('selection must target the currently active PSC Task/Contract')
        metadata = json.loads((contract / 'metadata.json').read_text(encoding='utf-8'))
        if metadata.get('status') != 'approved' or metadata.get('version') != version:
            raise ValueError('selection requires an Approved Contract')
        identity = project / 'runtime' / 'project.json'
        if identity.exists():
            info = json.loads(identity.read_text(encoding='utf-8'))
            if Path(info['repository']).resolve() != repository:
                raise ValueError('selection repository differs from PSC project')
        packet = pc.executor_runtime.build_task_contract_packet(
            {'id': task_id, 'text': task_text}, contract)
        if task_basis.strip() not in (task_text + '\n' + packet):
            raise ValueError('task_basis must quote an exact current Task/Contract requirement')

        # Excluding a Task's concrete Allowed Scope files would conceal a
        # mandatory baseline. Only optional configured or historical submitted
        # candidates are eligible for exclusion (still disclosed to PC).
        task_scope = set(pc._scope_concrete_files(repository,
                          {'id': task_id, 'text': task_text}, 128))
        if task_scope.intersection(exclude_paths):
            raise ValueError('cannot exclude a concrete Task Allowed Scope file')
        project_name = metadata.get('project_name')
        profiles = settings['include_files_by_project']
        configured = set(profiles[project_name] if project_name in profiles else settings['include_files'])
        submitted = legacy.load_submissions(
            project, repository, version, task_id, pc._sha256_text(task_text),
            pc._contract_binding(contract))
        permitted_exclusions = configured | {entry['path'] for entry in submitted}
        if not set(exclude_paths).issubset(permitted_exclusions):
            raise ValueError('can exclude only optional configured or prior submitted candidates')

        folder = _folder(project, version, task_id)
        old, current_sha = _read_latest(folder)
        if current_sha != expected_selection_sha256:
            raise ValueError('evidence_selection_conflict: refresh selection snapshot')

        def inspect(paths: list[str]) -> list[dict[str, Any]]:
            result = []
            for relative in paths:
                _source, normalized, data = legacy._safe_source(repository, relative)
                result.append({'path': normalized, 'sha256': _hash(data), 'bytes': len(data)})
            return result

        include = inspect(include_paths)
        exclude = inspect(exclude_paths)
        stamp = datetime.now(timezone.utc).isoformat()
        record = {
            'schema_version': 1, 'contract_version': version, 'task_id': task_id,
            'task_sha256': pc._sha256_text(task_text),
            'contract_files_sha256': pc._contract_binding(contract),
            'include': include, 'exclude': exclude,
            'task_basis': task_basis.strip(), 'reason': reason.strip(),
            'created_at': stamp, 'previous_selection_sha256': current_sha,
            'workflow_state_sha256': expected_state_sha256,
            'admission_granted': False,
        }
        folder.mkdir(parents=True, exist_ok=True)
        name = 'revision-' + uuid.uuid4().hex + '.json'
        revision = folder / name
        with revision.open('x', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(record, indent=2, ensure_ascii=False) + '\n')
            output.flush()
            os.fsync(output.fileno())
        digest = _hash(revision.read_bytes())
        # Only the pointer is mutable. A failed atomic replace leaves the
        # previous selection authoritative and the new revision audit-only.
        pc._write_json(folder / 'latest.json',
                       {'revision_file': name, 'revision_sha256': digest})
        return {
            'status': 'evidence_selection_updated',
            'contract_version': version, 'task_id': task_id,
            'selection_sha256': digest,
            'previous_selection_sha256': current_sha,
            'include': include, 'exclude': exclude,
            'revision_path': str(revision),
            'requires_fresh_preflight': True,
            'admission_granted': False, 'executor_attempt_charged': False,
        }
