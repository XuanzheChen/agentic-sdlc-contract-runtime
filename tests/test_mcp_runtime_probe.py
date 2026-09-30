from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from conftest import SKILL_ROOT


_SPEC = importlib.util.spec_from_file_location(
    "psc_mcp_runtime_probe", SKILL_ROOT / "scripts" / "probe_mcp_runtime.py"
)
assert _SPEC and _SPEC.loader
PROBE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PROBE)


def test_current_ci_python_is_mcp_ready(tmp_path):
    result = PROBE.probe_mcp_runtime(sys.executable, repository=tmp_path)
    assert result["status"] == "ready"
    assert result["independent"] is True
    assert result["ready"] is True
    assert result["installable"] is True
    assert result["pip_available"] is True
    assert result["mcp_available"] is True
    assert result["openssl_version"]


def test_same_project_python_is_rejected(tmp_path):
    result = PROBE.probe_mcp_runtime(
        sys.executable,
        repository=tmp_path,
        project_python=sys.executable,
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "same_as_project_python"
    assert result["independent"] is False
    assert result["ready"] is False
    assert result["installable"] is False


def test_missing_python_is_invalid(tmp_path):
    result = PROBE.probe_mcp_runtime(
        tmp_path / "does-not-exist" / "python.exe",
        repository=tmp_path,
    )
    assert result["status"] == "invalid"
    assert result["reason"] == "python_not_found"
    assert result["ready"] is False
