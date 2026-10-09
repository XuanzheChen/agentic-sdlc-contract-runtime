#!/usr/bin/env python3
"""PSC Preflight Checker (PC).

PC is a read-only, fail-closed admission check that runs before every PSC
Executor (E) attempt. It reuses the configured Executor harness -- adapter,
executable, executor home, provider, model, and reasoning effort -- but launches
it in an explicitly restricted, read-only mode from an isolated empty temporary
directory. PC never receives free filesystem access: the runtime gathers a
bounded, task-specific evidence bundle and transports it through a
runtime-owned prompt.

Design invariants
-----------------
* **Independent invocation.** PC is a separate process on the same route as E,
  with its own timeout, its own run id, and its own token/elapsed ledger. It
  never charges an E retry budget.
* **Read-only.** Codex is forced to ``--sandbox read-only
  --ask-for-approval never``. DSH composes a command-line ``--patch`` overlay
  that disables shell, filesystem writes, code editing, MCP/external tools, and
  other dangerous tools; the composed overlay is verified before PC runs and the
  check fails closed when the restrictions cannot be verified.
* **Bounded evidence.** PC sees only what the runtime gathered: the task file,
  the task-scoped Contract packet, the previous Supervisor review, the Contract
  binding, and a bounded set of task-specific repository files with
  deterministic SHA-256 hashes.
* **Fail closed.** An invalid report, schema violation, non-zero exit, timeout,
  unverifiable DSH configuration, binding mismatch, or stale evidence produces
  ``UNKNOWN`` and blocks E. There is no fail-open path other than an explicit
  ``preflight.enabled=false`` in the user-owned runtime configuration.
* **Fresh revalidation.** Immediately before E launch the runtime re-gathers the
  bundle and compares every binding. Any change invalidates the decision.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from adapters import codex as codex_adapter
from adapters import dsh as dsh_adapter
from executor_token_usage import (
    USAGE_FIELDS,
    collect_dsh_invocation_usage,
    dsh_session_snapshot,
    parse_codex_exec_jsonl,
    parse_dsh_headless_json,
    with_efficiency_metrics,
    zero_usage,
)
import invoke_executor as executor_runtime


SCHEMA_VERSION = 1

STATUS_ALLOW = 'allow'
STATUS_DENIED = 'denied'
STATUS_UNKNOWN = 'unknown'
STATUS_DISABLED = 'disabled'
STATUS_NOT_EVALUATED = 'not_evaluated'

DECISION_ALLOW = 'ALLOW'
DECISION_DENY = 'DENY'
DECISION_UNKNOWN = 'UNKNOWN'

DECISIONS = (DECISION_ALLOW, DECISION_DENY)
SEVERITIES = ('blocking', 'advisory')
RESOLUTION_OWNERS = ('supervisor', 'planner', 'runtime')
# Runtime-owned findings win because the Supervisor cannot repair a runtime
# fault, then supervisor-owned (repairable in-session), then planner-owned
# (requires a Contract revision).
RESOLUTION_PRECEDENCE = ('runtime', 'supervisor', 'planner')
REPORT_FIELDS = ('schema_version', 'decision', 'summary', 'findings')
FINDING_FIELDS = ('id', 'severity', 'statement', 'evidence', 'resolution_owner')

# Reasons that mean "E must not launch". They are stable strings so the
# Supervisor can branch on them without parsing prose.
REASON_INVALID_CONFIG = 'preflight_configuration_invalid'
REASON_UNSUPPORTED_ADAPTER = 'preflight_unsupported_adapter'
REASON_EVIDENCE_GATHER_FAILED = 'preflight_evidence_gather_failed'
REASON_PROCESS_FAILED = 'checker_process_failed'
REASON_TIMEOUT = 'checker_timeout'
REASON_EMPTY_RESPONSE = 'checker_response_empty'
REASON_INVALID_RESPONSE = 'invalid_checker_response'
REASON_STALE_EVIDENCE = 'preflight_evidence_stale'
REASON_DSH_UNVERIFIABLE = 'dsh_tool_restrictions_unverifiable'
REASON_LAUNCH_FAILED = 'checker_launch_failed'
REASON_DENIED = 'preflight_denied'

BLOCKING_REASONS = frozenset({
    REASON_INVALID_CONFIG,
    REASON_UNSUPPORTED_ADAPTER,
    REASON_EVIDENCE_GATHER_FAILED,
    REASON_PROCESS_FAILED,
    REASON_TIMEOUT,
    REASON_EMPTY_RESPONSE,
    REASON_INVALID_RESPONSE,
    REASON_STALE_EVIDENCE,
    REASON_DSH_UNVERIFIABLE,
    REASON_LAUNCH_FAILED,
    REASON_DENIED,
})

REPORT_OUTPUT_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': list(REPORT_FIELDS),
    'properties': {
        'schema_version': {'type': 'integer', 'const': 1},
        'decision': {'type': 'string', 'enum': list(DECISIONS)},
        'summary': {'type': 'string', 'minLength': 1},
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': list(FINDING_FIELDS),
                'properties': {
                    'id': {'type': 'string', 'minLength': 1},
                    'severity': {'type': 'string', 'enum': list(SEVERITIES)},
                    'statement': {'type': 'string', 'minLength': 1},
                    'evidence': {
                        'type': 'array',
                        'minItems': 1,
                        'items': {'type': 'string', 'minLength': 1},
                    },
                    'resolution_owner': {
                        'type': 'string',
                        'enum': ['supervisor', 'planner', 'runtime'],
                    },
                },
            },
        },
    },
}

CONTRACT_BIND_FILES = (
    'metadata.json',
    'requirements.md',
    'acceptance.md',
    'implementation.md',
    'constraints.md',
    'tasks.md',
)

ALLOWED_SETTINGS = frozenset({
    'enabled',
    'timeout',
    'include_scope_files',
    'include_files',
    'max_files',
})
DEFAULT_TIMEOUT_FLOOR = 60
DEFAULT_TIMEOUT_CEILING = 1800
MAX_INCLUDED_FILES = 12
MAX_FILE_CHARS = 8000
MAX_TOTAL_INCLUDED_CHARS = 48000
MAX_TASK_CHARS = 16000
MAX_PACKET_CHARS = 24000
MAX_REVIEW_CHARS = 12000
DIAGNOSTIC_CHARS = 4096

# DSH capability groups that a Preflight Checker must not be able to use. Each
# entry is (group, patch id, human description). The generated runtime patch
# disables every id, and the composed profile overlay is verified to leave each
# group disabled.
DSH_RESTRICTIONS: tuple[tuple[str, str, str], ...] = (
    ('shell', 'tool-shell', 'shell / command execution'),
    ('filesystem_write', 'tool-filesystem-write', 'filesystem writes'),
    ('code_editing', 'tool-code-edit', 'code editing and patch application'),
    ('mcp', 'tool-mcp-client', 'MCP client tools'),
    ('external', 'tool-external', 'external/network tools'),
    ('dangerous', 'tool-dangerous', 'dangerous or privileged tools'),
)
DSH_RESTRICTED_IDS = tuple(item[1] for item in DSH_RESTRICTIONS)
DSH_PATCH_HEADER = '# PSC Preflight Checker restriction overlay (runtime-owned, read-only).'

SECRET_PATTERNS = executor_runtime.SECRET_PATTERNS


class PreflightConfigurationError(ValueError):
    """The optional `preflight` block in runtime.json is invalid."""


class PreflightVerificationError(RuntimeError):
    """A hard prerequisite for a trustworthy Preflight Check could not be verified."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode('utf-8'))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r'\1[REDACTED]' if pattern.groups else '[REDACTED]', text)
    return text


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    os.replace(temporary, path)


