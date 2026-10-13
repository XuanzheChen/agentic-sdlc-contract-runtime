"""Supervisor-submitted PC evidence: offline fail-closed admission tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from test_preflight_checker import (
    PC, MCP, FakeCheckerProcess, _blocking_finding, _codex_jsonl, _report,
    make_config, make_project, make_repo, run_pc, write_runtime_config,
)
import preflight_evidence as submissions


def make_case(tmp_path, monkeypatch, owner="supervisor"):
    repository = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    evidence = repository / "audit" / "approval-note.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("Source evidence; not an authorization.\n", encoding="utf-8")
    task.write_text(task.read_text(encoding="utf-8")
                    + "\nRead-only reference: audit/approval-note.md\n", encoding="utf-8")
    state = {"contract_version": 1, "current_task": "T-001",
             "status": "ready", "execution_owner": "executor"}
    state_path = project / "runtime" / "workflow_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_sha = hashlib.sha256(state_path.read_bytes()).hexdigest()
    config = make_config(tmp_path)
    config_path = write_runtime_config(tmp_path, config)
    denied = run_pc(config, repository, project, task, contract,
                    monkeypatch=monkeypatch,
                    report_text=_report("DENY", [_blocking_finding(owner)]))
    assert denied["decision"] == "DENY"
    assert denied["admission_finalized"] is True
    return repository, project, task, contract, evidence, config, config_path, state_sha, denied


def submit(case, **kwargs):
    repository, project, task, contract, evidence, config, config_path, state_sha, denied = case
    params = {
        "repository": str(repository), "runtime_config": str(config_path),
        "project": str(project), "task": str(task), "contract": str(contract),
        "evidence_path": "audit/approval-note.md", "finding_id": "F-001",
        "reason": "Already-existing Task reference omitted from bounded bundle.",
        "expected_state_sha256": state_sha,
    }
    params.update(kwargs)
    return MCP.submit_preflight_evidence_tool(**params)


def test_submission_is_only_provenance_and_pc_must_recheck(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    repository, project, task, contract, evidence, config, _, state_sha, denied = case
    assert "audit/approval-note.md" not in [x["path"] for x in denied["evidence_manifest"]]
    original_latest = json.loads((project / "runtime/preflight/latest.json").read_text())
    response = submit(case)
    assert response["status"] == "evidence_submitted"
    assert response["requires_fresh_preflight"] is True
    assert response["admission_granted"] is False
    assert response["executor_attempt_charged"] is False
    assert json.loads((project / "runtime/preflight/latest.json").read_text()) == original_latest
    assert hashlib.sha256((project / "runtime/workflow_state.json").read_bytes()).hexdigest() == state_sha
    assert not (project / "runtime/executor_attempts.json").exists()

    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))
    next_pc = run_pc(config, repository, project, task, contract,
                     fake=fake, monkeypatch=monkeypatch)
    assert next_pc["decision"] == "ALLOW"
    assert next_pc["revalidation"]["matched"] is True
    assert next_pc["bindings"]["submitted_evidence_sha256"] != denied["bindings"]["submitted_evidence_sha256"]
    selected = {item["path"]: item for item in next_pc["evidence_manifest"]}
    assert selected["audit/approval-note.md"]["category"] == "supervisor_submitted"
    assert "Supervisor-submitted evidence" in fake.calls[0][1]["input"]
    assert "not an authorization" in fake.calls[0][1]["input"]


def test_submission_rejects_planner_finding(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch, owner="planner")
    with pytest.raises(ValueError, match="Supervisor-owned"):
        submit(case)


def test_submission_rejects_wrong_state_and_wrong_task(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="workflow_state_conflict"):
        submit(case, expected_state_sha256="0" * 64)
    with pytest.raises(ValueError, match="active Contract/Task"):
        submit(case, task=str(case[3] / "tasks.md"))


def test_submission_requires_published_ledger_matched_denial(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    project = case[1]
    latest_path = project / "runtime/preflight/latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest["admission_finalized"] = False
    latest_path.write_text(json.dumps(latest), encoding="utf-8")
    with pytest.raises(ValueError, match="finalized"):
        submit(case)


def test_submission_rejects_unreferenced_sources_and_traversal(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="explicitly referenced"):
        submit(case, evidence_path="outside/secret.py")
    for path in ("../outside.txt", "C:/Windows/win.ini", ".git/config",
                 ".agentic-sdlc/runtime.json"):
        with pytest.raises(ValueError, match="path|source"):
            submit(case, evidence_path=path)


def test_duplicate_and_tampered_submissions_fail_closed(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    submit(case)
    with pytest.raises(ValueError, match="already submitted"):
        submit(case)
    evidence = case[4]
    evidence.write_text("Tampered after receipt.\n", encoding="utf-8")
    outcome = run_pc(case[5], case[0], case[1], case[2], case[3],
                     monkeypatch=monkeypatch)
    assert outcome["decision"] == "UNKNOWN"
    assert outcome["reason"] == PC.REASON_EVIDENCE_GATHER_FAILED
    assert outcome["blocks_executor"] is True


def test_receipt_excluded_from_other_task_and_version(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    submit(case)
    repo, project, task, contract, *_rest = case
    bound = PC.gather_evidence(case[5], repo, task, contract, project=project)
    assert len(bound["submitted_evidence"]) == 1
    other_project, other_task, other_contract = make_project(tmp_path / "other")
    other = PC.gather_evidence(make_config(tmp_path / "other"), repo,
                               other_task, other_contract, project=other_project)
    assert other["submitted_evidence"] == []


def test_unfinalized_checker_allow_does_not_permit_submission(tmp_path, monkeypatch):
    case = make_case(tmp_path, monkeypatch)
    project = case[1]
    latest = project / "runtime/preflight/latest.json"
    obj = json.loads(latest.read_text(encoding="utf-8"))
    obj["decision"] = "ALLOW"
    latest.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(ValueError, match="DENY"):
        submit(case)
