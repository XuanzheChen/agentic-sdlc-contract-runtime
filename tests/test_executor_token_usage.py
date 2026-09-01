from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import zstandard as zstd

from conftest import SKILL_ROOT


sys.path.insert(0, str(SKILL_ROOT / "scripts"))
_SPEC = importlib.util.spec_from_file_location(
    "executor_token_usage",
    SKILL_ROOT / "scripts" / "executor_token_usage.py",
)
assert _SPEC and _SPEC.loader
USAGE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(USAGE)


def test_codex_jsonl_extracts_final_message_and_exact_usage():
    completion = json.dumps({
        "schema_version": 1,
        "plan": "p",
        "coding_summary": "c",
        "modified_files": [],
        "tests": [],
        "known_risks": [],
        "unresolved_issues": [],
    })
    stdout = "\n".join([
        json.dumps({
            "type": "item.completed",
            "item": {"id": "i1", "type": "agent_message", "text": completion},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 70,
                "cache_write_input_tokens": 10,
                "output_tokens": 20,
                "reasoning_output_tokens": 6,
            },
        }),
    ])
    final, usage = USAGE.parse_codex_exec_jsonl(stdout)
    assert final == completion
    assert usage == {
        "available": True,
        "exact": True,
        "source": "codex_exec_jsonl",
        "input_tokens": 100,
        "uncached_input_tokens": 20,
        "cached_input_tokens": 70,
        "cache_write_input_tokens": 10,
        "output_tokens": 20,
        "reasoning_output_tokens": 6,
        "total_tokens": 120,
        "completed_turns": 1,
    }


def test_codex_partial_stream_is_not_reported_as_exact():
    stdout = json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 50,
            "cached_input_tokens": 40,
            "cache_write_input_tokens": 0,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
        },
    })
    _, usage = USAGE.parse_codex_exec_jsonl(stdout, process_settled=False)
    assert usage["available"] is True
    assert usage["exact"] is False
    assert usage["lower_bound"] is True
    assert usage["total_tokens"] == 55


def test_dsh_fold_replaces_same_attempt_and_counts_retry_and_compaction():
    rows = [
        {
            "type": "assistant/chunk",
            "data": {
                "turn": 1, "step": 1,
                "chunk": {
                    "type": "usage",
                    "usage": {
                        "inputTokens": 10, "outputTokens": 4,
                        "cacheReadTokens": 90, "reasoningTokens": 2,
                    },
                },
            },
        },
        {
            "type": "assistant/message",
            "data": {
                "turn": 1, "step": 1,
                "usage": {
                    "inputTokens": 12, "outputTokens": 5,
                    "cacheReadTokens": 90, "reasoningTokens": 3,
                },
            },
        },
        {"type": "llm/retry-started", "data": {"turn": 1, "step": 1, "retry": 1}},
        {
            "type": "assistant/message",
            "data": {
                "turn": 1, "step": 1,
                "usage": {
                    "inputTokens": 3, "outputTokens": 2,
                    "cacheReadTokens": 100, "cacheWriteTokens": 1,
                    "reasoningTokens": 1,
                },
            },
        },
        {
            "type": "compaction/summary",
            "data": {
                "usage": {
                    "inputTokens": 7, "outputTokens": 3,
                    "cacheReadTokens": 20, "reasoningTokens": 2,
                },
            },
        },
    ]
    usage = USAGE.parse_dsh_session_events(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    # First chunk is replaced by final message: 102 input + 5 output.
    # Retry: 104 input + 2 output. Compaction: 27 input + 3 output.
    assert usage["available"] is True
    assert usage["exact"] is True
    assert usage["input_tokens"] == 233
    assert usage["uncached_input_tokens"] == 22
    assert usage["cached_input_tokens"] == 210
    assert usage["cache_write_input_tokens"] == 1
    assert usage["output_tokens"] == 10
    assert usage["reasoning_output_tokens"] == 6
    assert usage["total_tokens"] == 243


def test_dsh_zstd_multiframe_log_is_readable(tmp_path):
    first = b'{"type":"session","id":"a"}\n'
    second = (
        b'{"type":"assistant/message","data":{"turn":1,"step":1,'
        b'"usage":{"inputTokens":2,"outputTokens":1}}}\n'
    )
    compressor = zstd.ZstdCompressor()
    path = tmp_path / "session.jsonl.zstd"
    path.write_bytes(compressor.compress(first) + compressor.compress(second))
    decoded = USAGE._read_dsh_log(path)
    assert decoded == (first + second).decode("utf-8")


def test_dsh_invocation_sums_new_parent_and_child_sessions(tmp_path):
    root = tmp_path / "sessions"
    before = USAGE.dsh_session_snapshot(root)
    for index, usage in enumerate([
        {"inputTokens": 5, "outputTokens": 2, "cacheReadTokens": 10},
        {"inputTokens": 3, "outputTokens": 1, "cacheReadTokens": 4},
    ]):
        directory = root / f"session-{index}"
        directory.mkdir(parents=True)
        (directory / "session.jsonl").write_text(
            json.dumps({
                "type": "assistant/message",
                "data": {"turn": 1, "step": 1, "usage": usage},
            }) + "\n",
            encoding="utf-8",
        )
    total = USAGE.collect_dsh_invocation_usage(root, before, process_settled=True)
    assert total["exact"] is True
    assert total["session_count"] == 2
    assert total["input_tokens"] == 22
    assert total["output_tokens"] == 3
    assert total["total_tokens"] == 25


def test_usage_ledger_aggregates_per_contract_and_marks_inexact(tmp_path):
    project = tmp_path / "project"
    contract5 = project / "contract" / "v5"
    contract6 = project / "contract" / "v6"
    contract5.mkdir(parents=True)
    contract6.mkdir(parents=True)

    exact = USAGE._usage(
        source="test", input_tokens=100, uncached_input_tokens=10,
        cached_input_tokens=90, cache_write_input_tokens=0,
        output_tokens=20, reasoning_output_tokens=5,
    )
    first = USAGE.record_executor_usage(
        project, contract5, task="T-001", execution_round=1,
        retry_kind="initial", status="completed", reason=None,
        log_path="a.log", usage=exact,
    )
    assert first["contract_total"]["total_tokens"] == 120
    assert first["contract_total"]["exact"] is True

    unavailable = USAGE._unavailable("test", "provider usage absent")
    second = USAGE.record_executor_usage(
        project, contract5, task="T-002", execution_round=1,
        retry_kind="initial", status="failed", reason="timeout",
        log_path="b.log", usage=unavailable,
    )
    assert second["contract_total"]["total_tokens"] == 120
    assert second["contract_total"]["exact"] is False
    assert second["contract_total"]["unavailable_invocations"] == 1

    other = USAGE.record_executor_usage(
        project, contract6, task="T-001", execution_round=1,
        retry_kind="initial", status="completed", reason=None,
        log_path="c.log", usage=exact,
    )
    assert other["contract_total"]["total_tokens"] == 120
    summary = json.loads(
        USAGE.usage_summary_path(project).read_text(encoding="utf-8")
    )
    assert summary["contracts"]["v5"]["invocations"] == 2
    assert summary["contracts"]["v6"]["invocations"] == 1
