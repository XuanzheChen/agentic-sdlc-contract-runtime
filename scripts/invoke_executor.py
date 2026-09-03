#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from adapters import codex as codex_adapter
from adapters import dsh as dsh_adapter
from psc_runtime import runtime_config
from executor_token_usage import (
    collect_dsh_invocation_usage,
    dsh_session_snapshot,
    parse_codex_exec_jsonl,
    zero_usage,
)


FINGERPRINT_FIELDS = ('adapter', 'executable', 'executor_home', 'config_source', 'provider', 'model', 'effort', 'approval_policy', 'sandbox', 'approvals_reviewer', 'profile')
build_command = codex_adapter.build_command
prepare_command = codex_adapter.prepare_command
supports_auto_review = codex_adapter.supports_auto_review
SECRET_PATTERNS = (
    re.compile(r'(?i)(api[_-]?key\s*[=:]\s*)\S+'),
    re.compile(r'(?i)(authorization:\s*bearer\s+)\S+'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{12,}\b'),
)
COMPLETION_FIELDS = (
    'schema_version', 'plan', 'coding_summary', 'modified_files', 'tests',
    'known_risks', 'unresolved_issues',
)
COMPLETION_OUTPUT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': list(COMPLETION_FIELDS),
    'properties': {
        'schema_version': {'type': 'integer', 'const': 1},
        'plan': {'type': 'string', 'minLength': 1},
        'coding_summary': {'type': 'string', 'minLength': 1},
        'modified_files': {'type': 'array', 'items': {'type': 'string'}},
        'tests': {'type': 'array', 'items': {'type': 'string'}},
        'known_risks': {'type': 'array', 'items': {'type': 'string'}},
        'unresolved_issues': {'type': 'array', 'items': {'type': 'string'}},
    },
}
EXPECTED_SMOKE_BYTES = b'PSC_EXECUTOR_SMOKE_OK'
PSC_SMOKE_WORKSPACE_RE = re.compile(r'psc-executor-smoke-[0-9a-f]{32}')

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r'\1[REDACTED]' if pattern.groups else '[REDACTED]', text)
    return text


