from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from conftest import SKILL_ROOT


sys.path.insert(0, str(SKILL_ROOT / "scripts"))
_SPEC = importlib.util.spec_from_file_location(
    "psc_executor_mcp", SKILL_ROOT / "scripts" / "psc_mcp_server.py"
)
assert _SPEC and _SPEC.loader
MCP = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MCP)


def test_compact_executor_result_excludes_large_transcript_fields():
    result = {
        "status": "completed",
        "reason": None,
        "exit_code": 0,
        "stdout": "very large executor stdout",
        "stderr": "very large executor stderr",
        "completion": {"plan": "large structured completion"},
        "changed_paths": ["src/example.py"],
        "scope_violations": [],
        "artifact_paths": {"plan": "plan.md", "coding": "coding.md"},
        "log_path": "executor.log",
        "executor_config_sha256": "abc123",
        "errors": [],
    }

    compact = MCP.compact_executor_result(result)

    assert compact["status"] == "completed"
    assert compact["artifact_paths"]["coding"] == "coding.md"
    assert "stdout" not in compact
    assert "stderr" not in compact
    assert "completion" not in compact
    assert "diagnostic" not in compact


def test_mcp_wrapper_calls_existing_blocking_path_entrypoint(monkeypatch, tmp_path):
    observed = {}

    def fake_invoke_executor_from_paths(**kwargs):
        observed.update(kwargs)
        return {
            "status": "completed",
            "reason": None,
            "exit_code": 0,
            "stdout": "must not escape through MCP",
            "stderr": "",
            "completion": {"plan": "persisted elsewhere"},
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": "executor.log",
            "executor_config_sha256": "sha",
            "errors": [],
        }

    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        fake_invoke_executor_from_paths,
    )

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(tmp_path / "project"),
        task=str(tmp_path / "T-001.md"),
        contract=str(tmp_path / "contract" / "v1"),
        previous_review=str(tmp_path / "review.md"),
    )

    assert observed["repository"] == Path(tmp_path / "repo")
    assert observed["runtime_config"] == Path(tmp_path / "runtime.json")
    assert observed["project"] == Path(tmp_path / "project")
    assert observed["task_path"] == Path(tmp_path / "T-001.md")
    assert observed["contract_path"] == Path(tmp_path / "contract" / "v1")
    assert observed["previous_review_path"] == Path(tmp_path / "review.md")
    assert result["status"] == "completed"
    assert "stdout" not in result


def test_missing_mcp_dependency_has_actionable_error(monkeypatch):
    monkeypatch.setattr(MCP, "MCPServer", None)

    try:
        MCP.build_server()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected missing MCP dependency to fail")

    assert "mcp>=2,<3" in message


def test_build_server_with_installed_mcp_sdk():
    if MCP.MCPServer is None:
        raise AssertionError("CI must install requirements-mcp.txt")
    server = MCP.build_server()
    assert server is not None


def test_failed_result_returns_bounded_diagnostic_tails():
    stderr = "E" * (MCP.STDERR_DIAGNOSTIC_CHARS + 37)
    stdout = "O" * (MCP.STDOUT_DIAGNOSTIC_CHARS + 19)
    result = {
        "status": "failed",
        "reason": "process_failed",
        "exit_code": 1,
        "stdout": stdout,
        "stderr": stderr,
        "completion": None,
        "changed_paths": [],
        "scope_violations": [],
        "artifact_paths": {},
        "log_path": "executor.log",
        "executor_config_sha256": "abc123",
        "errors": [],
    }

    compact = MCP.compact_executor_result(result)
    diagnostic = compact["diagnostic"]

    assert len(diagnostic["stderr_tail"]) == MCP.STDERR_DIAGNOSTIC_CHARS
    assert len(diagnostic["stdout_tail"]) == MCP.STDOUT_DIAGNOSTIC_CHARS
    assert diagnostic["stderr_tail"] == stderr[-MCP.STDERR_DIAGNOSTIC_CHARS:]
    assert diagnostic["stdout_tail"] == stdout[-MCP.STDOUT_DIAGNOSTIC_CHARS:]
    assert diagnostic["stderr_truncated"] is True
    assert diagnostic["stdout_truncated"] is True
    assert "stdout" not in compact
    assert "stderr" not in compact
    assert "completion" not in compact


