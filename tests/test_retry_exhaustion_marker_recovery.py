"""Native P0 recovery of lost Executor exhaustion markers, without E dispatch."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import sys
from pathlib import Path

import pytest
from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import psc_runtime as RT
import supervisor_runtime as SR
import psc_mcp_server as MCP
import retry_exhaustion_recovery as R


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _fixture(tmp_path, *, quality=3, abnormal=0, blocked=True, stage=None, owner="executor"):
    project = tmp_path / "workflow"
    contract = project / "contract" / "v2"
    contract.mkdir(parents=True)
    _write_json(contract / "metadata.json", {
        "schema_version": 1, "version": 2, "status": "approved",
        "workflow_policy": {"restart": "all"}
    })
    (contract / "tasks.md").write_text(
        "# Tasks\n\n## T-001\nTask one\n\n## T-002\nTask two\n", encoding="utf-8")
    state = {
        "schema_version": 1, "contract_version": 2,
        "current_task": "T-001", "last_completed_task": None,
        "status": "blocked" if blocked else "ready",
        "last_stage": stage or ("supervisor_blocked" if blocked else "supervisor_quality_rework"),
        "execution_owner": owner, "execution_owner_history": [],
        "updated_at": "2026-10-11T00:00:00+00:00",
    }
    _write_json(project / "runtime" / "workflow_state.json", state)
    _write_json(project / "runtime" / "executor_attempts.json", {
        "schema_version": 2,
        "legacy_unclassified_attempts": {},
        "tasks": {
            "v2:T-001": {
                "execution_round": 4,
                "initial_attempted": True,
                "quality_retries_used": quality,
                "abnormal_retries_used": abnormal,
            },
            "v2:T-002": {
                "execution_round": 1,
                "initial_attempted": False,
                "quality_retries_used": 0,
                "abnormal_retries_used": 0,
            },
        },
    })
    return project, contract


def _state(project):
    return RT.load_json(project / "runtime" / "workflow_state.json")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recover(project, state_sha=None, ledger_sha=None):
    state_path = project / "runtime" / "workflow_state.json"
    ledger_path = project / "runtime" / "executor_attempts.json"
    return RT.recover_retry_exhaustion_marker(
        project, state_sha or _sha(state_path),
        ledger_sha if ledger_sha is not None else _sha(ledger_path)
    )


def test_missing_quality_marker_recovered_and_scoped_supervisor_handoff(tmp_path):
    project, _ = _fixture(tmp_path)
    state_file = project / "runtime" / "workflow_state.json"
    attempts = project / "runtime" / "executor_attempts.json"
    original_ledger = attempts.read_bytes()
    before = _sha(state_file)
    result = _recover(project)
    assert result["status"] == "retry_exhaustion_marker_recovered"
    assert result["previous_state_sha256"] == before
    assert result["workflow_state_sha256"] == _sha(state_file)
    assert result["task"] == "T-001"
    assert result["budget"] == "quality_rework" and result["used"] == 3
    assert result["execution_round"] == 4
    assert result["retry_counters_changed"] is False
    assert attempts.read_bytes() == original_ledger
    marker = _state(project)["retry_exhaustion"]
    assert marker["recovered_from_state_sha256"] == before
    assert marker["source_executor_attempts_sha256"] == _sha(attempts)
    assert _state(project)["status"] == "blocked"
    assert _state(project)["execution_owner"] == "executor"

    takeover = RT.resolve_retry_exhaustion(project, "switch-to-supervisor-for-current-task")
    assert takeover["execution_owner"] == "supervisor"
    assert takeover["execution_owner_scope"] == "current_task"
    assert _state(project)["scoped_supervisor_takeover"]["task"] == "T-001"
    assert _state(project)["retry_exhaustion_history"][-1]["recovered_from_state_sha256"] == before
    assert attempts.read_bytes() == original_ledger


def test_normal_review_rejection_publishes_quality_exhaustion_immediately(tmp_path):
    project, contract = _fixture(tmp_path, blocked=False)
    review = SR.commit_supervisor_transition(
        project, contract, "T-001", "quality_rework", "# Review\nFails acceptance.")
    state = _state(project)
    assert review["workflow_status"] == "blocked"
    assert state["retry_exhaustion"]["budget"] == "quality_rework"
    assert state["retry_exhaustion"]["used"] == 3
    assert state["last_stage"] == "executor_retry_budget_exhausted"
    assert RT.resolve_retry_exhaustion(
        project, "switch-to-supervisor-for-current-task")["execution_owner"] == "supervisor"


def test_review_blocked_publishes_marker_when_exhausted(tmp_path):
    project, contract = _fixture(tmp_path, blocked=False)
    SR.commit_supervisor_transition(project, contract, "T-001", "blocked", "# Review\nStill blocked.")
    assert _state(project)["retry_exhaustion"]["budget"] == "quality_rework"


def test_remaining_budget_does_not_publish_marker(tmp_path):
    project, contract = _fixture(tmp_path, quality=2, blocked=False)
    review = SR.commit_supervisor_transition(
        project, contract, "T-001", "quality_rework", "# Review\nOne retry remains.")
    assert review["workflow_status"] == "ready"
    assert "retry_exhaustion" not in _state(project)


def test_recovery_requires_blocked_and_previously_attempted_active_task(tmp_path):
    project, _ = _fixture(tmp_path, blocked=False)
    with pytest.raises(ValueError, match="requires_blocked"):
        _recover(project)
    assert "retry_exhaustion" not in _state(project)


@pytest.mark.parametrize("quality,abnormal", [(0,0), (2,2)])
def test_recovery_without_exhausted_budget_is_rejected(tmp_path, quality, abnormal):
    project, _ = _fixture(tmp_path, quality=quality, abnormal=abnormal)
    initial = (project / "runtime" / "workflow_state.json").read_bytes()
    with pytest.raises(ValueError, match="no_exhausted_executor_retry_budget"):
        _recover(project)
    assert (project / "runtime" / "workflow_state.json").read_bytes() == initial


def test_abnormal_exhaustion_recovered_with_independent_budget(tmp_path):
    project, _ = _fixture(tmp_path, quality=1, abnormal=3)
    old = (project / "runtime" / "executor_attempts.json").read_bytes()
    result = _recover(project)
    assert result["budget"] == "abnormal_retry"
    assert result["used"] == 3
    assert (project / "runtime" / "executor_attempts.json").read_bytes() == old


def test_stale_state_or_ledger_sha_fails_without_mutations(tmp_path):
    project, _ = _fixture(tmp_path)
    sf = project / "runtime" / "workflow_state.json"
    before = sf.read_bytes()
    with pytest.raises(ValueError, match="workflow_state_conflict"):
        _recover(project, state_sha="0"*64)
    with pytest.raises(ValueError, match="executor_attempts_conflict"):
        _recover(project, ledger_sha="f"*64)
    assert sf.read_bytes() == before


def test_repeat_recovery_fails_closed_without_duplicate_history(tmp_path):
    project, _ = _fixture(tmp_path)
    old_state = _sha(project / "runtime" / "workflow_state.json")
    _recover(project)
    current = _state(project)
    with pytest.raises(ValueError, match="workflow_state_conflict"):
        _recover(project, state_sha=old_state)
    with pytest.raises(ValueError, match="already_present_or_malformed"):
        _recover(project)
    assert _state(project) == current


@pytest.mark.parametrize("failure", [
    "different_task", "different_version", "invalid_contract",
    "unrelated_block", "runtime_failure", "supervisor_owner", "unreadable_ledger",
    "missing_attempt", "malformed_attempt", "already_has_marker",
])
def test_recovery_fails_closed_when_evidence_is_not_authoritative(tmp_path, failure):
    project, contract = _fixture(tmp_path)
    state = _state(project)
    ledger = RT.load_json(project / "runtime" / "executor_attempts.json")
    if failure == "different_task":
        state["current_task"] = "T-002"
    elif failure == "different_version":
        state["contract_version"] = 3
    elif failure == "invalid_contract":
        meta = RT.load_json(contract / "metadata.json")
        meta["status"] = "draft"
        _write_json(contract / "metadata.json", meta)
    elif failure == "unrelated_block":
        state["last_stage"] = "planner_preflight_denied"
    elif failure == "runtime_failure":
        state["runtime_failure"] = {"reason": "launch_transport_failed", "retryable": False}
    elif failure == "supervisor_owner":
        state["execution_owner"] = "supervisor"
    elif failure == "unreadable_ledger":
        (project / "runtime" / "executor_attempts.json").write_text("{bad",encoding="utf-8")
    elif failure == "missing_attempt":
        del ledger["tasks"]["v2:T-001"]
        _write_json(project / "runtime" / "executor_attempts.json", ledger)
    elif failure == "malformed_attempt":
        ledger["tasks"]["v2:T-001"]["quality_retries_used"] = "three"
        _write_json(project / "runtime" / "executor_attempts.json", ledger)
    else:
        state["retry_exhaustion"] = {}
    _write_json(project / "runtime" / "workflow_state.json", state)
    before = (project / "runtime" / "workflow_state.json").read_bytes()
    with pytest.raises((ValueError, OSError, json.JSONDecodeError)):
        _recover(project)
    assert (project / "runtime" / "workflow_state.json").read_bytes() == before


def test_generic_owner_change_cannot_bypass_lost_exhaustion_marker(tmp_path):
    project, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match="resolve-retry-exhaustion"):
        RT.set_execution_owner(project, "supervisor", "bypass missing marker")
    assert _state(project)["execution_owner"] == "executor"


def test_bound_marker_blocks_stale_retry_ledger_at_resolution(tmp_path):
    project, _ = _fixture(tmp_path)
    _recover(project)
    ledger_path = project / "runtime" / "executor_attempts.json"
    value = RT.load_json(ledger_path)
    value["tasks"]["v2:T-001"]["abnormal_retries_used"] = 1
    _write_json(ledger_path,value)
    with pytest.raises(ValueError, match="marker_authority_conflict"):
        RT.resolve_retry_exhaustion(project, "switch-to-supervisor-for-current-task")
    assert _state(project)["status"] == "blocked"
    assert _state(project)["execution_owner"] == "executor"


def test_recovery_failure_before_atomic_state_replace_preserves_state_and_ledger(tmp_path, monkeypatch):
    project, _ = _fixture(tmp_path)
    sf = project / "runtime" / "workflow_state.json"
    attempts = project / "runtime" / "executor_attempts.json"
    before, old_attempts = sf.read_bytes(), attempts.read_bytes()
    def fail(*args, **kwargs):
        raise PermissionError("simulated denied state publication")
    monkeypatch.setattr(RT, "dump_json", fail)
    with pytest.raises(PermissionError, match="simulated"):
        _recover(project)
    assert sf.read_bytes() == before
    assert attempts.read_bytes() == old_attempts


def test_parallel_native_recoveries_only_one_commits(tmp_path):
    project, _ = _fixture(tmp_path)
    sha = _sha(project / "runtime" / "workflow_state.json")
    ledger_sha = _sha(project / "runtime" / "executor_attempts.json")
    def attempt(_):
        try:
            return _recover(project, state_sha=sha, ledger_sha=ledger_sha)["status"]
        except ValueError:
            return "blocked"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(attempt, range(3)))
    assert results.count("retry_exhaustion_marker_recovered") == 1
    assert results.count("blocked") == 2


def test_real_mcp_sdk_exposes_recovery_tool(tmp_path):
    project, _ = _fixture(tmp_path)
    server = MCP.build_server()
    arguments = {
        "project": str(project),
        "expected_state_sha256": _sha(project / "runtime" / "workflow_state.json"),
        "expected_ledger_sha256": _sha(project / "runtime" / "executor_attempts.json"),
    }
    async def invoke():
        return await server.call_tool("psc_recover_retry_exhaustion_marker", arguments)
    result = asyncio.run(invoke())
    assert result.structured_content["status"] == "retry_exhaustion_marker_recovered"
    assert _state(project)["status"] == "blocked"
    assert _state(project)["execution_owner"] == "executor"
