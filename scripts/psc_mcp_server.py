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
    "timeout_adjustment",
    "errors",
)
STDERR_DIAGNOSTIC_CHARS = 8192
STDOUT_DIAGNOSTIC_CHARS = 4096
MAX_TASK_RETRIES = 3
MAX_TASK_ATTEMPTS = 1 + MAX_TASK_RETRIES


def _existing_task_attempts(project: Path, task_path: Path) -> int:
    task_id = executor_runtime._task_id(task_path)
    log_dir = Path(project) / "logs" / "executor"
    if not log_dir.is_dir():
        return 0
    return sum(
        1
        for path in log_dir.glob(f"{task_id}-*.log")
        if path.is_file()
    )


def _tail(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def compact_executor_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields a Supervisor needs after an Executor finishes.

    Raw stdout/stderr and the structured completion body are intentionally not
    returned through MCP. They are already persisted by invoke_executor() as the
    raw executor log plus plan.md/coding.md artifacts. On failure only, bounded
    stdout/stderr tails are included as diagnostic context so the Supervisor can
    handle common errors without loading the entire log. Keeping full transcripts
    out of the tool response prevents large Executor outputs from inflating the
    Supervisor context.
    """
    compact = {field: result.get(field) for field in PUBLIC_RESULT_FIELDS}
    if result.get("status") != "completed":
        stderr = str(result.get("stderr") or "")
        stdout = str(result.get("stdout") or "")
        if stderr or stdout:
            stderr_tail, stderr_truncated = _tail(stderr, STDERR_DIAGNOSTIC_CHARS)
            stdout_tail, stdout_truncated = _tail(stdout, STDOUT_DIAGNOSTIC_CHARS)
            compact["diagnostic"] = {
                "stderr_tail": stderr_tail,
                "stdout_tail": stdout_tail,
                "stderr_truncated": stderr_truncated,
                "stdout_truncated": stdout_truncated,
            }
    return compact


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
    project_path = Path(project)
    task_path = Path(task)
    prior_attempts = _existing_task_attempts(project_path, task_path)
    if prior_attempts >= MAX_TASK_ATTEMPTS:
        return {
            "status": "retry_limit_reached",
            "reason": "max_task_retries_exhausted",
            "exit_code": None,
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": None,
            "executor_config_sha256": None,
            "timeout_adjustment": None,
            "errors": [
                f"Task {executor_runtime._task_id(task_path)} already has "
                f"{prior_attempts} Executor attempts; maximum is "
                f"{MAX_TASK_ATTEMPTS} total attempts (1 initial + "
                f"{MAX_TASK_RETRIES} retries)."
            ],
            "retry_policy": {
                "max_retries": MAX_TASK_RETRIES,
                "max_attempts": MAX_TASK_ATTEMPTS,
                "attempts_used": prior_attempts,
            },
        }

    result = executor_runtime.invoke_executor_from_paths(
        repository=Path(repository),
        runtime_config=Path(runtime_config),
        project=project_path,
        task_path=task_path,
        contract_path=Path(contract),
        previous_review_path=Path(previous_review) if previous_review else None,
    )
    compact = compact_executor_result(result)
    compact["retry_policy"] = {
        "max_retries": MAX_TASK_RETRIES,
        "max_attempts": MAX_TASK_ATTEMPTS,
        "attempts_used": prior_attempts + 1,
    }
    return compact


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
