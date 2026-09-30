from __future__ import annotations

from conftest import SKILL_ROOT


def _read(relative: str) -> str:
    return (SKILL_ROOT / relative).read_text(encoding="utf-8")


def test_skill_has_codex_and_dsh_supervisor_mcp_initialization_branches():
    skill = _read("SKILL.md")

    assert "### Codex Supervisor MCP initialization" in skill
    assert "### DSH Supervisor MCP initialization" in skill
    assert "[mcp_servers.agentic_sdlc_executor]" in skill
    assert "direct_only_tool_namespaces" in skill
    assert "@deepseek-ai/dsh-mcp-client" in skill
    assert "serverName: agentic_sdlc_executor" in skill
    assert "mcp__agentic_sdlc_executor__psc_invoke_executor" in skill
    assert "Supervisor harness is independent of `executor.adapter`" in skill


def test_skill_forbids_silent_executor_dispatch_fallbacks():
    skill = _read("SKILL.md")

    assert "No silent compatibility fallback is permitted" in skill
    assert "supervisor_mcp_unavailable" in skill
    assert "mcp_transport_required" in skill
    assert "import `scripts/psc_mcp_server.py`" in skill
    assert "`invoke_executor_tool` / `_invoke_executor_impl`" in skill


def test_runtime_references_describe_harness_specific_supervisor_registration():
    runtime_config = _read("references/runtime-config.md")
    adapters = _read("references/executor-adapters.md")

    assert "Supervisor MCP" in runtime_config
    assert "@deepseek-ai/dsh-mcp-client" in runtime_config
    assert "mcp_servers.agentic_sdlc_executor" in runtime_config
    assert "Codex Supervisor" in adapters
    assert "DSH Supervisor" in adapters
    assert "mcp__agentic_sdlc_executor__psc_invoke_executor" in adapters
    assert "mcp_transport_required" in adapters
