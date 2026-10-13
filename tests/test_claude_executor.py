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
from executor_token_usage import parse_claude_stream_json, with_efficiency_metrics, record_executor_usage, contract_executor_usage


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


def test_claude_model_usage_includes_subagents_without_double_counting():
    output = _stream(
        {"type": "assistant", "message": {"id": "agent-main", "usage": {
            "input_tokens": 11, "output_tokens": 12}}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "done", "usage": {
             "input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 1},
         "modelUsage": {
             "deepseek-v4.1-flash": {
                 "inputTokens": 10, "cacheReadInputTokens": 50,
                 "cacheCreationInputTokens": 5, "outputTokens": 20},
             "glm-5.3-flash": {
                 "inputTokens": 7, "cacheReadInputTokens": 8,
                 "cacheCreationInputTokens": 0, "outputTokens": 4},
         }},
    )
    final, usage = parse_claude_stream_json(output)
    assert final == "done"
    assert usage["usage_scope"] == "full_agent_tree"
    assert usage["subagents_included"] is True
    assert usage["usage_exact"] is True
    assert usage["usage_counters_complete"] is True
    assert usage["exact"] is True
    assert usage["partial"] is False
    assert usage["input_tokens"] == 80
    assert usage["uncached_input_tokens"] == 17
    assert usage["cached_input_tokens"] == 58
    assert usage["cache_write_input_tokens"] == 5
    assert usage["output_tokens"] == 24
    assert usage["total_tokens"] == 104
    assert usage["reasoning_output_tokens"] is None
    assert usage["reasoning_tokens_available"] is False
    rated = with_efficiency_metrics(usage)
    assert rated["cache_hit_rate"] == 58 / 80
    assert rated["output_input_ratio"] == 24 / 80


def test_claude_explicit_reasoning_details_are_preserved_and_not_double_added():
    _, usage = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "success",
        "modelUsage": {
            "main": {"inputTokens": 20, "outputTokens": 10,
                     "outputTokensDetails": {"thinkingTokens": 6}},
            "helper": {"inputTokens": 30, "outputTokens": 5,
                       "output_tokens_details": {"thinking_tokens": 2}},
        },
    }))
    assert usage["reasoning_tokens_available"] is True
    assert usage["reasoning_output_tokens"] == 8
    assert usage["usage_exact"] is True
    assert usage["output_tokens"] == 15
    assert with_efficiency_metrics(usage)["output_input_ratio"] == 15 / 50
    # Reasoning is a subset of output, never added a second time.
    assert usage["total_tokens"] == 65


def test_claude_missing_reasoning_in_one_model_makes_aggregate_na():
    _, usage = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "success",
        "modelUsage": {
            "main": {"inputTokens": 10, "outputTokens": 5,
                     "outputTokensDetails": {"thinkingTokens": 0}},
            "helper": {"inputTokens": 20, "outputTokens": 8},
        },
    }))
    assert usage["reasoning_output_tokens"] is None
    assert usage["reasoning_tokens_available"] is False
    assert usage["exact"] is True
    assert usage["usage_counters_complete"] is True


def test_claude_result_usage_fallback_excludes_unknown_subagents():
    _, usage = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "success",
        "usage": {"input_tokens": 8, "cache_read_input_tokens": 12,
                  "cache_creation_input_tokens": 3, "output_tokens": 2},
    }))
    assert usage["usage_scope"] == "main_agent_only"
    assert usage["subagents_included"] is False
    assert usage["usage_exact"] is True
    assert usage["usage_counters_complete"] is True
    assert usage["exact"] is False  # whole invocation may include subagents
    assert usage["partial"] is False
    assert usage["lower_bound"] is True
    assert usage["warning"] == "subagent_usage_not_included"
    assert usage["reasoning_output_tokens"] is None
    assert with_efficiency_metrics(usage)["output_input_ratio"] == 2 / 23


