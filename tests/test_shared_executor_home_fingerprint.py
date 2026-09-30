from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from conftest import SKILL_ROOT


sys.path.insert(0, str(SKILL_ROOT / 'scripts'))
_SPEC = importlib.util.spec_from_file_location(
    'psc_executor_runtime_shared_home',
    SKILL_ROOT / 'scripts' / 'invoke_executor.py',
)
assert _SPEC and _SPEC.loader
EXECUTOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(EXECUTOR)


def _executor_home_config(tmp_runtime: Path) -> tuple[dict, Path]:
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    config['executor']['config_source'] = 'executor_home'
    for field in ('provider', 'model', 'effort'):
        config['executor'].pop(field, None)
    tmp_runtime.write_text(json.dumps(config), encoding='utf-8')
    home = Path(config['executor']['executor_home'])
    return config, home / 'config.toml'


def _project_table(path: Path, trust: str) -> str:
    escaped = str(path).replace('\\', '\\\\')
    return f'[projects."{escaped}"]\ntrust_level = "{trust}"\n'


def test_unrelated_repository_trust_does_not_invalidate_shared_home(tmp_path, tmp_runtime):
    config, config_path = _executor_home_config(tmp_runtime)
    repo_a = tmp_path / 'repo-a'
    repo_b = tmp_path / 'repo-b'
    repo_a.mkdir()
    repo_b.mkdir()

    config_path.write_text(
        'model = "stable"\n' + _project_table(repo_a, 'trusted'),
        encoding='utf-8',
    )
    before_a = EXECUTOR.executor_config_fingerprint(config, repo_a)

    config_path.write_text(
        'model = "stable"\n'
        + _project_table(repo_a, 'trusted')
        + _project_table(repo_b, 'trusted'),
        encoding='utf-8',
    )
    after_a = EXECUTOR.executor_config_fingerprint(config, repo_a)

    assert after_a == before_a


def test_other_repository_trust_changes_do_not_invalidate_current_repository(tmp_path, tmp_runtime):
    config, config_path = _executor_home_config(tmp_runtime)
    repo_a = tmp_path / 'repo-a'
    repo_b = tmp_path / 'repo-b'
    repo_a.mkdir()
    repo_b.mkdir()

    config_path.write_text(
        'model = "stable"\n'
        + _project_table(repo_a, 'trusted')
        + _project_table(repo_b, 'trusted'),
        encoding='utf-8',
    )
    trusted_b = EXECUTOR.executor_config_fingerprint(config, repo_a)

    config_path.write_text(
        'model = "stable"\n'
        + _project_table(repo_a, 'trusted')
        + _project_table(repo_b, 'untrusted'),
        encoding='utf-8',
    )
    untrusted_b = EXECUTOR.executor_config_fingerprint(config, repo_a)

    assert untrusted_b == trusted_b


def test_current_repository_trust_change_still_invalidates(tmp_path, tmp_runtime):
    config, config_path = _executor_home_config(tmp_runtime)
    repo_a = tmp_path / 'repo-a'
    repo_b = tmp_path / 'repo-b'
    repo_a.mkdir()
    repo_b.mkdir()

    config_path.write_text(
        'model = "stable"\n'
        + _project_table(repo_a, 'trusted')
        + _project_table(repo_b, 'trusted'),
        encoding='utf-8',
    )
    trusted_a = EXECUTOR.executor_config_fingerprint(config, repo_a)

    config_path.write_text(
        'model = "stable"\n'
        + _project_table(repo_a, 'untrusted')
        + _project_table(repo_b, 'trusted'),
        encoding='utf-8',
    )
    untrusted_a = EXECUTOR.executor_config_fingerprint(config, repo_a)

    assert untrusted_a != trusted_a


def test_global_executor_config_change_still_invalidates_for_all_repositories(tmp_path, tmp_runtime):
    config, config_path = _executor_home_config(tmp_runtime)
    repo_a = tmp_path / 'repo-a'
    repo_b = tmp_path / 'repo-b'
    repo_a.mkdir()
    repo_b.mkdir()

    config_path.write_text(
        'model = "first"\n'
        + _project_table(repo_a, 'trusted')
        + _project_table(repo_b, 'trusted'),
        encoding='utf-8',
    )
    before_a = EXECUTOR.executor_config_fingerprint(config, repo_a)
    before_b = EXECUTOR.executor_config_fingerprint(config, repo_b)

    config_path.write_text(
        'model = "second"\n'
        + _project_table(repo_a, 'trusted')
        + _project_table(repo_b, 'trusted'),
        encoding='utf-8',
    )

    assert EXECUTOR.executor_config_fingerprint(config, repo_a) != before_a
    assert EXECUTOR.executor_config_fingerprint(config, repo_b) != before_b
