from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def prepare_command(command: list[str]) -> list[str]:
    """Resolve Windows npm wrappers without passing argv through a shell."""
    if not command or os.name != 'nt':
        return list(command)
    resolved = shutil.which(str(command[0])) or str(command[0])
    suffix = Path(resolved).suffix.lower()
    if suffix not in {'.cmd', '.bat', '.ps1'}:
        return list(command)
    wrapper_dir = Path(resolved).parent
    node = shutil.which('node')
    script = wrapper_dir / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    if node and script.is_file():
        return [node, str(script), *command[1:]]
    raise OSError(f'unsupported Windows Codex wrapper: {resolved}')

def supports_auto_review(executable: str) -> bool:
    """Return whether this Codex CLI exposes the auto-review global flag."""
    try:
        completed = subprocess.run(
            prepare_command([executable, '--help']),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and '--approve-for-me' in completed.stdout


def build_command(
    executable: str,
    executor: dict[str, Any],
    prompt: str,
    *,
    output_schema: Path | None = None,
) -> list[str]:
    auto_review = executor.get('approvals_reviewer') == 'auto_review'
    if auto_review and (
        executor.get('approval_policy') != 'on-request'
        or executor.get('sandbox') != 'workspace-write'
    ):
        raise ValueError(
            'approvals_reviewer=auto_review requires approval_policy=on-request '
            'and sandbox=workspace-write'
        )
    command = [executable]
    if executor.get('config_source', 'runtime') == 'runtime':
        command.extend([
            '--model', str(executor['model']),
            '--config', 'model_provider=' + _toml_string(executor['provider']),
            '--config', 'model_reasoning_effort=' + _toml_string(executor['effort']),
        ])
    if auto_review:
        command.append('--approve-for-me')
    else:
        command.extend([
            '--sandbox', str(executor['sandbox']),
            '--ask-for-approval', str(executor['approval_policy']),
        ])
    command.extend(['exec', '--json', '--ephemeral', '--color', 'never', '--skip-git-repo-check', prompt])
    if output_schema is not None:
        command[-1:-1] = ['--output-schema', str(output_schema)]
    return command
