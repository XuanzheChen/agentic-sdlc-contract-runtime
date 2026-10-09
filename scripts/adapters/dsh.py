from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def prepare_command(command: list[str]) -> list[str]:
    """Resolve the Windows npm wrapper for the DeepSeek Harness without a shell."""
    if not command or os.name != 'nt':
        return list(command)
    resolved = shutil.which(str(command[0])) or str(command[0])
    suffix = Path(resolved).suffix.lower()
    if suffix not in {'.cmd', '.bat'}:
        return list(command)
    wrapper_dir = Path(resolved).parent
    node = shutil.which('node')
    script = wrapper_dir / 'node_modules' / '@deepseek-ai' / 'dsh' / 'lib' / 'bin.js'
    if node and script.is_file():
        return [node, str(script), *command[1:]]
    raise OSError(f'unsupported Windows DSH wrapper: {resolved}')


def build_command(executable: str, executor: dict[str, Any], prompt: str) -> list[str]:
    profile = str(executor.get('profile', '')).strip()
    if not profile:
        raise ValueError('DSH executor requires executor.profile')
    return [executable, '--profile', profile, '--json', prompt]


def build_preflight_command(
    executable: str,
    executor: dict[str, Any],
    prompt: str,
    *,
    patch_path: Path | None,
) -> list[str]:
    """Build the restricted Preflight Checker command on the Executor route.

    The Checker uses the same executable, profile, provider, model, and reasoning
    effort as the Executor. Its tool restrictions are supplied through a
    command-line ``--patch`` overlay that the launcher applies after the profile
    layer, so the runtime-owned restriction patch wins. The patch flag is a
    launcher flag and therefore precedes the first app-owned flag (``--json``),
    matching the existing Executor launch ordering.
    """
    profile = str(executor.get('profile', '')).strip()
    if not profile:
        raise ValueError('DSH executor requires executor.profile')
    if patch_path is None:
        raise ValueError('DSH preflight requires a restriction patch')
    return [executable, '--profile', profile, '--patch', str(patch_path), '--json', prompt]
