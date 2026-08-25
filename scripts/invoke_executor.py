#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from adapters.codex import build_command, prepare_command, supports_auto_review
from psc_runtime import runtime_config


FINGERPRINT_FIELDS = ('adapter', 'executable', 'executor_home', 'config_source', 'provider', 'model', 'effort', 'approval_policy', 'sandbox', 'approvals_reviewer')
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
        'schema_version': {'const': 1},
        'plan': {'type': 'string', 'minLength': 1},
        'coding_summary': {'type': 'string', 'minLength': 1},
        'modified_files': {'type': 'array', 'items': {'type': 'string'}},
        'tests': {'type': 'array', 'items': {'type': 'string'}},
        'known_risks': {'type': 'array', 'items': {'type': 'string'}},
        'unresolved_issues': {'type': 'array', 'items': {'type': 'string'}},
    },
}


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


def executor_home_config_sha256(config: dict[str, Any]) -> str | None:
    executor = config['executor']
    if executor.get('config_source', 'runtime') != 'executor_home':
        return None
    config_path = Path(str(executor['executor_home'])).expanduser() / 'config.toml'
    return hashlib.sha256(config_path.read_bytes()).hexdigest()

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
    if executor.get('adapter') != 'codex':
        errors.append('unsupported_adapter')
    executable = str(executor.get('executable', ''))
    if not executable or (Path(executable).is_absolute() and not Path(executable).is_file()) or (not Path(executable).is_absolute() and shutil.which(executable) is None):
        errors.append('executable_not_found')
    try:
        prepare_command([executable, '--version'])
    except OSError:
        errors.append('unsupported_executable_wrapper')
    home = Path(str(executor.get('executor_home', ''))).expanduser()
    if not home.is_dir():
        errors.append('executor_home_not_found')
    if executor.get('approvals_reviewer') == 'auto_review' and executable and not supports_auto_review(executable):
        errors.append('auto_review_unsupported')
    try:
        executor_home_config_sha256(config)
    except OSError:
        errors.append('executor_config_not_readable')
    if errors:
        return {'status': 'failed', 'reason': errors[0], 'errors': errors}
    return {'status': 'passed', 'adapter': 'codex', 'executor_home': str(home.resolve()), 'executor_config_sha256': executor_config_fingerprint(config)}


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