def test_claude_interrupted_or_malformed_model_usage_is_not_exact():
    terminal = _stream({"type": "result", "subtype": "success",
                        "modelUsage": {"main": {
                            "inputTokens": 4, "outputTokens": 3}}})
    _, interrupted = parse_claude_stream_json(terminal, process_settled=False)
    assert interrupted["partial"] is True
    assert interrupted["usage_exact"] is False
    assert interrupted["usage_counters_complete"] is False
    assert interrupted["exact"] is False
    assert interrupted["lower_bound"] is True

    _, invalid = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "success", "modelUsage": {
            "main": {"inputTokens": 4, "outputTokens": 3},
            "missing_output": {"inputTokens": 5},
        },
    }))
    assert invalid["input_tokens"] == 4
    assert invalid["partial"] is True
    assert invalid["usage_counters_complete"] is False
    assert invalid["exact"] is False

    _, failed = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "error_during_execution",
        "is_error": True, "modelUsage": {
            "main": {"inputTokens": 4, "outputTokens": 3},
        },
    }))
    assert failed["cli_result_error"] is True
    assert failed["partial"] is True


def test_claude_assistant_fallback_deduplicates_message_ids():
    _, usage = parse_claude_stream_json(_stream(
        {"type": "assistant", "message": {"id": "same", "usage": {
            "input_tokens": 1, "output_tokens": 2}}},
        {"type": "assistant", "message": {"id": "same", "usage": {
            "input_tokens": 3, "output_tokens": 4}}},
    ), process_settled=False)
    assert usage["usage_scope"] == "assistant_messages_partial"
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 4
    assert usage["reasoning_output_tokens"] is None
    assert usage["usage_exact"] is False
    assert usage["partial"] is True


def test_claude_reasoning_na_persists_through_e_and_pc_ledgers(tmp_path):
    import preflight_checker

    project = tmp_path / "project"
    contract = project / "contract" / "v1"
    contract.mkdir(parents=True)
    _, first = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "success",
        "modelUsage": {"main": {
            "inputTokens": 10, "cacheReadInputTokens": 90,
            "outputTokens": 5}},
    }))
    first["elapsed_seconds"] = 2.5
    report = record_executor_usage(
        project, contract, task="T-001", execution_round=1,
        retry_kind="initial", status="completed", reason=None,
        log_path=None, usage=first,
    )
    assert report["invocation"]["cache_hit_rate"] == 0.9
    assert report["invocation"]["output_input_ratio"] == 0.05
    assert report["invocation"]["reasoning_output_tokens"] is None
    total = contract_executor_usage(project, 1)
    assert total["exact"] is True
    assert total["usage_exact"] is True
    assert total["usage_counters_complete"] is True
    assert total["reasoning_output_tokens"] is None
    assert total["reasoning_tokens_available"] is False
    assert total["input_tokens"] == 100
    assert total["output_tokens"] == 5
    pc = preflight_checker._aggregate_checker_records(
        [{"contract_version": 1, "decision": "ALLOW", "usage": first}], 1,
    )
    assert pc["exact"] is True
    assert pc["reasoning_output_tokens"] is None
    assert pc["reasoning_tokens_available"] is False
    assert pc["cached_input_tokens"] == 90
    assert pc["output_tokens"] == 5


def test_claude_missing_modelusage_main_only_record_is_scope_inexact(tmp_path):
    project = tmp_path / "project"
    contract = project / "contract" / "v1"
    contract.mkdir(parents=True)
    _, usage = parse_claude_stream_json(_stream({
        "type": "result", "subtype": "success",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }))
    record_executor_usage(
        project, contract, task="T-001", execution_round=1,
        retry_kind="initial", status="completed", reason=None,
        log_path=None, usage=usage,
    )
    total = contract_executor_usage(project, 1)
    assert total["exact"] is False
    assert total["usage_exact"] is True
    assert total["usage_counters_complete"] is True
    assert total["main_agent_only_invocations"] == 1
    assert total["reasoning_output_tokens"] is None

