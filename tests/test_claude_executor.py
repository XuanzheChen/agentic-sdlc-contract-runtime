"""Claude Code executor regression tests (no network/provider calls)."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import invoke_executor as invoke
import psc_runtime
from adapters import claude
from executor_progress import summarize_event
from executor_token_usage import parse_claude_stream_json


def make_config(tmp_path, sandbox="workspace-write"):
    home = tmp_path / "independent-claude-home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"sandbox": {"enabled": True}, "env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}}),
        encoding="utf-8",
    )
    return {
        "schema_version": 1,
        "runtime_root": str(tmp_path / "runtime"),
        "project_naming": "YYYYMMDD-{requirement}",
        "executor": {
            "adapter": "claude",
            "executable": sys.executable,
            "executor_home": str(home),
            "config_source": "executor_home",
            "routing": {"provider": "independent-home", "model": "claude-test-model", "effort": "high"},
            "approval_policy": "never",
            "sandbox": sandbox,
            "timeout": 40,
            "maxTimeout": 80,
            "smoke_timeout": 10,
        },
    }


def test_runtime_validates_claude_and_rejects_global_home(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    config_path = tmp_path / ".agentic-sdlc" / "runtime.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    assert psc_runtime.runtime_config(config_path)["executor"]["adapter"] == "claude"
    cfg["executor"]["config_source"] = "runtime"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="config_source"):
        psc_runtime.runtime_config(config_path)
    cfg["executor"]["config_source"] = "executor_home"
    cfg["executor"]["executor_home"] = str(tmp_path / "global-claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "global-claude"))
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="shares the Supervisor"):
        psc_runtime.runtime_config(config_path)


def test_static_probe_requires_explicit_sandbox_and_fingerprints_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "native_windows", lambda: False)
    cfg = make_config(tmp_path)
    assert invoke.static_probe(cfg, tmp_path)["status"] == "passed"
    before = invoke.executor_config_fingerprint(cfg, tmp_path)
    home = Path(cfg["executor"]["executor_home"])
    (home / "settings.json").write_text('{"sandbox":{"enabled":false}}', encoding="utf-8")
    assert "claude_workspace_sandbox_not_enabled" in invoke.static_probe(cfg, tmp_path)["errors"]
    assert invoke.executor_config_fingerprint(cfg, tmp_path) != before
    home.joinpath("agents").mkdir()
    (home / "agents" / "worker.md").write_text("Use local agent", encoding="utf-8")
    with_agent = invoke.executor_config_fingerprint(cfg, tmp_path)
    (home / "agents" / "worker.md").write_text("Use updated local agent", encoding="utf-8")
    assert invoke.executor_config_fingerprint(cfg, tmp_path) != with_agent


def test_claude_commands_do_not_inherit_project_settings_or_shell_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "native_windows", lambda: False)
    cfg = make_config(tmp_path)
    command = claude.build_command("claude", cfg["executor"], "-", output_schema=None)
    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--setting-sources") + 1] == "user"
    assert command[command.index("--model") + 1] == "claude-test-model"
    assert command[command.index("--effort") + 1] == "high"
    assert "--no-session-persistence" in command
    assert "--permission-mode" in command
    strict = json.loads(command[command.index("--settings") + 1])["sandbox"]
    assert strict["enabled"] and strict["failIfUnavailable"]
    assert strict["allowUnsandboxedCommands"] is False
    pc = claude.build_preflight_command("claude", cfg["executor"], "-")
    assert pc[pc.index("--tools") + 1] == ""
    assert pc[pc.index("--disallowedTools") + 1] == "*"
    assert "--safe-mode" in pc
    assert "--strict-mcp-config" in pc
    assert "--setting-sources" in pc
    with pytest.raises(ValueError, match="stdin"):
        claude.build_command("claude", cfg["executor"], "long prompt")


def test_child_environment_scrubs_global_claude_and_codex(monkeypatch, tmp_path):
    cfg = make_config(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ccswitch-global"))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://global.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "global-secret")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "supervisor-secret")
    child = invoke._executor_child_env("claude", cfg["executor"])
    assert child["CLAUDE_CONFIG_DIR"] == str(Path(cfg["executor"]["executor_home"]).resolve())
    assert "ANTHROPIC_BASE_URL" not in child
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "CLAUDE_CODE_USE_BEDROCK" not in child
    assert "OPENAI_API_KEY" not in child
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://global.invalid"


def _stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_claude_result_usage_is_aggregated_once_with_cache_creation(tmp_path):
    payload = {"schema_version": 1, "plan": "plan", "coding_summary": "done",
        "modified_files": [], "tests": [], "known_risks": [], "unresolved_issues": []}
    output = _stream(
        {"type": "assistant", "message": {"id": "m1", "usage": {
            "input_tokens": 8, "cache_read_input_tokens": 15,
            "cache_creation_input_tokens": 4, "output_tokens": 3}}},
        {"type": "result", "is_error": False, "structured_output": payload, "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 20, "output_tokens": 15}},
    )
    final, usage = parse_claude_stream_json(output)
    assert json.loads(final) == payload
    assert usage["input_tokens"] == 80
    assert usage["uncached_input_tokens"] == 10
    assert usage["cached_input_tokens"] == 50
    assert usage["cache_write_input_tokens"] == 20
    assert usage["output_tokens"] == 15
    assert usage["total_tokens"] == 95
    assert usage["reasoning_tokens_available"] is False
    assert usage["usage_counters_complete"] is True
    assert usage["exact"] is False


def test_claude_cli_error_event_is_not_a_success():
    _, usage = parse_claude_stream_json(_stream(
        {"type": "result", "subtype": "error_during_execution",
         "is_error": True, "result": "Failed", "usage": {
             "input_tokens": 7, "output_tokens": 2}},
    ))
    assert usage["available"]
    assert usage["cli_result_error"] is True
    assert usage["exact"] is False


def test_claude_partial_usage_and_missing_usage_never_claim_exact():
    event = {"type": "assistant", "message": {"id": "m1", "usage": {
        "input_tokens": 3, "output_tokens": 2}}}
    final, usage = parse_claude_stream_json(_stream(event), process_settled=False)
    assert final is None
    assert usage["available"] and usage["lower_bound"]
    assert usage["input_tokens"] == 3
    _, unavailable = parse_claude_stream_json(_stream({"type": "result", "result": "done"}))
    assert not unavailable["available"]


def test_progress_only_reports_claude_tool_names():
    event = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo secret"}},
    ]}}
    summary = summarize_event("claude", event)
    assert summary[0] == "tool_call"
    assert summary[3] == 1
    assert "secret" not in summary[1]


@pytest.mark.parametrize("windows_mode,shell_allowed", [
    ("edit-only", False),
    ("trusted-shell", True),
])
def test_native_windows_workspace_write_without_wsl(
    tmp_path, monkeypatch, windows_mode, shell_allowed
):
    cfg = make_config(tmp_path)
    cfg["executor"]["claude_native_windows_mode"] = windows_mode
    home = Path(cfg["executor"]["executor_home"])
    # On Windows the Claude sandbox is unavailable; disable it explicitly.
    (home / "settings.json").write_text(
        json.dumps({"sandbox": {"enabled": False}}), encoding="utf-8"
    )
    monkeypatch.setattr(claude, "native_windows", lambda: True)
    assert invoke.static_probe(cfg, tmp_path)["status"] == "passed"
    command = claude.build_command("claude", cfg["executor"], "-")
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "--strict-mcp-config" in command
    assert "mcp__*" in command
    runtime_settings = json.loads(command[command.index("--settings") + 1])
    assert runtime_settings["sandbox"]["enabled"] is False
    if shell_allowed:
        assert command[command.index("--allowedTools") + 1] == "Bash"
    else:
        assert "--allowedTools" not in command
        assert "Bash" not in command[command.index("--tools") + 1].split(",")
    assert "bypassPermissions" not in command


def test_native_windows_workspace_write_requires_consent(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    monkeypatch.setattr(claude, "native_windows", lambda: True)
    probe = invoke.static_probe(cfg, tmp_path)
    assert probe["status"] == "failed"
    assert "claude_native_windows_mode_required" in probe["errors"]
    with pytest.raises(ValueError, match="claude_native_windows_mode"):
        claude.build_command("claude", cfg["executor"], "-")


def test_native_windows_mode_changes_smoke_fingerprint(tmp_path):
    cfg = make_config(tmp_path)
    original = invoke.executor_config_fingerprint(cfg, tmp_path)
    cfg["executor"]["claude_native_windows_mode"] = "trusted-shell"
    assert invoke.executor_config_fingerprint(cfg, tmp_path) != original


def test_runtime_rejects_unknown_native_windows_mode(tmp_path):
    cfg = make_config(tmp_path)
    config_path = tmp_path / ".agentic-sdlc" / "runtime.json"
    config_path.parent.mkdir()
    cfg["executor"]["claude_native_windows_mode"] = "unsupported"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="claude_native_windows_mode"):
        psc_runtime.runtime_config(config_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows native runner only")
def test_actual_native_windows_host_has_supported_trusted_shell(tmp_path):
    cfg = make_config(tmp_path)
    cfg["executor"]["claude_native_windows_mode"] = "trusted-shell"
    assert claude.native_windows() is True
    assert invoke.static_probe(cfg, tmp_path)["status"] == "passed"
    command = claude.build_command("claude", cfg["executor"], "-")
    assert command[command.index("--allowedTools") + 1] == "Bash"
    assert json.loads(command[command.index("--settings") + 1])["sandbox"] == {
        "enabled": False
    }
