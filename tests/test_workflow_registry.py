from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import workflow_registry as R


def create_workflow(tmp_path: Path, name: str = "demo", status: str = "ready") -> Path:
    project = tmp_path / "developing" / name
    rt = project / "runtime"
    rt.mkdir(parents=True)
    (rt / "project.json").write_text(json.dumps({
        "project_id": name, "repository": str(tmp_path), "created_at": "2026-10-09T00:00:00+00:00",
    }))
    (rt / "workflow_state.json").write_text(json.dumps({
        "status": status, "contract_version": 1, "current_task": "T-001",
        "updated_at": "2026-10-09T00:00:00+00:00",
    }))
    return project


def test_registration_status_and_closure(tmp_path):
    p = create_workflow(tmp_path)
    active, recent, _, marker = R._paths(p)
    assert R.sync_workflow(p)["lifecycle"] == "active"
    assert active.is_file()
    assert not recent.exists()
    state = p / "runtime" / "workflow_state.json"
    for status in ["blocked", "waiting_planner", "supervisor_review", "ready"]:
        value = json.loads(state.read_text())
        value["status"] = status
        state.write_text(json.dumps(value))
        assert R.sync_workflow(p)["lifecycle"] == "active"
        assert active.exists()
    raw = state.read_bytes()
    assert R.close_workflow(p, "manual closure", expected_state_sha256=hashlib.sha256(raw).hexdigest())["status"] == "closed"
    assert marker.is_file() and recent.is_file() and not active.exists()
    assert R.close_workflow(p, "duplicate")["status"] == "already_closed"
    assert R.sync_workflow(p)["lifecycle"] == "closed"
    R.reopen_for_contract_activation(p)
    assert active.exists() and not marker.exists()


def test_terminal_completion_and_recovery(tmp_path):
    p = create_workflow(tmp_path, status="workflow_passed")
    a, recent, _, _ = R._paths(p)
    assert R.sync_workflow(p)["lifecycle"] == "finished"
    assert recent.exists() and not a.exists()
    (p / "runtime" / "workflow_state.json").write_text(json.dumps({"status": "ready", "contract_version": 2}))
    assert R.sync_workflow(p)["lifecycle"] == "active"
    assert a.exists() and not recent.exists()


def test_close_rejects_live_execution_and_hash_conflict(tmp_path):
    p = create_workflow(tmp_path, status="executor_running")
    with pytest.raises(ValueError, match="running"):
        R.close_workflow(p, "abort")
    state = p / "runtime" / "workflow_state.json"
    state.write_text(json.dumps({"status": "ready"}))
    with pytest.raises(ValueError, match="conflict"):
        R.close_workflow(p, "abort", expected_state_sha256="wrong")
    (p / "runtime" / "executor-progress.json").write_text(json.dumps({
        "status": "running", "last_heartbeat_at": "2099-01-01T00:00:00+00:00"
    }))
    with pytest.raises(ValueError, match="live Executor"):
        R.close_workflow(p, "abort")


def test_reconcile_and_staging_not_indexed(tmp_path):
    p = create_workflow(tmp_path)
    assert R.sync_workflow(p)
    (p / "runtime" / "workflow_state.json").write_text(json.dumps({"status": "workflow_passed"}))
    response = R.reconcile_registry(tmp_path / "developing")
    assert response["status"] == "reconciled"
    assert R._paths(p)[1].exists()
    assert R.sync_workflow(tmp_path / "developing" / ".workflow-stage-123") is None


def test_registry_paths_outside_runtime_workflow_listing(tmp_path):
    p = create_workflow(tmp_path)
    R.sync_workflow(p)
    assert not (tmp_path / "developing" / ".psc-index").exists()
    assert (tmp_path / ".psc-index" / "developing" / "active" / "demo.json").exists()
