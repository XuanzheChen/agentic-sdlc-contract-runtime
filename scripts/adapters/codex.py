from __future__ import annotations

import json
from typing import Any


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def build_command(executable: str, executor: dict[str, Any], prompt: str) -> list[str]:
    command = [
        executable,
        '--model', str(executor['model']),
        '--sandbox', str(executor['sandbox']),
        '--ask-for-approval', str(executor['approval_policy']),
        '--config', 'model_provider=' + _toml_string(executor['provider']),
        '--config', 'model_reasoning_effort=' + _toml_string(executor['effort']),
    ]
    if executor.get('approvals_reviewer') == 'auto_review':
        command.append('--approve-for-me')
    command.extend(['exec', '--ephemeral', '--color', 'never', '--skip-git-repo-check', prompt])
    return command
