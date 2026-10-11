"""Authoritative recovery of a missing Executor retry-exhaustion decision marker.

Recovery is a metadata-only, compare-and-swap workflow-state mutation. It
cannot grant Executor attempts, modify retry budgets, or change ownership.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import task_continuity
import workflow_registry

DECISIONS = (
    "reset-and-continue-executor",
    "switch-to-supervisor-for-current-task",
    "switch-to-supervisor",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_order(contract: Path) -> list[str]:
    import psc_runtime
    blocks = psc_runtime.task_blocks((contract / "tasks.md").read_text(encoding="utf-8"))
    order = [key for key, _ in blocks]
    if not order or len(order) != len(set(order)):
        raise ValueError("Contract task order is absent or ambiguous")
    return order


def exhausted_marker(project: Path, workflow: dict[str, Any], *,
                     require_blocked: bool = False) -> dict[str, Any] | None:
    """Derive exhaustion ONLY from the active Contract's authoritative ledger.

    Caller is responsible for holding executor-dispatch.lock across any
    subsequent state mutation. None means no exhausted budget was proved.
    Malformed or contradictory authority is an error, never a free allowance.
    """
    project = Path(project).resolve()
    if require_blocked and workflow.get("status") != "blocked":
        raise ValueError("retry_exhaustion_recovery_requires_blocked_workflow")
    if workflow.get("execution_owner", "executor") != "executor":
        if require_blocked:
            raise ValueError("retry_exhaustion_recovery_requires_executor_owner")
        return None
    if isinstance(workflow.get("runtime_failure"), dict):
        raise ValueError("runtime_failure_requires_separate_resolution")
    version = workflow.get("contract_version")
    task = workflow.get("current_task")
    if type(version) is not int or version < 1 or not isinstance(task, str) or not re.fullmatch(r"T-\d{3,}", task):
        raise ValueError("active Contract version and current Task must be explicit")
    contract = project / "contract" / f"v{version}"
    metadata = json.loads((contract / "metadata.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("status") != "approved" or metadata.get("version") != version:
        raise ValueError("active Contract must be an Approved materialized version")
    order = _task_order(contract)
    completed = workflow.get("last_completed_task")
    if completed is None:
        expected_task = order[0]
    elif completed in order:
        pos = order.index(completed) + 1
        expected_task = order[pos] if pos < len(order) else None
    else:
        raise ValueError("last_completed_task is not in active Contract")
    if task != expected_task:
        raise ValueError("current_task does not match the unfinished Contract boundary")

    path = task_continuity.ledger_path(project)
    if not path.is_file():
        if require_blocked:
            raise ValueError("authoritative_executor_attempt_ledger_missing")
        return None
    ledger = task_continuity.read_ledger(project)
    key = f"v{version}:{task}"
    raw = ledger["tasks"].get(key)
    if raw is None:
        if require_blocked:
            raise ValueError("authoritative_retry_record_missing: " + key)
        return None
    retry = task_continuity.normalize(raw)
    checked = task_continuity.check_task(project, contract, task)
    if checked["status"] == "blocked":
        raise ValueError("task_continuity_unverified: " + checked["reason"])
    if checked["status"] == "verified" and checked["state"] != retry:
        raise ValueError("authoritative_retry_state_conflict")

    if not retry["initial_attempted"]:
        raise ValueError("retry_count_requires_real_initial_attempt") if (
            retry["quality_retries_used"] or retry["abnormal_retries_used"]) else (
            ValueError("retry exhaustion cannot precede first Executor attempt")
        )
    if retry["quality_retries_used"] >= task_continuity.MAX_QUALITY_RETRIES:
        budget, used, limit, reason = (
            "quality_rework", retry["quality_retries_used"],
            task_continuity.MAX_QUALITY_RETRIES, "quality_rework_limit_reached")
    elif retry["abnormal_retries_used"] >= task_continuity.MAX_ABNORMAL_RETRIES:
        budget, used, limit, reason = (
            "abnormal_retry", retry["abnormal_retries_used"],
            task_continuity.MAX_ABNORMAL_RETRIES, "executor_abnormal_retry_limit_reached")
    else:
        return None
    return {
        "contract_version": version,
        "task": task,
        "budget": budget,
        "used": used,
        "limit": limit,
        "reason": reason,
        "execution_round": retry["execution_round"],
        "decision_required": list(DECISIONS),
        "source_executor_attempts_sha256": _sha_file(path),
    }


def validate_bound_marker(project: Path, workflow: dict[str, Any], marker: dict[str, Any]) -> None:
    """Validate recovered/native v2 marker before allowing a decision."""
    if "source_executor_attempts_sha256" not in marker:
        return  # Original pre-existing marker format remains compatible.
    expected = exhausted_marker(project, workflow, require_blocked=True)
    if expected is None:
        raise ValueError("retry_exhaustion_marker_has_no_exhausted_budget")
    for field in ("contract_version", "task", "budget", "used", "limit",
                  "reason", "execution_round", "source_executor_attempts_sha256"):
        if marker.get(field) != expected[field]:
            raise ValueError("retry_exhaustion_marker_authority_conflict: " + field)


def recover(project: Path, expected_state_sha256: str,
            expected_ledger_sha256: str | None = None) -> dict[str, Any]:
    """Compare-and-swap an absent marker into an already blocked workflow.

    Never modifies the attempts ledger, Supervisor decisions or execution owner.
    """
    if not isinstance(expected_state_sha256, str) or not SHA256_RE.fullmatch(expected_state_sha256):
        raise ValueError("expected_state_sha256 must be an exact lowercase SHA256")
    if expected_ledger_sha256 is not None and (
        not isinstance(expected_ledger_sha256, str)
        or not SHA256_RE.fullmatch(expected_ledger_sha256)
    ):
        raise ValueError("expected_ledger_sha256 must be a lowercase SHA256")
    project = Path(project).resolve()
    with workflow_registry._locked(project / "runtime" / task_continuity.LOCK_NAME):
        import psc_runtime
        state_path = project / "runtime" / "workflow_state.json"
        state_sha = _sha_file(state_path)
        if state_sha != expected_state_sha256:
            raise ValueError("workflow_state_conflict: expected_state_sha256 is stale")
        state = psc_runtime.load_json(state_path)
        if not isinstance(state, dict) or state.get("status") != "blocked":
            raise ValueError("retry_exhaustion_recovery_requires_blocked_workflow")
        if "retry_exhaustion" in state:
            raise ValueError("retry_exhaustion_marker_already_present_or_malformed")
        if state.get("last_stage") not in {
            "supervisor_blocked", "executor_retry_budget_exhausted"
        }:
            raise ValueError("blocked_workflow_has_unrelated_or_unknown_cause")
        if state.get("execution_owner", "executor") != "executor":
            raise ValueError("retry_exhaustion_recovery_requires_executor_owner")
        if isinstance(state.get("runtime_failure"), dict):
            raise ValueError("runtime_failure_requires_separate_resolution")
        ledger_path = task_continuity.ledger_path(project)
        if not ledger_path.is_file():
            raise ValueError("authoritative_executor_attempt_ledger_missing")
        ledger_sha = _sha_file(ledger_path)
        if expected_ledger_sha256 is not None and expected_ledger_sha256 != ledger_sha:
            raise ValueError("executor_attempts_conflict: expected_ledger_sha256 is stale")
        marker = exhausted_marker(project, state, require_blocked=True)
        if marker is None:
            raise ValueError("no_exhausted_executor_retry_budget")
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        marker["recovered_from_state_sha256"] = state_sha
        marker["recovered_from_last_stage"] = state.get("last_stage")
        marker["recovered_at"] = stamp
        updated = dict(state)
        updated["retry_exhaustion"] = marker
        updated["last_stage"] = "executor_retry_exhaustion_marker_recovered"
        updated["updated_at"] = stamp
        psc_runtime.dump_json(state_path, updated)
        return {
            "status": "retry_exhaustion_marker_recovered",
            "task": marker["task"],
            "contract_version": marker["contract_version"],
            "execution_round": marker["execution_round"],
            "budget": marker["budget"],
            "used": marker["used"],
            "limit": marker["limit"],
            "execution_owner": updated.get("execution_owner", "executor"),
            "workflow_status": "blocked",
            "previous_state_sha256": state_sha,
            "workflow_state_sha256": _sha_file(state_path),
            "executor_attempts_sha256": ledger_sha,
            "retry_counters_changed": False,
            "fresh_preflight_required_for_executor": True,
            "decision_required": list(DECISIONS),
        }
