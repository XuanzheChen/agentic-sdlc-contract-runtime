from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

BEGIN_MARKER = "# PSC-SUPERVISOR-MCP-BEGIN"
END_MARKER = "# PSC-SUPERVISOR-MCP-END"
PLUGIN_ID = "mcp-agentic-sdlc-executor"
SERVER_NAME = "agentic_sdlc_executor"
EXPECTED_TOOLS = [
    f"mcp__{SERVER_NAME}__psc_supervisor_snapshot",
    f"mcp__{SERVER_NAME}__psc_ensure_executor_ready",
    f"mcp__{SERVER_NAME}__psc_invoke_executor",
    f"mcp__{SERVER_NAME}__psc_commit_supervisor_transition",
]


def default_dsh_home() -> Path:
    value = os.environ.get("DSH_HOME")
    if value and value.strip():
        return Path(value).expanduser()
    return Path.home() / ".dsh"


def _json_scalar(value: str) -> str:
    # JSON double-quoted strings are valid YAML scalars and handle Windows
    # backslashes safely without depending on PyYAML.
    return json.dumps(value, ensure_ascii=False)


def managed_block(
    *,
    mcp_python: Path,
    server_script: Path,
    tool_timeout_ms: int,
) -> str:
    return "\n".join(
        [
            BEGIN_MARKER,
            "- insert:",
            f"    - id: {PLUGIN_ID}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {SERVER_NAME}",
            "        transport: stdio",
            f"        command: {_json_scalar(str(mcp_python))}",
            "        args:",
            f"          - {_json_scalar(str(server_script))}",
            f"        toolCallTimeoutMs: {tool_timeout_ms}",
            "        failOnStartupError: true",
            END_MARKER,
            "",
        ]
    )


def target_patch(dsh_home: Path, scope: str, profile: str | None) -> Path:
    home = dsh_home.expanduser().resolve()
    if scope == "home":
        return home / "cordis.patch.yml"
    if not profile:
        raise ValueError("--profile is required when --scope=profile")
    if profile == "desktop":
        raise ValueError(
            "DSH Desktop owns profiles/desktop. Configure PSC MCP at the "
            "home-level $DSH_HOME/cordis.patch.yml instead."
        )
    return home / "profiles" / profile / "cordis.patch.yml"


def validate_inputs(mcp_python: Path, server_script: Path, tool_timeout_ms: int) -> None:
    if not mcp_python.is_file():
        raise ValueError(f"MCP Python not found: {mcp_python}")
    if not server_script.is_file():
        raise ValueError(f"PSC MCP server not found: {server_script}")
    if tool_timeout_ms <= 0:
        raise ValueError("--tool-timeout-ms must be a positive integer")


def _replace_managed(existing: str, block: str) -> tuple[str, str]:
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if begin >= 0 or end >= 0:
        if begin < 0 or end < 0 or end < begin:
            raise ValueError("Malformed PSC managed markers in cordis.patch.yml")
        end += len(END_MARKER)
        suffix = existing[end:]
        if suffix.startswith("\r\n"):
            suffix = suffix[2:]
        elif suffix.startswith("\n"):
            suffix = suffix[1:]
        merged = existing[:begin] + block + suffix
        return merged, "updated"

    if PLUGIN_ID in existing or f"serverName: {SERVER_NAME}" in existing:
        raise ValueError(
            "An unmanaged PSC MCP entry already exists. Remove it or surround "
            f"the managed entry with {BEGIN_MARKER} / {END_MARKER} before retrying."
        )

    stripped = existing.strip()
    if stripped in {"", "[]"}:
        return block, "created"

    prefix = existing.rstrip() + "\n\n"
    return prefix + block, "appended"


def configure(
    *,
    dsh_home: Path,
    scope: str,
    profile: str | None,
    mcp_python: Path,
    server_script: Path,
    tool_timeout_ms: int,
) -> dict[str, Any]:
    validate_inputs(mcp_python, server_script, tool_timeout_ms)
    patch = target_patch(dsh_home, scope, profile)
    existing = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    block = managed_block(
        mcp_python=mcp_python.resolve(),
        server_script=server_script.resolve(),
        tool_timeout_ms=tool_timeout_ms,
    )
    merged, action = _replace_managed(existing, block)
    patch.parent.mkdir(parents=True, exist_ok=True)
    tmp = patch.with_name(patch.name + ".psc-tmp")
    tmp.write_text(merged, encoding="utf-8")
    os.replace(tmp, patch)
    return {
        "status": "configured",
        "action": action,
        "scope": scope,
        "profile": profile,
        "dsh_home": str(dsh_home.expanduser().resolve()),
        "patch_path": str(patch),
        "server_name": SERVER_NAME,
        "expected_tools": EXPECTED_TOOLS,
        "restart_required": True,
        "message": (
            "Restart/refesh the DSH Supervisor session. MCP tools are registered "
            "during Harness startup and cannot appear in an already-running session."
        ),
    }


def check(
    *,
    dsh_home: Path,
    scope: str,
    profile: str | None,
) -> dict[str, Any]:
    patch = target_patch(dsh_home, scope, profile)
    text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    managed = (
        BEGIN_MARKER in text
        and END_MARKER in text
        and PLUGIN_ID in text
        and f"serverName: {SERVER_NAME}" in text
        and "@deepseek-ai/dsh-mcp-client" in text
    )
    return {
        "status": "configured" if managed else "missing",
        "scope": scope,
        "profile": profile,
        "dsh_home": str(dsh_home.expanduser().resolve()),
        "patch_path": str(patch),
        "patch_exists": patch.is_file(),
        "managed_entry_present": managed,
        "expected_tools": EXPECTED_TOOLS,
        "note": (
            "This checks persistent configuration only. Confirm actual runtime "
            "exposure from a freshly started DSH session's tool inventory."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure the PSC stdio MCP client for a DSH Supervisor."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dsh-home", type=Path, default=default_dsh_home())
        p.add_argument("--scope", choices=("home", "profile"), default="home")
        p.add_argument("--profile")

    cfg = sub.add_parser("configure", help="write/update the managed PSC MCP entry")
    common(cfg)
    cfg.add_argument("--mcp-python", type=Path, required=True)
    cfg.add_argument(
        "--skill-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="agentic-sdlc-contract-runtime root",
    )
    cfg.add_argument("--tool-timeout-ms", type=int, default=7_200_000)

    chk = sub.add_parser("check", help="check the persistent PSC MCP entry")
    common(chk)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            result = configure(
                dsh_home=args.dsh_home,
                scope=args.scope,
                profile=args.profile,
                mcp_python=args.mcp_python.expanduser().resolve(),
                server_script=(
                    args.skill_root.expanduser().resolve()
                    / "scripts"
                    / "psc_mcp_server.py"
                ),
                tool_timeout_ms=args.tool_timeout_ms,
            )
        else:
            result = check(
                dsh_home=args.dsh_home,
                scope=args.scope,
                profile=args.profile,
            )
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "configuration_error", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
