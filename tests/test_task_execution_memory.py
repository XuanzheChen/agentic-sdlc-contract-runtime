"""Harness-independent task memory: immutable attempts and bounded E handoffs."""
from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path

import pytest
from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import task_execution_memory as M
import invoke_executor as E


def workspace(tmp_path):
    project = tmp_path / "psc"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "fix.py").write_text("original\n", encoding="utf-8")
    return project, repo


def completion(with_handoff=True):
    obj = {
        "schema_version": 1,
        "plan": "Change a single scoped file.",
        "coding_summary": "Updated src/fix.py and tested error handling.",
        "modified_files": ["src/fix.py"],
        "tests": ["python -m pytest tests/test_fix.py -q passed"],
        "known_risks": ["An uncommon branch is untested"],
        "unresolved_issues": ["Verify second input branch"],
    }
    if with_handoff:
        obj["handoff"] = {
            "implementation_state": "Fallback implemented but final boundary pending.",
            "decisions": ["Use single source of truth in src/fix.py"],
            "failed_approaches": ["Naive retry double-charged a spent call"],
            "next_steps": ["Add test for failed backend return"],
        }
    return obj


def result(status="completed", *, has_completion=True):
    return {
        "status": status, "reason": None if status == "completed" else "timeout",
        "completion": completion() if has_completion else None,
        "changed_paths": ["src/fix.py"], "artifact_paths": {},
        "log_path": "test-executor-run.log", "token_usage": {
            "input_tokens": 900, "output_tokens": 20, "total_tokens": 920
        }
    }


def test_archive_is_immutable_and_contains_bounded_task_handoff(tmp_path):
    project, repo = workspace(tmp_path)
    a = M.archive_attempt(project, repo, "T-007", 2, "task", "packet",
                          "quality_rework", result())
    first = Path(a["path"])
    payload = first.read_bytes()
    assert a["sha256"] == M._sha(payload)
    decoded = json.loads(payload)
    assert decoded["handoff"] if "handoff" in decoded else decoded["implementation_state"]
    assert "Naive retry" in decoded["failed_approaches"][0]
    b = M.archive_attempt(project, repo, "T-007", 2, "task", "packet",
                          "abnormal_retry", result())
    assert a["path"] != b["path"]
    assert first.read_bytes() == payload
    assert len(M._archive_files(first.parent)) == 2


def test_second_executor_gets_same_task_context_and_drift_marker(tmp_path):
    project, repo = workspace(tmp_path)
    M.archive_attempt(project, repo, "T-007", 3, "task-A", "packet-A",
                      "initial", result())
    context = M.prepare_memory(project, repo, "T-007", 3, "task-A", "packet-A")
    assert context["compatible_count"] == 1
    assert "Failed approaches [E claims]" in context["text"]
    assert "Naive retry double-charged" in context["text"]
    assert context["records"][0]["workspace_evidence_status"] == "same_recorded_file_bytes"
    (repo / "src" / "fix.py").write_text("changed\n", encoding="utf-8")
    changed = M.prepare_memory(project, repo, "T-007", 3, "task-A", "packet-A")
    assert changed["records"][0]["workspace_evidence_status"] == "changed_since_attempt"
    assert "changed_since_attempt" in changed["text"]
    assert "NOT Supervisor-accepted" in changed["text"]


def test_cross_contract_task_and_packet_drift_are_not_injected(tmp_path):
    project, repo = workspace(tmp_path)
    M.archive_attempt(project, repo, "T-007", 2, "task", "packet",
                      "initial", result())
    for task, version, packet in [("task", 3, "packet"),
                                  ("different task", 2, "packet"),
                                  ("task", 2, "updated packet")]:
        record = M.prepare_memory(project, repo, "T-007", version, task, packet)
        assert record["compatible_count"] == 0
        assert "No compatible earlier" in record["text"]
    assert M.prepare_memory(project, repo, "T-008", 2, "task", "packet")["archive_count"] == 0


def test_existing_completion_json_keeps_backward_compatibility():
    assert E._completion_validation_error(completion(False)) is None
    assert E._completion_validation_error(completion(True)) is None
    assert "handoff" in E.COMPLETION_OUTPUT_SCHEMA["properties"]
    assert "handoff" in E.COMPLETION_OUTPUT_SCHEMA["required"]
    assert "handoff" in E._executor_prompt({"id": "T-007", "text": "task"},
                                          {"text": "contract"}, "review")
    incomplete = completion()
    incomplete["handoff"]["failed_approaches"] = "wrong type"
    assert "handoff failed_approaches" in E._completion_validation_error(incomplete)
    extra = completion()
    extra["invented_grant"] = True
    assert "exactly" in E._completion_validation_error(extra)


def test_recover_abnormal_without_structured_completion(tmp_path):
    project, repo = workspace(tmp_path)
    M.archive_attempt(project, repo, "T-007", 2, "task", "packet",
                      "initial", result("failed", has_completion=False))
    memory = M.prepare_memory(project, repo, "T-007", 2, "task", "packet")
    assert "ended without a valid completion" in memory["text"]
    assert memory["compatible_count"] == 1


