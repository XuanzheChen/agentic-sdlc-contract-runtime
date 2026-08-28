from __future__ import annotations

import importlib.util
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


def test_mcp_blocks_fifth_task_attempt(monkeypatch, tmp_path):
    project = tmp_path / "project"
    runtime_dir = project / "runtime"
    runtime_dir.mkdir(parents=True)
    task = tmp_path / "T-001.md"
    task.write_text("# T-001\n", encoding="utf-8")
    contract = tmp_path / "contract" / "v5"
    contract.mkdir(parents=True)
    (runtime_dir / "executor_attempts.json").write_text(
        '{"schema_version":1,"attempts":{"v5:T-001":4}}\n',
        encoding="utf-8",
    )

    called = False

    def fake_invoke_executor_from_paths(**kwargs):
        nonlocal called
        called = True
        return {"status": "completed"}

    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        fake_invoke_executor_from_paths,
    )

    result = MCP.invoke_executor_tool(
        repository=str(tmp_path / "repo"),
        runtime_config=str(tmp_path / "runtime.json"),
        project=str(project),
        task=str(task),
        contract=str(contract),
    )

    assert called is False
    assert result["status"] == "retry_limit_reached"
    assert result["reason"] == "max_task_retries_exhausted"
    assert result["retry_policy"] == {
        "max_retries": 3,
        "max_attempts": 4,
        "attempts_used": 4,
    }


def test_invalid_mcp_input_does_not_consume_retry_budget(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    task = tmp_path / "T-002.md"
    task.write_text("# T-002\n", encoding="utf-8")
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

    assert result["retry_policy"]["attempts_used"] == 0
    assert not MCP._attempt_counter_path(project).exists()


def test_retry_budget_is_scoped_by_contract_version(tmp_path):
    project = tmp_path / "project"
    runtime_dir = project / "runtime"
    runtime_dir.mkdir(parents=True)
    task = tmp_path / "T-001.md"
    task.write_text("# T-001\n", encoding="utf-8")
    (runtime_dir / "executor_attempts.json").write_text(
        '{"schema_version":1,"attempts":{"v4:T-001":4}}\n',
        encoding="utf-8",
    )

    attempts = MCP._load_attempt_counters(project)
    assert attempts.get(MCP._attempt_key(tmp_path / "contract" / "v4", task)) == 4
    assert attempts.get(MCP._attempt_key(tmp_path / "contract" / "v5", task), 0) == 0
