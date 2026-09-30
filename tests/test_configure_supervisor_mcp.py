from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from conftest import SKILL_ROOT


_SPEC = importlib.util.spec_from_file_location(
    "configure_supervisor_mcp",
    SKILL_ROOT / "scripts" / "configure_supervisor_mcp.py",
)
assert _SPEC and _SPEC.loader
CFG = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CFG)


def _inputs(tmp_path: Path):
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    server = tmp_path / "skill" / "scripts" / "psc_mcp_server.py"
    server.parent.mkdir(parents=True)
    server.write_text("# server\n", encoding="utf-8")
    return python, server


def test_home_level_config_replaces_literal_empty_list(tmp_path):
    python, server = _inputs(tmp_path)
    home = tmp_path / ".dsh"
    home.mkdir()
    patch = home / "cordis.patch.yml"
    patch.write_text("[]\n", encoding="utf-8")

    result = CFG.configure(
        dsh_home=home,
        scope="home",
        profile=None,
        mcp_python=python,
        server_script=server,
        tool_timeout_ms=123456,
    )

    text = patch.read_text(encoding="utf-8")
    assert result["status"] == "configured"
    assert result["action"] == "created"
    assert result["restart_required"] is True
    assert text.startswith(CFG.BEGIN_MARKER)
    assert "name: '@deepseek-ai/dsh-mcp-client'" in text
    assert "serverName: agentic_sdlc_executor" in text
    assert "toolCallTimeoutMs: 123456" in text
    assert "[]" not in text


def test_config_is_idempotently_replaced(tmp_path):
    python, server = _inputs(tmp_path)
    home = tmp_path / ".dsh"

    first = CFG.configure(
        dsh_home=home,
        scope="home",
        profile=None,
        mcp_python=python,
        server_script=server,
        tool_timeout_ms=1000,
    )
    second = CFG.configure(
        dsh_home=home,
        scope="home",
        profile=None,
        mcp_python=python,
        server_script=server,
        tool_timeout_ms=2000,
    )

    text = (home / "cordis.patch.yml").read_text(encoding="utf-8")
    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert text.count(CFG.BEGIN_MARKER) == 1
    assert text.count(CFG.END_MARKER) == 1
    assert "toolCallTimeoutMs: 2000" in text
    assert "toolCallTimeoutMs: 1000" not in text


def test_existing_unrelated_patch_is_preserved(tmp_path):
    python, server = _inputs(tmp_path)
    home = tmp_path / ".dsh"
    home.mkdir()
    patch = home / "cordis.patch.yml"
    patch.write_text(
        "- insert:\n    - id: unrelated\n      name: example-plugin\n",
        encoding="utf-8",
    )

    result = CFG.configure(
        dsh_home=home,
        scope="home",
        profile=None,
        mcp_python=python,
        server_script=server,
        tool_timeout_ms=1000,
    )

    text = patch.read_text(encoding="utf-8")
    assert result["action"] == "appended"
    assert "id: unrelated" in text
    assert CFG.PLUGIN_ID in text


def test_unmanaged_duplicate_fails_closed(tmp_path):
    python, server = _inputs(tmp_path)
    home = tmp_path / ".dsh"
    home.mkdir()
    patch = home / "cordis.patch.yml"
    patch.write_text(
        "- insert:\n"
        "    - id: mcp-agentic-sdlc-executor\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n",
        encoding="utf-8",
    )

    try:
        CFG.configure(
            dsh_home=home,
            scope="home",
            profile=None,
            mcp_python=python,
            server_script=server,
            tool_timeout_ms=1000,
        )
    except ValueError as exc:
        assert "unmanaged PSC MCP entry already exists" in str(exc)
    else:
        raise AssertionError("expected unmanaged duplicate to fail closed")


def test_desktop_profile_scope_is_rejected(tmp_path):
    try:
        CFG.target_patch(tmp_path / ".dsh", "profile", "desktop")
    except ValueError as exc:
        assert "Desktop owns profiles/desktop" in str(exc)
        assert "home-level" in str(exc)
    else:
        raise AssertionError("expected desktop profile mutation to be rejected")


def test_check_reports_managed_configuration(tmp_path):
    python, server = _inputs(tmp_path)
    home = tmp_path / ".dsh"
    CFG.configure(
        dsh_home=home,
        scope="home",
        profile=None,
        mcp_python=python,
        server_script=server,
        tool_timeout_ms=1000,
    )

    result = CFG.check(dsh_home=home, scope="home", profile=None)
    assert result["status"] == "configured"
    assert result["managed_entry_present"] is True
    assert (
        "mcp__agentic_sdlc_executor__psc_invoke_executor"
        in result["expected_tools"]
    )
