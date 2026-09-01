from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server import MCPServer
except ImportError:  # Keep non-MCP unit tests and CLI usage dependency-free.
    MCPServer = None  # type: ignore[assignment]

import invoke_executor as executor_runtime
from executor_token_usage import record_executor_usage


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
    "retryable",
    "token_usage",
    "errors",
)
STDERR_DIAGNOSTIC_CHARS = 8192
STDOUT_DIAGNOSTIC_CHARS = 4096
MAX_QUALITY_RETRIES = 3
MAX_ABNORMAL_RETRIES = 3
RETRY_KINDS = frozenset({"initial", "quality_rework", "abnormal_retry"})
ABNORMAL_RESULT_REASONS = frozenset({
    "timeout",
    "process_failed",
    "spawn_failed",
    "invalid_executor_output",
    "artifact_persistence_failed",
})


def _attempt_counter_path(project: Path) -> Path:
    return Path(project) / "runtime" / "executor_attempts.json"


def _task_id_from_path(task_path: Path) -> str:
    match = re.search(r"\bT-\d{3,}\b", task_path.name)
    if match:
        return match.group(0)
    return executor_runtime._task_id(task_path)


def _contract_version(contract_path: Path) -> int | None:
    match = re.fullmatch(r"v(\d+)", contract_path.name)
    return int(match.group(1)) if match else None


def _attempt_key(contract_path: Path, task_path: Path) -> str:
    version = _contract_version(contract_path)
    version_text = f"v{version}" if version is not None else contract_path.name
    return f"{version_text}:{_task_id_from_path(task_path)}"


def _empty_retry_state() -> dict[str, Any]:
    return {
        "execution_round": 1,
        "initial_attempted": False,
        "quality_retries_used": 0,
        "abnormal_retries_used": 0,
    }


def _normalize_retry_state(value: Any) -> dict[str, Any]:
    state = _empty_retry_state()
    if not isinstance(value, dict):
        return state
    round_number = value.get("execution_round")
    if isinstance(round_number, int) and round_number >= 1:
        state["execution_round"] = round_number
    state["initial_attempted"] = value.get("initial_attempted") is True
    for field in ("quality_retries_used", "abnormal_retries_used"):
        count = value.get(field)
        if isinstance(count, int) and count >= 0:
            state[field] = count
    return state


