"""Offline failure injection for native PSC MCP diagnostics and Supervisor journal."""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

from conftest import SKILL_ROOT
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import mcp_diagnostics as D
import psc_mcp_server as MCP
import supervisor_runtime as SR
from test_supervisor_runtime import _workflow


def _wrapped_transition():
    @D.guard_tool("psc_commit_supervisor_transition")
    def wrapped(project: str, contract: str, task_id: str, decision: str,
                review_markdown: str, expected_state_sha256: str | None = None):
        return SR.commit_supervisor_transition(
            Path(project), Path(contract), task_id, decision, review_markdown,
            expected_state_sha256=expected_state_sha256,
        )
    return wrapped


def test_stale_hash_returns_structured_diagnostic_and_does_not_change_state(tmp_path):
    project, contract = _workflow(tmp_path)
    before = SR.workflow_state_sha256(project)
    result = _wrapped_transition()(str(project), str(contract), "T-001",
                                   "quality_rework", "# review", "deadbeef")
    assert result["status"] == "tool_error"
    assert result["error_code"] == "STATE_SHA_CONFLICT"
    assert result["failure_phase"] == "validate_state"
    assert result["mutation_status"] == "no_monitored_mutation"
    assert result["retryable"] is False
    assert result["executor_attempt_charged"] is False
    assert SR.workflow_state_sha256(project) == before
    assert result["diagnostic_log_path"]
    records = [json.loads(line) for line in Path(result["diagnostic_log_path"]).read_text(encoding="utf-8").splitlines()]
    assert records[-1]["id"] == result["error_id"]
    assert "Traceback" in records[-1]["traceback"]


def test_successful_commit_writes_durable_transaction_journal(tmp_path):
    project, contract = _workflow(tmp_path)
    result = _wrapped_transition()(str(project), str(contract), "T-001", "quality_rework", "Review pass")
    assert result["status"] == "transition_committed"
    assert result["transaction_id"].startswith("st-")
    detail = D.transition_diagnostics(project)
    record = detail["transactions"][0]
    assert record["phase"] == "committed"
    assert record["state_sha_target"] == detail["workflow_state_sha256"]
    assert record["applied_paths"][-1] == "runtime/workflow_state.json"
    assert detail["errors"] == []


def test_partial_replace_failure_is_audited_and_not_replayed(tmp_path, monkeypatch):
    project, contract = _workflow(tmp_path)
    before = SR.workflow_state_sha256(project)
    original_replace = SR.os.replace
    def fail_final_state(src, dest):
        if Path(dest).name == "workflow_state.json":
            raise PermissionError("simulated ACL fail")
        return original_replace(src, dest)
    monkeypatch.setattr(SR.os, "replace", fail_final_state)
    result = _wrapped_transition()(str(project), str(contract), "T-001",
        "quality_rework", "Review partially saved", expected_state_sha256=before)
    assert result["status"] == "tool_error"
    assert result["error_code"] == "FILESYSTEM_ACCESS_DENIED"
    assert result["mutation_status"] == "partial_mutation"
    assert result["retryable"] is False
    assert result["state_sha_before"] == result["state_sha_after"] == before
    assert (project/"developing"/"artifacts"/"T-001"/"review.md").is_file()
    assert not (project/"runtime"/"executor_attempts.json").exists()
    info = D.transition_diagnostics(project)
    assert info["transactions"][0]["phase"] == "partial"
    assert info["errors"][0]["id"] == result["error_id"]


def test_before_replacement_failure_does_not_modify_review_or_state(tmp_path, monkeypatch):
    project, contract = _workflow(tmp_path)
    state_sha = SR.workflow_state_sha256(project)
    original = SR._stage_text
    def fail_stage(path, text):
        if Path(path).name == "workflow_state.json":
            raise PermissionError("cannot stage state")
        return original(path, text)
    monkeypatch.setattr(SR, "_stage_text", fail_stage)
    result = _wrapped_transition()(str(project),str(contract),"T-001","quality_rework","no change")
    assert result["status"] == "tool_error"
    assert result["mutation_status"] == "no_monitored_mutation"
    assert result["state_sha_before"] == result["state_sha_after"] == state_sha
    assert not (project/"developing"/"artifacts"/"T-001"/"review.md").exists()
    assert not list((project/"developing"/"artifacts"/"T-001").glob("review.md.*"))


def test_diagnostic_fallback_is_configurable_without_project(tmp_path,monkeypatch):
    monkeypatch.setenv("PSC_MCP_DIAGNOSTIC_DIR",str(tmp_path/"diag"))
    @D.guard_tool("psc_ensure_executor_ready")
    def fails(repository: str):
        raise FileNotFoundError("required runtime missing")
    result = fails("not-a-repo")
    assert result["status"] == "tool_error"
    assert result["error_code"] == "FILE_NOT_FOUND"
    assert Path(result["diagnostic_log_path"]).parent == (tmp_path/"diag")


def test_diagnostics_never_echo_raw_review_text_or_credentials(tmp_path):
    project, _ = _workflow(tmp_path)
    secret_review = "SECRET_REVIEW_PAYLOAD_SHOULD_NEVER_BE_LOGGED"
    @D.guard_tool("psc_commit_supervisor_transition")
    def fails(project: str, task_id: str, review_markdown: str):
        raise ValueError("api_key=TOPSECRETVALUE invalid")
    result = fails(str(project),"T-001",secret_review)
    contents = Path(result["diagnostic_log_path"]).read_text(encoding="utf-8")
    assert secret_review not in contents
    assert "TOPSECRETVALUE" not in contents
    assert secret_review not in json.dumps(result)
    assert "TOPSECRETVALUE" not in json.dumps(result)