def _read_bounded(path: Path, limit: int) -> tuple[str, bool]:
    try:
        data = path.read_bytes()
    except OSError:
        return '', False
    text = data.decode('utf-8', errors='replace')
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _task_id_from_path(task_path: Path, task_text: str = '') -> str:
    match = re.search(r'\bT-\d{3,}\b', Path(task_path).name)
    if match:
        return match.group(0)
    match = re.search(r'\bT-\d{3,}\b', task_text)
    return match.group(0) if match else 'T-UNKNOWN'


def _contract_version(contract_path: Path) -> int | None:
    match = re.fullmatch(r'v(\d+)', Path(contract_path).name)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def preflight_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize the optional `preflight` block of runtime.json.

    An absent block means "enabled with defaults": PC runs before every
    Executor attempt unless the user explicitly disables it. Every other
    failure mode is fail-closed.
    """
    executor = config.get('executor') if isinstance(config, dict) else None
    if not isinstance(executor, dict):
        raise PreflightConfigurationError('runtime configuration has no executor block')
    block = config.get('preflight')
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise PreflightConfigurationError('preflight must be an object')
    unknown = sorted(str(key) for key in set(block) - ALLOWED_SETTINGS)
    if unknown:
        raise PreflightConfigurationError(
            'preflight contains unsupported keys: ' + ', '.join(unknown)
        )

    enabled = block.get('enabled', True)
    if not isinstance(enabled, bool):
        raise PreflightConfigurationError('preflight.enabled must be a boolean')

    executor_timeout = executor.get('timeout')
    default_timeout = 900
    if (
        isinstance(executor_timeout, int)
        and not isinstance(executor_timeout, bool)
        and executor_timeout > 0
    ):
        default_timeout = max(
            DEFAULT_TIMEOUT_FLOOR,
            min(executor_timeout, DEFAULT_TIMEOUT_CEILING),
        )
    timeout = block.get('timeout', default_timeout)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise PreflightConfigurationError('preflight.timeout must be a positive integer')

    include_scope_files = block.get('include_scope_files', True)
    if not isinstance(include_scope_files, bool):
        raise PreflightConfigurationError('preflight.include_scope_files must be a boolean')

    max_files = block.get('max_files', MAX_INCLUDED_FILES)
    if (
        not isinstance(max_files, int)
        or isinstance(max_files, bool)
        or not 1 <= max_files <= 64
    ):
        raise PreflightConfigurationError(
            'preflight.max_files must be an integer between 1 and 64'
        )

    raw_include = block.get('include_files', [])
    if not isinstance(raw_include, list):
        raise PreflightConfigurationError('preflight.include_files must be an array of paths')
    include_files: list[str] = []
    for item in raw_include:
        if not isinstance(item, str) or not item.strip():
            raise PreflightConfigurationError(
                'preflight.include_files entries must be non-empty repository-relative paths'
            )
        candidate = item.replace('\\', '/').strip()
        if candidate.startswith('/') or re.match(r'^[A-Za-z]:', candidate):
            raise PreflightConfigurationError(
                f'preflight.include_files entry must be repository-relative: {item!r}'
            )
        parts = [part for part in candidate.split('/') if part not in ('', '.')]
        if not parts or any(part == '..' for part in parts):
            raise PreflightConfigurationError(
                f'preflight.include_files entry escapes the repository: {item!r}'
            )
        normalized = '/'.join(parts)
        if normalized not in include_files:
            include_files.append(normalized)

    return {
        'enabled': enabled,
        'timeout': timeout,
        'include_scope_files': include_scope_files,
        'include_files': include_files,
        'max_files': max_files,
    }


# --------------------------------------------------------------------------
# Checker report schema
# --------------------------------------------------------------------------


def validate_checker_report(value: Any) -> str | None:
    """Return a schema error for a checker report, or None when it is valid."""
    if not isinstance(value, dict):
        return 'checker report must be a JSON object'
    if set(value) != set(REPORT_FIELDS):
        missing = sorted(set(REPORT_FIELDS) - set(value))
        extra = sorted(set(value) - set(REPORT_FIELDS))
        detail = []
        if missing:
            detail.append('missing ' + ', '.join(missing))
        if extra:
            detail.append('unexpected ' + ', '.join(extra))
        return 'checker report must contain exactly ' + ', '.join(REPORT_FIELDS) + ' (' + '; '.join(detail) + ')'
    if value.get('schema_version') != SCHEMA_VERSION:
        return f'checker report schema_version must be {SCHEMA_VERSION}'
    decision = value.get('decision')
    if decision not in DECISIONS:
        return 'checker report decision must be ALLOW or DENY'
    summary = value.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        return 'checker report summary must be a non-empty string'
    findings = value.get('findings')
    if not isinstance(findings, list):
        return 'checker report findings must be an array'

    seen_ids: set[str] = set()
    blocking = 0
    for index, finding in enumerate(findings):
        prefix = f'checker report findings[{index}]'
        if not isinstance(finding, dict):
            return f'{prefix} must be an object'
        if set(finding) != set(FINDING_FIELDS):
            return f'{prefix} must contain exactly ' + ', '.join(FINDING_FIELDS)
        identifier = finding.get('id')
        if not isinstance(identifier, str) or not identifier.strip():
            return f'{prefix}.id must be a non-empty string'
        if identifier in seen_ids:
            return f'{prefix}.id must be unique ({identifier!r} is duplicated)'
        seen_ids.add(identifier)
        severity = finding.get('severity')
        if severity not in SEVERITIES:
            return f'{prefix}.severity must be one of {", ".join(SEVERITIES)}'
        statement = finding.get('statement')
        if not isinstance(statement, str) or not statement.strip():
            return f'{prefix}.statement must be a non-empty string'
        evidence = finding.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            return f'{prefix}.evidence must be a non-empty array of strings'
        if any(not isinstance(item, str) or not item.strip() for item in evidence):
            return f'{prefix}.evidence entries must be non-empty strings'
        if finding.get('resolution_owner') not in RESOLUTION_OWNERS:
            return f'{prefix}.resolution_owner must be one of ' + ', '.join(RESOLUTION_OWNERS)
        if severity == 'blocking':
            blocking += 1

    if decision == DECISION_ALLOW and blocking:
        return 'checker report decision ALLOW must not contain blocking findings'
    if decision == DECISION_DENY and not blocking:
        return 'checker report decision DENY requires at least one blocking finding'
    return None


def _parse_report_text(text: str, *, allow_wrapped: bool) -> tuple[dict[str, Any] | None, str | None]:
    text = (text or '').strip()
    if not text:
        return None, REASON_EMPTY_RESPONSE
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        strict_error = f'checker report is not valid JSON: {exc.msg}'
        if not allow_wrapped:
            return None, strict_error
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        for index, char in enumerate(text):
            if char != '{':
                continue
            try:
                candidate, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue
            if validate_checker_report(candidate) is None:
                candidates.append(candidate)
        return (candidates[-1], None) if candidates else (None, strict_error)
    error = validate_checker_report(value)
    if error is not None:
        return None, error
    return value, None


def decision_resolution_owner(report: dict[str, Any] | None) -> str | None:
    """Return the owner that must act on a report, most-blocking first."""
    if not isinstance(report, dict):
        return None
    owners = {
        finding.get('resolution_owner')
        for finding in report.get('findings', [])
        if isinstance(finding, dict) and finding.get('severity') == 'blocking'
    }
    for owner in RESOLUTION_PRECEDENCE:
        if owner in owners:
            return owner
    return None


# --------------------------------------------------------------------------
# Runtime-bound evidence
# --------------------------------------------------------------------------


def _contract_binding(contract_path: Path) -> str:
    """Deterministic hash over the bound Contract version artifacts."""
    digest = hashlib.sha256()
    root = Path(contract_path)
    for name in CONTRACT_BIND_FILES:
        path = root / name
        digest.update(name.encode('utf-8'))
        try:
            payload = path.read_bytes() if path.is_file() else None
        except OSError:
            payload = None
        digest.update(b'\0present\0' if payload is not None else b'\0missing\0')
        if payload is not None:
            digest.update(payload)
    return digest.hexdigest()


def _safe_repository_file(repository: Path, relative: str) -> tuple[Path, str] | None:
    candidate = (Path(repository) / relative).resolve()
    try:
        resolved_relative = candidate.relative_to(Path(repository).resolve()).as_posix()
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate, resolved_relative


def _scope_concrete_files(repository: Path, task: Any, limit: int) -> list[str]:
    values = executor_runtime._scope_values(task, 'Allowed Scope')
    files: list[str] = []
    for value in values:
        normalized = str(value).replace('\\', '/').strip().rstrip('/')
        if not normalized or normalized.lower() in {'none', '.', '*'}:
            continue
        if any(char in normalized for char in '*?['):
            continue
        if _safe_repository_file(repository, normalized) is None:
            continue
        if normalized not in files:
            files.append(normalized)
        if len(files) >= limit:
            break
    return files


def gather_evidence(
    config: dict[str, Any],
    repository: Path,
    task_path: Path,
    contract_path: Path,
    previous_review_path: Path | None = None,
) -> dict[str, Any]:
    """Gather the bounded, task-specific evidence bundle PC is allowed to see.

    Everything here is produced by the runtime from explicit paths. PC itself
    never walks the filesystem.
    """
    settings = preflight_settings(config)
    repository = Path(repository).resolve()
    task_path = Path(task_path)
    contract_path = Path(contract_path)

    task_text_raw = task_path.read_text(encoding='utf-8')
    task_truncated = len(task_text_raw) > MAX_TASK_CHARS
    task_text = task_text_raw[:MAX_TASK_CHARS]
    task_id = _task_id_from_path(task_path, task_text_raw)
    task = {'id': task_id, 'text': task_text_raw}

    packet_text_raw = executor_runtime.build_task_contract_packet(task, contract_path)
    packet_truncated = len(packet_text_raw) > MAX_PACKET_CHARS
    packet_text = packet_text_raw[:MAX_PACKET_CHARS]

    review_text = ''
    review_present = False
    if previous_review_path is not None:
        review_path = Path(previous_review_path)
        if review_path.is_file():
            review_text = review_path.read_text(encoding='utf-8')
            review_present = True
    review_raw = review_text
    review_truncated = len(review_text) > MAX_REVIEW_CHARS
    review_text = review_text[:MAX_REVIEW_CHARS]

    requested: list[tuple[str, str]] = []
    if settings['include_scope_files']:
        for relative in _scope_concrete_files(repository, task, settings['max_files']):
            requested.append((relative, 'allowed_scope'))
    for relative in settings['include_files']:
        if len(requested) >= settings['max_files']:
            break
        if all(relative != existing for existing, _ in requested):
            requested.append((relative, 'configured'))
    requested = requested[: settings['max_files']]

    files: list[dict[str, Any]] = []
    total_chars = 0
    for relative, category in requested:
        resolved = _safe_repository_file(repository, relative)
        if resolved is None:
            continue
        path, normalized = resolved
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        text = payload.decode('utf-8', errors='replace')
        remaining = max(0, MAX_TOTAL_INCLUDED_CHARS - total_chars)
        limit = min(MAX_FILE_CHARS, remaining)
        included = text[:limit]
        total_chars += len(included)
        files.append({
            'path': normalized,
            'category': category,
            'sha256': _sha256_bytes(payload),
            'bytes': len(payload),
            'content': included,
            'content_chars': len(included),
            'truncated': len(text) > len(included),
        })
    files.sort(key=lambda item: item['path'])

    manifest = [
        {'path': item['path'], 'category': item['category'], 'sha256': item['sha256'], 'bytes': item['bytes']}
        for item in files
    ]
    bindings = {
        'task_id': task_id,
        'contract_version': _contract_version(contract_path),
        'task_sha256': _sha256_text(task_text_raw),
        'contract_packet_sha256': _sha256_text(packet_text_raw),
        'contract_files_sha256': _contract_binding(contract_path),
        'review_sha256': _sha256_text(review_raw) if review_present else None,
        'configuration_sha256': executor_runtime.executor_config_fingerprint(config, repository),
        'evidence_sha256': _sha256_text(_canonical_json(manifest)),
    }
    return {
        'schema_version': SCHEMA_VERSION,
        'bindings': bindings,
        'bindings_sha256': _sha256_text(_canonical_json(bindings)),
        'files': files,
        'manifest': manifest,
        'task': {
            'id': task_id,
            'path': str(task_path),
            'text': task_text,
            'truncated': task_truncated,
            'sha256': bindings['task_sha256'],
        },
        'contract': {
            'path': str(contract_path),
            'version': bindings['contract_version'],
            'packet': packet_text,
            'truncated': packet_truncated,
            'sha256': bindings['contract_packet_sha256'],
        },
        'review': {
            'present': review_present,
            'text': review_text,
            'truncated': review_truncated,
            'sha256': bindings['review_sha256'],
        },
        'settings': settings,
    }


def revalidate_evidence(
    prior_bindings: dict[str, Any],
    config: dict[str, Any],
    repository: Path,
    task_path: Path,
    contract_path: Path,
    previous_review_path: Path | None = None,
) -> dict[str, Any]:
    """Re-gather evidence immediately before E launch and compare bindings.

    Any difference -- including a single changed file hash -- invalidates the
    PC decision, so E cannot start on stale preflight facts.
    """
    fresh = gather_evidence(
        config, repository, task_path, contract_path, previous_review_path
    )
    changed: list[str] = []
    prior_bindings = prior_bindings if isinstance(prior_bindings, dict) else {}
    for key in sorted(set(prior_bindings) | set(fresh['bindings'])):
        if prior_bindings.get(key) != fresh['bindings'].get(key):
            changed.append(key)
    return {
        'matched': not changed,
        'changed': changed,
        'bindings': fresh['bindings'],
        'evidence_sha256': fresh['bindings'].get('evidence_sha256'),
        'fresh': fresh,
    }


# --------------------------------------------------------------------------
# Prompts and transports
# --------------------------------------------------------------------------


def build_preflight_prompt(bundle: dict[str, Any]) -> str:
    """Render the runtime-owned Preflight Checker prompt.

    This is the only channel through which PC learns anything about the
    repository. PC has no repository access of its own.
    """
    bindings = bundle['bindings']
    lines: list[str] = [
        '# PSC Preflight Checker (read-only admission review)',
        '',
        'You are the PSC Preflight Checker. You are a read-only admission reviewer.',
        'You have no shell, no filesystem writes, no code editing, no MCP/external',
        'tools, and no repository access. Everything you may rely on is contained in',
        'this prompt; do not claim to have inspected anything else.',
        '',
        'Decide whether the PSC runtime may launch the Executor for the bound task',
        'now. Answer with one JSON object only, using the exact schema at the end.',
        '',
        '## Bound identity (task + contract + review + configuration)',
        f'- task_id: {bindings["task_id"]}',
        f'- contract_version: {bindings["contract_version"]}',
        f'- configuration_sha256: {bindings["configuration_sha256"]}',
        f'- task_sha256: {bindings["task_sha256"]}',
        f'- contract_packet_sha256: {bindings["contract_packet_sha256"]}',
        f'- contract_files_sha256: {bindings["contract_files_sha256"]}',
        f'- review_sha256: {bindings["review_sha256"]}',
        f'- evidence_sha256: {bindings["evidence_sha256"]}',
        '',
        '## Current Task',
        bundle['task']['text'] or '(empty task text)',
    ]
    if bundle['task']['truncated']:
        lines.append('[... task text truncated by the runtime ...]')
    lines.extend([
        '',
        '## Task-Scoped Contract Packet',
        bundle['contract']['packet'] or '(no contract packet available)',
    ])
    if bundle['contract']['truncated']:
        lines.append('[... contract packet truncated by the runtime ...]')
    lines.extend([
        '',
        '## Previous Supervisor Review',
    ])
    if bundle['review']['present']:
        lines.append(bundle['review']['text'] or '(empty review)')
        if bundle['review']['truncated']:
            lines.append('[... review truncated by the runtime ...]')
    else:
        lines.append('No previous Supervisor review exists for this task.')
    lines.extend([
        '',
        '## Runtime-gathered repository evidence (read-only)',
    ])
    if bundle['files']:
        for item in bundle['files']:
            lines.append('')
            lines.append(
                f'### {item["path"]}  [category={item["category"]}, '
                f'sha256={item["sha256"]}, bytes={item["bytes"]}]'
            )
            lines.append('```')
            lines.append(item['content'])
            lines.append('```')
            if item['truncated']:
                lines.append('[... file content truncated by the runtime ...]')
    else:
        lines.append('No task-specific files were gathered for this task.')
    lines.extend([
        '',
        '## Decision rules',
        '- ALLOW only when the bound task, the task-scoped Contract packet, the',
        '  runtime-gathered evidence, and the current repository state are mutually',
        '  consistent and the task is safe to start exactly as scoped.',
        '- DENY when scope is missing/ambiguous, the Contract and task disagree, the',
        '  gathered evidence contradicts the task, a Forbidden Scope boundary would be',
        '  crossed, or the required verification cannot be demonstrated.',
        '- Fail closed: if the evidence is insufficient to allow the task, DENY.',
        '- Every finding must carry concrete evidence strings (quote the exact task,',
        '  Contract, hash, or file excerpt you relied on).',
        f'- resolution_owner must be one of: {", ".join(RESOLUTION_OWNERS)}.',
        '  Use runtime when the PSC runtime must change, supervisor when the',
        '  Supervisor can repair it in-session, planner when the Contract must be',
        '  revised.',
        '- decision ALLOW must contain no blocking findings.',
        '- decision DENY must contain at least one blocking finding.',
        '',
        '## Required response schema (exactly this object, no extra keys)',
        '{"schema_version":1,"decision":"ALLOW|DENY","summary":"...","findings":'
        '[{"id":"F-001","severity":"blocking|advisory","statement":"...",'
        '"evidence":["..."],"resolution_owner":"runtime|supervisor|planner"}]}',
        'Use an empty findings array for a clean ALLOW.',
    ])
    return '\n'.join(lines).rstrip() + '\n'


def build_dsh_preflight_patch(executor: dict[str, Any]) -> str:
    """Build the DSH command-line restriction patch for the Checker.

    The overlay is applied after the profile layer, so every listed capability
    group ends up disabled regardless of the profile's own defaults.
    """
    lines = [DSH_PATCH_HEADER, '- id: session-title-llm', '  disabled: true']
    routing = executor.get('routing') if isinstance(executor, dict) else None
    if isinstance(routing, dict):
        lines.extend([
            '- id: agent-default-model',
            '  config:',
            '    provider: ' + json.dumps(str(routing['provider']), ensure_ascii=False),
            '    model: ' + json.dumps(str(routing['model']), ensure_ascii=False),
            '    reasoningEffort: ' + json.dumps(str(routing['effort']), ensure_ascii=False),
        ])
    for _group, patch_id, _description in DSH_RESTRICTIONS:
        lines.extend(['- id: ' + patch_id, '  disabled: true'])
    return '\n'.join(lines) + '\n'


def parse_patch_disabled_states(text: str) -> dict[str, bool]:
    """Parse `- id: <name>` / `disabled:` pairs from a DSH patch overlay.

    This is a deliberately narrow, dependency-free reader for the overlay shape
    PSC composes. It is not a general YAML parser: it records the last explicit
    `disabled:` value seen for each id before the next entry begins.
    """
    states: dict[str, bool] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        entry = re.match(r'^\s*-\s*id:\s*(\S+)\s*$', line)
        if entry:
            current = entry.group(1).strip('\'"')
            states.setdefault(current, False)
            continue
        if current is None:
            continue
        disabled = re.match(r'^\s*disabled:\s*(\S+)\s*$', line)
        if disabled:
            value = disabled.group(1).strip().lower()
            if value in {'true', 'false'}:
                states[current] = value == 'true'
    return states


def verify_dsh_tool_restrictions(
    patch_path: Path,
    executor_home: Path,
    profile: str,
) -> dict[str, Any]:
    """Verify the composed DSH overlay actually disables every restricted group.

    Raises PreflightVerificationError when any restriction cannot be proven, so
    the caller fails closed. Verification composes the profile overlay first and
    the runtime patch last, matching the command-line `--patch` application
    order.
    """
    patch_path = Path(patch_path)
    if not patch_path.is_file():
        raise PreflightVerificationError('preflight DSH restriction patch is missing')
    try:
        runtime_text = patch_path.read_text(encoding='utf-8')
    except OSError as exc:
        raise PreflightVerificationError(
            f'preflight DSH restriction patch is unreadable: {exc}'
        ) from exc
    runtime_states = parse_patch_disabled_states(runtime_text)
    for group, patch_id, description in DSH_RESTRICTIONS:
        if runtime_states.get(patch_id) is not True:
            raise PreflightVerificationError(
                f'preflight DSH patch does not disable {description} ({patch_id})'
            )

    home = Path(executor_home)
    profile_patch = home / 'profiles' / str(profile) / 'cordis.patch.yml'
    if not profile_patch.is_file():
        raise PreflightVerificationError(
            f'DSH profile patch is missing; composed restrictions for {profile} are unverifiable'
        )
    try:
        profile_text = profile_patch.read_text(encoding='utf-8')
    except OSError as exc:
        raise PreflightVerificationError(
            f'DSH profile patch is unreadable: {exc}'
        ) from exc
    profile_states = parse_patch_disabled_states(profile_text)

    # Last writer wins: the profile layer is applied first, the runtime patch
    # last, so an explicit runtime disable always ends up effective.
    composed = dict(profile_states)
    composed.update(runtime_states)
    for group, patch_id, description in DSH_RESTRICTIONS:
        if composed.get(patch_id) is not True:
            raise PreflightVerificationError(
                f'composed DSH configuration leaves {description} enabled ({patch_id})'
            )
    return {
        'verified': True,
        'profile': str(profile),
        'profile_patch': str(profile_patch),
        'profile_patch_sha256': _sha256_bytes(profile_patch.read_bytes()),
        'runtime_patch_sha256': _sha256_bytes(runtime_text.encode('utf-8')),
        'composed_sha256': _sha256_text(_canonical_json(composed)),
        'restrictions': [
            {'group': group, 'patch_id': patch_id, 'description': description}
            for group, patch_id, description in DSH_RESTRICTIONS
        ],
    }


def _prepare_prompt_transport(
    adapter: str,
    inputs_dir: Path,
    prompt: str,
) -> tuple[str, str | None, Path | None]:
    """Transport the prompt without exposing a repository path.

    Codex receives the prompt on stdin. DSH headless requires a positional task,
    so PSC writes the prompt to the runtime-owned isolated input directory and
    passes a short bootstrap instruction.
    """
    if adapter == 'codex':
        return '-', prompt, None
    if adapter == 'dsh':
        # Headless accepts '-' to read a complete task from stdin. No PC
        # filesystem tool or prompt bootstrap file is required.
        return '-', prompt, None
    raise ValueError(f'unsupported adapter: {adapter}')


@contextmanager
def _isolated_preflight_workspace():
    """Yield (workspace, inputs) outside the product repository.

    `workspace` is the process working directory and stays empty; `inputs` holds
    the runtime-owned prompt/output-schema/patch files.
    """
    root = Path(tempfile.mkdtemp(prefix='psc-preflight-'))
    workspace = root / 'workspace'
    inputs = root / 'inputs'
    workspace.mkdir()
    inputs.mkdir()
    try:
        yield workspace, inputs
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _checker_output_schema_file(inputs_dir: Path) -> Path:
    path = Path(inputs_dir) / 'psc-preflight-output-schema.json'
    path.write_text(json.dumps(REPORT_OUTPUT_SCHEMA, ensure_ascii=False) + '\n', encoding='utf-8')
    return path


# --------------------------------------------------------------------------
# Independent metrics / budget
# --------------------------------------------------------------------------


def preflight_dir(project: Path) -> Path:
    return Path(project).resolve() / 'runtime' / 'preflight'


def preflight_latest_path(project: Path) -> Path:
    return preflight_dir(project) / 'latest.json'


def preflight_usage_ledger_path(project: Path) -> Path:
    return Path(project).resolve() / 'runtime' / 'preflight_token_usage.jsonl'


def preflight_usage_summary_path(project: Path) -> Path:
    return Path(project).resolve() / 'runtime' / 'preflight_token_usage_summary.json'


def _read_usage_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _aggregate_checker_records(records: list[dict[str, Any]], version: int) -> dict[str, Any]:
    selected = [record for record in records if record.get('contract_version') == version]
    totals = {field: 0 for field in USAGE_FIELDS}
    exact = True
    unavailable = 0
    inexact = 0
    elapsed_seconds = 0.0
    timed_invocations = 0
    decisions: dict[str, int] = {}
    for record in selected:
        decision = record.get('decision')
        if isinstance(decision, str):
            decisions[decision] = decisions.get(decision, 0) + 1
        usage = record.get('usage') if isinstance(record.get('usage'), dict) else {}
        duration = usage.get('elapsed_seconds')
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and 0 <= duration < float('inf')
        ):
            elapsed_seconds += float(duration)
            timed_invocations += 1
        if not usage.get('available'):
            exact = False
            unavailable += 1
            continue
        for field in USAGE_FIELDS:
            value = _count(usage.get(field))
            if value is not None:
                totals[field] += value
        if not usage.get('exact'):
            exact = False
            inexact += 1
    totals['total_tokens'] = totals['input_tokens'] + totals['output_tokens']
    return {
        'contract_version': version,
        'contract': f'v{version}',
        'checker_invocations': len(selected),
        'exact_checker_invocations': len(selected) - unavailable - inexact,
        'inexact_checker_invocations': inexact,
        'unavailable_checker_invocations': unavailable,
        'decisions': decisions,
        'exact': exact,
        'lower_bound': not exact,
        **totals,
        'elapsed_seconds': round(elapsed_seconds, 3) if timed_invocations == len(selected) else None,
        'timed_checker_invocations': timed_invocations,
    }


def record_preflight_usage(
    project: Path,
    contract_path: Path,
    *,
    task: str,
    retry_kind: str,
    status: str,
    decision: str,
    reason: str | None,
    run_id: str,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Persist checker usage to its own ledger, never the Executor ledger."""
    usage = with_efficiency_metrics(usage)
    version = _contract_version(contract_path)
    if version is None:
        raise ValueError(f'cannot derive Contract version from {contract_path}')
    project = Path(project).resolve()
    ledger = preflight_usage_ledger_path(project)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    record = {
        'schema_version': 1,
        'recorded_at': _now(),
        'contract_version': version,
        'contract': f'v{version}',
        'task': task,
        'retry_kind': retry_kind,
        'run_id': run_id,
        'status': status,
        'decision': decision,
        'reason': reason,
        'usage': usage,
    }
    with ledger.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        handle.flush()
        os.fsync(handle.fileno())

    records = _read_usage_ledger(ledger)
    versions = sorted({
        int(item['contract_version'])
        for item in records
        if isinstance(item.get('contract_version'), int)
    })
    contracts = {f'v{item}': _aggregate_checker_records(records, item) for item in versions}
    summary = {
        'schema_version': 1,
        'updated_at': record['recorded_at'],
        'ledger': 'preflight_token_usage.jsonl',
        'contracts': contracts,
    }
    summary_path = preflight_usage_summary_path(project)
    temporary = summary_path.with_name(summary_path.name + '.tmp')
    temporary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    os.replace(temporary, summary_path)
    return {
        'checker_invocation': usage,
        'checker_contract_total': contracts[f'v{version}'],
        'ledger_path': str(ledger),
        'summary_path': str(summary_path),
    }


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------