def test_failed_result_keeps_short_diagnostics_untruncated():
    result = {
        "status": "failed",
        "reason": "spawn_failed",
        "exit_code": None,
        "stdout": "short stdout",
        "stderr": "short stderr",
        "changed_paths": [],
        "scope_violations": [],
        "artifact_paths": {},
        "log_path": "executor.log",
        "executor_config_sha256": "abc123",
        "errors": [],
    }

    diagnostic = MCP.compact_executor_result(result)["diagnostic"]
    assert diagnostic == {
        "stderr_tail": "short stderr",
        "stdout_tail": "short stdout",
        "stderr_truncated": False,
        "stdout_truncated": False,
    }


def _write_retry_state(project, key, *, initial=True, quality=0, abnormal=0):
    runtime_dir = project / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "executor_attempts.json").write_text(
        (
            '{"schema_version":2,"tasks":{"' + key + '":{'
            + '"initial_attempted":' + ('true' if initial else 'false') + ','
            + '"quality_retries_used":' + str(quality) + ','
            + '"abnormal_retries_used":' + str(abnormal)
            + '}},"legacy_unclassified_attempts":{}}\n'
        ),
        encoding="utf-8",
    )


def _completed_result():
    return {
        "status": "completed",
        "reason": None,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "completion": None,
        "changed_paths": [],
        "scope_violations": [],
        "artifact_paths": {},
        "log_path": "executor.log",
        "executor_config_sha256": "sha",
        "errors": [],
    }


def _timeout_result():
    value = _completed_result()
    value.update({"status": "failed", "reason": "timeout", "exit_code": None})
    return value


def test_retry_budgets_count_independently_but_exhaustion_blocks_task(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-001.md"
    task.write_text("# T-001\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v5"
    contract.mkdir(parents=True)
    _write_workflow_owner(project, "executor")
    _write_retry_state(project, "v5:T-001", quality=2, abnormal=2)

    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: _completed_result(),
    )

    quality = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
        retry_kind="quality_rework",
    )
    assert quality["retry_policy"]["quality_retries_used"] == 3
    assert quality["retry_policy"]["abnormal_retries_used"] == 2
    assert quality["retry_policy"]["charged_budget"] == "quality_rework"

    # The counters are independent, but once either budget is exhausted the
    # whole task becomes a user-decision point. The remaining abnormal budget
    # cannot be consumed until the user resolves the block.
    abnormal = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
        retry_kind="abnormal_retry",
    )
    assert abnormal["status"] == "retry_limit_reached"
    assert abnormal["reason"] == "quality_rework_limit_reached"
    assert abnormal["retry_policy"]["quality_retries_used"] == 3
    assert abnormal["retry_policy"]["abnormal_retries_used"] == 2


def test_quality_rework_timeout_charges_only_abnormal_budget(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-002.md"
    task.write_text("# T-002\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v3"
    contract.mkdir(parents=True)
    _write_retry_state(project, "v3:T-002", quality=1, abnormal=1)

    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: _timeout_result(),
    )

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
        retry_kind="quality_rework",
    )

    assert result["reason"] == "timeout"
    assert result["retry_policy"]["quality_retries_used"] == 1
    assert result["retry_policy"]["abnormal_retries_used"] == 2
    assert result["retry_policy"]["charged_budget"] == "abnormal_retry"


