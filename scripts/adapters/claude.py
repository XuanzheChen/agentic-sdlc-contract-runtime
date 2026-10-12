"""Claude Code headless Executor. Never reads or mutates the CCSwitch/global home."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def prepare_command(command: list[str]) -> list[str]:
    """Resolve Windows npm .cmd shims to node directly, without shell=True."""
    if not command or os.name != "nt":
        return list(command)
    resolved = shutil.which(str(command[0])) or str(command[0])
    if Path(resolved).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return list(command)
    script = Path(resolved).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = shutil.which("node")
    if node and script.is_file():
        return [node, str(script), *command[1:]]
    raise OSError(f"unsupported Windows Claude Code wrapper: {resolved}")


def _route(command: list[str], executor: dict[str, Any]) -> None:
    route = executor.get("routing")
    if not isinstance(route, dict):
        raise ValueError("Claude Code requires explicit executor.routing.model and effort")
    command.extend(["--model", str(route["model"]), "--effort", str(route["effort"])])


def build_command(
    executable: str, executor: dict[str, Any], prompt: str, *,
    output_schema: Path | None = None,
) -> list[str]:
    """Prompt travels on stdin; PSC completion schema is a CLI output constraint."""
    if prompt != "-":
        raise ValueError("Claude Executor requires stdin prompt transport")
    sandbox = executor["sandbox"]
    if sandbox == "read-only":
        mode, tools = "dontAsk", "Read,Glob,Grep"
    elif sandbox == "workspace-write":
        # acceptEdits is NOT an OS sandbox. static_probe requires sandbox.enabled.
        mode, tools = "acceptEdits", "default"
    elif sandbox == "danger-full-access":
        mode, tools = "bypassPermissions", "default"
    else:
        raise ValueError(f"unsupported Claude sandbox mapping: {sandbox}")
    command = [
        executable, "-p", "--verbose", "--output-format", "stream-json",
        "--no-session-persistence", "--setting-sources", "user",
        "--permission-mode", mode, "--tools", tools,
    ]
    _route(command, executor)
    if output_schema is not None:
        import json
        command.extend(["--json-schema", output_schema.read_text(encoding="utf-8")])
    return command


def build_preflight_command(
    executable: str, executor: dict[str, Any], prompt: str, *,
    output_schema: Path | None = None,
) -> list[str]:
    if prompt != "-":
        raise ValueError("Claude Preflight requires stdin prompt transport")
    # No built-in or MCP tools, project settings, custom hooks, skills or
    # interactive approvals. Evidence arrives solely over stdin.
    command = [
        executable, "-p", "--verbose", "--output-format", "stream-json",
        "--no-session-persistence", "--setting-sources", "user",
        "--safe-mode", "--disable-slash-commands", "--strict-mcp-config",
        "--tools", "", "--disallowedTools", "*", "--permission-mode", "dontAsk",
    ]
    _route(command, executor)
    if output_schema is not None:
        import json
        command.extend(["--json-schema", output_schema.read_text(encoding="utf-8")])
    return command
