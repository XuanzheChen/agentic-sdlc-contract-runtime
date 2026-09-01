from __future__ import annotations

import datetime as dt
import errno
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import SKILL_ROOT, build_bundle_text, default_sections, project_dir, run_cli, write_external_bundle


sys.path.insert(0, str(SKILL_ROOT / 'scripts'))
_SPEC = importlib.util.spec_from_file_location('psc_executor_runtime', SKILL_ROOT / 'scripts' / 'invoke_executor.py')
assert _SPEC and _SPEC.loader
EXECUTOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(EXECUTOR)
from adapters import codex as CODEX_ADAPTER


def _fake_run_factory(write_marker: bool = True, timeout: bool = False, marker_bytes: bytes = b'PSC_EXECUTOR_SMOKE_OK'):
    observed = {}
    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        observed['env'] = kwargs['env']
        observed['command'] = command
        observed['cwd'] = kwargs['cwd']
        if timeout:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        if write_marker:
            marker = Path(kwargs['cwd']) / 'psc-executor-smoke.txt'
            marker.write_bytes(marker_bytes)
        return SimpleNamespace(stdout='ok', stderr='', returncode=0)
    return fake_run, observed


def test_executor_child_home_isolated(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    supervisor_home = tmp_path / 'supervisor-home'
    supervisor_home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(supervisor_home))
    fake_run, observed = _fake_run_factory()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = EXECUTOR.smoke_executor(tmp_path, tmp_runtime)
    assert result['status'] == 'passed'
    assert os.environ['CODEX_HOME'] == str(supervisor_home)
    assert observed['env']['CODEX_HOME'] == config['executor']['executor_home']


def test_smoke_uses_isolated_temporary_workspace(monkeypatch, tmp_path, tmp_runtime):
    fake_run, observed = _fake_run_factory()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = EXECUTOR.smoke_executor(tmp_path, tmp_runtime)
    assert result['status'] == 'passed'
    assert observed['cwd'] != str(tmp_path.resolve())
    assert Path(observed['cwd']).name.startswith('psc-executor-smoke-')
    assert not (tmp_path / 'temp').exists()


def test_smoke_rejects_non_exact_marker_bytes(monkeypatch, tmp_path, tmp_runtime):
    fake_run, _ = _fake_run_factory(marker_bytes=b'PSC_EXECUTOR_SMOKE_OK\n')
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = EXECUTOR.smoke_executor(tmp_path, tmp_runtime)
    assert result['status'] == 'failed'
    assert result['reason'] == 'wrong_marker_content'


@pytest.mark.parametrize('write_marker,timeout,reason', [(False, False, 'expected_marker_missing'), (True, True, 'timeout')])
def test_smoke_failure_modes(monkeypatch, tmp_path, tmp_runtime, write_marker, timeout, reason):
    fake_run, _ = _fake_run_factory(write_marker, timeout)
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = EXECUTOR.smoke_executor(tmp_path, tmp_runtime)
    assert result['status'] == 'failed'
    assert result['reason'] == reason


def test_static_probe_invalid_executable_and_home(tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['executable'] = 'missing-psc-executor'
    result = EXECUTOR.static_probe(config)
    assert result['status'] == 'failed' and result['reason'] == 'executable_not_found'
    config['executor']['executable'] = sys.executable
    config['executor']['executor_home'] = str(tmp_path / 'missing-home')
    assert EXECUTOR.static_probe(config)['reason'] == 'executor_home_not_found'


def test_smoke_fingerprint_invalidates_on_config_change(monkeypatch, tmp_path, tmp_runtime):
    fake_run, _ = _fake_run_factory()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    assert EXECUTOR.smoke_executor(tmp_path, tmp_runtime)['status'] == 'passed'
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['model'] = 'other-model'
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    assert not EXECUTOR.smoke_is_valid(tmp_path, tmp_runtime)


def test_codex_command_places_global_flags_before_exec():
    executor = {'model': 'm', 'sandbox': 'workspace-write', 'approval_policy': 'never', 'provider': 'p', 'effort': 'medium'}
    command = EXECUTOR.build_command('codex', executor, 'prompt')
    assert command.index('--ask-for-approval') < command.index('exec')
    assert command[command.index('--ask-for-approval') + 1] == 'never'


def test_missing_executor_values_are_not_inferred(helper, monkeypatch, tmp_path):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'supervisor'))
    value = {'schema_version': 1, 'runtime_root': str(tmp_path), 'project_naming': 'YYYYMMDD-{requirement}', 'executor': {}}
    assert 'executor.model' in helper.runtime_configuration_requirements(value)


