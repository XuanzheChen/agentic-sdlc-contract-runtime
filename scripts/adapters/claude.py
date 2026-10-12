"""Claude Code headless Executor. Never reads or mutates the CCSwitch/global home."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


def native_windows() -> bool:
    """Native Windows only (not WSL2, which reports a POSIX OS)."""
    return os.name == "nt"


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
    windows_mode = None
    if sandbox == "read-only":
        mode, tools = "dontAsk", "Read,Glob,Grep"
    elif sandbox == "workspace-write":
        mode = "acceptEdits"
        if native_windows():
            windows_mode = executor.get("claude_native_windows_mode")
            if windows_mode == "edit-only":
                # No command execution tools: coding and file edits, but no
                # ability to execute tests inside this Executor process.
                tools = "Read,Glob,Grep,Edit,Write"
            elif windows_mode == "trusted-shell":
                # Explicitly user-opted-in shell access. This is NOT a sandbox
                # and may access files outside the repository. PSC audit is
                # post-execution and does not prevent those side effects.
                tools = "default"
            else:
                raise ValueError(
                    "native Windows Claude workspace-write requires explicit "
                    "claude_native_windows_mode: edit-only or trusted-shell"
                )
        else:
            tools = "default"
    elif sandbox == "danger-full-access":
        mode, tools = "bypassPermissions", "default"
    else:
        raise ValueError(f"unsupported Claude sandbox mapping: {sandbox}")
    command = [
        executable, "-p", "--verbose", "--output-format", "stream-json",
        "--no-session-persistence", "--setting-sources", "user",
        "--strict-mcp-config", "--disallowedTools", "mcp__*",
        "--permission-mode", mode, "--tools", tools,
    ]
    if sandbox == "workspace-write":
        if native_windows():
            # Avoid attempting unsupported OS sandbox startup. Per-tool
            # permission checks still apply; CLI cannot give an OS boundary.
            command.extend(["--settings", json.dumps({
                "sandbox": {"enabled": False}
            }, separators=(",", ":"))])
            if windows_mode == "trusted-shell":
                # Noninteractive E must run build/test commands without waiting
                # for a permission prompt. This is an explicit risky choice.
                command.extend(["--allowedTools", "Bash"])
        else:
            # Supported POSIX shell sandbox: fail rather than downgrade.
            command.extend(["--settings", json.dumps({
                "sandbox": {
                    "enabled": True, "failIfUnavailable": True,
                    "allowUnsandboxedCommands": False,
                }
            }, separators=(",", ":"))])
    _route(command, executor)
    if output_schema is not None:
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