def _unknown_result(reason: str, errors: list[str], **extra: Any) -> dict[str, Any]:
    result = {
        'schema_version': SCHEMA_VERSION,
        'status': STATUS_UNKNOWN,
        'decision': DECISION_UNKNOWN,
        'reason': reason,
        'enforced': True,
        'blocks_executor': True,
        'report': None,
        'findings': [],
        'resolution_owner': None,
        'bindings': None,
        'revalidation': None,
        'report_path': None,
        'latest_path': None,
        'log_path': None,
        'command': None,
        'exit_code': None,
        'elapsed_seconds': None,
        'token_usage': None,
        'errors': list(errors),
    }
    result.update(extra)
    return result


def _checker_log_path(project: Path, task_id: str, run_id: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return preflight_dir(project) / 'logs' / f'{task_id}-{stamp}-{run_id[:8]}.log'


def _write_checker_log(path: Path, command: list[str], stdout: str, stderr: str, exit_code: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        'Command:\n' + ' '.join(command)
        + f'\nExit code: {exit_code}\n\nSTDOUT\n{_redact(stdout)}\n\nSTDERR\n{_redact(stderr)}\n'
    )
    path.write_text(content, encoding='utf-8')


def run_preflight_check(
    config: dict[str, Any],
    repository: Path,
    project: Path | None,
    task_path: Path,
    contract_path: Path,
    previous_review_path: Path | None = None,
    *,
    retry_kind: str = 'initial',
) -> dict[str, Any]:
    """Run one independent, read-only Preflight Check.

    Returns a decision envelope. `blocks_executor` is True for every outcome
    except an explicit configuration-level disable, so callers can enforce the
    gate without re-deriving policy.
    """
    repository = Path(repository)
    task_path = Path(task_path)
    contract_path = Path(contract_path)
    previous_review_path = Path(previous_review_path) if previous_review_path else None

    try:
        settings = preflight_settings(config)
    except PreflightConfigurationError as exc:
        return _unknown_result(REASON_INVALID_CONFIG, [str(exc)])

    if not settings['enabled']:
        return {
            'schema_version': SCHEMA_VERSION,
            'status': STATUS_DISABLED,
            'decision': DECISION_UNKNOWN,
            'reason': 'preflight_disabled_by_configuration',
            'enforced': False,
            'blocks_executor': False,
            'report': None,
            'findings': [],
            'resolution_owner': None,
            'bindings': None,
            'revalidation': None,
            'report_path': None,
            'latest_path': None,
            'log_path': None,
            'command': None,
            'exit_code': None,
            'elapsed_seconds': None,
            'token_usage': None,
            'errors': [],
        }

    executor = config['executor']
    adapter = executor.get('adapter')
    if adapter not in {'codex', 'dsh'}:
        return _unknown_result(
            REASON_UNSUPPORTED_ADAPTER,
            [f'configured adapter is {adapter!r}'],
        )

    run_id = uuid.uuid4().hex
    try:
        bundle = gather_evidence(
            config, repository, task_path, contract_path, previous_review_path
        )
    except (OSError, ValueError, json.JSONDecodeError, PreflightConfigurationError) as exc:
        return _unknown_result(REASON_EVIDENCE_GATHER_FAILED, [str(exc)])

    task_id = bundle['bindings']['task_id']
    log_path = (
        _checker_log_path(Path(project), task_id, run_id)
        if project is not None
        else None
    )
    prompt = build_preflight_prompt(bundle)
    token_usage: dict[str, Any] = zero_usage('no_model_call')
    exit_code: int | None = None
    stdout = ''
    stderr = ''
    command: list[str] = []
    elapsed_seconds = 0.0
    reason: str | None = None
    report: dict[str, Any] | None = None
    errors: list[str] = []
    dsh_verification: dict[str, Any] | None = None
    dsh_session_root = (
        Path(str(executor['executor_home'])).expanduser().resolve() / 'sessions'
    )
    dsh_sessions_before = dsh_session_snapshot(dsh_session_root) if adapter == 'dsh' else {}

    try:
        with _isolated_preflight_workspace() as (workspace, inputs):
            prompt_argument, stdin_prompt, _prompt_path = _prepare_prompt_transport(
                adapter, inputs, prompt
            )
            if adapter == 'codex':
                schema_path = _checker_output_schema_file(inputs)
                command = codex_adapter.build_preflight_command(
                    str(executor['executable']),
                    executor,
                    prompt_argument,
                    output_schema=schema_path,
                )
            else:
                patch_path = inputs / 'psc-preflight-restrictions.yml'
                patch_path.write_text(
                    build_dsh_preflight_patch(executor), encoding='utf-8', newline='\n'
                )
                dsh_verification = verify_dsh_tool_restrictions(
                    patch_path,
                    Path(str(executor['executor_home'])).expanduser(),
                    str(executor.get('profile', '')).strip(),
                )
                command = dsh_adapter.build_preflight_command(
                    str(executor['executable']),
                    executor,
                    prompt_argument,
                    patch_path=patch_path,
                )
            launch_command = executor_runtime._prepare_command(adapter, command)
            child_env = executor_runtime._executor_child_env(adapter, executor)
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    launch_command,
                    cwd=str(workspace),
                    env=child_env,
                    input=stdin_prompt,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=settings['timeout'],
                )
                stdout = completed.stdout or ''
                stderr = completed.stderr or ''
                exit_code = completed.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = (
                    exc.stdout.decode('utf-8', errors='replace')
                    if isinstance(exc.stdout, bytes)
                    else str(exc.stdout or '')
                )
                stderr = (
                    exc.stderr.decode('utf-8', errors='replace')
                    if isinstance(exc.stderr, bytes)
                    else str(exc.stderr or '')
                )
                reason = REASON_TIMEOUT
                errors.append(
                    f'Preflight Checker exceeded its {settings["timeout"]}s timeout.'
                )
            except OSError as exc:
                stderr = str(exc)
                reason = REASON_LAUNCH_FAILED
                errors.append(str(exc))
            elapsed_seconds = max(0.0, time.perf_counter() - started)
            command = launch_command
    except PreflightVerificationError as exc:
        return _unknown_result(
            REASON_DSH_UNVERIFIABLE,
            [str(exc)],
            bindings=bundle['bindings'],
        )
    except (OSError, ValueError) as exc:
        return _unknown_result(
            REASON_LAUNCH_FAILED,
            [str(exc)],
            bindings=bundle['bindings'],
        )

    process_settled = exit_code is not None
    if adapter == 'codex':
        final_text, token_usage = parse_codex_exec_jsonl(stdout, process_settled=process_settled)
        usage_text = final_text if final_text is not None else stdout
    else:
        final_text, _session_id, headless_usage = parse_dsh_headless_json(
            stdout, process_settled=process_settled
        )
        usage_text = final_text if final_text is not None else stdout
        token_usage = collect_dsh_invocation_usage(
            dsh_session_root, dsh_sessions_before, process_settled=process_settled
        )
        if not token_usage.get('available') and headless_usage.get('available'):
            token_usage = dict(headless_usage)
            token_usage['source'] = 'dsh_headless_json_fallback'
    if reason == REASON_LAUNCH_FAILED:
        token_usage = zero_usage('no_model_call')
    token_usage = dict(token_usage)
    token_usage['elapsed_seconds'] = round(elapsed_seconds, 3)

    if reason is None:
        if exit_code != 0:
            reason = REASON_PROCESS_FAILED
            errors.append(f'Preflight Checker exited with code {exit_code}.')
        else:
            report, parse_error = _parse_report_text(
                final_text if final_text is not None else usage_text,
                allow_wrapped=(adapter == 'dsh'),
            )
            if report is None:
                reason = (
                    REASON_EMPTY_RESPONSE if parse_error == REASON_EMPTY_RESPONSE
                    else REASON_INVALID_RESPONSE
                )
                errors.append(str(parse_error))

    revalidation: dict[str, Any] | None = None
    if reason is None and report is not None and report['decision'] == DECISION_ALLOW:
        try:
            revalidation = revalidate_evidence(
                bundle['bindings'],
                config,
                repository,
                task_path,
                contract_path,
                previous_review_path,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            reason = REASON_EVIDENCE_GATHER_FAILED
            errors.append(str(exc))
        else:
            if not revalidation['matched']:
                reason = REASON_STALE_EVIDENCE
                errors.append(
                    'Preflight evidence changed between the check and the Executor '
                    'launch: ' + ', '.join(revalidation['changed'])
                )

    if reason is None and report is not None:
        status = STATUS_ALLOW if report['decision'] == DECISION_ALLOW else STATUS_DENIED
        if status == STATUS_DENIED:
            reason = REASON_DENIED
    else:
        status = STATUS_UNKNOWN

    decision = (
        report['decision'] if report is not None and status in {STATUS_ALLOW, STATUS_DENIED}
        else DECISION_UNKNOWN
    )
    blocks_executor = status != STATUS_ALLOW

    if log_path is not None:
        try:
            _write_checker_log(log_path, command, stdout, stderr, exit_code)
        except OSError:
            log_path = None

    result: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'status': status,
        'decision': decision,
        'reason': reason,
        'enforced': True,
        'blocks_executor': blocks_executor,
        'run_id': run_id,
        'task_id': task_id,
        'contract_version': bundle['bindings']['contract_version'],
        'adapter': adapter,
        'retry_kind': retry_kind,
        'report': report,
        'report_sha256': _sha256_text(_canonical_json(report)) if report is not None else None,
        'findings': report['findings'] if isinstance(report, dict) else [],
        'resolution_owner': decision_resolution_owner(report),
        'summary': report['summary'] if isinstance(report, dict) else None,
        'bindings': bundle['bindings'],
        'evidence_manifest': bundle['manifest'],
        'revalidation': revalidation,
        'report_path': None,
        'latest_path': None,
        'log_path': str(log_path) if log_path is not None else None,
        'command': list(command),
        'exit_code': exit_code,
        'elapsed_seconds': round(elapsed_seconds, 3),
        'token_usage': token_usage,
        'checker_usage': None,
        'dsh_tool_restrictions': dsh_verification,
        'errors': errors,
    }

    if project is not None:
        try:
            record = dict(result)
            record['checked_at'] = _now()
            record['evidence_manifest'] = bundle['manifest']
            stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            report_path = preflight_dir(Path(project)) / f'{task_id}-{stamp}-{run_id[:8]}.json'
            _write_json(report_path, record)
            _write_json(preflight_latest_path(Path(project)), record)
            result['report_path'] = str(report_path)
            result['latest_path'] = str(preflight_latest_path(Path(project)))
            usage_record = record_preflight_usage(
                Path(project),
                contract_path,
                task=task_id,
                retry_kind=retry_kind,
                status=status,
                decision=decision,
                reason=reason,
                run_id=run_id,
                usage=token_usage,
            )
            result['checker_usage'] = usage_record
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result['errors'].append(f'preflight persistence failed: {exc}')

    return result


