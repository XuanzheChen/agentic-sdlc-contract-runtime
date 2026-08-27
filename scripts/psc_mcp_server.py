from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server import MCPServer
except ImportError:  # Keep non-MCP unit tests and CLI usage dependency-free.
    MCPServer = None  # type: ignore[assignment]

import invoke_executor as executor_runtime


PUBLIC_RESULT_FIELDS = (
    "status",
    "reason",
    "exit_code",
    "changed_paths",
    "scope_violations",
    "artifact_paths",
    "log_path",
    "executor_config_sha256",
    "errors",
)


def compact_executor_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields a Supervisor needs after an Executor finishes.

    Raw stdout/stderr and the structured completion body are intentionally not
    returned through MCP. They are already persisted by invoke_executor() as the
    raw executor log plus plan.md/coding.md artifacts. Keeping them out of the
    tool response prevents large Executor transcripts from inflating the
    Supervisor context.
    """
    return {field: result.get(field) for field in PUBLIC_RESULT_FIELDS}


def invoke_executor_tool(
    repository: str,
    runtime_config: str,
    project: str,
    task: str,
    contract: str,
    previous_review: str | None = None,
) -> dict[str, Any]:
    """Run one PSC Executor attempt and wait until it reaches a terminal result.

    This call is deliberately synchronous. When exposed through MCP, Codex
    awaits the single tools/call request while the existing Executor runtime
    blocks on the child harness. The Supervisor therefore performs no polling
    inference and never needs exec_command/write_stdin for Executor lifecycle
    management.
    """
    result = executor_runtime.invoke_executor_from_paths(
        repository=Path(repository),
        runtime_config=Path(runtime_config),
        project=Path(project),
        task_path=Path(task),
        contract_path=Path(contract),
        previous_review_path=Path(previous_review) if previous_review else None,
    )
    return compact_executor_result(result)


def build_server() -> Any:
    if MCPServer is None:
        raise RuntimeError(
            'PSC Executor MCP requires the Python MCP SDK. '
            'Install it with: python -m pip install "mcp>=2,<3"'
        )

    server = MCPServer("agentic-sdlc-executor")

    @server.tool(name="psc_invoke_executor")
    def psc_invoke_executor(
        repository: str,
        runtime_config: str,
        project: str,
        task: str,
        contract: str,
        previous_review: str | None = None,
    ) -> dict[str, Any]:
        """Run a PSC Executor attempt and block until completion.

        Use this for normal Supervisor dispatch. The tool returns compact
        structured metadata only; inspect artifact_paths or log_path separately
        when verification requires more detail.
        """
        return invoke_executor_tool(
            repository=repository,
            runtime_config=runtime_config,
            project=project,
            task=task,
            contract=contract,
            previous_review=previous_review,
        )

    return server


def main() -> int:
    try:
        server = build_server()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
