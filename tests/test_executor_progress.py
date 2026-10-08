from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from executor_progress import ProgressRecorder, run_streaming, summarize_event


def test_dsh_progress_counts_steps_and_tools_without_leaking_transcripts(tmp_path):
    snapshots = []
    recorder = ProgressRecorder(tmp_path, "T-003", "initial", "dsh", "deepseek-v4.1-flash", snapshots.append)
    recorder.accept_line(json.dumps({"type": "thinking", "text": "internal chain of thought"}))
    recorder.accept_line(json.dumps({
        "type": "tool_call", "tool": "read", "input": {"path": "scripts/run_b2.py"},
    }))
    recorder.accept_line(json.dumps({
        "type": "tool_result", "result": "SECRET_RESULT_DO_NOT_EXPOSE",
    }))
    recorder.accept_line(json.dumps({"type": "status", "phase": "step_end", "step": 1}))
    recorder.heartbeat()
    recorder.finish("completed")
    state = json.loads(recorder.status_path.read_text(encoding="utf-8"))
    history = recorder.events_path.read_text(encoding="utf-8")
    assert state["status"] == "completed"
    assert state["steps"] == 1
    assert state["tool_calls"] == 1
    assert state["run_id"] == recorder.run_id
    assert "scripts/run_b2.py" in history
    assert "SECRET_RESULT_DO_NOT_EXPOSE" not in history
    assert "internal chain of thought" not in history
    assert [item["sequence"] for item in snapshots] == sorted(item["sequence"] for item in snapshots)


def test_safe_tool_summary_does_not_echo_shell_input_or_secrets():
    event = {"type": "tool_call", "tool": "pwsh", "input": {
        "command": "echo api_key=SECRET_VALUE; pytest tests -q",
    }}
    kind, summary, _steps, tools = summarize_event("dsh", event)
    assert kind == "tool_call" and tools == 1
    assert "pytest" in summary
    assert "SECRET_VALUE" not in summary
    assert "echo" not in summary
    assert summarize_event("dsh", {"type": "thinking", "text": "hidden"}) is None


def test_streaming_drains_both_pipes_and_reconstructs_final_output(tmp_path):
    snapshots = []
    recorder = ProgressRecorder(tmp_path, "T-001", "initial", "dsh", "test", snapshots.append)
    script = (
        "import json, sys, time\n"
        "print(json.dumps({'type':'tool_call','tool':'read','input':{'path':'one.py'}}), flush=True)\n"
        "sys.stderr.write('e' * 150000); sys.stderr.flush()\n"
        "time.sleep(0.05)\n"
        "print(json.dumps({'type':'status','phase':'step_end','step':1}), flush=True)\n"
        "print(json.dumps({'type':'final','text':'done'}), flush=True)\n"
    )
    result = run_streaming([sys.executable, "-u", "-c", script], cwd=str(tmp_path),
                           env=dict(os.environ), input=None, timeout=10, recorder=recorder)
    recorder.finish("completed")
    assert result.returncode == 0
    assert len(result.stderr) == 150000
    assert '"text": "done"' in result.stdout
    assert any(item["tool_calls"] == 1 for item in snapshots)
    assert any(item["steps"] == 1 for item in snapshots)


def test_streaming_delivers_stdin_without_deadlock(tmp_path):
    recorder = ProgressRecorder(tmp_path, "T-001", "initial", "codex", "test")
    result = run_streaming(
        [sys.executable, "-u", "-c",
         "import sys; data=sys.stdin.read(); print(len(data), flush=True)"],
        cwd=str(tmp_path), env=dict(os.environ), input="x" * 100000,
        timeout=10, recorder=recorder,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "100000"


def test_streaming_timeout_keeps_partial_stdout_and_stderr(tmp_path):
    recorder = ProgressRecorder(tmp_path, "T-002", "abnormal_retry", "dsh", "test")
    script = (
        "import sys,time\n"
        "print('partial stdout', flush=True)\n"
        "print('partial stderr', file=sys.stderr, flush=True)\n"
        "time.sleep(10)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_streaming([sys.executable, "-u", "-c", script], cwd=str(tmp_path),
                      env=dict(os.environ), input=None, timeout=0.8, recorder=recorder)
    assert "partial stdout" in caught.value.stdout
    assert "partial stderr" in caught.value.stderr


def test_progress_persistence_and_callback_failure_never_break_executor(tmp_path, monkeypatch):
    def broken(_):
        raise RuntimeError("progress client gone")
    recorder = ProgressRecorder(tmp_path, "T-001", "initial", "dsh", "test", broken)
    recorder.accept_line("{broken json")
    recorder.accept_line(json.dumps({"type": "tool_call", "tool": "read"}))
    recorder.finish("failed", "timeout")
    assert json.loads(recorder.status_path.read_text(encoding="utf-8"))["reason"] == "timeout"


def test_codex_exec_events_have_bounded_progress_without_agent_text():
    event = {
        "type": "item.started",
        "item": {"type": "command_execution", "command": "echo secret"},
    }
    kind, summary, _steps, tools = summarize_event("codex", event)
    assert kind == "tool_call" and tools == 1
    assert "secret" not in summary
    assert summarize_event("codex", {"type": "item.completed", "item": {
        "type": "agent_message", "text": "do not publish",
    }}) is None
