from __future__ import annotations

import datetime as dt
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


def test_executor_dispatch_preserves_cwd_and_child_home(monkeypatch, tmp_path, tmp_runtime):
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
    task = {'id': 'T-001', 'text': 'prompt with spaces and & symbols', 'Allowed Scope': ['none'], 'Forbidden Scope': ['none']}
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, task, 'contract', None, tmp_runtime,
        require_smoke=False, persist_task_artifacts=False,
    )
    assert result['status'] == 'completed'
    assert observed['cwd'] == str(repository.resolve())
    assert observed['env']['CODEX_HOME'] == json.loads(tmp_runtime.read_text(encoding='utf-8'))['executor']['executor_home']
    assert any('prompt with spaces and & symbols' in str(item) for item in observed['command'])


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

    result = EXECUTOR.invoke_executor(
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
    subprocess.run(['git', '-C', str(repository), 'init'], check=True, capture_output=True)
    target = repository / 'src'
    target.mkdir()
    dirty = target / 'example.py'
    dirty.write_text('before\n', encoding='utf-8')

    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return real_run(command, **kwargs)
        dirty.write_text('after\n', encoding='utf-8')
        return SimpleNamespace(stdout=_structured_completion(), stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = EXECUTOR.invoke_executor(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )
    assert 'src/example.py' in result['changed_paths']


def test_dirty_file_unchanged_during_executor_is_not_reported(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository'
    project = tmp_path / 'runtime-project'
    repository.mkdir()
    project.mkdir()
    subprocess.run(['git', '-C', str(repository), 'init'], check=True, capture_output=True)
    target = repository / 'src'
    target.mkdir()
    dirty = target / 'example.py'
    dirty.write_text('unchanged\n', encoding='utf-8')

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', _fake_dispatch(_structured_completion()))
    result = EXECUTOR.invoke_executor(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )
    assert 'src/example.py' not in result['changed_paths']