def _load_retry_states(project: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Load split retry budgets.

    Schema v1 stored only an undifferentiated attempt count, which cannot be
    safely assigned to quality or abnormal retries. Preserve those values as
    legacy_unclassified_attempts for audit, but do not charge either new budget.
    """
    path = _attempt_counter_path(project)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(value, dict):
        return {}, {}
    if value.get("schema_version") == 2:
        raw_tasks = value.get("tasks")
        tasks = {
            str(key): _normalize_retry_state(state)
            for key, state in raw_tasks.items()
        } if isinstance(raw_tasks, dict) else {}
        legacy = value.get("legacy_unclassified_attempts")
        legacy_counts = {
            str(key): count
            for key, count in legacy.items()
            if isinstance(count, int) and count >= 0
        } if isinstance(legacy, dict) else {}
        return tasks, legacy_counts
    raw_attempts = value.get("attempts")
    legacy_counts = {
        str(key): count
        for key, count in raw_attempts.items()
        if isinstance(count, int) and count >= 0
    } if isinstance(raw_attempts, dict) else {}
    return {}, legacy_counts


def _store_retry_states(
    project: Path,
    tasks: dict[str, dict[str, Any]],
    legacy_unclassified_attempts: dict[str, int],
) -> None:
    path = _attempt_counter_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": tasks,
                "legacy_unclassified_attempts": legacy_unclassified_attempts,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _retry_policy(
    state: dict[str, Any],
    *,
    dispatch_kind: str,
    charged_budget: str | None = None,
    legacy_unclassified_attempts: int = 0,
) -> dict[str, Any]:
    return {
        "dispatch_kind": dispatch_kind,
        "charged_budget": charged_budget,
        "execution_round": state["execution_round"],
        "initial_attempted": state["initial_attempted"],
        "max_quality_retries": MAX_QUALITY_RETRIES,
        "quality_retries_used": state["quality_retries_used"],
        "quality_retries_remaining": max(0, MAX_QUALITY_RETRIES - state["quality_retries_used"]),
        "max_abnormal_retries": MAX_ABNORMAL_RETRIES,
        "abnormal_retries_used": state["abnormal_retries_used"],
        "abnormal_retries_remaining": max(0, MAX_ABNORMAL_RETRIES - state["abnormal_retries_used"]),
        "legacy_unclassified_attempts": legacy_unclassified_attempts,
    }


def _budget_block(
    *,
    project: Path,
    contract_path: Path,
    task_path: Path,
    state: dict[str, Any],
    dispatch_kind: str,
    legacy_unclassified_attempts: int,
) -> dict[str, Any] | None:
    exhausted = _exhausted_budget(state)
    if exhausted is not None:
        budget, used, limit, reason = exhausted
        marker = _mark_retry_exhaustion_blocked(
            project,
            contract_path,
            task_path,
            budget=budget,
            used=used,
            limit=limit,
            reason=reason,
        )
        return {
            "status": "retry_limit_reached",
            "reason": reason,
            "exit_code": None,
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": None,
            "executor_config_sha256": None,
            "timeout_adjustment": None,
            "errors": [
                f"Task {_task_id_from_path(task_path)} exhausted its "
                f"{budget} retry budget. The task is blocked pending an "
                "explicit user decision: reset this task-local exhausted "
                "budget and continue with E, or switch execution to S."
            ],
            "retry_exhaustion": marker,
            "retry_policy": _retry_policy(
                state,
                dispatch_kind=dispatch_kind,
                legacy_unclassified_attempts=legacy_unclassified_attempts,
            ),
        }

    if dispatch_kind not in RETRY_KINDS:
        reason = "invalid_retry_kind"
        message = (
            f"retry_kind must be one of {sorted(RETRY_KINDS)}; got "
            f"{dispatch_kind!r}."
        )
    elif dispatch_kind == "initial" and state["initial_attempted"]:
        reason = "retry_kind_required"
        message = (
            f"Task {_task_id_from_path(task_path)} already has an initial "
            "Executor attempt. Classify the next dispatch as quality_rework "
            "or abnormal_retry."
        )
    else:
        return None
    return {
        "status": "retry_classification_required",
        "reason": reason,
        "exit_code": None,
        "changed_paths": [],
        "scope_violations": [],
        "artifact_paths": {},
        "log_path": None,
        "executor_config_sha256": None,
        "timeout_adjustment": None,
        "errors": [message],
        "retry_policy": _retry_policy(
            state,
            dispatch_kind=dispatch_kind,
            legacy_unclassified_attempts=legacy_unclassified_attempts,
        ),
    }


def _charge_actual_attempt(
    project: Path,
    contract_path: Path,
    task_path: Path,
    *,
    dispatch_kind: str,
    result: dict[str, Any],
    prior_state: dict[str, Any],
    legacy_unclassified_attempts: dict[str, int],
) -> tuple[dict[str, Any], str | None]:
    """Charge only a real Executor attempt, keeping quality/abnormal budgets independent.

    Initial work never consumes a retry budget. A quality-rework dispatch that
    itself fails abnormally (for example timeout/no-return) charges the abnormal
    budget instead of the quality budget, so infrastructure/runtime failure does
    not steal a quality rework opportunity.
    """
    if not result.get("log_path"):
        return prior_state, None

    state = dict(prior_state)
    charged_budget: str | None = None
    reason = result.get("reason")

    # Deterministic pre-launch transport failures (notably WinError 206)
    # never executed E. Repeating the same launch is useless and must not
    # consume either retry budget or mark the round's initial attempt used.
    if reason == "launch_transport_failed" or result.get("retryable") is False:
        return prior_state, None

    if dispatch_kind == "initial":
        state["initial_attempted"] = True
    elif dispatch_kind == "abnormal_retry":
        state["abnormal_retries_used"] += 1
        charged_budget = "abnormal_retry"
    elif dispatch_kind == "quality_rework":
        if reason in ABNORMAL_RESULT_REASONS:
            state["abnormal_retries_used"] += 1
            charged_budget = "abnormal_retry"
        else:
            state["quality_retries_used"] += 1
            charged_budget = "quality_rework"

    tasks, legacy = _load_retry_states(project)
    key = _attempt_key(contract_path, task_path)
    tasks[key] = state
    for legacy_key, count in legacy_unclassified_attempts.items():
        legacy.setdefault(legacy_key, count)
    _store_retry_states(project, tasks, legacy)
    return state, charged_budget


def _workflow_execution_owner(project: Path) -> str:
    state_path = Path(project) / "runtime" / "workflow_state.json"
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "executor"
    owner = value.get("execution_owner") if isinstance(value, dict) else None
    return owner if owner in {"executor", "supervisor"} else "executor"


def _workflow_state_path(project: Path) -> Path:
    return Path(project) / "runtime" / "workflow_state.json"


def _write_workflow_state(project: Path, state: dict[str, Any]) -> None:
    path = _workflow_state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _mark_retry_exhaustion_blocked(
    project: Path,
    contract_path: Path,
    task_path: Path,
    *,
    budget: str,
    used: int,
    limit: int,
    reason: str,
) -> dict[str, Any] | None:
    """Persist the user decision point for one exhausted task-local E budget."""
    path = _workflow_state_path(project)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    task_id = _task_id_from_path(task_path)
    version = _contract_version(contract_path)
    marker = {
        "contract_version": version,
        "task": task_id,
        "budget": budget,
        "used": used,
        "limit": limit,
        "reason": reason,
        "execution_round": _normalize_retry_state(
            _load_retry_states(project)[0].get(
                _attempt_key(contract_path, task_path)
            )
        )["execution_round"],
        "decision_required": [
            "reset-and-continue-executor",
            "switch-to-supervisor",
        ],
    }
    state = dict(state)
    state["status"] = "blocked"
    state["current_task"] = task_id
    state["retry_exhaustion"] = marker
    state["last_stage"] = "executor_retry_budget_exhausted"
    state["updated_at"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    _write_workflow_state(project, state)
    return marker


def _mark_nonretryable_runtime_blocked(
    project: Path,
    contract_path: Path,
    task_path: Path,
    *,
    reason: str,
    errors: list[str],
) -> dict[str, Any] | None:
    """Persist a hard runtime block without consuming Executor retry budget."""
    path = _workflow_state_path(project)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    marker = {
        "contract_version": _contract_version(contract_path),
        "task": _task_id_from_path(task_path),
        "reason": reason,
        "retryable": False,
        "errors": [str(item) for item in errors],
        "resolution": "repair_runtime_then_continue_same_task",
    }
    state = dict(state)
    state["status"] = "blocked"
    state["current_task"] = marker["task"]
    state["runtime_failure"] = marker
    state["last_stage"] = "executor_nonretryable_runtime_failure"
    state["updated_at"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    _write_workflow_state(project, state)
    return marker


def _active_nonretryable_runtime_block(project: Path, task_path: Path) -> dict[str, Any] | None:
    path = _workflow_state_path(project)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or state.get("status") != "blocked":
        return None
    marker = state.get("runtime_failure")
    if not isinstance(marker, dict) or marker.get("retryable") is not False:
        return None
    if marker.get("task") != _task_id_from_path(task_path):
        return None
    return marker

def _exhausted_budget(state: dict[str, Any]) -> tuple[str, int, int, str] | None:
    if state["quality_retries_used"] >= MAX_QUALITY_RETRIES:
        return (
            "quality_rework",
            state["quality_retries_used"],
            MAX_QUALITY_RETRIES,
            "quality_rework_limit_reached",
        )
    if state["abnormal_retries_used"] >= MAX_ABNORMAL_RETRIES:
        return (
            "abnormal_retry",
            state["abnormal_retries_used"],
            MAX_ABNORMAL_RETRIES,
            "executor_abnormal_retry_limit_reached",
        )
    return None


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
    retry_kind: str = "initial",
) -> dict[str, Any]:
    """Run one PSC Executor attempt and wait until it reaches a terminal result.

    retry_kind is required semantically after the initial attempt:
    - initial: first Executor attempt for this Contract/Task.
    - quality_rework: Supervisor rejected a completed implementation on quality
      or acceptance grounds.
    - abnormal_retry: previous Executor attempt failed abnormally (timeout,
      process failure, invalid completion framing, etc.).

    Quality and abnormal retry budgets are independent. A quality-rework
    dispatch that itself ends in an Executor abnormality charges only the
    abnormal retry budget.
    """
    project_path = Path(project)
    task_path = Path(task)
    contract_path = Path(contract)
    if _workflow_execution_owner(project_path) == "supervisor":
        return {
            "status": "execution_owner_mismatch",
            "reason": "supervisor_owns_task_execution",
            "exit_code": None,
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": None,
            "executor_config_sha256": None,
            "timeout_adjustment": None,
            "errors": [
                "workflow_state.execution_owner is supervisor; persist an "
                "explicit handoff back to executor before dispatching E."
            ],
        }
    runtime_block = _active_nonretryable_runtime_block(project_path, task_path)
    if runtime_block is not None:
        return {
            "status": "runtime_blocked",
            "reason": runtime_block.get("reason"),
            "retryable": False,
            "exit_code": None,
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": None,
            "executor_config_sha256": None,
            "timeout_adjustment": None,
            "errors": runtime_block.get("errors", []),
            "runtime_failure": runtime_block,
        }
    attempt_key = _attempt_key(contract_path, task_path)
    states, legacy = _load_retry_states(project_path)
    state = _normalize_retry_state(states.get(attempt_key))
    legacy_count = legacy.get(attempt_key, 0)

    blocked = _budget_block(
        project=project_path,
        contract_path=contract_path,
        task_path=task_path,
        state=state,
        dispatch_kind=retry_kind,
        legacy_unclassified_attempts=legacy_count,
    )
    if blocked is not None:
        return blocked

    result = executor_runtime.invoke_executor_from_paths(
        repository=Path(repository),
        runtime_config=Path(runtime_config),
        project=project_path,
        task_path=task_path,
        contract_path=contract_path,
        previous_review_path=Path(previous_review) if previous_review else None,
    )
    executor_usage: dict[str, Any] | None = None
    token_usage = result.get("token_usage")
    if isinstance(token_usage, dict):
        try:
            executor_usage = record_executor_usage(
                project_path,
                contract_path,
                task=_task_id_from_path(task_path),
                execution_round=state["execution_round"],
                retry_kind=retry_kind,
                status=str(result.get("status") or "unknown"),
                reason=str(result["reason"]) if result.get("reason") is not None else None,
                log_path=str(result["log_path"]) if result.get("log_path") else None,
                usage=token_usage,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            executor_usage = {
                "invocation": token_usage,
                "persistence_error": str(exc),
            }
    state, charged_budget = _charge_actual_attempt(
        project_path,
        contract_path,
        task_path,
        dispatch_kind=retry_kind,
        result=result,
        prior_state=state,
        legacy_unclassified_attempts=legacy,
    )
    runtime_failure = None
    if result.get("retryable") is False or result.get("reason") == "launch_transport_failed":
        runtime_failure = _mark_nonretryable_runtime_blocked(
            project_path,
            contract_path,
            task_path,
            reason=str(result.get("reason") or "nonretryable_runtime_failure"),
            errors=[str(item) for item in result.get("errors", [])]
                + ([str(result.get("stderr"))] if result.get("stderr") else []),
        )
    retry_exhaustion = None
    if (
        charged_budget == "abnormal_retry"
        and state["abnormal_retries_used"] >= MAX_ABNORMAL_RETRIES
        and result.get("reason") in ABNORMAL_RESULT_REASONS
    ):
        retry_exhaustion = _mark_retry_exhaustion_blocked(
            project_path,
            contract_path,
            task_path,
            budget="abnormal_retry",
            used=state["abnormal_retries_used"],
            limit=MAX_ABNORMAL_RETRIES,
            reason="executor_abnormal_retry_limit_reached",
        )
    compact = compact_executor_result(result)
    if executor_usage is not None:
        compact["executor_usage"] = executor_usage
    if runtime_failure is not None:
        compact["runtime_failure"] = runtime_failure
        compact["workflow_status"] = "blocked"
    if retry_exhaustion is not None:
        compact["retry_exhaustion"] = retry_exhaustion
        compact["workflow_status"] = "blocked"
        compact["decision_required"] = retry_exhaustion["decision_required"]
    compact["retry_policy"] = _retry_policy(
        state,
        dispatch_kind=retry_kind,
        charged_budget=charged_budget,
        legacy_unclassified_attempts=legacy_count,
    )
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
        retry_kind: str = "initial",
    ) -> dict[str, Any]:
        """Run a PSC Executor attempt and block until completion.

        Use retry_kind="initial" for the first attempt of each task execution
        round. Thereafter use "quality_rework" when Supervisor verification rejects a
        completed implementation, or "abnormal_retry" after an Executor/runtime
        abnormality such as timeout/no-return. The two retry budgets are
        independent and capped at three each.
        """
        return invoke_executor_tool(
            repository=repository,
            runtime_config=runtime_config,
            project=project,
            task=task,
            contract=contract,
            previous_review=previous_review,
            retry_kind=retry_kind,
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
