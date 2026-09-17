from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected one match, found {count}')
    p.write_text(text.replace(old, new, 1), encoding='utf-8', newline='\n')


# invocation layer: isolate Supervisor auth/session state from Executor.
path = 'scripts/invoke_executor.py'
replace_once(
    path,
    "PSC_SMOKE_WORKSPACE_RE = re.compile(r'psc-executor-smoke-[0-9a-f]{32}')\n",
    "PSC_SMOKE_WORKSPACE_RE = re.compile(r'psc-executor-smoke-[0-9a-f]{32}')\n"
    "SUPERVISOR_ENV_DENYLIST = frozenset({\n"
    "    'OPENAI_API_KEY',\n"
    "    'CODEX_API_KEY',\n"
    "    'CODEX_CI',\n"
    "    'CODEX_SESSION_ID',\n"
    "    'CODEX_THREAD_ID',\n"
    "})\n",
    'env denylist',
)
replace_once(
    path,
    "def _spawn_failure_reason(exc: OSError) -> str:\n",
    "def _executor_child_env(adapter: str, executor: dict[str, Any]) -> dict[str, str]:\n"
    "    \"\"\"Build an Executor child environment without Supervisor auth/session state.\n\n"
    "    Preserve ordinary OS/process infrastructure (PATH, temp dirs, proxies, etc.)\n"
    "    but never allow Supervisor OpenAI/Codex credentials or Codex session identity\n"
    "    to override the independent Executor home. Codex must authenticate from its\n"
    "    configured Executor home (for example auth.json), not from the Supervisor.\n"
    "    \"\"\"\n"
    "    child_env = os.environ.copy()\n"
    "    for name in SUPERVISOR_ENV_DENYLIST:\n"
    "        child_env.pop(name, None)\n"
    "    home = str(Path(str(executor['executor_home'])).expanduser().resolve())\n"
    "    if adapter == 'codex':\n"
    "        child_env['CODEX_HOME'] = home\n"
    "    elif adapter == 'dsh':\n"
    "        child_env['DSH_HOME'] = home\n"
    "    else:\n"
    "        raise ValueError(f'unsupported adapter: {adapter}')\n"
    "    return child_env\n\n\n"
    "def _spawn_failure_reason(exc: OSError) -> str:\n",
    'child env helper',
)
replace_once(
    path,
    "    child_env = os.environ.copy()\n"
    "    if adapter == 'codex':\n"
    "        child_env['CODEX_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())\n"
    "    else:\n"
    "        child_env['DSH_HOME'] = str(Path(str(executor['executor_home'])).expanduser().resolve())\n",
    "    child_env = _executor_child_env(adapter, executor)\n",
    'invoke child env',
)