def test_runtime_config_source_requires_provider_model_effort(helper, tmp_path):
    value = {
        'schema_version': 1,
        'runtime_root': str(tmp_path),
        'project_naming': 'YYYYMMDD-{requirement}',
        'executor': {
            'adapter': 'codex', 'executable': 'codex', 'executor_home': str(tmp_path),
            'config_source': 'runtime', 'approval_policy': 'never',
            'sandbox': 'workspace-write', 'timeout': 10,
        },
    }
    missing = helper.runtime_configuration_requirements(value)
    assert {'executor.provider', 'executor.model', 'executor.effort'} <= set(missing)


def test_executor_home_config_source_omits_provider_model_effort(helper, tmp_path):
    value = {
        'schema_version': 1,
        'runtime_root': str(tmp_path),
        'project_naming': 'YYYYMMDD-{requirement}',
        'executor': {
            'adapter': 'codex', 'executable': 'codex', 'executor_home': str(tmp_path),
            'config_source': 'executor_home', 'approval_policy': 'never',
            'sandbox': 'workspace-write', 'timeout': 10,
        },
    }
    missing = helper.runtime_configuration_requirements(value)
    assert not {'executor.provider', 'executor.model', 'executor.effort'} & set(missing)


def test_yyyy_mm_dd_naming_expands(helper):
    name = helper._project_directory_name({'project_naming': 'YYYYMMDD-{requirement}'}, 'example')
    assert name == dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d') + '-example'


def test_activation_refreshes_queue_and_preserves_artifacts(tmp_path, tmp_repo, tmp_runtime):
    first = write_external_bundle(tmp_path, build_bundle_text(version=1), name='v1.md')
    assert run_cli('import-bundle', str(first), '--repository', str(tmp_repo), '--runtime-config', str(tmp_runtime)).returncode == 0
    project = project_dir(tmp_path)
    old_artifact = project / 'developing' / 'artifacts' / 'T-002' / 'coding.md'
    old_artifact.write_text('historical evidence', encoding='utf-8')
    sections = default_sections(version=2)
    sections['tasks.md'] = sections['tasks.md'].replace('## T-002', '## T-003').replace('Task two', 'Task three')
    second = write_external_bundle(tmp_path, build_bundle_text(sections=sections, version=2), name='v2.md')
    assert run_cli('import-bundle', str(second), '--repository', str(tmp_repo), '--runtime-config', str(tmp_runtime)).returncode == 0
    state = json.loads((project / 'runtime' / 'workflow_state.json').read_text(encoding='utf-8'))
    assert state['contract_version'] == 1
    activated = run_cli('activate-contract', '--project', str(project), '--repository', str(tmp_repo))
    assert activated.returncode == 0
    assert sorted(path.name for path in (project / 'developing' / 'tasks').glob('T-*.md')) == ['T-001.md', 'T-003.md']
    assert old_artifact.read_text(encoding='utf-8') == 'historical evidence'
    assert json.loads((project / 'runtime' / 'workflow_state.json').read_text(encoding='utf-8'))['contract_version'] == 2


def test_draft_approval_requires_new_immutable_version(tmp_path, tmp_repo, tmp_runtime):
    draft = write_external_bundle(tmp_path, build_bundle_text(version=1, status='draft'), name='draft.md')
    assert run_cli('import-bundle', str(draft), '--repository', str(tmp_repo), '--runtime-config', str(tmp_runtime)).returncode == 0
    project = project_dir(tmp_path)
    before = hashlib.sha256((project / 'contract' / 'v1' / 'metadata.json').read_bytes()).hexdigest()
    approved = write_external_bundle(tmp_path, build_bundle_text(version=2), name='approved.md')
    assert run_cli('import-bundle', str(approved), '--repository', str(tmp_repo), '--runtime-config', str(tmp_runtime)).returncode == 0
    assert hashlib.sha256((project / 'contract' / 'v1' / 'metadata.json').read_bytes()).hexdigest() == before
    assert json.loads((project / 'contract' / 'v2' / 'metadata.json').read_text(encoding='utf-8'))['status'] == 'approved'


def _structured_completion() -> str:
    return json.dumps({
        'schema_version': 1,
        'plan': 'Inspect the target module and make the scoped change.',
        'coding_summary': 'Updated the scoped module and added its regression test.',
        'modified_files': ['src/example.py', 'tests/test_example.py'],
        'tests': ['python -m pytest tests/test_example.py -q'],
        'known_risks': ['No integration environment was available.'],
        'unresolved_issues': [],
    })


def _fake_dispatch(stdout: str, returncode: int = 0):
    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        return SimpleNamespace(stdout=stdout, stderr='', returncode=returncode)
    return fake_run