# --------------------------------------------------------------------------
# Executor prompt injection
# --------------------------------------------------------------------------


def verified_facts(result: dict[str, Any]) -> dict[str, Any]:
    """Build the compact verified-facts payload injected into the E prompt."""
    bindings = result.get('bindings') or {}
    files = [
        {'path': item.get('path'), 'sha256': item.get('sha256'), 'bytes': item.get('bytes')}
        for item in (result.get('evidence_manifest') or [])
        if isinstance(item, dict)
    ]
    findings = [
        {
            'id': finding.get('id'),
            'severity': finding.get('severity'),
            'statement': finding.get('statement'),
            'resolution_owner': finding.get('resolution_owner'),
        }
        for finding in (result.get('findings') or [])
        if isinstance(finding, dict)
    ]
    return {
        'decision': DECISION_ALLOW,
        'checked_at': _now(),
        'report_sha256': result.get('report_sha256'),
        'summary': result.get('summary'),
        'task_id': bindings.get('task_id') or result.get('task_id'),
        'contract_version': bindings.get('contract_version'),
        'configuration_sha256': bindings.get('configuration_sha256'),
        'task_sha256': bindings.get('task_sha256'),
        'contract_packet_sha256': bindings.get('contract_packet_sha256'),
        'contract_files_sha256': bindings.get('contract_files_sha256'),
        'review_sha256': bindings.get('review_sha256'),
        'evidence_sha256': bindings.get('evidence_sha256'),
        'verified_files': files,
        'advisory_findings': findings,
    }