def test_secret_redaction_and_unsafe_changed_path_skipping(tmp_path):
    project, repo = workspace(tmp_path)
    c = result()
    c["completion"]["handoff"]["implementation_state"] = "api_key=supersecret bearer"
    c["completion"]["handoff"]["decisions"] = ["authorization: bearer abcd123"]
    c["changed_paths"] = ["../private.key", "src/fix.py", "C:\\secrets\\credentials"]
    receipt = M.archive_attempt(project, repo, "T-007", 2, "task", "packet", "initial", c)
    payload = Path(receipt["path"]).read_text(encoding="utf-8")
    assert "supersecret" not in payload
    assert "abcd123" not in payload
    assert "../private.key" not in payload
    assert "src/fix.py" in payload


def test_invalid_archive_fails_closed_before_next_model_call(tmp_path):
    project, repo = workspace(tmp_path)
    archive = M._archive_dir(project, "T-007")
    archive.mkdir(parents=True)
    (archive / "attempt-20261012-invalid.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(M.TaskMemoryError, match="unreadable"):
        M.prepare_memory(project, repo, "T-007", 3, "task", "packet")


def test_attempt_receipt_length_and_prompt_are_bounded(tmp_path):
    project, repo = workspace(tmp_path)
    for _ in range(6):
        c = result()
        c["completion"]["handoff"]["implementation_state"] = "Z" * 1200
        c["completion"]["handoff"]["next_steps"] = ["NEXT" * 70] * 8
        M.archive_attempt(project, repo, "T-007", 2, "task", "packet", "quality_rework", c)
    prepared = M.prepare_memory(project, repo, "T-007", 2, "task", "packet")
    assert len(prepared["text"]) <= M.MAX_PROMPT_CHARS
    assert len(prepared["records"]) <= M.MAX_RECORDS_IN_PROMPT
    assert prepared["archive_count"] == 6


def test_parallel_attempt_archives_are_not_overwritten(tmp_path):
    project, repo = workspace(tmp_path)
    def produce(_):
        return M.archive_attempt(project, repo, "T-007", 2,
                                 "task", "packet", "quality_rework", result())
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(produce, range(4)))
    assert len({x["path"] for x in receipts}) == 4
    assert all(Path(x["path"]).exists() for x in receipts)


def test_input_path_dispatch_archives_then_injects_memory_without_changing_harness(tmp_path, monkeypatch):
    project, repo = workspace(tmp_path)
    task = project / "developing" / "tasks" / "T-007.md"
    task.parent.mkdir(parents=True)
    task.write_text("# T-007\nAllowed Scope:\n- src/**\n", encoding="utf-8")
    contract = project / "contract" / "v3"
    contract.mkdir(parents=True)
    runtime = project / "runtime" / "runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(E, "_config", lambda _cfg: {"executor": {"adapter": "codex"}})
    observed = []
    def fake_invoke(adapter, repo_path, task_value, packet, review,
                    cfg, **kwargs):
        observed.append(kwargs)
        return result()
    monkeypatch.setattr(E, "invoke_executor", fake_invoke)
    kw = dict(repository=repo, runtime_config=runtime, project=project,
              task_path=task, contract_path=contract)
    first = E.invoke_executor_from_paths(**kw, retry_kind="initial")
    assert first["status"] == "completed"
    assert Path(first["artifact_paths"]["execution_memory_attempt"]).is_file()
    second = E.invoke_executor_from_paths(**kw, retry_kind="quality_rework")
    assert second["status"] == "completed"
    assert "task_memory" not in observed[0]
    assert observed[1]["task_memory"]["compatible_count"] == 1
    assert "Failed approaches" in observed[1]["task_memory"]["text"]
    assert "executor_packet" in second["artifact_paths"]


def test_archive_write_failure_never_reports_successful_execution(tmp_path, monkeypatch):
    project, repo = workspace(tmp_path)
    task = project / "developing" / "tasks" / "T-007.md"
    task.parent.mkdir(parents=True)
    task.write_text("# T-007\n", encoding="utf-8")
    contract = project / "contract" / "v1"
    contract.mkdir(parents=True)
    monkeypatch.setattr(E, "_config", lambda _cfg: {"executor": {"adapter": "dsh"}})
    monkeypatch.setattr(E, "invoke_executor", lambda *a, **kw: result())
    def fail_archive(*args):
        raise PermissionError("deny receipt")
    monkeypatch.setattr(E.task_execution_memory, "archive_attempt", fail_archive)
    output = E.invoke_executor_from_paths(repository=repo, runtime_config=tmp_path,
        project=project, task_path=task, contract_path=contract)
    assert output["status"] == "failed"
    assert output["reason"] == "task_execution_memory_persistence_failed"
    assert output["token_usage"]["input_tokens"] == 900
    assert any("deny receipt" in msg for msg in output["errors"])


def test_memory_prompt_provenance_never_claims_permission(tmp_path):
    project, repo = workspace(tmp_path)
    M.archive_attempt(project, repo, "T-007", 1, "task", "packet", "initial", result())
    memory = M.prepare_memory(project, repo, "T-007", 1, "task", "packet")
    prompt = E._executor_prompt({"id": "T-007", "text": "task"},
                                 {"text": "packet"}, "# S rejected",
                                 retry_kind="quality_rework",
                                 task_memory=memory)
    assert prompt.index("## Previous Executor Attempts") < prompt.index("## Previous Supervisor Review")
    assert "NOT Supervisor-accepted" in prompt
    assert prompt.index("## Previous Supervisor Review") > prompt.index("## Relevant Contract")
    assert "Current Contract, repository" in prompt