def _dispatch_task() -> dict[str, object]:
    return {
        'id': 'T-001',
        'text': 'Implement the scoped change.',
        'Allowed Scope': ['src', 'tests'],
        'Forbidden Scope': ['none'],
    }


def test_structured_completion_materializes_executor_artifacts(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', _fake_dispatch(_structured_completion()))

    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    artifact_dir = project / 'developing' / 'artifacts' / 'T-001'
    assert result['status'] == 'completed'
    assert (artifact_dir / 'plan.md').read_text(encoding='utf-8') == 'Inspect the target module and make the scoped change.\n'
    coding = (artifact_dir / 'coding.md').read_text(encoding='utf-8')
    assert 'Updated the scoped module and added its regression test.' in coding
    assert '- src/example.py' in coding
    assert '- python -m pytest tests/test_example.py -q' in coding
    assert result['artifact_paths'] == {
        'plan': str(artifact_dir / 'plan.md'),
        'coding': str(artifact_dir / 'coding.md'),
    }


def test_invalid_structured_completion_creates_no_task_artifacts(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', _fake_dispatch('not valid structured output'))

    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    assert result['status'] == 'failed'
    assert result['reason'] == 'invalid_executor_output'
    assert result['artifact_paths'] == {}
    assert not (project / 'developing' / 'artifacts' / 'T-001').exists()


def test_failed_executor_process_creates_no_task_artifacts(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', _fake_dispatch(_structured_completion(), returncode=1))

    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    assert result['status'] == 'failed'
    assert result['reason'] == 'process_failed'
    assert result['artifact_paths'] == {}
    assert not (project / 'developing' / 'artifacts' / 'T-001').exists()


def test_auto_review_argv_omits_conflicting_approval_and_sandbox_flags():
    executor = {
        'model': 'm', 'sandbox': 'workspace-write', 'approval_policy': 'on-request',
        'provider': 'p', 'effort': 'medium', 'approvals_reviewer': 'auto_review',
    }
    command = EXECUTOR.build_command('codex', executor, 'prompt')
    assert '--approve-for-me' in command
    assert '--ask-for-approval' not in command
    assert '--sandbox' not in command
    assert command.index('--approve-for-me') < command.index('exec')


def test_auto_review_rejects_non_workspace_write_runtime_config(helper, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor'].update({
        'approval_policy': 'on-request',
        'sandbox': 'danger-full-access',
        'approvals_reviewer': 'auto_review',
    })
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='sandbox=workspace-write'):
        helper.runtime_config(tmp_runtime)


def test_auto_review_requires_cli_support(monkeypatch, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor'].update({
        'approval_policy': 'on-request',
        'sandbox': 'workspace-write',
        'approvals_reviewer': 'auto_review',
    })
    monkeypatch.setattr(EXECUTOR, 'supports_auto_review', lambda executable: False)
    result = EXECUTOR.static_probe(config)
    assert result['status'] == 'failed'
    assert result['reason'] == 'auto_review_unsupported'


def test_default_supervisor_home_requires_explicit_sharing(helper, monkeypatch, tmp_runtime):
    monkeypatch.delenv('CODEX_HOME', raising=False)
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['executor_home'] = str(Path.home() / '.codex')
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='allow_shared_executor_home'):
        helper.runtime_config(tmp_runtime)
    config['executor']['allow_shared_executor_home'] = True
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    assert helper.runtime_config(tmp_runtime)['executor']['allow_shared_executor_home'] is True


def test_non_windows_launcher_preserves_prompt_argv():
    prompt = 'prompt with spaces, quotes " and special chars & unicode 漢字'
    command = ['codex', 'exec', prompt]
    prepared = EXECUTOR.prepare_command(command)
    assert prepared[-1] == prompt
    assert prepared[1:] == [str(EXECUTOR.Path(prepared[1])), 'exec', prompt] if prepared[0].lower().endswith('node.exe') else command[1:]