def _parse_completion(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        return None, f'final response is not valid JSON: {exc.msg}'
    if not isinstance(value, dict) or set(value) != set(COMPLETION_FIELDS):
        return None, 'final response must be exactly the PSC structured completion schema'
    if value.get('schema_version') != 1:
        return None, 'structured completion schema_version must be 1'
    for name in ('plan', 'coding_summary'):
        if not isinstance(value[name], str) or not value[name].strip():
            return None, f'structured completion {name} must be a non-empty string'
    for name in ('modified_files', 'tests', 'known_risks', 'unresolved_issues'):
        if not isinstance(value[name], list) or any(not isinstance(item, str) for item in value[name]):
            return None, f'structured completion {name} must be an array of strings'
    return value, None


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

def _git_paths(repository: Path) -> set[str]:
    try:
        status = subprocess.run(['git', '-C', str(repository), 'status', '--porcelain'], capture_output=True, text=True, check=True).stdout
        diff = subprocess.run(['git', '-C', str(repository), 'diff', '--name-only'], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    paths = {line[3:].strip().replace('\\', '/') for line in status.splitlines() if len(line) > 3}
    paths.update(line.strip().replace('\\', '/') for line in diff.splitlines() if line.strip())
    return paths


def _scope_values(task: Any, label: str) -> list[str]:
    if isinstance(task, dict) and isinstance(task.get(label), list):
        return [str(value).strip() for value in task[label]]
    match = re.search(rf'(?im)^{re.escape(label)}:\s*\n((?:\s*[-*]\s*.*\n?)*)', _task_text(task))
    if not match:
        return []
    return [line.strip()[1:].strip() for line in match.group(1).splitlines() if line.strip().startswith(('-', '*'))]


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
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return root / 'logs' / 'executor' / f'{_task_id(task)}-{stamp}.log'

def _write_log(path: Path, command: list[str], stdout: str, stderr: str, exit_code: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = 'Command:\n' + ' '.join(command) + f'\nExit code: {exit_code}\n\nSTDOUT\n{_redact(stdout)}\n\nSTDERR\n{_redact(stderr)}\n'
    path.write_text(content, encoding='utf-8')


def smoke_artifact_path(repository: Path) -> Path:
    return Path(repository).resolve() / '.agentic-sdlc' / 'executor-smoke.json'


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
    if adapter != executor['adapter'] or adapter != 'codex':
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
    try:
        prompt = _executor_prompt(
            task,
            contract,
            previous_review,
            structured_completion=persist_task_artifacts,
        )
        command = build_command(
            str(executor['executable']),
            executor,
            prompt,
            output_schema=schema_path,
        )
        launch_command = prepare_command(command)
    except (OSError, ValueError) as exc:
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
        return {'status': 'executor_unavailable', 'reason': 'invalid_executor_configuration', 'errors': [str(exc)]}
    child_env = os.environ.copy()
    child_env['CODEX_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())
    before = _git_paths(repository)
    log_path = _log_path(repository, task, contract, project)
    run_timeout = timeout if timeout is not None else executor['timeout']
    try:
        completed = subprocess.run(
            launch_command,
            cwd=str(repository),
            env=child_env,
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
        stdout, stderr, exit_code, reason = '', str(exc), None, 'spawn_failed'
    finally:
        if schema_path is not None:
            schema_path.unlink(missing_ok=True)
    _write_log(log_path, command, stdout, stderr, exit_code)
    after = _git_paths(repository)
    changed_paths = after - before
    violations = _scope_violations(task, changed_paths)
    if violations:
        reason = 'scope_violation'
    completion: dict[str, Any] | None = None
    artifact_paths: dict[str, str] = {}
    errors: list[str] = []
    if reason is None and persist_task_artifacts:
        completion, parse_error = _parse_completion(stdout)
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
        'errors': errors,
    }

def smoke_executor(repository: Path, runtime: Path | str | dict[str, Any]) -> dict[str, Any]:
    repository = Path(repository).resolve()
    probe = static_probe(runtime)
    if probe['status'] != 'passed':
        artifact = {'schema_version': 1, 'tested_at': _now(), 'status': 'failed', 'reason': probe['reason'], 'errors': probe['errors']}
        _dump_json(smoke_artifact_path(repository), artifact)
        return artifact
    config = _config(runtime)
    smoke_parent = repository / 'temp'
    smoke_parent.mkdir(parents=True, exist_ok=True)
    marker = f'psc-executor-smoke-{uuid.uuid4().hex}.txt'
    relative_marker = smoke_parent.relative_to(repository).as_posix() + '/' + marker
    marker_path = repository / relative_marker
    marker_path.unlink(missing_ok=True)
    task = {
        'id': 'T-PSC-SMOKE',
        'text': f'Create {relative_marker} using PowerShell [System.IO.File]::WriteAllText with UTF8Encoding(false), exact bytes PSC_EXECUTOR_SMOKE_OK and no trailing newline. Read the file bytes back and require exactly 20 bytes before reporting success. Do not use apply_patch. Do not access outside this workspace.',
        'Allowed Scope': [relative_marker],
        'Forbidden Scope': ['none'],
        'log_path': str(repository / '.agentic-sdlc' / 'logs' / 'executor' / 'smoke.log'),
    }
    try:
        result = invoke_executor('codex', repository, task, 'PSC Executor smoke test.', None, runtime, timeout=config['executor']['smoke_timeout'], require_smoke=False, persist_task_artifacts=False)
        if result['status'] != 'completed':
            reason = result.get('reason') or 'process_failed'
        elif not marker_path.is_file():
            reason = 'expected_marker_missing'
        elif marker_path.read_text(encoding='utf-8') != 'PSC_EXECUTOR_SMOKE_OK':
            reason = 'wrong_marker_content'
        else:
            reason = None
    finally:
        marker_path.unlink(missing_ok=True)
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
        'approval_policy': executor['approval_policy'], 'sandbox': executor['sandbox'],
        'static_probe': static_probe(config), 'last_smoke': artifact, 'smoke_current': smoke_is_valid(repository, runtime),
    }


def main() -> int:
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
    review = args.previous_review.read_text(encoding='utf-8') if args.previous_review else None
    task = {'id': _task_id(args.task), 'text': args.task.read_text(encoding='utf-8')}
    config = _config(args.runtime_config)
    result = invoke_executor(config['executor']['adapter'], args.repository, task, args.contract, review, args.runtime_config, project=args.project)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