def test_quality_budget_exhaustion_blocks_all_executor_retry_classes(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-003.md"
    task.write_text("# T-003\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v2"
    contract.mkdir(parents=True)
    _write_workflow_owner(project, "executor")
    _write_retry_state(project, "v2:T-003", quality=3, abnormal=0)

    calls = 0
    def fake_invoke(**kwargs):
        nonlocal calls
        calls += 1
        return _completed_result()
    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    blocked = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
        retry_kind="abnormal_retry",
    )
    assert blocked["status"] == "retry_limit_reached"
    assert blocked["reason"] == "quality_rework_limit_reached"
    assert blocked["retry_exhaustion"]["task"] == "T-003"
    assert blocked["retry_exhaustion"]["budget"] == "quality_rework"
    assert calls == 0

    state = json.loads(
        (project / "runtime" / "workflow_state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "blocked"
    assert state["current_task"] == "T-003"


def test_abnormal_budget_exhaustion_blocks_all_executor_retry_classes(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-004.md"
    task.write_text("# T-004\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v2"
    contract.mkdir(parents=True)
    _write_workflow_owner(project, "executor")
    _write_retry_state(project, "v2:T-004", quality=0, abnormal=3)

    calls = 0
    def fake_invoke(**kwargs):
        nonlocal calls
        calls += 1
        return _completed_result()
    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    blocked = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
        retry_kind="quality_rework",
    )
    assert blocked["status"] == "retry_limit_reached"
    assert blocked["reason"] == "executor_abnormal_retry_limit_reached"
    assert blocked["retry_exhaustion"]["budget"] == "abnormal_retry"
    assert calls == 0



def test_second_initial_dispatch_requires_retry_classification(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-005.md"
    task.write_text("# T-005\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v1"
    contract.mkdir(parents=True)
    _write_retry_state(project, "v1:T-005", initial=True)

    called = False

    def fake_invoke(**kwargs):
        nonlocal called
        called = True
        return _completed_result()

    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
    )
    assert result["status"] == "retry_classification_required"
    assert result["reason"] == "retry_kind_required"
    assert called is False


def test_invalid_mcp_input_does_not_consume_retry_budget(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    task = tmp_path / "T-006.md"
    task.write_text("# T-006\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v7"
    contract.mkdir(parents=True)

    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: {
            "status": "executor_unavailable",
            "reason": "invalid_executor_inputs",
            "errors": ["bad input"],
        },
    )

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
    )

    assert result["retry_policy"]["initial_attempted"] is False
    assert result["retry_policy"]["quality_retries_used"] == 0
    assert result["retry_policy"]["abnormal_retries_used"] == 0
    assert not MCP._attempt_counter_path(project).exists()


def test_retry_budget_is_scoped_by_contract_version(tmp_path):
    project = tmp_path / "project"
    _write_retry_state(project, "v4:T-001", quality=3, abnormal=3)
    task = tmp_path / "T-001.md"
    task.write_text("# T-001\n", encoding="utf-8")

    states, _ = MCP._load_retry_states(project)
    assert states[MCP._attempt_key(tmp_path / "contract" / "v4", task)]["quality_retries_used"] == 3
    assert MCP._attempt_key(tmp_path / "contract" / "v5", task) not in states


def test_legacy_aggregate_attempts_are_preserved_but_not_charged(tmp_path):
    project = tmp_path / "project"
    runtime_dir = project / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "executor_attempts.json").write_text(
        '{"schema_version":1,"attempts":{"v4:T-001":4}}\n',
        encoding="utf-8",
    )

    states, legacy = MCP._load_retry_states(project)
    assert states == {}
    assert legacy == {"v4:T-001": 4}


def _write_workflow_owner(project, owner):
    runtime_dir = project / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "workflow_state.json").write_text(
        '{"schema_version":1,"contract_version":1,"current_task":"T-001",'
        '"status":"ready","attempt":0,"last_completed_task":null,'
        '"last_stage":"test","execution_owner":"' + owner + '",'
        '"updated_at":"2026-08-28T00:00:00+00:00"}\n',
        encoding="utf-8",
    )


def test_mcp_refuses_executor_dispatch_when_supervisor_owns_execution(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-001.md"
    task.write_text("# T-001\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v1"
    contract.mkdir(parents=True)
    _write_workflow_owner(project, "supervisor")

    called = False

    def fake_invoke(**kwargs):
        nonlocal called
        called = True
        return _completed_result()

    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
    )
    assert result["status"] == "execution_owner_mismatch"
    assert result["reason"] == "supervisor_owns_task_execution"
    assert called is False


def test_handoff_back_to_executor_does_not_reset_retry_budgets(monkeypatch, tmp_path):
    project = tmp_path / "project"
    task = tmp_path / "T-001.md"
    task.write_text("# T-001\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v5"
    contract.mkdir(parents=True)
    _write_workflow_owner(project, "executor")
    _write_retry_state(project, "v5:T-001", quality=2, abnormal=1)

    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: _completed_result(),
    )

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
        retry_kind="quality_rework",
    )
    assert result["status"] == "completed"
    assert result["retry_policy"]["quality_retries_used"] == 3
    assert result["retry_policy"]["abnormal_retries_used"] == 1


def test_retry_budgets_are_independent_per_task(tmp_path):
    project = tmp_path / "project"
    _write_retry_state(project, "v9:T-001", quality=3, abnormal=3)

    states, _ = MCP._load_retry_states(project)
    first = MCP._normalize_retry_state(states.get("v9:T-001"))
    second = MCP._normalize_retry_state(states.get("v9:T-002"))

    assert first["quality_retries_used"] == 3
    assert first["abnormal_retries_used"] == 3
    assert second == {
        "initial_attempted": False,
        "quality_retries_used": 0,
        "abnormal_retries_used": 0,
    }
