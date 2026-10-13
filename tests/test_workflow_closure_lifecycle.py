from __future__ import annotations

import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import psc_runtime
import psc_mcp_server
import supervisor_runtime
import workflow_registry as registry


def workflow(tmp_path: Path, status: str = "ready") -> Path:
    project = tmp_path / "developing" / "demo"
    (project / "runtime").mkdir(parents=True)
    (project / "contract" / "v1").mkdir(parents=True)
    (project / "developing" / "tasks").mkdir(parents=True)
    (project / "runtime" / "project.json").write_text(json.dumps({
        "project_id": "demo", "repository": str(tmp_path),
        "created_at": "2026-10-09T00:00:00+00:00",
    }))
    (project / "runtime" / "workflow_state.json").write_text(json.dumps({
        "status": status, "contract_version": 1, "current_task": "T-001",
        "attempt": 0, "updated_at": "2026-10-09T00:00:00+00:00",
    }))
    (project / "contract" / "v1" / "metadata.json").write_text('{"version": 1}')
    (project / "contract" / "v1" / "tasks.md").write_text("# Tasks\n\n## T-001\nFirst.\n")
    (project / "developing" / "tasks" / "T-001.md").write_text("## T-001\nFirst.\n")
    return project


def test_discovery_preserves_recoverable_blocks_and_excludes_closed(tmp_path, monkeypatch):
    project = workflow(tmp_path, "waiting_planner")
    monkeypatch.setattr(psc_runtime, "runtime_config",
                        lambda _: {"runtime_root": str(tmp_path / "developing")})
    discovered = psc_runtime.discover(tmp_path, tmp_path / "unused.json")
    assert discovered["active"][0]["lifecycle"] == "active"
    result = registry.close_workflow(project, "archive at user request")
    assert result["status"] == "closed"
    assert result["registry_synced"] is True
    discovered = psc_runtime.discover(tmp_path, tmp_path / "unused.json")
    assert discovered["active"] == []
    assert discovered["projects"][0]["lifecycle"] == "closed"
    assert registry._paths(project)[1].exists()
    assert not registry._paths(project)[0].exists()


def test_close_is_idempotent_and_never_rewrites_state_or_evidence(tmp_path):
    project = workflow(tmp_path)
    state = project / "runtime" / "workflow_state.json"
    original = state.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    result = registry.close_workflow(project, "manual", expected_state_sha256=expected)
    assert result["registry_synced"] is True
    assert state.read_bytes() == original
    assert registry.close_workflow(project, "duplicate")["status"] == "already_closed"
    with pytest.raises(ValueError, match="workflow_closed"):
        registry.require_open(project)
    registry.reopen_for_contract_activation(project)
    registry.require_open(project)
    assert registry._paths(project)[0].exists()


def test_corrupt_marker_fails_closed_for_discovery_and_dispatch(tmp_path, monkeypatch):
    project = workflow(tmp_path)
    (project / "runtime" / "workflow_closure.json").write_text('{"closed": false}')
    monkeypatch.setattr(psc_runtime, "runtime_config",
                        lambda _: {"runtime_root": str(tmp_path / "developing")})
    discovered = psc_runtime.discover(tmp_path, tmp_path / "unused.json")
    assert discovered["active"] == []
    assert discovered["projects"][0]["lifecycle"] == "closure_invalid"
    blocked = psc_mcp_server._closed_workflow_result(project)
    assert blocked["status"] == "workflow_closed"
    assert blocked["reason"] == "workflow_closure_invalid"
    assert blocked["executor_attempt_charged"] is False


def test_closure_blocks_pc_executor_and_supervisor_before_mutations(tmp_path):
    project = workflow(tmp_path)
    repo = str(tmp_path)
    contract = str(project / "contract" / "v1")
    task = str(project / "developing" / "tasks" / "T-001.md")
    config = tmp_path / "runtime.json"
    config.write_text("{}")
    registry.close_workflow(project, "user archive")
    pc = psc_mcp_server.preflight_check_tool(repo, str(config), str(project), task, contract)
    assert pc["status"] == "workflow_closed"
    assert pc["blocks_executor"] and pc["executor_attempt_charged"] is False
    executor = psc_mcp_server._invoke_executor_impl(repo, str(config), str(project), task, contract)
    assert executor["status"] == "workflow_closed"
    assert executor["executor_attempt_charged"] is False
    snapshot = supervisor_runtime.supervisor_snapshot(project)
    assert snapshot["lifecycle"] == "closed"
    assert snapshot["workflow_closure"]["reason"] == "user archive"
    with pytest.raises(ValueError, match="workflow_closed"):
        supervisor_runtime.commit_supervisor_transition(
            project, Path(contract), "T-001", "quality_rework", "recheck")
    assert not (project / "developing" / "artifacts" / "T-001" / "review.md").exists()


def test_index_failure_report_is_explicit_and_reconcile_repairs(tmp_path, monkeypatch):
    project = workflow(tmp_path)
    real_sync = registry.sync_workflow
    def fail(_):
        raise OSError("injected registry failure")
    monkeypatch.setattr(registry, "sync_workflow", fail)
    result = registry.close_workflow(project, "archive")
    assert result["status"] == "closed_registry_sync_failed"
    assert result["registry_synced"] is False
    assert (project / "runtime" / "workflow_closure.json").exists()
    monkeypatch.setattr(registry, "sync_workflow", real_sync)
    assert registry.reconcile_registry(tmp_path / "developing")["status"] == "reconciled"
    assert registry._paths(project)[1].exists()
    assert registry.close_workflow(project, "again")["registry_synced"] is True


def test_close_waits_for_executor_dispatch_lock(tmp_path):
    project = workflow(tmp_path)
    ready, release = threading.Event(), threading.Event()
    def hold_lock():
        with registry._locked(project / "runtime" / "executor-dispatch.lock"):
            ready.set()
            release.wait(5)
    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        assert ready.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(registry.close_workflow, project, "after executor")
            assert not pending.done()
            assert not (project / "runtime" / "workflow_closure.json").exists()
            release.set()
            assert pending.result(timeout=8)["status"] == "closed"
    finally:
        release.set()
        thread.join(timeout=5)
