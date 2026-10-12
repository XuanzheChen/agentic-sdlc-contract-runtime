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


def test_static_probe_requires_explicit_sandbox_and_fingerprints_settings(tmp_path):
    cfg = make_config(tmp_path)
    assert invoke.static_probe(cfg, tmp_path)["status"] == "passed"
    before = invoke.executor_config_fingerprint(cfg, tmp_path)
    home = Path(cfg["executor"]["executor_home"])
    (home / "settings.json").write_text('{"sandbox":{"enabled":false}}', encoding="utf-8")
    assert "claude_workspace_sandbox_not_enabled" in invoke.static_probe(cfg, tmp_path)["errors"]
    assert invoke.executor_config_fingerprint(cfg, tmp_path) != before


def test_claude_commands_do_not_inherit_project_settings_or_shell_wrapper(tmp_path):
    cfg = make_config(tmp_path)
    command = claude.build_command("claude", cfg["executor"], "-", output_schema=None)
    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--setting-sources") + 1] == "user"
    assert command[command.index("--model") + 1] == "claude-test-model"
    assert command[command.index("--effort") + 1] == "high"
    assert "--no-session-persistence" in command
    assert "--permission-mode" in command
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


def test_executor_dispatch_uses_isolated_env_and_parses_terminal_result(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, sandbox="read-only")
    repo = tmp_path / "repository"
    repo.mkdir()
    monkeypatch.setattr(invoke, "_git_snapshot", lambda _repo: {})
    seen = {}

    def fake_run(command, **kwargs):
        seen.update({"command": command, "env": kwargs["env"], "input": kwargs["input"]})
        return SimpleNamespace(returncode=0, stderr="", stdout=_stream(
            {"type": "result", "is_error": False, "result": "PSC_MODEL: claude-test-model",
             "usage": {"input_tokens": 6, "output_tokens": 4}},
        ))

    monkeypatch.setattr(invoke.subprocess, "run", fake_run)
    result = invoke.invoke_executor(
        "claude", repo, {"id": "T-001", "text": "read only"}, "contract",
        None, cfg, require_smoke=False, persist_task_artifacts=False,
    )
    assert result["status"] == "completed"
    assert result["token_usage"]["total_tokens"] == 10
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == cfg["executor"]["executor_home"]
    assert "--tools" in seen["command"]
    assert "read only" in seen["input"]
