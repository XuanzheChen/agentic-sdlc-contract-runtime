"""Audited Supervisor evidence references for a *fresh* read-only PC run.

Receipts are immutable per active Contract/Task and never rewrite a Checker
report, approval, Contract, runtime configuration or workflow State. A receipt
only nominates an existing repository file for the next independent PC review.
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

import workflow_registry
import task_continuity

MAX_SUBMISSIONS = 12
MAX_SOURCE_BYTES = 2 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^T-\d{3,}$")
EXCLUDED_PARTS = frozenset({
    '.git', '.agentic-sdlc', '.codex', '.claude', 'node_modules',
    '__pycache__', 'runtime', '.ssh',
})
SECRET_NAME = re.compile(
    r"(^\.env(?:\.|$)|(?:^|[._-])(secret|credential|password|private[-_]?key|"
    r"api[-_]?key|access[-_]?token)(?:[._-]|$)|^id_(rsa|ed25519)$)",
    re.IGNORECASE,
)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _receipt_dir(project: Path, version: int, task_id: str) -> Path:
    if type(version) is not int or version < 1 or not TASK_RE.fullmatch(task_id):
        raise ValueError('invalid evidence receipt Contract/Task identity')
    return Path(project).resolve() / 'runtime' / 'preflight' / 'submitted_evidence' / f'v{version}' / task_id


def _safe_source(repository: Path, relative: str) -> tuple[Path, str, bytes]:
    if not isinstance(relative, str) or not relative or len(relative) > 320:
        raise ValueError('evidence_path must be a bounded repository-relative file path')
    if '\\' in relative or relative.startswith('/') or ':' in relative or '\x00' in relative:
        raise ValueError('evidence_path must be a normalized relative POSIX path')
    parts = relative.split('/')
    if any(p in {'', '.', '..'} for p in parts):
        raise ValueError('evidence_path contains unsafe path components')
    if any(p.lower() in EXCLUDED_PARTS for p in parts) or any(SECRET_NAME.search(p) for p in parts):
        raise ValueError('evidence_path targets a runtime, private, or credential location')
    root = Path(repository).resolve()
    path = root.joinpath(*parts)
    if path.is_symlink():
        raise ValueError('evidence source cannot be a symlink')
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('evidence source is missing or outside the repository')
    if resolved.relative_to(root).as_posix() != relative:
        raise ValueError('evidence path must be canonical')
    if any((root.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts))):
        raise ValueError('evidence source cannot traverse symlinks')
    if resolved.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('evidence source exceeds the size limit')
    data = resolved.read_bytes()
    if len(data) > MAX_SOURCE_BYTES or b'\x00' in data:
        raise ValueError('evidence source must be a bounded text file')
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('evidence source must be UTF-8') from exc
    return resolved, relative, data


def load_submissions(
    project: Path, repository: Path, version: int, task_id: str,
    task_sha256: str, contract_sha256: str,
) -> list[dict[str, Any]]:
    """Return verified receipt metadata, with each source rehashed on every read.

    This is intentionally fail-closed: a changed, removed or corrupted submitted
    file cannot silently drop out of the PC bundle and leave an old ALLOW usable.
    """
    folder = _receipt_dir(project, version, task_id)
    if not folder.exists():
        return []
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('submitted evidence directory is invalid')
    files = sorted(folder.iterdir())
    if len(files) > MAX_SUBMISSIONS or any(p.suffix != '.json' or p.is_symlink() or not p.is_file() for p in files):
        raise ValueError('submitted evidence receipt set is malformed or exceeds limit')
    entries = []
    seen = set()
    for path in files:
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(receipt, dict) or receipt.get('schema_version') != 1:
            raise ValueError('submitted evidence receipt schema invalid')
        if (receipt.get('contract_version') != version or receipt.get('task_id') != task_id
            or receipt.get('task_sha256') != task_sha256
            or receipt.get('contract_files_sha256') != contract_sha256):
            raise ValueError('submitted evidence receipt has stale Contract/Task binding')
        relative = receipt.get('path')
        if relative in seen:
            raise ValueError('duplicate submitted evidence path')
        seen.add(relative)
        _source, normalized, data = _safe_source(repository, relative)
        if receipt.get('sha256') != _hash(data) or receipt.get('bytes') != len(data):
            raise ValueError(f'submitted evidence changed since submission: {relative}')
        if (not isinstance(receipt.get('finding_id'), str)
            or not isinstance(receipt.get('reason'), str)
            or not receipt['reason'].strip()
            or not isinstance(receipt.get('source_pc_run_id'), str)):
            raise ValueError('submitted evidence provenance is malformed')
        entries.append({
            'path': normalized, 'sha256': receipt['sha256'],
            'bytes': len(data), 'finding_id': receipt['finding_id'],
            'reason': receipt['reason'], 'source_pc_run_id': receipt['source_pc_run_id'],
            'receipt_sha256': _hash(path.read_bytes()),
        })
    return entries


def submit_evidence(
    project: Path, repository: Path, task: Path, contract: Path,
    evidence_path: str, finding_id: str, reason: str,
    expected_state_sha256: str,
    previous_review: Path | None = None,
) -> dict[str, Any]:
    """Register an already-existing source referenced by the bound Task/Contract.

    Requires the latest *finalized*, ledger-matched DENY with a blocking,
    Supervisor-owned finding. Never grants admission or changes workflow state.
    """
    import preflight_checker as pc  # Avoid import cycle: PC also reads receipts.

    project, repository = Path(project).resolve(), Path(repository).resolve()
    task, contract = Path(task).resolve(), Path(contract).resolve()
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 1600
        or not isinstance(finding_id, str) or not finding_id.strip()):
        raise ValueError('non-empty bounded reason and finding_id are required')
    if not isinstance(expected_state_sha256, str) or not SHA_RE.fullmatch(expected_state_sha256):
        raise ValueError('expected_state_sha256 is required')

    lock = project / 'runtime' / task_continuity.LOCK_NAME
    with workflow_registry._locked(lock):
        state_bytes = (project / 'runtime' / 'workflow_state.json').read_bytes()
        if _hash(state_bytes) != expected_state_sha256:
            raise ValueError('workflow_state_conflict: fresh Snapshot required')
        state = json.loads(state_bytes)
        version = pc._contract_version(contract)
        task_id = pc._task_id_from_path(task, task.read_text(encoding='utf-8'))
        if (version is None or contract != project / 'contract' / f'v{version}'
            or task != project / 'developing' / 'tasks' / f'{task_id}.md'
            or state.get('contract_version') != version or state.get('current_task') != task_id
            or state.get('status') in {'workflow_passed', 'failed'}):
            raise ValueError('evidence submission must target the active Contract/Task')
        metadata = json.loads((contract / 'metadata.json').read_text(encoding='utf-8'))
        if metadata.get('status') != 'approved' or metadata.get('version') != version:
            raise ValueError('evidence submission requires an Approved Contract')
        project_info_path = project / 'runtime' / 'project.json'
        if project_info_path.is_file():
            project_info = json.loads(project_info_path.read_text(encoding='utf-8'))
            if Path(project_info['repository']).resolve() != repository:
                raise ValueError('repository does not match the active PSC project')
        publication = pc.preflight_publication_status(project)
        if publication['admission_finalized'] is not True or publication['admission_status'] != pc.STATUS_DENIED:
            raise ValueError('a finalized, ledger-matched PC DENY is required')
        latest = json.loads(pc.preflight_latest_path(project).read_text(encoding='utf-8'))
        if (latest.get('decision') != 'DENY' or latest.get('reason') != pc.REASON_DENIED
            or latest.get('run_id') != publication['latest_attempt_run_id']
            or latest.get('task_id') != task_id or latest.get('contract_version') != version):
            raise ValueError('latest PC DENY does not match the active Task')
        findings = latest.get('findings') or []
        if not any(isinstance(item, dict) and item.get('id') == finding_id
                   and item.get('severity') == 'blocking'
                   and item.get('resolution_owner') == 'supervisor' for item in findings):
            raise ValueError('finding is not a blocking Supervisor-owned PC issue')

        task_raw = task.read_text(encoding='utf-8')
        task_hash = pc._sha256_text(task_raw)
        contract_hash = pc._contract_binding(contract)
        bindings = latest.get('bindings') or {}
        if (bindings.get('task_sha256') != task_hash
            or bindings.get('contract_files_sha256') != contract_hash):
            raise ValueError('PC DENY was based on a stale Task/Contract')
        source, normalized, data = _safe_source(repository, evidence_path)
        packet = pc.executor_runtime.build_task_contract_packet(
            {'id': task_id, 'text': task_raw}, contract)
        review = ''
        if previous_review is not None:
            review_path = Path(previous_review).resolve()
            approved_review_path = project / 'developing' / 'artifacts' / task_id / 'review.md'
            if review_path != approved_review_path or not review_path.is_file():
                raise ValueError('previous_review must be the active Task Review file')
            review = review_path.read_text(encoding='utf-8')
            if bindings.get('review_sha256') != pc._sha256_text(review):
                raise ValueError('previous_review has changed since the bound PC DENY')
        references = '\n'.join([task_raw, packet, review])
        if not re.search(r'(?<![\w./-])' + re.escape(normalized) + r'(?![\w./-])', references):
            raise ValueError('evidence path must be explicitly referenced by Task, Contract packet or Review')

        current = load_submissions(project, repository, version, task_id, task_hash, contract_hash)
        if any(item['path'] == normalized for item in current):
            raise ValueError('evidence file already submitted for this Task/Contract')
        if len(current) >= MAX_SUBMISSIONS:
            raise ValueError('submitted evidence file limit reached')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        record = {
            'schema_version': 1, 'submitted_at': datetime.now(timezone.utc).isoformat(),
            'contract_version': version, 'task_id': task_id,
            'task_sha256': task_hash, 'contract_files_sha256': contract_hash,
            'path': normalized, 'sha256': _hash(data), 'bytes': len(data),
            'finding_id': finding_id, 'reason': reason.strip(),
            'source_pc_run_id': latest['run_id'],
            'source_pc_report_sha256': latest.get('report_sha256'),
            'expected_state_sha256': expected_state_sha256,
            'admission_granted': False,
        }
        folder = _receipt_dir(project, version, task_id)
        folder.mkdir(parents=True, exist_ok=True)
        receipt = folder / f'{stamp}-{uuid.uuid4().hex}.json'
        # Exclusive creation; never modify or replace a past receipt.
        with receipt.open('x', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(record, indent=2, ensure_ascii=False) + '\n')
            output.flush()
            os.fsync(output.fileno())
        return {
            'status': 'evidence_submitted',
            'task_id': task_id, 'contract_version': version,
            'evidence_path': normalized, 'evidence_sha256': record['sha256'],
            'receipt_path': str(receipt), 'receipt_sha256': _hash(receipt.read_bytes()),
            'source_pc_run_id': latest['run_id'],
            'requires_fresh_preflight': True, 'admission_granted': False,
            'executor_attempt_charged': False,
        }