# regression tests: smoke and normal invocation share the sanitized child env.
path = 'tests/test_runtime_hardening.py'
p = Path(path)
text = p.read_text(encoding='utf-8')
marker = 'def test_executor_child_env_drops_supervisor_credentials_and_codex_session_state'
if marker not in text:
    insertion = r'''


def test_executor_child_env_drops_supervisor_credentials_and_codex_session_state(monkeypatch, tmp_path, tmp_runtime):
    config = json.loads(tmp_runtime.read_text(encoding='utf-8'))
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-supervisor-secret')
    monkeypatch.setenv('CODEX_API_KEY', 'codex-supervisor-secret')
    monkeypatch.setenv('CODEX_CI', '1')
    monkeypatch.setenv('CODEX_SESSION_ID', 'supervisor-session')
    monkeypatch.setenv('CODEX_THREAD_ID', 'supervisor-thread')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'supervisor-home'))
    monkeypatch.setenv('PSC_SAFE_SENTINEL', 'preserve-me')

    env = EXECUTOR._executor_child_env('codex', config['executor'])

    for name in EXECUTOR.SUPERVISOR_ENV_DENYLIST:
        assert name not in env
    assert env['CODEX_HOME'] == str(Path(config['executor']['executor_home']).resolve())
    assert env['PSC_SAFE_SENTINEL'] == 'preserve-me'
    assert os.environ['OPENAI_API_KEY'] == 'sk-supervisor-secret'
    assert os.environ['CODEX_SESSION_ID'] == 'supervisor-session'


def test_smoke_uses_sanitized_executor_environment(monkeypatch, tmp_path, tmp_runtime):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-supervisor-secret')
    monkeypatch.setenv('CODEX_CI', '1')
    monkeypatch.setenv('CODEX_SESSION_ID', 'supervisor-session')
    monkeypatch.setenv('CODEX_THREAD_ID', 'supervisor-thread')
    fake_run, observed = _fake_run_factory()
    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)

    result = EXECUTOR.smoke_executor(tmp_path, tmp_runtime)

    assert result['status'] == 'passed'
    assert 'OPENAI_API_KEY' not in observed['env']
    assert 'CODEX_CI' not in observed['env']
    assert 'CODEX_SESSION_ID' not in observed['env']
    assert 'CODEX_THREAD_ID' not in observed['env']


def test_normal_executor_uses_sanitized_executor_environment(monkeypatch, tmp_path, tmp_runtime):
    repository = tmp_path / 'repository-env-isolation'
    project = tmp_path / 'runtime-project-env-isolation'
    repository.mkdir()
    project.mkdir()
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-supervisor-secret')
    monkeypatch.setenv('CODEX_CI', '1')
    monkeypatch.setenv('CODEX_SESSION_ID', 'supervisor-session')
    monkeypatch.setenv('CODEX_THREAD_ID', 'supervisor-thread')
    observed = {}

    def fake_run(command, **kwargs):
        if command[0] == 'git':
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        observed['env'] = kwargs['env']
        return SimpleNamespace(stdout=_structured_completion(), stderr='', returncode=0)

    monkeypatch.setattr(EXECUTOR.subprocess, 'run', fake_run)
    result = getattr(EXECUTOR, 'invoke_' + 'executor')(
        'codex', repository, _dispatch_task(), 'contract excerpt', None, tmp_runtime,
        project=project, require_smoke=False,
    )

    assert result['status'] == 'completed'
    for name in ('OPENAI_API_KEY', 'CODEX_CI', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID'):
        assert name not in observed['env']
'''
    p.write_text(text.rstrip() + insertion + '\n', encoding='utf-8', newline='\n')

# Skill contract and runtime protocol: make credential/session isolation explicit.
replace_once(
    'SKILL.md',
    "Never copy or expose credentials. `runtime.json` contains configuration only;\nauthentication remains in the selected Executor environment.\n",
    "Never copy or expose credentials. `runtime.json` contains configuration only;\n"
    "authentication remains in the selected Executor environment. The invocation\n"
    "layer may inherit ordinary OS environment needed to launch a process, but it\n"
    "must strip Supervisor authentication/session overrides before every smoke or\n"
    "normal Executor launch. At minimum remove `OPENAI_API_KEY`, `CODEX_API_KEY`,\n"
    "`CODEX_CI`, `CODEX_SESSION_ID`, and `CODEX_THREAD_ID`, then set the configured\n"
    "Executor `CODEX_HOME`/`DSH_HOME`. A Codex Executor must therefore authenticate\n"
    "from its independent Executor home (for example `auth.json`) rather than a\n"
    "Supervisor process credential.\n",
    'skill env isolation docs',
)

path = 'references/runtime-protocol.md'
p = Path(path)
text = p.read_text(encoding='utf-8')
anchor = '## Executor health\n\n'
if '## Executor process environment isolation' not in text:
    block = '''## Executor process environment isolation\n\nSupervisor and Executor process identity are separate even when the operating\nsystem child process starts from a copy of the parent environment. Before every\nsmoke or normal Executor launch, preserve ordinary launch infrastructure such as\n`PATH`, temp-directory variables, and proxies, but remove Supervisor credential\nand Codex-session overrides: at minimum `OPENAI_API_KEY`, `CODEX_API_KEY`,\n`CODEX_CI`, `CODEX_SESSION_ID`, and `CODEX_THREAD_ID`. Then set only the\nconfigured independent `CODEX_HOME` or `DSH_HOME` for the selected adapter.\n\nFor Codex this guarantees that credentials under the configured Executor home\n(for example `auth.json`) are authoritative and cannot be silently shadowed by a\nSupervisor `OPENAI_API_KEY`. Sanitization applies identically to semantic smoke\nand real task invocation because both use the same invocation boundary.\n\n'''
    if anchor not in text:
        raise SystemExit('runtime protocol anchor missing')
    p.write_text(text.replace(anchor, block + anchor, 1), encoding='utf-8', newline='\n')

print('executor environment isolation patch applied')