def _dump_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _config(runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    return runtime_config(Path(runtime))


def _adaptive_timeout_update(
    runtime: Path | str | dict[str, Any],
    config: dict[str, Any],
    *,
    explicit_timeout: int | None,
) -> dict[str, Any] | None:
    """Double executor.timeout after a normal-task Executor timeout.

    Reaching subprocess.TimeoutExpired means the Executor process was launched
    and remained under runtime control until the configured deadline. No
    stdout/stderr or repository-change evidence is required: slow workers may
    legitimately produce nothing observable before timeout. Smoke/explicit
    timeout overrides never mutate normal runtime timeout. A legacy runtime
    without an explicit maxTimeout stays fixed for backward compatibility.
    """
    if explicit_timeout is not None:
        return None
    executor = config.get('executor', {})
    current = executor.get('timeout')
    maximum = executor.get('maxTimeout')
    if not isinstance(current, int) or current <= 0:
        return None
    if isinstance(runtime, dict):
        return {
            'status': 'not_adjusted',
            'reason': 'runtime_not_persisted',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum if isinstance(maximum, int) else None,
        }
    runtime_path = Path(runtime)
    try:
        raw = json.loads(runtime_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {
            'status': 'not_adjusted',
            'reason': 'runtime_config_unreadable',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum if isinstance(maximum, int) else None,
        }
    raw_executor = raw.get('executor') if isinstance(raw, dict) else None
    if not isinstance(raw_executor, dict) or 'maxTimeout' not in raw_executor:
        return {
            'status': 'not_adjusted',
            'reason': 'maxTimeout_not_configured',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': None,
        }
    maximum = raw_executor.get('maxTimeout')
    if not isinstance(maximum, int) or maximum <= 0 or maximum < current:
        return {
            'status': 'not_adjusted',
            'reason': 'invalid_maxTimeout',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum,
        }
    if current >= maximum:
        return {
            'status': 'at_max',
            'reason': 'maxTimeout_reached',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum,
        }
    new_timeout = min(current * 2, maximum)
    raw_executor['timeout'] = new_timeout
    try:
        _dump_json(runtime_path, raw)
    except OSError:
        return {
            'status': 'not_adjusted',
            'reason': 'runtime_config_write_failed',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum,
        }
    return {
        'status': 'adjusted',
        'reason': 'executor_timed_out',
        'old_timeout': current,
        'new_timeout': new_timeout,
        'maxTimeout': maximum,
    }


def _normalized_project_key(value: str) -> str:
    return str(value).replace('\\', '/').rstrip('/')


def _is_psc_smoke_project_key(value: str, repository: Path | None) -> bool:
    """Return True only for PSC-owned ephemeral smoke siblings of repository."""
    if repository is None:
        return False
    candidate = _normalized_project_key(value)
    if '/' not in candidate:
        return False
    parent, basename = candidate.rsplit('/', 1)
    if PSC_SMOKE_WORKSPACE_RE.fullmatch(basename) is None:
        return False
    repository_parent = _normalized_project_key(str(Path(repository).resolve().parent))
    return parent.casefold() == repository_parent.casefold()


def _canonical_toml_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_toml_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_canonical_toml_value(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return value


def _semantic_codex_config_bytes(path: Path, repository: Path | None) -> bytes:
    """Hash stable Codex semantics, excluding only PSC smoke trust bookkeeping.

    Codex may persist project trust state in config.toml. PSC smoke runs use a
    fresh sibling directory named psc-executor-smoke-<32 hex chars>; those
    entries are runtime bookkeeping created by the smoke itself and must not
    invalidate the smoke gate. Every other config key, including trust entries
    for real projects, remains security-significant and is fingerprinted.
    """
    with path.open('rb') as handle:
        value = tomllib.load(handle)
    if not isinstance(value, dict):
        raise ValueError('Codex config.toml must parse to a TOML table')
    semantic = dict(value)
    projects = semantic.get('projects')
    if isinstance(projects, dict):
        filtered = {
            key: item
            for key, item in projects.items()
            if not _is_psc_smoke_project_key(str(key), repository)
        }
        if filtered:
            semantic['projects'] = filtered
        else:
            semantic.pop('projects', None)
    canonical = _canonical_toml_value(semantic)
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')


def executor_home_config_sha256(
    config: dict[str, Any],
    repository: Path | None = None,
) -> str | None:
    executor = config['executor']
    if executor.get('config_source', 'runtime') != 'executor_home':
        return None
    home = Path(str(executor['executor_home'])).expanduser()
    if executor.get('adapter') == 'dsh':
        profile = str(executor.get('profile', '')).strip()
        paths = (
            home / 'settings.yaml',
            home / 'profiles' / profile / 'package.json',
            home / 'profiles' / profile / 'cordis.patch.yml',
        )
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode('utf-8'))
            digest.update(path.read_bytes())
        return digest.hexdigest()
    config_path = home / 'config.toml'
    return hashlib.sha256(_semantic_codex_config_bytes(config_path, repository)).hexdigest()


def executor_config_fingerprint(
    config: dict[str, Any],
    repository: Path | None = None,
) -> str:
    executor = config['executor']
    stable = {field: executor.get(field) for field in FINGERPRINT_FIELDS}
    stable['executor_home_config_sha256'] = executor_home_config_sha256(
        config,
        repository,
    )
    encoded = json.dumps(stable, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def static_probe(
    runtime: Path | str | dict[str, Any],
    repository: Path | None = None,
) -> dict[str, Any]:
    try:
        config = _config(runtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {'status': 'failed', 'reason': 'invalid_runtime_config', 'errors': [str(exc)]}
    executor = config.get('executor', {})
    errors: list[str] = []
    adapter = executor.get('adapter')
    if adapter not in {'codex', 'dsh'}:
        errors.append('unsupported_adapter')
    executable = str(executor.get('executable', ''))
    if not executable or (Path(executable).is_absolute() and not Path(executable).is_file()) or (not Path(executable).is_absolute() and shutil.which(executable) is None):
        errors.append('executable_not_found')
    try:
        _prepare_command(str(adapter), [executable, '--version'])
    except OSError:
        errors.append('unsupported_executable_wrapper')
    home = Path(str(executor.get('executor_home', ''))).expanduser()
    if not home.is_dir():
        errors.append('executor_home_not_found')
    if adapter == 'dsh':
        profile = str(executor.get('profile', '')).strip()
        if not profile or not (home / 'profiles' / profile / 'package.json').is_file():
            errors.append('dsh_profile_not_found')
    if executor.get('approvals_reviewer') == 'auto_review' and executable and not supports_auto_review(executable):
        errors.append('auto_review_unsupported')
    try:
        executor_home_config_sha256(config, repository)
    except OSError:
        errors.append('executor_config_not_readable')
    if errors:
        return {'status': 'failed', 'reason': errors[0], 'errors': errors}
    return {'status': 'passed', 'adapter': adapter, 'executor_home': str(home.resolve()), 'executor_config_sha256': executor_config_fingerprint(config, repository)}


def _prepare_command(adapter: str, command: list[str]) -> list[str]:
    if adapter == 'codex':
        return codex_adapter.prepare_command(command)
    if adapter == 'dsh':
        return dsh_adapter.prepare_command(command)
    raise OSError(f'unsupported adapter: {adapter}')


def _build_command(adapter: str, executable: str, executor: dict[str, Any], prompt: str, *, output_schema: Path | None) -> list[str]:
    if adapter == 'codex':
        return codex_adapter.build_command(executable, executor, prompt, output_schema=output_schema)
    if adapter == 'dsh':
        return dsh_adapter.build_command(executable, executor, prompt)
    raise ValueError(f'unsupported adapter: {adapter}')


def _task_text(task: Any) -> str:
    if isinstance(task, Path):
        return task.read_text(encoding='utf-8')
    if isinstance(task, dict):
        return str(task.get('text') or task.get('markdown') or 'No task text provided.')
    return str(task)


def _task_id(task: Any) -> str:
    if isinstance(task, dict) and task.get('id'):
        return str(task['id'])
    match = re.search(r'\bT-\d{3,}\b', _task_text(task))
    return match.group(0) if match else 'T-UNKNOWN'


def _contract_text(contract: Any) -> str:
    if isinstance(contract, (str, Path)) and Path(contract).is_dir():
        root = Path(contract)
        pieces = []
        for name in ('requirements.md', 'acceptance.md', 'implementation.md', 'constraints.md'):
            path = root / name
            if path.is_file():
                pieces.append(path.read_text(encoding='utf-8'))
        return '\n\n'.join(pieces)
    if isinstance(contract, dict):
        return str(contract.get('text') or 'No Contract excerpt provided.')
    return str(contract)


def _executor_prompt(task: Any, contract: Any, previous_review: Any, *, structured_completion: bool = True) -> str:
    review = str(previous_review or 'No previous Supervisor review exists.')
    sections = [
        'You are a disposable PSC Executor. Work only on the current repository and task.',
        'You may edit only Allowed Scope, respect Forbidden Scope, and may add required tests. Do not edit contract files, runtime state, review.md, or result.md.',
        '## Current Task\n' + _task_text(task),
        '## Relevant Contract\n' + _contract_text(contract),
        '## Previous Supervisor Review\n' + review,
    ]
    if structured_completion:
        sections.append(
            'Do not write PSC runtime artifacts directly. Your final response must be exactly one JSON object, without Markdown fences or extra text, using this schema: '
            '{"schema_version":1,"plan":"...","coding_summary":"...","modified_files":["..."],"tests":["..."],"known_risks":["..."],"unresolved_issues":["..."]}. '
            'Use empty arrays when a list has no entries. The invocation layer persists this Executor-owned content.'
        )
    else:
        sections.append('Return a concise completion report after verifying the requested smoke marker.')
    return '\n\n'.join(sections)


def _prepare_prompt_transport(
    adapter: str,
    repository: Path,
    prompt: str,
) -> tuple[str, str | None, Path | None]:
    """Keep large Executor prompts out of argv.

    Codex uses its explicit stdin sentinel, so the complete prompt is written
    to stdin. DSH headless currently requires a positional task, so PSC writes
    the complete prompt to a short-lived runtime-owned workspace file and
    passes only a short bootstrap instruction in argv.
    """
    if adapter == 'codex':
        return '-', prompt, None
    if adapter == 'dsh':
        repository = Path(repository).resolve()
        prompt_dir = repository / '.agentic-sdlc' / 'runtime' / 'executor-inputs'
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f'psc-executor-prompt-{uuid.uuid4().hex}.md'
        prompt_path.write_text(prompt, encoding='utf-8', newline='\n')
        relative = prompt_path.relative_to(repository).as_posix()
        bootstrap = (
            'Read the complete PSC Executor instructions from the UTF-8 file '
            f'{relative} in the current workspace. Follow that file exactly as '
            'the user task. The file is runtime-owned and read-only: do not '
            'modify, rename, or delete it.'
        )
        return bootstrap, None, prompt_path
    raise ValueError(f'unsupported adapter: {adapter}')


def _cleanup_prompt_transport(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
    parent = path.parent
    for candidate in (parent, parent.parent):
        try:
            candidate.rmdir()
        except OSError:
            break


def _dsh_metering_patch_file() -> Path:
    """Disable unmetered automatic session-title LLM calls for disposable E."""
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        suffix='.yml',
        prefix='psc-dsh-metering-',
        delete=False,
    ) as handle:
        handle.write("- id: session-title-llm\n  disabled: true\n")
        return Path(handle.name)


def _spawn_failure_reason(exc: OSError) -> str:
    """Classify deterministic command-line transport failures separately."""
    if getattr(exc, 'winerror', None) == 206 or getattr(exc, 'errno', None) == errno.ENAMETOOLONG:
        return 'launch_transport_failed'
    return 'spawn_failed'

def _completion_validation_error(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != set(COMPLETION_FIELDS):
        return 'final response must be exactly the PSC structured completion schema'
    if value.get('schema_version') != 1:
        return 'structured completion schema_version must be 1'
    for name in ('plan', 'coding_summary'):
        if not isinstance(value[name], str) or not value[name].strip():
            return f'structured completion {name} must be a non-empty string'
    for name in ('modified_files', 'tests', 'known_risks', 'unresolved_issues'):
        if not isinstance(value[name], list) or any(not isinstance(item, str) for item in value[name]):
            return f'structured completion {name} must be an array of strings'
    return None


def _parse_completion(
    stdout: str,
    *,
    allow_wrapped_json: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    text = stdout.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        strict_error = f'final response is not valid JSON: {exc.msg}'
    else:
        validation_error = _completion_validation_error(value)
        if validation_error is None:
            return value, None
        strict_error = validation_error

    if not allow_wrapped_json:
        return None, strict_error

    # Some harnesses (notably DSH-backed models) may emit explanatory prose or
    # Markdown fences before the required completion object. Scan for JSON
    # objects and accept only the last object that independently satisfies the
    # exact PSC completion schema. This is framing tolerance, not schema
    # tolerance: malformed or partial objects are still rejected.
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != '{':
            continue
        try:
            candidate, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if _completion_validation_error(candidate) is None:
            candidates.append(candidate)
    if candidates:
        return candidates[-1], None
    return None, strict_error


def _write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def _markdown_list(items: list[str]) -> str:
    return '\n'.join(f'- {item}' for item in items)


def _materialize_executor_artifacts(artifact_dir: Path, completion: dict[str, Any]) -> dict[str, str]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_dir / 'plan.md'
    coding_path = artifact_dir / 'coding.md'
    plan = completion['plan'].rstrip() + '\n'
    coding_sections = [
        '# Executor Coding Summary',
        '',
        completion['coding_summary'].rstrip(),
        '',
        '## Modified Files',
        _markdown_list(completion['modified_files']),
        '',
        '## Tests',
        _markdown_list(completion['tests']),
        '',
        '## Known Risks',
        _markdown_list(completion['known_risks']),
        '',
        '## Unresolved Issues',
        _markdown_list(completion['unresolved_issues']),
        '',
    ]
    _write_text_atomically(plan_path, plan)
    _write_text_atomically(coding_path, '\n'.join(coding_sections))
    return {'plan': str(plan_path), 'coding': str(coding_path)}


def _task_artifact_dir(project: Path, task: Any) -> Path:
    task_id = _task_id(task)
    if task_id == 'T-UNKNOWN':
        raise ValueError('task_artifact_directory_requires_stable_task_id')
    return Path(project).resolve() / 'developing' / 'artifacts' / task_id


def _completion_schema_file() -> Path:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', prefix='psc-executor-output-schema-', delete=False) as handle:
        json.dump(COMPLETION_OUTPUT_SCHEMA, handle, ensure_ascii=False)
        handle.write('\n')
        return Path(handle.name)

def _git_dirty_paths(repository: Path) -> set[str]:
    try:
        diff = subprocess.run(
            ['git', '-C', str(repository), 'diff', '--name-only'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
        cached = subprocess.run(
            ['git', '-C', str(repository), 'diff', '--cached', '--name-only'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
        untracked = subprocess.run(
            ['git', '-C', str(repository), 'ls-files', '--others', '--exclude-standard'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {
        line.strip().replace('\\', '/')
        for output in (diff, cached, untracked)
        for line in output.splitlines()
        if line.strip()
    }


def _git_path_fingerprint(repository: Path, relative_path: str) -> str:
    path = repository / relative_path
    digest = hashlib.sha256()
    digest.update(relative_path.encode('utf-8', errors='replace'))
    if path.is_file():
        digest.update(b'\\0file\\0')
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b'<unreadable>')
    elif path.exists():
        digest.update(b'\\0non-file\\0')
    else:
        digest.update(b'\\0missing\\0')
    try:
        index_entry = subprocess.run(
            ['git', '-C', str(repository), 'ls-files', '-s', '--', relative_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        index_entry = ''
    digest.update(b'\\0index\\0')
    digest.update(index_entry.encode('utf-8', errors='replace'))
    return digest.hexdigest()


def _git_snapshot(repository: Path) -> dict[str, str]:
    repository = Path(repository).resolve()
    return {
        path: _git_path_fingerprint(repository, path)
        for path in _git_dirty_paths(repository)
    }


def _git_paths(repository: Path) -> set[str]:
    return set(_git_snapshot(repository))


def _changed_paths_between(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }


def _scope_values(task: Any, label: str) -> list[str]:
    if isinstance(task, dict) and isinstance(task.get(label), list):
        return [str(value).strip() for value in task[label]]
    match = re.search(rf'(?im)^{re.escape(label)}:\s*\n((?:\s*[-*]\s*.*\n?)*)', _task_text(task))
    if not match:
        return []
    values = [line.strip()[1:].strip() for line in match.group(1).splitlines() if line.strip().startswith(('-', '*'))]
    return [re.split(r'\s+(?:for|only if)\s+', value, maxsplit=1, flags=re.IGNORECASE)[0].strip() for value in values]


def _matches_scope(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.replace('\\', '/').strip().rstrip('/')
        if normalized.lower() in {'', 'none'}:
            continue
        if normalized in {'.', '*'} or path == normalized or path.startswith(normalized + '/') or fnmatch(path, normalized):
            return True
    return False


def _scope_violations(task: Any, changed_paths: set[str]) -> list[str]:
    allowed = _scope_values(task, 'Allowed Scope')
    forbidden = _scope_values(task, 'Forbidden Scope')
    violations = [path for path in changed_paths if forbidden and _matches_scope(path, forbidden)]
    if allowed:
        violations.extend(path for path in changed_paths if not _matches_scope(path, allowed))
    return sorted(set(violations))


def _log_path(repository: Path, task: Any, contract: Any, project: Path | None = None) -> Path:
    if isinstance(task, dict) and task.get('log_path'):
        return Path(str(task['log_path']))
    if project is not None:
        root = Path(project).resolve()
    elif isinstance(contract, (str, Path)) and Path(contract).parent.name == 'contract':
        root = Path(contract).parent.parent
    else:
        root = repository / '.agentic-sdlc'
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return root / 'logs' / 'executor' / f'{_task_id(task)}-{stamp}.log'

def _write_log(path: Path, command: list[str], stdout: str, stderr: str, exit_code: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = 'Command:\n' + ' '.join(command) + f'\nExit code: {exit_code}\n\nSTDOUT\n{_redact(stdout)}\n\nSTDERR\n{_redact(stderr)}\n'
    path.write_text(content, encoding='utf-8')


def smoke_artifact_path(repository: Path) -> Path:
    return Path(repository).resolve() / '.agentic-sdlc' / 'executor-smoke.json'


@contextmanager
def _smoke_workspace(repository: Path):
    parent = Path(repository).resolve().parent
    for _ in range(10):
        scratch = parent / f'psc-executor-smoke-{uuid.uuid4().hex}'
        try:
            scratch.mkdir()
        except FileExistsError:
            continue
        try:
            yield scratch
        finally:
            shutil.rmtree(scratch, ignore_errors=False)
        return
    raise OSError('unable to allocate isolated smoke workspace')


def smoke_is_valid(repository: Path, runtime: Path | str | dict[str, Any]) -> bool:
    try:
        config = _config(runtime)
        artifact = json.loads(smoke_artifact_path(repository).read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return artifact.get('status') == 'passed' and artifact.get('executor_config_sha256') == executor_config_fingerprint(config, repository)


def invoke_executor(
    adapter: str,
    repository: Path,
    task: Any,
    contract: Any,
    previous_review: Any,
    runtime_config_value: Path | str | dict[str, Any],
    *,
    project: Path | None = None,
    timeout: int | None = None,
    require_smoke: bool = True,
    persist_task_artifacts: bool = True,
) -> dict[str, Any]:
    repository = Path(repository).resolve()
    try:
        config = _config(runtime_config_value)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {'status': 'executor_unavailable', 'reason': 'invalid_runtime_config', 'errors': [str(exc)]}
    executor = config['executor']
    if adapter != executor['adapter'] or adapter not in {'codex', 'dsh'}:
        return {'status': 'executor_unavailable', 'reason': 'unsupported_adapter', 'errors': ['configured adapter is ' + str(executor['adapter'])]}
    if require_smoke and not smoke_is_valid(repository, runtime_config_value):
        return {'status': 'executor_smoke_required', 'reason': 'smoke_missing_or_stale'}
    probe = static_probe(config, repository)
    if probe['status'] != 'passed':
        return {'status': 'executor_unavailable', 'reason': probe['reason'], 'errors': probe['errors']}
    if persist_task_artifacts:
        if project is None:
            return {'status': 'executor_unavailable', 'reason': 'task_artifact_directory_required'}
        try:
            artifact_dir = _task_artifact_dir(project, task)
        except ValueError as exc:
            return {'status': 'executor_unavailable', 'reason': str(exc)}
        schema_path = _completion_schema_file()
    else:
        artifact_dir = None
        schema_path = None
    prompt = _executor_prompt(
        task,
        contract,
        previous_review,
        structured_completion=persist_task_artifacts,
    )
    # Snapshot before creating the runtime-owned DSH prompt file so transport
    # bookkeeping cannot appear in product changed_paths.
    before = _git_snapshot(repository)
    prompt_path: Path | None = None
    dsh_metering_patch: Path | None = None
    dsh_session_root = Path(str(executor['executor_home'])).expanduser().resolve() / 'sessions'
    dsh_sessions_before = dsh_session_snapshot(dsh_session_root) if adapter == 'dsh' else {}
    try:
        prompt_argument, stdin_prompt, prompt_path = _prepare_prompt_transport(
            adapter,
            repository,
            prompt,
        )
        command = _build_command(
            adapter,
            str(executor['executable']),
            executor,
            prompt_argument,
            output_schema=schema_path,
        )
        if adapter == 'dsh':
            # Session-title LLM usage is not durably exposed by DSH. It is not
            # needed for disposable Executors, so disable that auxiliary call
            # to keep all E model usage auditable.
            dsh_metering_patch = _dsh_metering_patch_file()
            command[-1:-1] = ['--patch', str(dsh_metering_patch)]
        launch_command = _prepare_command(adapter, command)
    except (OSError, ValueError) as exc:
        _cleanup_prompt_transport(prompt_path)
        if dsh_metering_patch is not None:
            dsh_metering_patch.unlink(missing_ok=True)
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
        return {'status': 'executor_unavailable', 'reason': 'invalid_executor_configuration', 'errors': [str(exc)]}
    child_env = os.environ.copy()
    if adapter == 'codex':
        child_env['CODEX_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())
    else:
        child_env['DSH_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())
    log_path = _log_path(repository, task, contract, project)
    run_timeout = timeout if timeout is not None else executor['timeout']
    try:
        completed = subprocess.run(
            launch_command,
            cwd=str(repository),
            env=child_env,
            input=stdin_prompt,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=run_timeout,
        )
        stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        reason = None if exit_code == 0 else 'process_failed'
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or '')
        stderr = str(exc.stderr or '')
        exit_code, reason = None, 'timeout'
    except OSError as exc:
        stdout, stderr, exit_code, reason = '', str(exc), None, _spawn_failure_reason(exc)
    finally:
        _cleanup_prompt_transport(prompt_path)
        if dsh_metering_patch is not None:
            dsh_metering_patch.unlink(missing_ok=True)
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
    process_settled = exit_code is not None
    completion_stdout = stdout
    if adapter == 'codex':
        codex_final_text, token_usage = parse_codex_exec_jsonl(
            stdout,
            process_settled=process_settled,
        )
        if codex_final_text is not None:
            completion_stdout = codex_final_text
        elif reason == 'launch_transport_failed':
            token_usage = zero_usage('no_model_call')
    else:
        token_usage = collect_dsh_invocation_usage(
            dsh_session_root,
            dsh_sessions_before,
            process_settled=process_settled,
        )
        if reason == 'launch_transport_failed':
            token_usage = zero_usage('no_model_call')
    # The transport file is gone before the after-snapshot.
    after = _git_snapshot(repository)
    changed_paths = _changed_paths_between(before, after)
    violations = _scope_violations(task, changed_paths)
    timeout_adjustment: dict[str, Any] | None = None
    if reason == 'timeout' and not violations:
        timeout_adjustment = _adaptive_timeout_update(
            runtime_config_value,
            config,
            explicit_timeout=timeout,
        )
    if violations:
        reason = 'scope_violation'
    _write_log(log_path, command, stdout, stderr, exit_code)
    completion: dict[str, Any] | None = None
    artifact_paths: dict[str, str] = {}
    errors: list[str] = []
    if reason is None and persist_task_artifacts:
        completion, parse_error = _parse_completion(
            completion_stdout,
            allow_wrapped_json=(adapter == 'dsh'),
        )
        if parse_error is not None:
            reason = 'invalid_executor_output'
            errors.append(parse_error)
        else:
            try:
                artifact_paths = _materialize_executor_artifacts(artifact_dir, completion)
            except OSError as exc:
                reason = 'artifact_persistence_failed'
                errors.append(str(exc))
    if reason is None:
        status = 'completed'
    elif reason == 'scope_violation':
        status = 'scope_violation'
    else:
        status = 'failed'
    return {
        'status': status,
        'reason': reason,
        'exit_code': exit_code,
        'stdout': _redact(stdout),
        'stderr': _redact(stderr),
        'log_path': str(log_path),
        'changed_paths': sorted(changed_paths),
        'scope_violations': violations,
        'executor_config_sha256': executor_config_fingerprint(config, repository),
        'completion': completion,
        'artifact_paths': artifact_paths,
        'timeout_adjustment': timeout_adjustment,
        'retryable': reason != 'launch_transport_failed',
        'token_usage': token_usage,
        'errors': errors,
    }

def invoke_executor_from_paths(
    *,
    repository: Path,
    runtime_config: Path,
    project: Path,
    task_path: Path,
    contract_path: Path,
    previous_review_path: Path | None = None,
) -> dict[str, Any]:
    """Load one persisted PSC task and invoke the configured Executor.

    This is the shared filesystem entrypoint used by both the CLI and the MCP
    transport. It intentionally delegates all execution, isolation, smoke,
    scope, logging, and artifact semantics to invoke_executor().
    """
    repository = Path(repository)
    runtime_config = Path(runtime_config)
    project = Path(project)
    task_path = Path(task_path)
    contract_path = Path(contract_path)
    previous_review_path = Path(previous_review_path) if previous_review_path else None

    try:
        review = previous_review_path.read_text(encoding='utf-8') if previous_review_path else None
        task = {'id': _task_id(task_path), 'text': task_path.read_text(encoding='utf-8')}
        config = _config(runtime_config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            'status': 'executor_unavailable',
            'reason': 'invalid_executor_inputs',
            'errors': [str(exc)],
        }

    return invoke_executor(
        config['executor']['adapter'],
        repository,
        task,
        contract_path,
        review,
        runtime_config,
        project=project,
    )


def smoke_executor(repository: Path, runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    repository = Path(repository).resolve()
    probe = static_probe(runtime, repository)
    if probe['status'] != 'passed':
        artifact = {'schema_version': 1, 'tested_at': _now(), 'status': 'failed', 'reason': probe['reason'], 'errors': probe['errors']}
        _dump_json(smoke_artifact_path(repository), artifact)
        return artifact
    config = _config(runtime)
    with _smoke_workspace(repository) as scratch:
        relative_marker = 'psc-executor-smoke.txt'
        marker_path = scratch / relative_marker
        task = {
            'id': 'T-PSC-SMOKE',
            'text': (
                f'Create the marker file {relative_marker} with exact content:\n\n'
                'PSC_EXECUTOR_SMOKE_OK\n\n'
                'Before the completion report, state the exact active model on one separate line as PSC_MODEL: <model-id>.\n'
                'Requirements:\n'
                '- no BOM\n'
                '- no trailing newline\n'
                '- verify the exact bytes after writing\n'
                'Do not use apply_patch. Do not access outside this workspace.'
            ),
            'Allowed Scope': [relative_marker],
            'Forbidden Scope': ['none'],
            'log_path': str(repository / '.agentic-sdlc' / 'logs' / 'executor' / 'smoke.log'),
        }
        result = invoke_executor(
            config['executor']['adapter'], scratch, task, 'PSC Executor smoke test.', None, runtime,
            timeout=config['executor']['smoke_timeout'], require_smoke=False,
            persist_task_artifacts=False,
        )
        if result['status'] != 'completed':
            reason = result.get('reason') or 'process_failed'
        elif not marker_path.is_file():
            reason = 'expected_marker_missing'
        elif marker_path.read_bytes() != EXPECTED_SMOKE_BYTES:
            reason = 'wrong_marker_content'
        else:
            reason = None
    executor = config['executor']
    artifact = {
        'schema_version': 1,
        'tested_at': _now(),
        'adapter': executor['adapter'],
        'executor_home': str(Path(str(executor['executor_home'])).expanduser()),
        'config_source': executor.get('config_source', 'runtime'),
        'provider': executor.get('provider'),
        'model': executor.get('model'),
        'effort': executor.get('effort'),
        'profile': executor.get('profile'),
        'model_identity': _dsh_model_identity(result.get('stdout', '')) if executor.get('adapter') == 'dsh' else executor.get('model'),
        'approval_policy': executor['approval_policy'],
        'sandbox': executor['sandbox'],
        'executor_config_sha256': executor_config_fingerprint(config, repository),
        'status': 'passed' if reason is None else 'failed',
        'reason': reason,
        'exit_code': result.get('exit_code'),
        'log_path': result.get('log_path'),
    }
    _dump_json(smoke_artifact_path(repository), artifact)
    return artifact


def _dsh_model_identity(stdout: str) -> str | None:
    match = re.search(r'(?im)^\s*PSC_MODEL\s*:\s*([^\r\n]+?)\s*$', stdout)
    if not match:
        return None
    value = match.group(1).strip()
    return value if value and value.upper() != 'UNKNOWN' else None


def executor_status(repository: Path, runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    try:
        config = _config(runtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {'status': 'configuration_required', 'error': str(exc)}
    executor = config['executor']
    artifact_path = smoke_artifact_path(repository)
    try:
        artifact = json.loads(artifact_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        artifact = None
    return {
        'adapter': executor['adapter'], 'executable': executor['executable'], 'executor_home': executor['executor_home'],
        'config_source': executor.get('config_source', 'runtime'),
        'provider': executor.get('provider'), 'model': executor.get('model'), 'effort': executor.get('effort'),
        'profile': executor.get('profile'),
        'approval_policy': executor['approval_policy'], 'sandbox': executor['sandbox'],
        'static_probe': static_probe(config, repository), 'last_smoke': artifact, 'smoke_current': smoke_is_valid(repository, runtime),
    }


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='PSC Executor invocation and health helper')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('smoke', 'status'):
        current = sub.add_parser(command)
        current.add_argument('--repository', type=Path, required=True)
        current.add_argument('--runtime-config', type=Path, required=True)
    invoke = sub.add_parser('invoke')
    invoke.add_argument('--repository', type=Path, required=True)
    invoke.add_argument('--runtime-config', type=Path, required=True)
    invoke.add_argument('--project', type=Path, required=True, help='workflow project containing developing/artifacts')
    invoke.add_argument('--task', type=Path, required=True)
    invoke.add_argument('--contract', type=Path, required=True)
    invoke.add_argument('--previous-review', type=Path)
    args = parser.parse_args()
    if args.command == 'smoke':
        result = smoke_executor(args.repository, args.runtime_config)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result['status'] == 'passed' else 2
    if args.command == 'status':
        print(json.dumps(executor_status(args.repository, args.runtime_config), indent=2, ensure_ascii=False))
        return 0
    result = invoke_executor_from_paths(
        repository=args.repository,
        runtime_config=args.runtime_config,
        project=args.project,
        task_path=args.task,
        contract_path=args.contract,
        previous_review_path=args.previous_review,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r'\1[REDACTED]' if pattern.groups else '[REDACTED]', text)
    return text


def _dump_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _config(runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    return runtime_config(Path(runtime))


def _adaptive_timeout_update(
    runtime: Path | str | dict[str, Any],
    config: dict[str, Any],
    *,
    explicit_timeout: int | None,
) -> dict[str, Any] | None:
    """Double executor.timeout after a normal-task Executor timeout.

    Reaching subprocess.TimeoutExpired means the Executor process was launched
    and remained under runtime control until the configured deadline. No
    stdout/stderr or repository-change evidence is required: slow workers may
    legitimately produce nothing observable before timeout. Smoke/explicit
    timeout overrides never mutate normal runtime timeout. A legacy runtime
    without an explicit maxTimeout stays fixed for backward compatibility.
    """
    if explicit_timeout is not None:
        return None
    executor = config.get('executor', {})
    current = executor.get('timeout')
    maximum = executor.get('maxTimeout')
    if not isinstance(current, int) or current <= 0:
        return None
    if isinstance(runtime, dict):
        return {
            'status': 'not_adjusted',
            'reason': 'runtime_not_persisted',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum if isinstance(maximum, int) else None,
        }
    runtime_path = Path(runtime)
    try:
        raw = json.loads(runtime_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {
            'status': 'not_adjusted',
            'reason': 'runtime_config_unreadable',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum if isinstance(maximum, int) else None,
        }
    raw_executor = raw.get('executor') if isinstance(raw, dict) else None
    if not isinstance(raw_executor, dict) or 'maxTimeout' not in raw_executor:
        return {
            'status': 'not_adjusted',
            'reason': 'maxTimeout_not_configured',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': None,
        }
    maximum = raw_executor.get('maxTimeout')
    if not isinstance(maximum, int) or maximum <= 0 or maximum < current:
        return {
            'status': 'not_adjusted',
            'reason': 'invalid_maxTimeout',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum,
        }
    if current >= maximum:
        return {
            'status': 'at_max',
            'reason': 'maxTimeout_reached',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum,
        }
    new_timeout = min(current * 2, maximum)
    raw_executor['timeout'] = new_timeout
    try:
        _dump_json(runtime_path, raw)
    except OSError:
        return {
            'status': 'not_adjusted',
            'reason': 'runtime_config_write_failed',
            'old_timeout': current,
            'new_timeout': current,
            'maxTimeout': maximum,
        }
    return {
        'status': 'adjusted',
        'reason': 'executor_timed_out',
        'old_timeout': current,
        'new_timeout': new_timeout,
        'maxTimeout': maximum,
    }


def executor_home_config_sha256(config: dict[str, Any]) -> str | None:
    executor = config['executor']
    if executor.get('config_source', 'runtime') != 'executor_home':
        return None
    home = Path(str(executor['executor_home'])).expanduser()
    if executor.get('adapter') == 'dsh':
        profile = str(executor.get('profile', '')).strip()
        paths = (
            home / 'settings.yaml',
            home / 'profiles' / profile / 'package.json',
            home / 'profiles' / profile / 'cordis.patch.yml',
        )
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode('utf-8'))
            digest.update(path.read_bytes())
        return digest.hexdigest()
    return hashlib.sha256((home / 'config.toml').read_bytes()).hexdigest()

def executor_config_fingerprint(config: dict[str, Any]) -> str:
    executor = config['executor']
    stable = {field: executor.get(field) for field in FINGERPRINT_FIELDS}
    stable['executor_home_config_sha256'] = executor_home_config_sha256(config)
    encoded = json.dumps(stable, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def static_probe(runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    try:
        config = _config(runtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {'status': 'failed', 'reason': 'invalid_runtime_config', 'errors': [str(exc)]}
    executor = config.get('executor', {})
    errors: list[str] = []
    adapter = executor.get('adapter')
    if adapter not in {'codex', 'dsh'}:
        errors.append('unsupported_adapter')
    executable = str(executor.get('executable', ''))
    if not executable or (Path(executable).is_absolute() and not Path(executable).is_file()) or (not Path(executable).is_absolute() and shutil.which(executable) is None):
        errors.append('executable_not_found')
    try:
        _prepare_command(str(adapter), [executable, '--version'])
    except OSError:
        errors.append('unsupported_executable_wrapper')
    home = Path(str(executor.get('executor_home', ''))).expanduser()
    if not home.is_dir():
        errors.append('executor_home_not_found')
    if adapter == 'dsh':
        profile = str(executor.get('profile', '')).strip()
        if not profile or not (home / 'profiles' / profile / 'package.json').is_file():
            errors.append('dsh_profile_not_found')
    if executor.get('approvals_reviewer') == 'auto_review' and executable and not supports_auto_review(executable):
        errors.append('auto_review_unsupported')
    try:
        executor_home_config_sha256(config)
    except OSError:
        errors.append('executor_config_not_readable')
    if errors:
        return {'status': 'failed', 'reason': errors[0], 'errors': errors}
    return {'status': 'passed', 'adapter': adapter, 'executor_home': str(home.resolve()), 'executor_config_sha256': executor_config_fingerprint(config)}


def _prepare_command(adapter: str, command: list[str]) -> list[str]:
    if adapter == 'codex':
        return codex_adapter.prepare_command(command)
    if adapter == 'dsh':
        return dsh_adapter.prepare_command(command)
    raise OSError(f'unsupported adapter: {adapter}')


def _build_command(adapter: str, executable: str, executor: dict[str, Any], prompt: str, *, output_schema: Path | None) -> list[str]:
    if adapter == 'codex':
        return codex_adapter.build_command(executable, executor, prompt, output_schema=output_schema)
    if adapter == 'dsh':
        return dsh_adapter.build_command(executable, executor, prompt)
    raise ValueError(f'unsupported adapter: {adapter}')


def _task_text(task: Any) -> str:
    if isinstance(task, Path):
        return task.read_text(encoding='utf-8')
    if isinstance(task, dict):
        return str(task.get('text') or task.get('markdown') or 'No task text provided.')
    return str(task)


def _task_id(task: Any) -> str:
    if isinstance(task, dict) and task.get('id'):
        return str(task['id'])
    match = re.search(r'\bT-\d{3,}\b', _task_text(task))
    return match.group(0) if match else 'T-UNKNOWN'


def _contract_text(contract: Any) -> str:
    if isinstance(contract, (str, Path)) and Path(contract).is_dir():
        root = Path(contract)
        pieces = []
        for name in ('requirements.md', 'acceptance.md', 'implementation.md', 'constraints.md'):
            path = root / name
            if path.is_file():
                pieces.append(path.read_text(encoding='utf-8'))
        return '\n\n'.join(pieces)
    if isinstance(contract, dict):
        return str(contract.get('text') or 'No Contract excerpt provided.')
    return str(contract)


def _executor_prompt(task: Any, contract: Any, previous_review: Any, *, structured_completion: bool = True) -> str:
    review = str(previous_review or 'No previous Supervisor review exists.')
    sections = [
        'You are a disposable PSC Executor. Work only on the current repository and task.',
        'You may edit only Allowed Scope, respect Forbidden Scope, and may add required tests. Do not edit contract files, runtime state, review.md, or result.md.',
        '## Current Task\n' + _task_text(task),
        '## Relevant Contract\n' + _contract_text(contract),
        '## Previous Supervisor Review\n' + review,
    ]
    if structured_completion:
        sections.append(
            'Do not write PSC runtime artifacts directly. Your final response must be exactly one JSON object, without Markdown fences or extra text, using this schema: '
            '{"schema_version":1,"plan":"...","coding_summary":"...","modified_files":["..."],"tests":["..."],"known_risks":["..."],"unresolved_issues":["..."]}. '
            'Use empty arrays when a list has no entries. The invocation layer persists this Executor-owned content.'
        )
    else:
        sections.append('Return a concise completion report after verifying the requested smoke marker.')
    return '\n\n'.join(sections)


def _prepare_prompt_transport(
    adapter: str,
    repository: Path,
    prompt: str,
) -> tuple[str, str | None, Path | None]:
    """Keep large Executor prompts out of argv.

    Codex uses its explicit stdin sentinel, so the complete prompt is written
    to stdin. DSH headless currently requires a positional task, so PSC writes
    the complete prompt to a short-lived runtime-owned workspace file and
    passes only a short bootstrap instruction in argv.
    """
    if adapter == 'codex':
        return '-', prompt, None
    if adapter == 'dsh':
        repository = Path(repository).resolve()
        prompt_dir = repository / '.agentic-sdlc' / 'runtime' / 'executor-inputs'
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f'psc-executor-prompt-{uuid.uuid4().hex}.md'
        prompt_path.write_text(prompt, encoding='utf-8', newline='\n')
        relative = prompt_path.relative_to(repository).as_posix()
        bootstrap = (
            'Read the complete PSC Executor instructions from the UTF-8 file '
            f'{relative} in the current workspace. Follow that file exactly as '
            'the user task. The file is runtime-owned and read-only: do not '
            'modify, rename, or delete it.'
        )
        return bootstrap, None, prompt_path
    raise ValueError(f'unsupported adapter: {adapter}')


def _cleanup_prompt_transport(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
    parent = path.parent
    for candidate in (parent, parent.parent):
        try:
            candidate.rmdir()
        except OSError:
            break


def _dsh_metering_patch_file() -> Path:
    """Disable unmetered automatic session-title LLM calls for disposable E."""
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        suffix='.yml',
        prefix='psc-dsh-metering-',
        delete=False,
    ) as handle:
        handle.write("- id: session-title-llm\n  disabled: true\n")
        return Path(handle.name)


def _spawn_failure_reason(exc: OSError) -> str:
    """Classify deterministic command-line transport failures separately."""
    if getattr(exc, 'winerror', None) == 206 or getattr(exc, 'errno', None) == errno.ENAMETOOLONG:
        return 'launch_transport_failed'
    return 'spawn_failed'

def _completion_validation_error(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != set(COMPLETION_FIELDS):
        return 'final response must be exactly the PSC structured completion schema'
    if value.get('schema_version') != 1:
        return 'structured completion schema_version must be 1'
    for name in ('plan', 'coding_summary'):
        if not isinstance(value[name], str) or not value[name].strip():
            return f'structured completion {name} must be a non-empty string'
    for name in ('modified_files', 'tests', 'known_risks', 'unresolved_issues'):
        if not isinstance(value[name], list) or any(not isinstance(item, str) for item in value[name]):
            return f'structured completion {name} must be an array of strings'
    return None


def _parse_completion(
    stdout: str,
    *,
    allow_wrapped_json: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    text = stdout.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        strict_error = f'final response is not valid JSON: {exc.msg}'
    else:
        validation_error = _completion_validation_error(value)
        if validation_error is None:
            return value, None
        strict_error = validation_error

    if not allow_wrapped_json:
        return None, strict_error

    # Some harnesses (notably DSH-backed models) may emit explanatory prose or
    # Markdown fences before the required completion object. Scan for JSON
    # objects and accept only the last object that independently satisfies the
    # exact PSC completion schema. This is framing tolerance, not schema
    # tolerance: malformed or partial objects are still rejected.
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != '{':
            continue
        try:
            candidate, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if _completion_validation_error(candidate) is None:
            candidates.append(candidate)
    if candidates:
        return candidates[-1], None
    return None, strict_error


def _write_text_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def _markdown_list(items: list[str]) -> str:
    return '\n'.join(f'- {item}' for item in items)


def _materialize_executor_artifacts(artifact_dir: Path, completion: dict[str, Any]) -> dict[str, str]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_dir / 'plan.md'
    coding_path = artifact_dir / 'coding.md'
    plan = completion['plan'].rstrip() + '\n'
    coding_sections = [
        '# Executor Coding Summary',
        '',
        completion['coding_summary'].rstrip(),
        '',
        '## Modified Files',
        _markdown_list(completion['modified_files']),
        '',
        '## Tests',
        _markdown_list(completion['tests']),
        '',
        '## Known Risks',
        _markdown_list(completion['known_risks']),
        '',
        '## Unresolved Issues',
        _markdown_list(completion['unresolved_issues']),
        '',
    ]
    _write_text_atomically(plan_path, plan)
    _write_text_atomically(coding_path, '\n'.join(coding_sections))
    return {'plan': str(plan_path), 'coding': str(coding_path)}


def _task_artifact_dir(project: Path, task: Any) -> Path:
    task_id = _task_id(task)
    if task_id == 'T-UNKNOWN':
        raise ValueError('task_artifact_directory_requires_stable_task_id')
    return Path(project).resolve() / 'developing' / 'artifacts' / task_id


def _completion_schema_file() -> Path:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', prefix='psc-executor-output-schema-', delete=False) as handle:
        json.dump(COMPLETION_OUTPUT_SCHEMA, handle, ensure_ascii=False)
        handle.write('\n')
        return Path(handle.name)

def _git_dirty_paths(repository: Path) -> set[str]:
    try:
        diff = subprocess.run(
            ['git', '-C', str(repository), 'diff', '--name-only'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
        cached = subprocess.run(
            ['git', '-C', str(repository), 'diff', '--cached', '--name-only'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
        untracked = subprocess.run(
            ['git', '-C', str(repository), 'ls-files', '--others', '--exclude-standard'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {
        line.strip().replace('\\', '/')
        for output in (diff, cached, untracked)
        for line in output.splitlines()
        if line.strip()
    }


def _git_path_fingerprint(repository: Path, relative_path: str) -> str:
    path = repository / relative_path
    digest = hashlib.sha256()
    digest.update(relative_path.encode('utf-8', errors='replace'))
    if path.is_file():
        digest.update(b'\\0file\\0')
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b'<unreadable>')
    elif path.exists():
        digest.update(b'\\0non-file\\0')
    else:
        digest.update(b'\\0missing\\0')
    try:
        index_entry = subprocess.run(
            ['git', '-C', str(repository), 'ls-files', '-s', '--', relative_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        index_entry = ''
    digest.update(b'\\0index\\0')
    digest.update(index_entry.encode('utf-8', errors='replace'))
    return digest.hexdigest()


def _git_snapshot(repository: Path) -> dict[str, str]:
    repository = Path(repository).resolve()
    return {
        path: _git_path_fingerprint(repository, path)
        for path in _git_dirty_paths(repository)
    }


def _git_paths(repository: Path) -> set[str]:
    return set(_git_snapshot(repository))


def _changed_paths_between(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    }


def _scope_values(task: Any, label: str) -> list[str]:
    if isinstance(task, dict) and isinstance(task.get(label), list):
        return [str(value).strip() for value in task[label]]
    match = re.search(rf'(?im)^{re.escape(label)}:\s*\n((?:\s*[-*]\s*.*\n?)*)', _task_text(task))
    if not match:
        return []
    values = [line.strip()[1:].strip() for line in match.group(1).splitlines() if line.strip().startswith(('-', '*'))]
    return [re.split(r'\s+(?:for|only if)\s+', value, maxsplit=1, flags=re.IGNORECASE)[0].strip() for value in values]


def _matches_scope(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.replace('\\', '/').strip().rstrip('/')
        if normalized.lower() in {'', 'none'}:
            continue
        if normalized in {'.', '*'} or path == normalized or path.startswith(normalized + '/') or fnmatch(path, normalized):
            return True
    return False


def _scope_violations(task: Any, changed_paths: set[str]) -> list[str]:
    allowed = _scope_values(task, 'Allowed Scope')
    forbidden = _scope_values(task, 'Forbidden Scope')
    violations = [path for path in changed_paths if forbidden and _matches_scope(path, forbidden)]
    if allowed:
        violations.extend(path for path in changed_paths if not _matches_scope(path, allowed))
    return sorted(set(violations))


def _log_path(repository: Path, task: Any, contract: Any, project: Path | None = None) -> Path:
    if isinstance(task, dict) and task.get('log_path'):
        return Path(str(task['log_path']))
    if project is not None:
        root = Path(project).resolve()
    elif isinstance(contract, (str, Path)) and Path(contract).parent.name == 'contract':
        root = Path(contract).parent.parent
    else:
        root = repository / '.agentic-sdlc'
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return root / 'logs' / 'executor' / f'{_task_id(task)}-{stamp}.log'

def _write_log(path: Path, command: list[str], stdout: str, stderr: str, exit_code: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = 'Command:\n' + ' '.join(command) + f'\nExit code: {exit_code}\n\nSTDOUT\n{_redact(stdout)}\n\nSTDERR\n{_redact(stderr)}\n'
    path.write_text(content, encoding='utf-8')


def smoke_artifact_path(repository: Path) -> Path:
    return Path(repository).resolve() / '.agentic-sdlc' / 'executor-smoke.json'


@contextmanager
def _smoke_workspace(repository: Path):
    parent = Path(repository).resolve().parent
    for _ in range(10):
        scratch = parent / f'psc-executor-smoke-{uuid.uuid4().hex}'
        try:
            scratch.mkdir()
        except FileExistsError:
            continue
        try:
            yield scratch
        finally:
            shutil.rmtree(scratch, ignore_errors=False)
        return
    raise OSError('unable to allocate isolated smoke workspace')


def smoke_is_valid(repository: Path, runtime: Path | str | dict[str, Any]) -> bool:
    try:
        config = _config(runtime)
        artifact = json.loads(smoke_artifact_path(repository).read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return artifact.get('status') == 'passed' and artifact.get('executor_config_sha256') == executor_config_fingerprint(config)


def invoke_executor(
    adapter: str,
    repository: Path,
    task: Any,
    contract: Any,
    previous_review: Any,
    runtime_config_value: Path | str | dict[str, Any],
    *,
    project: Path | None = None,
    timeout: int | None = None,
    require_smoke: bool = True,
    persist_task_artifacts: bool = True,
) -> dict[str, Any]:
    repository = Path(repository).resolve()
    try:
        config = _config(runtime_config_value)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {'status': 'executor_unavailable', 'reason': 'invalid_runtime_config', 'errors': [str(exc)]}
    executor = config['executor']
    if adapter != executor['adapter'] or adapter not in {'codex', 'dsh'}:
        return {'status': 'executor_unavailable', 'reason': 'unsupported_adapter', 'errors': ['configured adapter is ' + str(executor['adapter'])]}
    if require_smoke and not smoke_is_valid(repository, runtime_config_value):
        return {'status': 'executor_smoke_required', 'reason': 'smoke_missing_or_stale'}
    probe = static_probe(config)
    if probe['status'] != 'passed':
        return {'status': 'executor_unavailable', 'reason': probe['reason'], 'errors': probe['errors']}
    if persist_task_artifacts:
        if project is None:
            return {'status': 'executor_unavailable', 'reason': 'task_artifact_directory_required'}
        try:
            artifact_dir = _task_artifact_dir(project, task)
        except ValueError as exc:
            return {'status': 'executor_unavailable', 'reason': str(exc)}
        schema_path = _completion_schema_file()
    else:
        artifact_dir = None
        schema_path = None
    prompt = _executor_prompt(
        task,
        contract,
        previous_review,
        structured_completion=persist_task_artifacts,
    )
    # Snapshot before creating the runtime-owned DSH prompt file so transport
    # bookkeeping cannot appear in product changed_paths.
    before = _git_snapshot(repository)
    prompt_path: Path | None = None
    dsh_metering_patch: Path | None = None
    dsh_session_root = Path(str(executor['executor_home'])).expanduser().resolve() / 'sessions'
    dsh_sessions_before = dsh_session_snapshot(dsh_session_root) if adapter == 'dsh' else {}
    try:
        prompt_argument, stdin_prompt, prompt_path = _prepare_prompt_transport(
            adapter,
            repository,
            prompt,
        )
        command = _build_command(
            adapter,
            str(executor['executable']),
            executor,
            prompt_argument,
            output_schema=schema_path,
        )
        if adapter == 'dsh':
            # Session-title LLM usage is not durably exposed by DSH. It is not
            # needed for disposable Executors, so disable that auxiliary call
            # to keep all E model usage auditable.
            dsh_metering_patch = _dsh_metering_patch_file()
            command[-1:-1] = ['--patch', str(dsh_metering_patch)]
        launch_command = _prepare_command(adapter, command)
    except (OSError, ValueError) as exc:
        _cleanup_prompt_transport(prompt_path)
        if dsh_metering_patch is not None:
            dsh_metering_patch.unlink(missing_ok=True)
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
        return {'status': 'executor_unavailable', 'reason': 'invalid_executor_configuration', 'errors': [str(exc)]}
    child_env = os.environ.copy()
    if adapter == 'codex':
        child_env['CODEX_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())
    else:
        child_env['DSH_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())
    log_path = _log_path(repository, task, contract, project)
    run_timeout = timeout if timeout is not None else executor['timeout']
    try:
        completed = subprocess.run(
            launch_command,
            cwd=str(repository),
            env=child_env,
            input=stdin_prompt,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=run_timeout,
        )
        stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
        reason = None if exit_code == 0 else 'process_failed'
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or '')
        stderr = str(exc.stderr or '')
        exit_code, reason = None, 'timeout'
    except OSError as exc:
        stdout, stderr, exit_code, reason = '', str(exc), None, _spawn_failure_reason(exc)
    finally:
        _cleanup_prompt_transport(prompt_path)
        if dsh_metering_patch is not None:
            dsh_metering_patch.unlink(missing_ok=True)
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
    process_settled = exit_code is not None
    completion_stdout = stdout
    if adapter == 'codex':
        codex_final_text, token_usage = parse_codex_exec_jsonl(
            stdout,
            process_settled=process_settled,
        )
        if codex_final_text is not None:
            completion_stdout = codex_final_text
        elif reason == 'launch_transport_failed':
            token_usage = zero_usage('no_model_call')
    else:
        token_usage = collect_dsh_invocation_usage(
            dsh_session_root,
            dsh_sessions_before,
            process_settled=process_settled,
        )
        if reason == 'launch_transport_failed':
            token_usage = zero_usage('no_model_call')
    # The transport file is gone before the after-snapshot.
    after = _git_snapshot(repository)
    changed_paths = _changed_paths_between(before, after)
    violations = _scope_violations(task, changed_paths)
    timeout_adjustment: dict[str, Any] | None = None
    if reason == 'timeout' and not violations:
        timeout_adjustment = _adaptive_timeout_update(
            runtime_config_value,
            config,
            explicit_timeout=timeout,
        )
    if violations:
        reason = 'scope_violation'
    _write_log(log_path, command, stdout, stderr, exit_code)
    completion: dict[str, Any] | None = None
    artifact_paths: dict[str, str] = {}
    errors: list[str] = []
    if reason is None and persist_task_artifacts:
        completion, parse_error = _parse_completion(
            completion_stdout,
            allow_wrapped_json=(adapter == 'dsh'),
        )
        if parse_error is not None:
            reason = 'invalid_executor_output'
            errors.append(parse_error)
        else:
            try:
                artifact_paths = _materialize_executor_artifacts(artifact_dir, completion)
            except OSError as exc:
                reason = 'artifact_persistence_failed'
                errors.append(str(exc))
    if reason is None:
        status = 'completed'
    elif reason == 'scope_violation':
        status = 'scope_violation'
    else:
        status = 'failed'
    return {
        'status': status,
        'reason': reason,
        'exit_code': exit_code,
        'stdout': _redact(stdout),
        'stderr': _redact(stderr),
        'log_path': str(log_path),
        'changed_paths': sorted(changed_paths),
        'scope_violations': violations,
        'executor_config_sha256': executor_config_fingerprint(config),
        'completion': completion,
        'artifact_paths': artifact_paths,
        'timeout_adjustment': timeout_adjustment,
        'retryable': reason != 'launch_transport_failed',
        'token_usage': token_usage,
        'errors': errors,
    }

def invoke_executor_from_paths(
    *,
    repository: Path,
    runtime_config: Path,
    project: Path,
    task_path: Path,
    contract_path: Path,
    previous_review_path: Path | None = None,
) -> dict[str, Any]:
    """Load one persisted PSC task and invoke the configured Executor.

    This is the shared filesystem entrypoint used by both the CLI and the MCP
    transport. It intentionally delegates all execution, isolation, smoke,
    scope, logging, and artifact semantics to invoke_executor().
    """
    repository = Path(repository)
    runtime_config = Path(runtime_config)
    project = Path(project)
    task_path = Path(task_path)
    contract_path = Path(contract_path)
    previous_review_path = Path(previous_review_path) if previous_review_path else None

    try:
        review = previous_review_path.read_text(encoding='utf-8') if previous_review_path else None
        task = {'id': _task_id(task_path), 'text': task_path.read_text(encoding='utf-8')}
        config = _config(runtime_config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            'status': 'executor_unavailable',
            'reason': 'invalid_executor_inputs',
            'errors': [str(exc)],
        }

    return invoke_executor(
        config['executor']['adapter'],
        repository,
        task,
        contract_path,
        review,
        runtime_config,
        project=project,
    )


def smoke_executor(repository: Path, runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    repository = Path(repository).resolve()
    probe = static_probe(runtime)
    if probe['status'] != 'passed':
        artifact = {'schema_version': 1, 'tested_at': _now(), 'status': 'failed', 'reason': probe['reason'], 'errors': probe['errors']}
        _dump_json(smoke_artifact_path(repository), artifact)
        return artifact
    config = _config(runtime)
    with _smoke_workspace(repository) as scratch:
        relative_marker = 'psc-executor-smoke.txt'
        marker_path = scratch / relative_marker
        task = {
            'id': 'T-PSC-SMOKE',
            'text': (
                f'Create the marker file {relative_marker} with exact content:\n\n'
                'PSC_EXECUTOR_SMOKE_OK\n\n'
                'Before the completion report, state the exact active model on one separate line as PSC_MODEL: <model-id>.\n'
                'Requirements:\n'
                '- no BOM\n'
                '- no trailing newline\n'
                '- verify the exact bytes after writing\n'
                'Do not use apply_patch. Do not access outside this workspace.'
            ),
            'Allowed Scope': [relative_marker],
            'Forbidden Scope': ['none'],
            'log_path': str(repository / '.agentic-sdlc' / 'logs' / 'executor' / 'smoke.log'),
        }
        result = invoke_executor(
            config['executor']['adapter'], scratch, task, 'PSC Executor smoke test.', None, runtime,
            timeout=config['executor']['smoke_timeout'], require_smoke=False,
            persist_task_artifacts=False,
        )
        if result['status'] != 'completed':
            reason = result.get('reason') or 'process_failed'
        elif not marker_path.is_file():
            reason = 'expected_marker_missing'
        elif marker_path.read_bytes() != EXPECTED_SMOKE_BYTES:
            reason = 'wrong_marker_content'
        else:
            reason = None
    executor = config['executor']
    artifact = {
        'schema_version': 1,
        'tested_at': _now(),
        'adapter': executor['adapter'],
        'executor_home': str(Path(str(executor['executor_home'])).expanduser()),
        'config_source': executor.get('config_source', 'runtime'),
        'provider': executor.get('provider'),
        'model': executor.get('model'),
        'effort': executor.get('effort'),
        'profile': executor.get('profile'),
        'model_identity': _dsh_model_identity(result.get('stdout', '')) if executor.get('adapter') == 'dsh' else executor.get('model'),
        'approval_policy': executor['approval_policy'],
        'sandbox': executor['sandbox'],
        'executor_config_sha256': executor_config_fingerprint(config),
        'status': 'passed' if reason is None else 'failed',
        'reason': reason,
        'exit_code': result.get('exit_code'),
        'log_path': result.get('log_path'),
    }
    _dump_json(smoke_artifact_path(repository), artifact)
    return artifact


def _dsh_model_identity(stdout: str) -> str | None:
    match = re.search(r'(?im)^\s*PSC_MODEL\s*:\s*([^\r\n]+?)\s*$', stdout)
    if not match:
        return None
    value = match.group(1).strip()
    return value if value and value.upper() != 'UNKNOWN' else None


def executor_status(repository: Path, runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    try:
        config = _config(runtime)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {'status': 'configuration_required', 'error': str(exc)}
    executor = config['executor']
    artifact_path = smoke_artifact_path(repository)
    try:
        artifact = json.loads(artifact_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        artifact = None
    return {
        'adapter': executor['adapter'], 'executable': executor['executable'], 'executor_home': executor['executor_home'],
        'config_source': executor.get('config_source', 'runtime'),
        'provider': executor.get('provider'), 'model': executor.get('model'), 'effort': executor.get('effort'),
        'profile': executor.get('profile'),
        'approval_policy': executor['approval_policy'], 'sandbox': executor['sandbox'],
        'static_probe': static_probe(config), 'last_smoke': artifact, 'smoke_current': smoke_is_valid(repository, runtime),
    }


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='PSC Executor invocation and health helper')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('smoke', 'status'):
        current = sub.add_parser(command)
        current.add_argument('--repository', type=Path, required=True)
        current.add_argument('--runtime-config', type=Path, required=True)
    invoke = sub.add_parser('invoke')
    invoke.add_argument('--repository', type=Path, required=True)
    invoke.add_argument('--runtime-config', type=Path, required=True)
    invoke.add_argument('--project', type=Path, required=True, help='workflow project containing developing/artifacts')
    invoke.add_argument('--task', type=Path, required=True)
    invoke.add_argument('--contract', type=Path, required=True)
    invoke.add_argument('--previous-review', type=Path)
    args = parser.parse_args()
    if args.command == 'smoke':
        result = smoke_executor(args.repository, args.runtime_config)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result['status'] == 'passed' else 2
    if args.command == 'status':
        print(json.dumps(executor_status(args.repository, args.runtime_config), indent=2, ensure_ascii=False))
        return 0
    result = invoke_executor_from_paths(
        repository=args.repository,
        runtime_config=args.runtime_config,
        project=args.project,
        task_path=args.task,
        contract_path=args.contract,
        previous_review_path=args.previous_review,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
