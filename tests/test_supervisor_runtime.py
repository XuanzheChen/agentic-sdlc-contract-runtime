from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import supervisor_runtime as SR


def _workflow(tmp_path: Path):
    project = tmp_path / "project"
    contract = project / "contract" / "v1"
    tasks = project / "developing" / "tasks"
    runtime = project / "runtime"
    contract.mkdir(parents=True)
    tasks.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (contract / "metadata.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    (contract / "tasks.md").write_text(
        "# Tasks\n\n## T-001\nFirst.\n\n## T-002\nSecond.\n", encoding="utf-8"
    )
    (tasks / "T-001.md").write_text("## T-001\nFirst.\n", encoding="utf-8")
    (tasks / "T-002.md").write_text("## T-002\nSecond.\n", encoding="utf-8")
    (runtime / "workflow_state.json").write_text(
        json.dumps({
            "schema_version": 1,
            "contract_version": 1,
            "current_task": None,
            "status": "initialized",
            "attempt": 0,
            "last_completed_task": None,
            "last_stage": "bootstrap",
            "execution_owner": "executor",
            "execution_owner_history": [],
            "updated_at": "2026-09-18T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    return project, contract


def test_supervisor_snapshot_compacts_state_task_retry_and_resume(tmp_path):
    project, contract = _workflow(tmp_path)
    (project / "runtime" / "executor_attempts.json").write_text(
        json.dumps({
            "schema_version": 2,
            "tasks": {
                "v1:T-001": {
                    "execution_round": 2,
                    "initial_attempted": True,
                    "quality_retries_used": 1,
                    "abnormal_retries_used": 0,
                }
            },
            "legacy_unclassified_attempts": {},
        }),
        encoding="utf-8",
    )
    snapshot = SR.supervisor_snapshot(project, contract=contract)
    assert snapshot["current_task"] == "T-001"
    assert snapshot["task_markdown"].startswith("## T-001")
    assert snapshot["retry"]["execution_round"] == 2
    assert snapshot["workflow_state_sha256"]
    assert snapshot["artifacts"]["review.md"]["exists"] is False


def test_quality_rework_transition_writes_review_and_state_atomically(tmp_path):
    project, contract = _workflow(tmp_path)
    before = SR.workflow_state_sha256(project)
    result = SR.commit_supervisor_transition(
        project, contract, "T-001", "quality_rework",
        "# Review\nNeeds a counterexample test.", expected_state_sha256=before,
    )
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert result["workflow_status"] == "ready"
    assert state["current_task"] == "T-001"
    assert state["last_stage"] == "supervisor_quality_rework"
    assert (project / "developing" / "artifacts" / "T-001" / "review.md").is_file()
    assert not (project / "developing" / "artifacts" / "T-001" / "result.md").exists()


def test_pass_transition_advances_task_writes_result_and_resume_capsule(tmp_path):
    project, contract = _workflow(tmp_path)
    state_path = project / "runtime" / "workflow_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["current_task"] = "T-001"
    state["execution_owner"] = "supervisor"
    state["scoped_supervisor_takeover"] = {
        "contract_version": 1, "task": "T-001", "scope": "current_task", "return_owner": "executor"
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = SR.commit_supervisor_transition(
        project, contract, "T-001", "pass", "# Review\nPASS.",
        result_markdown="# Result\nAll acceptance criteria pass.",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_completed_task"] == "T-001"
    assert state["current_task"] == "T-002"
    assert state["execution_owner"] == "executor"
    assert "scoped_supervisor_takeover" not in state
    assert (project / "developing" / "artifacts" / "T-001" / "result.md").is_file()
    latest = json.loads((project / "runtime" / "supervisor_resume.json").read_text(encoding="utf-8"))
    assert (project / "runtime" / "resume" / "T-001.json").is_file()
    assert latest["completed_task"] == "T-001"
    assert latest["current_task"] == "T-002"
    assert latest["workflow_state_sha256"] == result["workflow_state_sha256"]


def test_supervisor_transition_rejects_stale_state_hash(tmp_path):
    project, contract = _workflow(tmp_path)
    try:
        SR.commit_supervisor_transition(
            project, contract, "T-001", "quality_rework", "review",
            expected_state_sha256="deadbeef",
        )
    except ValueError as exc:
        assert "workflow_state_conflict" in str(exc)
    else:
        raise AssertionError("expected stale workflow state to fail closed")


def _append_executor_attempt(project: Path, *, status: str, reason: str | None):
    ledger = project / "runtime" / "executor_token_usage.jsonl"
    record = {
        "schema_version": 1,
        "contract_version": 1,
        "task": "T-001",
        "status": status,
        "reason": reason,
        "log_path": "executor-T-001.log",
        "invocation_id": "a" * 32,
    }
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def test_verified_task_can_pass_after_executor_protocol_failure(tmp_path):
    project, contract = _workflow(tmp_path)
    _append_executor_attempt(project, status="failed", reason="invalid_executor_output")
    attempts = project / "runtime" / "executor_attempts.json"
    attempts.write_text(json.dumps({
        "schema_version": 2, "tasks": {"v1:T-001": {
            "execution_round": 1, "initial_attempted": True,
            "quality_retries_used": 0, "abnormal_retries_used": 0,
        }}, "legacy_unclassified_attempts": {},
    }), encoding="utf-8")
    original_attempts = attempts.read_bytes()
    result = SR.commit_supervisor_transition(
        project, contract, "T-001", "pass",
        "# Review\nIndependent Task test passed; diff inspected; no scope violation. "
        "Executor Completion framing was invalid.",
        result_markdown="# Result\nAll Task acceptance criteria verified; PASS.",
        expected_state_sha256=SR.workflow_state_sha256(project),
    )
    assert result["decision"] == "pass"
    disposition = result["acceptance_disposition"]
    assert disposition["kind"] == "verified_after_executor_anomaly"
    assert disposition["executor_anomaly"]["reason"] == "invalid_executor_output"
    assert disposition["executor_anomaly"]["invocation_id"] == "a" * 32
    state = json.loads((project / "runtime" / "workflow_state.json").read_text())
    assert state["last_completed_task"] == "T-001"
    assert state["last_acceptance_disposition"] == disposition
    assert state["supervisor_transition_history"][-1]["acceptance_disposition"] == disposition
    assert attempts.read_bytes() == original_attempts
    assert json.loads(
        (project / "runtime" / "executor_token_usage.jsonl").read_text().strip()
    )["status"] == "failed"


def test_pass_rejects_unresolved_scope_violation(tmp_path):
    import pytest
    project, contract = _workflow(tmp_path)
    _append_executor_attempt(project, status="scope_violation", reason="scope_violation")
    before = SR.workflow_state_sha256(project)
    with pytest.raises(ValueError, match="unsafe_executor_scope_violation"):
        SR.commit_supervisor_transition(
            project, contract, "T-001", "pass", "Review: passed",
            result_markdown="Result: passed", expected_state_sha256=before,
        )
    assert SR.workflow_state_sha256(project) == before


def test_pass_rejects_unresolved_hard_runtime_block(tmp_path):
    import pytest
    project, contract = _workflow(tmp_path)
    state_path = project / "runtime" / "workflow_state.json"
    state = json.loads(state_path.read_text())
    state["runtime_failure"] = {
        "task": "T-001", "reason": "launch_transport_failed", "retryable": False,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="active_runtime_failure"):
        SR.commit_supervisor_transition(
            project, contract, "T-001", "pass", "Review: passed",
            result_markdown="Result: passed",
        )