# --------------------------------------------------------------------------
# CLI (manual/debug only; never a Supervisor dispatch replacement)
# --------------------------------------------------------------------------


def compact_preflight_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields the Supervisor needs from a Preflight Check."""
    findings = [
        {
            'id': finding.get('id'),
            'severity': finding.get('severity'),
            'statement': finding.get('statement'),
            'evidence': list(finding.get('evidence') or []),
            'resolution_owner': finding.get('resolution_owner'),
        }
        for finding in (result.get('findings') or [])
        if isinstance(finding, dict)
    ]
    compact: dict[str, Any] = {
        'status': result.get('status'),
        'decision': result.get('decision'),
        'reason': result.get('reason'),
        'enforced': result.get('enforced'),
        'blocks_executor': result.get('blocks_executor'),
        'task_id': result.get('task_id'),
        'contract_version': result.get('contract_version'),
        'adapter': result.get('adapter'),
        'report_sha256': result.get('report_sha256'),
        'summary': result.get('summary'),
        'findings': findings,
        'resolution_owner': result.get('resolution_owner'),
        'bindings': result.get('bindings'),
        'revalidation': result.get('revalidation'),
        'report_path': result.get('report_path'),
        'latest_path': result.get('latest_path'),
        'log_path': result.get('log_path'),
        'elapsed_seconds': result.get('elapsed_seconds'),
        'token_usage': result.get('token_usage'),
        'checker_usage': result.get('checker_usage'),
        'dsh_tool_restrictions': result.get('dsh_tool_restrictions'),
        'errors': list(result.get('errors') or []),
    }
    return compact


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(
        description='PSC Preflight Checker helper (read-only admission check)'
    )
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check')
    check.add_argument('--repository', type=Path, required=True)
    check.add_argument('--runtime-config', type=Path, required=True)
    check.add_argument('--project', type=Path)
    check.add_argument('--task', type=Path, required=True)
    check.add_argument('--contract', type=Path, required=True)
    check.add_argument('--previous-review', type=Path)
    check.add_argument('--retry-kind', default='initial')
    args = parser.parse_args()

    from psc_runtime import runtime_config as load_runtime_config

    try:
        config = load_runtime_config(args.runtime_config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({'status': STATUS_UNKNOWN, 'reason': REASON_INVALID_CONFIG,
                          'errors': [str(exc)]}, indent=2, ensure_ascii=False))
        return 2
    result = run_preflight_check(
        config,
        args.repository,
        args.project,
        args.task,
        args.contract,
        args.previous_review,
        retry_kind=args.retry_kind,
    )
    print(json.dumps(compact_preflight_result(result), indent=2, ensure_ascii=False))
    return 0 if result['status'] == STATUS_ALLOW else 2


if __name__ == '__main__':
    raise SystemExit(main())
