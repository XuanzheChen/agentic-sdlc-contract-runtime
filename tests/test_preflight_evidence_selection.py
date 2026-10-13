"""Proactive Supervisor choice of Task-scoped PC evidence (no provider calls)."""
from __future__ import annotations

import hashlib
import json

import pytest

from test_preflight_checker import (
    PC, MCP, _report, make_config, make_project, make_repo,
    run_pc, write_runtime_config,
)
import preflight_selection as selection


def setup_case(tmp_path):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    (repo / "audit").mkdir()
    for name in ("primary.md", "secondary.md", "configured.md"):
        (repo / "audit" / name).write_text(f"Existing evidence: {name}\n", encoding="utf-8")
    config = make_config(tmp_path, preflight={
        "include_files": ["audit/configured.md"], "max_files": 12,
    })
    config_path = write_runtime_config(tmp_path, config)
    state = {"contract_version": 1, "current_task": "T-001",
             "status": "ready", "execution_owner": "executor"}
    state_path = project / "runtime" / "workflow_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_sha = hashlib.sha256(state_path.read_bytes()).hexdigest()
    return repo, project, task, contract, config, config_path, state_sha


def update(case, *, include=None, exclude=None, basis="Goal: implement one.",
           reason="Prioritize primary task-related evidence.", expected=None, state=None):
    repo, project, task, contract, config, config_path, state_sha = case
    return MCP.set_preflight_evidence_selection_tool(
        str(repo), str(config_path), str(project), str(task), str(contract),
        ["audit/primary.md"] if include is None else include,
        [] if exclude is None else exclude,
        basis, reason,
        state_sha if state is None else state,
        expected,
    )


def test_proactive_selection_without_prior_pc_and_independent_recheck(tmp_path, monkeypatch):
    case = setup_case(tmp_path)
    repo, project, task, contract, config, _, state_sha = case
    previous = PC.gather_evidence(config, repo, task, contract, project=project)
    assert [x["path"] for x in previous["files"]] == [
        "audit/configured.md", "src/example.py"
    ]
    assert not (project / "runtime/preflight/latest.json").exists()
    result = update(case, exclude=["audit/configured.md"])
    assert result["status"] == "evidence_selection_updated"
    assert result["admission_granted"] is False
    assert result["executor_attempt_charged"] is False
    assert result["requires_fresh_preflight"]
    assert hashlib.sha256((project / "runtime/workflow_state.json").read_bytes()).hexdigest() == state_sha
    assert not (project / "runtime/executor_attempts.json").exists()
    assert not (project / "runtime/preflight/latest.json").exists()

    new = PC.gather_evidence(config, repo, task, contract, project=project)
    assert [x["path"] for x in new["files"]] == ["audit/primary.md", "src/example.py"]
    assert new["bindings"]["evidence_selection_sha256"] != previous["bindings"]["evidence_selection_sha256"]
    assert new["evidence_selection"]["exclude"][0]["path"] == "audit/configured.md"
    verdict = run_pc(config, repo, project, task, contract, monkeypatch=monkeypatch,
                     report_text=_report("ALLOW"))
    assert verdict["decision"] == "ALLOW"
    assert verdict["revalidation"]["matched"] is True


def test_get_and_replace_and_remove_lists_keep_immutable_history(tmp_path):
    case = setup_case(tmp_path)
    repo, project, task, contract, *_ = case
    first = update(case, include=["audit/secondary.md", "audit/primary.md"])
    snapshot = MCP.get_preflight_evidence_selection_tool(
        str(repo), str(project), str(task), str(contract))
    assert snapshot["revision_sha256"] == first["selection_sha256"]
    assert [x["path"] for x in snapshot["include"]] == [
        "audit/secondary.md", "audit/primary.md"
    ]
    second = update(case, include=["audit/primary.md"], expected=first["selection_sha256"])
    assert second["previous_selection_sha256"] == first["selection_sha256"]
    assert second["selection_sha256"] != first["selection_sha256"]
    assert [x["path"] for x in snapshot["include"]] == [
        "audit/secondary.md", "audit/primary.md"
    ]
    assert len(list((project / "runtime/preflight/evidence_selection/v1/T-001").glob("revision-*.json"))) == 2
    reset = update(case, include=[], exclude=[], expected=second["selection_sha256"])
    assert reset["include"] == [] and reset["exclude"] == []


def test_compare_and_swap_rejects_stale_snapshot(tmp_path):
    case = setup_case(tmp_path)
    first = update(case)
    with pytest.raises(ValueError, match="evidence_selection_conflict"):
        update(case, include=["audit/secondary.md"], expected=None)
    with pytest.raises(ValueError, match="workflow_state_conflict"):
        update(case, state="0" * 64, expected=first["selection_sha256"])