def test_windows_wrapper_dispatch_is_deterministic(monkeypatch, tmp_path):
    prompt = 'prompt with spaces, quotes " and special chars & unicode 婕㈠瓧'
    wrapper = tmp_path / 'codex.cmd'
    node = tmp_path / 'node.exe'
    script = tmp_path / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    script.parent.mkdir(parents=True)
    script.write_text('// fake codex', encoding='utf-8')
    which = {'codex': str(wrapper), 'node': str(node)}
    monkeypatch.setattr(CODEX_ADAPTER, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(CODEX_ADAPTER.shutil, 'which', lambda name: which.get(name))
    prepared = CODEX_ADAPTER.prepare_command(['codex', 'exec', prompt])
    assert prepared == [str(node), str(script), 'exec', prompt]
    assert len(prepared) == 4


def test_executor_dispatch_preserves_cwd_home_and_sends_codex_prompt_via_stdin(monkeypatch, tmp_path, tmp_runtime):
    observed = {}

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        observed['command'] = command
        observed.update(kwargs)
        return SimpleNamespace(stdout='ok', stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    repository = tmp_path / 'repository'
    repository.mkdir()
    task = {
        'id': 'T-001',
        'text': 'prompt with spaces and & symbols',
        'Allowed Scope': ['none'],
        'Forbidden Scope': ['none'],
    }
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, task, 'contract', None, tmp_runtime,
        require_smoke=False, persist_task_artifacts=False,
    )
    assert result['status'] == 'completed'
    assert observed['cwd'] == str(repository.resolve())
    assert observed['env']['CODEX_HOME'] == json.loads(tmp_runtime.read_text(encoding='utf-8'))['executor']['executor_home']
    assert observed['command'][-1] == '-'
    assert 'prompt with spaces and & symbols' in observed['input']
    assert not any('prompt with spaces and & symbols' in str(item) for item in observed['command'])


def test_executor_home_config_fingerprint_invalidates_on_config_change(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['config_source'] = 'executor_home'
    for field in ('provider', 'model', 'effort'):
        config['executor'].pop(field, None)
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    executor_home = Path(config['executor']['executor_home'])
    (executor_home / 'config.toml').write_text('model = "first"\n', encoding='utf-8')
    fake_run, _ = _fake_run_factory()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    assert EXECUTOR.smoke_executor(tmp_path, tmp_runtime)['status'] == 'passed'
    assert EXECUTOR.smoke_is_valid(tmp_path, tmp_runtime)
    (executor_home / 'config.toml').write_text('model = "second"\n', encoding='utf-8')
    assert not EXECUTOR.smoke_is_valid(tmp_path, tmp_runtime)


def test_executor_home_config_missing_fails_static_probe(tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['config_source'] = 'executor_home'
    for field in ('provider', 'model', 'effort'):
        config['executor'].pop(field, None)
    result = EXECUTOR.static_probe(config)
    assert result['status'] == 'failed'
    assert result['reason'] == 'executor_config_not_readable'


def test_dsh_completion_accepts_prose_before_valid_json(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor'].update({
        'adapter': 'dsh',
        'executable': sys.executable,
        'config_source': 'executor_home',
        'profile': 'headless',
    })
    for field in ('provider', 'model', 'effort'):
        config['executor'].pop(field, None)
    executor_home = Path(config['executor']['executor_home'])
    profiles = executor_home / 'profiles' / 'headless'
    profiles.mkdir(parents=True, exist_ok=True)
    (executor_home / 'settings.yaml').write_text('x: 1\n', encoding='utf-8')
    (profiles / 'package.json').write_text('{}\n', encoding='utf-8')
    (profiles / 'cordis.patch.yml').write_text('{}\n', encoding='utf-8')
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')

    wrapped = 'Implementation finished successfully.\n\n```json\n' + _structured_completion() + '\n```\n'
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    monkeypatch.setattr(EXECUTOR, 'smoke_is_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(EXECUTOR, 'static_probe', lambda *args, **kwargs: {
        'status': 'passed', 'executor_config_sha256': 'x'
    })
    monkeypatch.setattr(EXECUTOR, 'executor_config_fingerprint', lambda *args, **kwargs: 'x')
    monkeypatch.setattr(EXECUTOR, '_prepare_command', lambda adapter, command: command)
    monkeypatch.setattr(EXECUTOR, '_build_command', lambda *args, **kwargs: ['dsh', 'run'])
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', _fake_dispatch(wrapped))

    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'dsh', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )
    assert result['status'] == 'completed'
    assert result['completion']['schema_version'] == 1
    assert (project / 'developing' / 'artifacts' / 'T-001' / 'coding.md').is_file()


def test_codex_completion_remains_strict_about_wrapped_json():
    wrapped = 'done\n```json\n' + _structured_completion() + '\n```'
    value, error = EXECUTOR._parse_completion(wrapped, allow_wrapped_json=False)
    assert value is None
    assert error.startswith('final response is not valid JSON')


