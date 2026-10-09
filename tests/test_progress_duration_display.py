"""Regression tests for readable Executor elapsed times."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from executor_progress import ProgressRecorder, format_elapsed_time
import psc_mcp_server as MCP


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 h 0 m 0 s"),
        (0.9, "0 h 0 m 0 s"),
        (59, "0 h 0 m 59 s"),
        (60, "0 h 1 m 0 s"),
        (3599, "0 h 59 m 59 s"),
        (3600, "1 h 0 m 0 s"),
        (3661, "1 h 1 m 1 s"),
        (90061, "25 h 1 m 1 s"),
    ],
)
def test_duration_formatter(seconds, expected):
    assert format_elapsed_time(seconds) == expected


def test_snapshot_has_display_and_preserves_numeric_fields(tmp_path, monkeypatch):
    recorder = ProgressRecorder(tmp_path, "T-001", "initial", "dsh", "test")
    recorder.started = 100.0
    recorder.last_event = 200.0
    monkeypatch.setattr("executor_progress.time.monotonic", lambda: 3761.2)
    recorder.heartbeat()
    state = json.loads(recorder.status_path.read_text(encoding="utf-8"))
    assert state["elapsed_seconds"] == 3661.2
    assert state["elapsed_display"] == "1 h 1 m 1 s"
    assert state["seconds_since_executor_event"] == 3561.2
    assert state["last_event_age_display"] == "0 h 59 m 21 s"
    assert state["last_activity"].endswith("last event 0 h 59 m 21 s ago")


def test_mcp_progress_message_uses_display(monkeypatch):
    messages = []

    class Context:
        async def report_progress(self, **kwargs):
            messages.append(kwargs)

    def fake_invoke(**kwargs):
        kwargs["progress_callback"]({
            "sequence": 1,
            "last_activity": "Model step completed",
            "elapsed_display": "1 h 2 m 3 s",
        })
        return {"status": "completed"}

    monkeypatch.setattr(MCP, "_invoke_executor_impl", fake_invoke)
    result = asyncio.run(MCP._invoke_with_progress(Context()))
    assert result["status"] == "completed"
    assert messages == [{
        "progress": 3,
        "message": "Model step completed · elapsed 1 h 2 m 3 s",
    }]