def test_source_tampering_blocks_pc_but_supervisor_can_repair_selection(tmp_path, monkeypatch):
    case = setup_case(tmp_path)
    first = update(case)
    (case[0] / "audit" / "primary.md").write_text("changed after selection\n", encoding="utf-8")
    denied = run_pc(case[4], case[0], case[1], case[2], case[3],
                    monkeypatch=monkeypatch)
    assert denied["decision"] == "UNKNOWN"
    assert denied["reason"] == PC.REASON_EVIDENCE_GATHER_FAILED
    assert denied["blocks_executor"] is True
    repaired = update(case, expected=first["selection_sha256"],
                      reason="Refresh source SHA for current audited Task.")
    assert repaired["selection_sha256"] != first["selection_sha256"]


def test_exclusion_limits_and_task_basis(tmp_path):
    case = setup_case(tmp_path)
    with pytest.raises(ValueError, match="Allowed Scope"):
        update(case, exclude=["src/example.py"])
    with pytest.raises(ValueError, match="optional configured"):
        update(case, exclude=["audit/secondary.md"])
    with pytest.raises(ValueError, match="exact current"):
        update(case, basis="Something not in the Contract")
    with pytest.raises(ValueError, match="overlap"):
        update(case, include=["audit/configured.md"], exclude=["audit/configured.md"])
    with pytest.raises(ValueError, match="unique"):
        update(case, include=["audit/primary.md", "audit/primary.md"])


def test_secrets_and_escape_are_disallowed(tmp_path):
    case = setup_case(tmp_path)
    for name in ("../escape.txt", ".git/config", ".env", "audit/../../etc/passwd"):
        with pytest.raises(ValueError, match="path|source"):
            update(case, include=[name])


def test_revision_pointer_tampering_blocks_checker(tmp_path, monkeypatch):
    case = setup_case(tmp_path)
    update(case)
    pointer_path = case[1] / "runtime/preflight/evidence_selection/v1/T-001/latest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["revision_sha256"] = "0" * 64
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    denied = run_pc(case[4], case[0], case[1], case[2], case[3],
                    monkeypatch=monkeypatch)
    assert denied["status"] == "unknown"
    assert denied["reason"] == PC.REASON_EVIDENCE_GATHER_FAILED


def test_selection_does_not_leak_to_other_project(tmp_path):
    case = setup_case(tmp_path)
    update(case)
    repo, project, task, contract, *_ = case
    other, other_task, other_contract = make_project(tmp_path / "other")
    other_bundle = PC.gather_evidence(case[4], repo, other_task, other_contract, project=other)
    assert other_bundle["evidence_selection"]["include"] == []


def test_manual_priority_wins_bounded_cap_without_changing_contract(tmp_path):
    case = setup_case(tmp_path)
    # Selecting one file outranks default candidates with only one slot.
    config = dict(case[4])
    config["preflight"] = {"include_files": ["audit/configured.md"], "max_files": 1}
    case[5].write_text(json.dumps(config), encoding="utf-8")
    response = update(case)
    assert response["admission_granted"] is False
    bundle = PC.gather_evidence(config, case[0], case[2], case[3], project=case[1])
    assert [x["path"] for x in bundle["files"]] == ["audit/primary.md"]
    assert "src/example.py" in [x["path"] for x in bundle["omitted_evidence"]]


def test_priority_order_controls_bounded_selection(tmp_path):
    case = setup_case(tmp_path)
    config = dict(case[4])
    config["preflight"] = {"include_files": [], "max_files": 1}
    case[5].write_text(json.dumps(config), encoding="utf-8")
    first = update(case, include=["audit/secondary.md"], reason="Select secondary first.")
    current = PC.gather_evidence(config, case[0], case[2], case[3], project=case[1])
    assert [x["path"] for x in current["files"]] == ["audit/secondary.md"]
    # A filename that appears in the Task text must not defeat an explicit
    # Supervisor priority revision.
    task = case[2]
    task.write_text(task.read_text(encoding="utf-8") +
                    "\nReference: audit/primary.md\n", encoding="utf-8")
    # Task itself changed, so the old selection is correctly stale.
    with pytest.raises(ValueError, match="Task/Contract"):
        PC.gather_evidence(config, case[0], task, case[3], project=case[1])


def test_missing_latest_pointer_does_not_drop_binding_silently(tmp_path, monkeypatch):
    case = setup_case(tmp_path)
    update(case)
    pointer_path = case[1] / "runtime/preflight/evidence_selection/v1/T-001/latest.json"
    pointer_path.unlink()
    denied = run_pc(case[4], case[0], case[1], case[2], case[3],
                    monkeypatch=monkeypatch)
    assert denied["decision"] == "UNKNOWN"
    assert denied["reason"] == PC.REASON_EVIDENCE_GATHER_FAILED


def test_selection_revision_change_invalidates_previous_pc_bindings(tmp_path):
    case = setup_case(tmp_path)
    first = update(case)
    old_bundle = PC.gather_evidence(case[4], case[0], case[2], case[3], project=case[1])
    update(case, include=["audit/secondary.md"], expected=first["selection_sha256"])
    fresh = PC.revalidate_evidence(
        old_bundle["bindings"], case[4], case[0], case[2], case[3], project=case[1])
    assert fresh["matched"] is False
    assert "evidence_selection_sha256" in fresh["changed"]