def test_guard_preserves_sync_and_async_signatures(tmp_path):
    @D.guard_tool("psc_progress_probe")
    async def async_func(project: str, ctx: object) -> dict:
        raise RuntimeError("progress transport")
    @D.guard_tool("psc_supervisor_snapshot")
    def sync_func(project: str) -> dict:
        raise RuntimeError("snapshot")
    assert inspect.iscoroutinefunction(async_func)
    assert list(inspect.signature(async_func).parameters) == ["project", "ctx"]
    assert list(inspect.signature(sync_func).parameters) == ["project"]
    assert asyncio.run(async_func(str(tmp_path), None))["status"] == "tool_error"
    assert sync_func(str(tmp_path))["status"] == "tool_error"


def test_no_tool_failure_charges_executor_budget(tmp_path):
    project, contract = _workflow(tmp_path)
    result = _wrapped_transition()(str(project),str(contract),"T-001",
                                   "quality_rework","review","stale")
    assert result["executor_attempt_charged"] is False
    assert not (project/"runtime"/"executor_attempts.json").exists()


def test_diagnostics_tool_is_read_only(tmp_path):
    project, contract = _workflow(tmp_path)
    before = SR.workflow_state_sha256(project)
    info = D.transition_diagnostics(project,limit=2)
    assert info["status"] == "ok"
    assert info["workflow_state_sha256"] == before
    assert SR.workflow_state_sha256(project) == before


def test_error_classifier_handles_permission_conflict_and_timeout():
    assert D.classify_exception(PermissionError("x"),"psc_x")[0] == "FILESYSTEM_ACCESS_DENIED"
    assert D.classify_exception(ValueError("workflow_state_conflict"),"psc_x")[0] == "STATE_SHA_CONFLICT"
    assert D.classify_exception(TimeoutError("x"),"psc_x")[0] == "TOOL_TIMEOUT"


def test_server_exports_diagnostic_tool_and_guarded_tools():
    server = MCP.build_server()
    # SDK registration retains its original tool signatures.
    assert server is not None
    tools = getattr(server, "_tool_manager", None)
    # Depending on MCP SDK version the tool registry might be internal;
    # at minimum ensure all decorators are included in the registered source.
    source = (SKILL_ROOT/"scripts"/"psc_mcp_server.py").read_text(encoding="utf-8")
    for name in ("psc_invoke_executor","psc_preflight_check",
                 "psc_commit_supervisor_transition","psc_transition_diagnostics",
                 "psc_supervisor_snapshot","psc_close_workflow"):
        assert f'@mcp_diagnostics.guard_tool("{name}")' in source


def test_real_mcp_sdk_call_preserves_diagnostic_payload(tmp_path):
    project, contract = _workflow(tmp_path)
    server = MCP.build_server()
    async def invoke():
        return await server.call_tool("psc_commit_supervisor_transition", {
            "project": str(project), "contract": str(contract),
            "task_id": "T-001", "decision": "quality_rework",
            "review_markdown": "# Review", "expected_state_sha256": "stale-hash",
        })
    result = asyncio.run(invoke())
    assert result.structured_content["status"] == "tool_error"
    assert result.structured_content["error_code"] == "STATE_SHA_CONFLICT"
    assert Path(result.structured_content["diagnostic_log_path"]).is_file()
    assert result.structured_content["executor_attempt_charged"] is False


def test_failure_after_state_commit_reports_committed_state(tmp_path, monkeypatch):
    project, contract = _workflow(tmp_path)
    original = SR._write_transaction_journal
    def fail_post_state(path, record):
        if record.get("phase") == "applying" and (
                "runtime/workflow_state.json" in record.get("applied_paths", [])):
            raise OSError("simulated journal update failure")
        return original(path, record)
    monkeypatch.setattr(SR, "_write_transaction_journal", fail_post_state)
    before = SR.workflow_state_sha256(project)
    result = _wrapped_transition()(str(project),str(contract),
        "T-001","quality_rework","# Review",expected_state_sha256=before)
    assert result["status"] == "tool_error"
    assert result["mutation_status"] == "committed_state"
    assert result["retryable"] is False
    assert result["state_sha_after"] != before
    assert D.transition_diagnostics(project)["transactions"][0]["phase"] == "partial"


def test_old_partial_transaction_is_not_misattributed_to_new_validation_error(tmp_path, monkeypatch):
    project, contract = _workflow(tmp_path)
    root = project / "runtime" / "supervisor-transactions"
    root.mkdir(parents=True)
    (root / "st-previous.json").write_text(json.dumps({
        "id": "st-previous", "phase": "partial",
        "state_sha_before": "before", "state_sha_target": "after"}),encoding="utf-8")
    result = _wrapped_transition()(str(project),str(contract),
        "T-001","quality_rework","review",expected_state_sha256="stale")
    assert result["error_code"] == "STATE_SHA_CONFLICT"
    assert result["mutation_status"] == "no_monitored_mutation"
