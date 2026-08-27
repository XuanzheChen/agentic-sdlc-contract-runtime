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