def test_dirty_untracked_file_modified_during_executor_is_reported(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    real_run = getattr(subprocess, 'run')
    real_run(['git', '-C', str(repository), 'init'], check=True, capture_output=True)
    target = repository / 'src'
    target.mkdir()
    dirty = target / 'example.py'
    dirty.write_text('before\n', encoding='utf-8')

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return real_run(command, **kwargs)
        dirty.write_text('after\n', encoding='utf-8')
        return SimpleNamespace(stdout=_structured_completion(), stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )
    assert 'src/example.py' in result['changed_paths']


def test_dirty_file_unchanged_during_executor_is_not_reported(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    real_run = getattr(subprocess, 'run')
    real_run(['git', '-C', str(repository), 'init'], check=True, capture_output=True)
    target = repository / 'src'
    target.mkdir()
    dirty = target / 'example.py'
    dirty.write_text('unchanged\n', encoding='utf-8')

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return real_run(command, **kwargs)
        return SimpleNamespace(stdout=_structured_completion(), stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )
    assert 'src/example.py' not in result['changed_paths']


def test_runtime_config_rejects_max_timeout_below_timeout(helper, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['timeout'] = 100
    config['executor']['maxTimeout'] = 99
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='maxTimeout'):
        helper.runtime_config(tmp_runtime)


def test_legacy_runtime_without_max_timeout_keeps_fixed_timeout(helper, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor'].pop('maxTimeout', None)
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    loaded = helper.runtime_config(tmp_runtime)
    assert loaded['executor']['maxTimeout'] == loaded['executor']['timeout']


def test_progressing_timeout_doubles_runtime_timeout(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['timeout'] = 10
    config['executor']['maxTimeout'] = 40
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')

    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    dirty = repository / 'src'
    dirty.mkdir()
    target = dirty / 'example.py'
    target.write_text('before\n', encoding='utf-8')

    real_run = getattr(subprocess, 'run')
    real_run(['git', '-C', str(repository), 'init'], check=True, capture_output=True)

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return real_run(command, **kwargs)
        target.write_text('after\n', encoding='utf-8')
        raise subprocess.TimeoutExpired(command, kwargs['timeout'], output='still working')

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    assert result['reason'] == 'timeout'
    assert result['timeout_adjustment']['status'] == 'adjusted'
    assert result['timeout_adjustment']['old_timeout'] == 10
    assert result['timeout_adjustment']['new_timeout'] == 20
    updated = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    assert updated['executor']['timeout'] == 20


def test_timeout_growth_is_capped_at_max_timeout(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['timeout'] = 30
    config['executor']['maxTimeout'] = 40
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')

    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'], output='progress')

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    assert result['timeout_adjustment']['new_timeout'] == 40
    assert json.loads(tmp_runtime.read_text(encoding='utf-8'))['executor']['timeout'] == 40


def test_timeout_without_output_or_file_changes_still_doubles(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['timeout'] = 10
    config['executor']['maxTimeout'] = 40
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')

    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    assert result['reason'] == 'timeout'
    assert result['timeout_adjustment']['status'] == 'adjusted'
    assert result['timeout_adjustment']['reason'] == 'executor_timed_out'
    assert result['timeout_adjustment']['old_timeout'] == 10
    assert result['timeout_adjustment']['new_timeout'] == 20
    assert json.loads(tmp_runtime.read_text(encoding='utf-8'))['executor']['timeout'] == 20


def test_smoke_timeout_never_changes_normal_timeout(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['timeout'] = 10
    config['executor']['maxTimeout'] = 40
    config['executor']['smoke_timeout'] = 3
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')

    fake_run, _ = _fake_run_factory(timeout=True)
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = EXECUTOR.smoke_executor(tmp_path, tmp_runtime)

    assert result['reason'] == 'timeout'
    assert json.loads(tmp_runtime.read_text(encoding='utf-8'))['executor']['timeout'] == 10


def test_new_workflow_defaults_execution_owner_to_executor(tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(version=1), name='owner-default.md')
    assert run_cli('import-bundle', str(bundle), '--repository', str(tmp_repo), '--runtime-config', str(tmp_runtime)).returncode == 0
    project = project_dir(tmp_path)
    state = json.loads((project / 'runtime' / 'workflow_state.json').read_text(encoding='utf-8'))
    assert state['execution_owner'] == 'executor'
    assert state['execution_owner_history'][0]['owner'] == 'executor'


def test_execution_owner_handoff_is_durable_outside_retry_block(tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(version=1), name='owner-handoff.md')
    assert run_cli('import-bundle', str(bundle), '--repository', str(tmp_repo), '--runtime-config', str(tmp_runtime)).returncode == 0
    project = project_dir(tmp_path)
    state_path = project / 'runtime' / 'workflow_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['status'] = 'ready'
    state['current_task'] = 'T-001'
    state_path.write_text(json.dumps(state), encoding='utf-8')

    take = run_cli(
        'set-execution-owner', '--project', str(project),
        '--owner', 'supervisor', '--reason', 'user requested S takeover'
    )
    assert take.returncode == 0, take.stdout + take.stderr
    value = json.loads(take.stdout)
    assert value['execution_owner'] == 'supervisor'
    assert value['workflow_status'] == 'ready'

    persisted = json.loads(state_path.read_text(encoding='utf-8'))
    assert persisted['execution_owner'] == 'supervisor'
    assert persisted['execution_owner_history'][-1]['previous_owner'] == 'executor'
    assert persisted['execution_owner_history'][-1]['task'] == 'T-001'

    give_back = run_cli(
        'set-execution-owner', '--project', str(project),
        '--owner', 'executor', '--reason', 'user requested E from next task'
    )
    assert give_back.returncode == 0, give_back.stdout + give_back.stderr
    persisted = json.loads(state_path.read_text(encoding='utf-8'))
    assert persisted['execution_owner'] == 'executor'
    assert persisted['execution_owner_history'][-1]['previous_owner'] == 'supervisor'


def test_execution_owner_handoff_rejected_while_running(helper, tmp_path):
    project = tmp_path / 'project'
    runtime = project / 'runtime'
    runtime.mkdir(parents=True)
    (runtime / 'workflow_state.json').write_text(json.dumps({
        'schema_version': 1,
        'contract_version': 1,
        'current_task': 'T-001',
        'status': 'executor_running',
        'attempt': 1,
        'last_completed_task': None,
        'last_stage': 'executor',
        'updated_at': '2026-08-28T00:00:00+00:00',
    }), encoding='utf-8')
    with pytest.raises(ValueError, match='while a task execution is running'):
        helper.set_execution_owner(project, 'supervisor', 'take over')


def _write_retry_exhaustion_fixture(project, *, task='T-001', version=5, budget='abnormal_retry'):
    runtime = project / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    state_path = runtime / 'workflow_state.json'
    state_path.write_text(json.dumps({
        'schema_version': 1,
        'contract_version': version,
        'current_task': task,
        'status': 'blocked',
        'attempt': 0,
        'last_completed_task': None,
        'last_stage': 'executor_retry_budget_exhausted',
        'execution_owner': 'executor',
        'execution_owner_reason': 'default',
        'execution_owner_updated_at': '2026-08-28T00:00:00+00:00',
        'execution_owner_history': [],
        'retry_exhaustion': {
            'contract_version': version,
            'task': task,
            'budget': budget,
            'used': 3,
            'limit': 3,
            'reason': (
                'executor_abnormal_retry_limit_reached'
                if budget == 'abnormal_retry'
                else 'quality_rework_limit_reached'
            ),
            'decision_required': [
                'reset-and-continue-executor',
                'switch-to-supervisor',
            ],
        },
        'updated_at': '2026-08-28T00:00:00+00:00',
    }), encoding='utf-8')
    return state_path


def test_generic_owner_handoff_cannot_bypass_retry_exhaustion(helper, tmp_path):
    project = tmp_path / 'project'
    _write_retry_exhaustion_fixture(project)
    with pytest.raises(ValueError, match='resolve-retry-exhaustion'):
        helper.set_execution_owner(project, 'supervisor', 'bypass')


def test_reset_retry_exhaustion_only_resets_blocked_task_budget(helper, tmp_path):
    project = tmp_path / 'project'
    state_path = _write_retry_exhaustion_fixture(
        project, task='T-001', version=5, budget='abnormal_retry'
    )
    attempts_path = project / 'runtime' / 'executor_attempts.json'
    attempts_path.write_text(json.dumps({
        'schema_version': 2,
        'tasks': {
            'v5:T-001': {
                'execution_round': 1,
                'initial_attempted': True,
                'quality_retries_used': 2,
                'abnormal_retries_used': 3,
            },
            'v5:T-002': {
                'execution_round': 1,
                'initial_attempted': True,
                'quality_retries_used': 1,
                'abnormal_retries_used': 2,
            },
        },
        'legacy_unclassified_attempts': {},
    }), encoding='utf-8')

    result = helper.resolve_retry_exhaustion(
        project, 'reset-and-continue-executor'
    )
    assert result['task'] == 'T-001'
    assert result['reset_budget'] == 'both'
    assert result['reset_budgets'] == ['quality_rework', 'abnormal_retry']
    assert result['execution_round'] == 2
    assert result['execution_owner'] == 'executor'

    attempts = json.loads(attempts_path.read_text(encoding='utf-8'))
    assert attempts['tasks']['v5:T-001'] == {
        'execution_round': 2,
        'initial_attempted': False,
        'quality_retries_used': 0,
        'abnormal_retries_used': 0,
    }
    assert attempts['tasks']['v5:T-002'] == {
        'execution_round': 1,
        'initial_attempted': True,
        'quality_retries_used': 1,
        'abnormal_retries_used': 2,
    }

    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['status'] == 'ready'
    assert state['current_task'] == 'T-001'
    assert state['execution_owner'] == 'executor'
    assert 'retry_exhaustion' not in state
    assert state['retry_exhaustion_history'][-1]['decision'] == 'reset-and-continue-executor'
    assert state['retry_exhaustion_history'][-1]['reset_budgets'] == ['quality_rework', 'abnormal_retry']
    assert state['retry_exhaustion_history'][-1]['new_execution_round'] == 2


def test_switch_to_supervisor_preserves_all_retry_budgets(helper, tmp_path):
    project = tmp_path / 'project'
    state_path = _write_retry_exhaustion_fixture(
        project, task='T-003', version=7, budget='quality_rework'
    )
    attempts_path = project / 'runtime' / 'executor_attempts.json'
    original = {
        'schema_version': 2,
        'tasks': {
            'v7:T-003': {
                'execution_round': 1,
                'initial_attempted': True,
                'quality_retries_used': 3,
                'abnormal_retries_used': 1,
            }
        },
        'legacy_unclassified_attempts': {},
    }
    attempts_path.write_text(json.dumps(original), encoding='utf-8')

    result = helper.resolve_retry_exhaustion(project, 'switch-to-supervisor')
    assert result['reset_budget'] is None
    assert result['execution_owner'] == 'supervisor'
    assert json.loads(attempts_path.read_text(encoding='utf-8')) == original

    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['status'] == 'ready'
    assert state['current_task'] == 'T-003'
    assert state['execution_owner'] == 'supervisor'
    assert 'retry_exhaustion' not in state


def test_runtime_config_records_mcp_python_interpreter(helper, tmp_runtime):
    config = helper.runtime_config(tmp_runtime)
    assert config['mcp']['python_interpreter']


def test_runtime_config_rejects_invalid_mcp_python_interpreter(helper, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['mcp']['python_interpreter'] = '   '
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='mcp.python_interpreter'):
        helper.runtime_config(tmp_runtime)


def test_legacy_runtime_without_mcp_block_remains_valid(helper, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config.pop('mcp', None)
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    loaded = helper.runtime_config(tmp_runtime)
    assert 'mcp' not in loaded


def test_runtime_configuration_requirements_rejects_non_object_mcp(helper, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['mcp'] = 'not-an-object'
    missing = helper.runtime_configuration_requirements(config)
    assert 'mcp must be an object' in missing


def test_large_codex_prompt_never_enters_argv(monkeypatch, tmp_path, tmp_runtime):
    observed = {}
    huge = 'LONG-PROMPT-' + ('x' * 50000)

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        observed['command'] = list(command)
        observed['input'] = kwargs.get('input')
        return SimpleNamespace(stdout='ok', stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    repository = tmp_path / 'repository'
    repository.mkdir()
    task = {
        'id': 'T-101',
        'text': huge,
        'Allowed Scope': ['none'],
        'Forbidden Scope': ['none'],
    }
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, task, 'contract', None, tmp_runtime,
        require_smoke=False, persist_task_artifacts=False,
    )

    assert result['status'] == 'completed'
    assert huge in observed['input']
    assert observed['command'][-1] == '-'
    assert len(' '.join(map(str, observed['command']))) < 4096
    assert huge not in ' '.join(map(str, observed['command']))


def test_dsh_prompt_transport_uses_short_runtime_owned_file(tmp_path):
    repository = tmp_path / 'repository'
    repository.mkdir()
    huge = 'DSH-LONG-' + ('y' * 50000)

    argument, stdin_prompt, path = EXECUTOR._prepare_prompt_transport(
        'dsh', repository, huge
    )
    try:
        assert stdin_prompt is None
        assert path is not None and path.is_file()
        assert path.read_text(encoding='utf-8') == huge
        assert len(argument) < 512
        assert huge not in argument
        assert '.agentic-sdlc/runtime/executor-inputs/' in argument
    finally:
        EXECUTOR._cleanup_prompt_transport(path)
    assert not path.exists()


def test_windows_command_too_long_is_nonretryable_launch_transport_failure(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    repository.mkdir()

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        raise OSError(errno.ENAMETOOLONG, 'command line too long')

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    task = {
        'id': 'T-102',
        'text': 'task',
        'Allowed Scope': ['none'],
        'Forbidden Scope': ['none'],
    }
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, task, 'contract', None, tmp_runtime,
        require_smoke=False, persist_task_artifacts=False,
    )

    assert result['status'] == 'failed'
    assert result['reason'] == 'launch_transport_failed'
    assert result['retryable'] is False


def test_resolve_runtime_failure_preserves_retry_state(helper, tmp_path):
    project = tmp_path / 'project'
    runtime = project / 'runtime'
    runtime.mkdir(parents=True)
    state_path = runtime / 'workflow_state.json'
    state_path.write_text(json.dumps({
        'schema_version': 1,
        'contract_version': 2,
        'current_task': 'T-002',
        'status': 'blocked',
        'attempt': 0,
        'last_completed_task': 'T-001',
        'last_stage': 'executor_nonretryable_runtime_failure',
        'execution_owner': 'executor',
        'runtime_failure': {
            'contract_version': 2,
            'task': 'T-002',
            'reason': 'launch_transport_failed',
            'retryable': False,
            'errors': ['WinError 206'],
            'resolution': 'repair_runtime_then_continue_same_task',
        },
        'updated_at': '2026-09-01T00:00:00+00:00',
    }), encoding='utf-8')
    attempts_path = runtime / 'executor_attempts.json'
    retry_state = {
        'schema_version': 2,
        'tasks': {
            'v2:T-002': {
                'execution_round': 1,
                'initial_attempted': False,
                'quality_retries_used': 1,
                'abnormal_retries_used': 2,
            }
        },
        'legacy_unclassified_attempts': {},
    }
    attempts_path.write_text(json.dumps(retry_state), encoding='utf-8')

    result = helper.resolve_runtime_failure(
        project, 'updated executor prompt transport'
    )
    assert result['status'] == 'runtime_failure_resolved'
    assert result['retry_counters_changed'] is False
    assert result['execution_round_changed'] is False
    assert json.loads(attempts_path.read_text(encoding='utf-8')) == retry_state
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['status'] == 'ready'
    assert 'runtime_failure' not in state


def test_codex_jsonl_completion_is_materialized_with_usage(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    stdout = '\n'.join([
        json.dumps({
            'type': 'item.completed',
            'item': {
                'id': 'item-1',
                'type': 'agent_message',
                'text': _structured_completion(),
            },
        }),
        json.dumps({
            'type': 'turn.completed',
            'usage': {
                'input_tokens': 1000,
                'cached_input_tokens': 900,
                'cache_write_input_tokens': 0,
                'output_tokens': 100,
                'reasoning_output_tokens': 25,
            },
        }),
    ]) + '\n'
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', _fake_dispatch(stdout))

    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None,
        tmp_runtime, project=project, require_smoke=False,
    )

    assert result['status'] == 'completed'
    assert result['completion']['schema_version'] == 1
    assert result['token_usage']['exact'] is True
    assert result['token_usage']['input_tokens'] == 1000
    assert result['token_usage']['cached_input_tokens'] == 900
    assert result['token_usage']['total_tokens'] == 1100


def test_dsh_invocation_disables_unmetered_session_title_llm(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor'].update({
        'adapter': 'dsh',
        'executable': sys.executable,
        'config_source': 'executor_home',
        'profile': 'headless',
    })
    for field in ('provider', 'model', 'effort'):
        config['executor'].pop(field, None)
    executor_home = Path(config['executor']['executor_home'])
    profiles = executor_home / 'profiles' / 'headless'
    profiles.mkdir(parents=True, exist_ok=True)
    (executor_home / 'settings.yaml').write_text('x: 1\n', encoding='utf-8')
    (profiles / 'package.json').write_text('{}\n', encoding='utf-8')
    (profiles / 'cordis.patch.yml').write_text('{}\n', encoding='utf-8')
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')

    repository = tmp_path / 'repository'
    repository.mkdir()
    observed = {}

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        observed['command'] = list(command)
        patch_index = command.index('--patch') + 1
        patch_path = Path(command[patch_index])
        observed['patch_text'] = patch_path.read_text(encoding='utf-8')
        return SimpleNamespace(stdout='done', stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR, 'static_probe', lambda *args, **kwargs: {
        'status': 'passed', 'executor_config_sha256': 'x'
    })
    monkeypatch.setattr(EXECUTOR, 'executor_config_fingerprint', lambda *args, **kwargs: 'x')
    monkeypatch.setattr(EXECUTOR, '_prepare_command', lambda adapter, command: command)
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)

    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'dsh', repository,
        {'id': 'T-901', 'text': 'test', 'Allowed Scope': ['none'], 'Forbidden Scope': ['none']},
        'contract', None, tmp_runtime,
        require_smoke=False, persist_task_artifacts=False,
    )

    assert result['status'] == 'completed'
    assert '--patch' in observed['command']
    assert 'id: session-title-llm' in observed['patch_text']
    assert 'disabled: true' in observed['patch_text']
