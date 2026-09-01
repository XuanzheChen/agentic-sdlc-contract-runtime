from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

USAGE_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _unavailable(source: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "exact": False, "source": source, "error": error, **extra}


def _usage(*, source: str, input_tokens: int, uncached_input_tokens: int,
           cached_input_tokens: int, cache_write_input_tokens: int,
           output_tokens: int, reasoning_output_tokens: int,
           exact: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "available": True, "exact": exact, "source": source,
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens,
        **extra,
    }


def zero_usage(source: str = "no_model_call") -> dict[str, Any]:
    return _usage(
        source=source, input_tokens=0, uncached_input_tokens=0,
        cached_input_tokens=0, cache_write_input_tokens=0,
        output_tokens=0, reasoning_output_tokens=0,
    )


def _add_usage(left: dict[str, Any], right: dict[str, Any], *, source: str) -> dict[str, Any]:
    if not left.get("available"):
        return dict(right)
    if not right.get("available"):
        return dict(left)
    values = {field: int(left.get(field, 0)) + int(right.get(field, 0)) for field in USAGE_FIELDS}
    values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return {
        "available": True,
        "exact": bool(left.get("exact")) and bool(right.get("exact")),
        "source": source,
        **values,
    }


def parse_codex_exec_jsonl(stdout: str, *, process_settled: bool = True) -> tuple[str | None, dict[str, Any]]:
    """Extract final assistant text and provider usage from Codex exec JSONL."""
    final_text: str | None = None
    total = zero_usage("codex_exec_jsonl")
    completed_turns = 0
    malformed = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                final_text = item["text"]
        if event.get("type") != "turn.completed":
            continue
        raw = event.get("usage")
        if not isinstance(raw, dict):
            malformed = True
            continue
        input_tokens = _count(raw.get("input_tokens"))
        cached = _count(raw.get("cached_input_tokens"))
        cache_write = _count(raw.get("cache_write_input_tokens"))
        output = _count(raw.get("output_tokens"))
        reasoning = _count(raw.get("reasoning_output_tokens"))
        if None in (input_tokens, cached, cache_write, output, reasoning):
            malformed = True
            continue
        assert input_tokens is not None and cached is not None and cache_write is not None
        assert output is not None and reasoning is not None
        if cached + cache_write > input_tokens or reasoning > output:
            malformed = True
            continue
        turn = _usage(
            source="codex_exec_jsonl",
            input_tokens=input_tokens,
            uncached_input_tokens=input_tokens - cached - cache_write,
            cached_input_tokens=cached,
            cache_write_input_tokens=cache_write,
            output_tokens=output,
            reasoning_output_tokens=reasoning,
        )
        total = _add_usage(total, turn, source="codex_exec_jsonl")
        completed_turns += 1
    if completed_turns == 0:
        return final_text, _unavailable(
            "codex_exec_jsonl", "no_valid_turn_completed_usage", completed_turns=0
        )
    total["exact"] = bool(process_settled) and not malformed
    total["completed_turns"] = completed_turns
    if not total["exact"]:
        total["lower_bound"] = True
        total["warning"] = "partial_or_malformed_codex_usage_stream"
    return final_text, total


def dsh_session_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    root = Path(root)
    if not root.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or not (
            path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zstd")
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path.resolve())] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _read_dsh_log(path: Path) -> str:
    if path.name.endswith(".jsonl"):
        return path.read_text(encoding="utf-8")
    if not path.name.endswith(".jsonl.zstd"):
        raise ValueError(f"unsupported DSH session artifact: {path}")
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("zstandard is required to read DSH session usage") from exc
    with path.open("rb") as handle:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(handle, read_across_frames=True) as reader:
            return reader.read().decode("utf-8")


def _normalize_dsh_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    uncached = _count(value.get("inputTokens"))
    output = _count(value.get("outputTokens"))
    if uncached is None or output is None:
        return None
    cached = _count(value.get("cacheReadTokens", 0))
    cache_write = _count(value.get("cacheWriteTokens", 0))
    reasoning = _count(value.get("reasoningTokens", 0))
    if cached is None or cache_write is None or reasoning is None or reasoning > output:
        return None
    input_total = uncached + cached + cache_write
    provider_total = value.get("totalTokens")
    if provider_total is not None:
        checked = _count(provider_total)
        if checked is None or checked != input_total + output:
            return None
    return _usage(
        source="dsh_session_events",
        input_tokens=input_total,
        uncached_input_tokens=uncached,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        output_tokens=output,
        reasoning_output_tokens=reasoning,
    )


def parse_dsh_session_events(text: str) -> dict[str, Any]:
    """Replay DSH provider-usage replacement semantics for one durable Session."""
    totals = zero_usage("dsh_session_events")
    last_key: tuple[int, int] | None = None
    last_usage: dict[str, Any] | None = None
    samples = 0
    malformed = False

    def replace_or_add(key: tuple[int, int], usage: dict[str, Any]) -> None:
        nonlocal totals, last_key, last_usage, samples
        if last_key == key and last_usage is not None:
            values: dict[str, int] = {}
            for field in USAGE_FIELDS:
                values[field] = (
                    int(totals.get(field, 0))
                    - int(last_usage.get(field, 0))
                    + int(usage.get(field, 0))
                )
            values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
            totals = {"available": True, "exact": True, "source": "dsh_session_events", **values}
        else:
            totals = _add_usage(totals, usage, source="dsh_session_events")
        last_key = key
        last_usage = usage
        samples += 1

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue

        if event_type == "llm/retry-started":
            turn = _count(data.get("turn"))
            step = _count(data.get("step"))
            if turn is not None and step is not None and last_key == (turn, step):
                last_key = None
                last_usage = None
            continue

        usage_value: Any = None
        key: tuple[int, int] | None = None
        if event_type == "assistant/chunk":
            chunk = data.get("chunk")
            if isinstance(chunk, dict) and chunk.get("type") == "usage":
                usage_value = chunk.get("usage")
                turn = _count(data.get("turn"))
                step = _count(data.get("step"))
                if turn is not None and step is not None:
                    key = (turn, step)
        elif event_type == "assistant/message":
            usage_value = data.get("usage")
            turn = _count(data.get("turn"))
            step = _count(data.get("step"))
            if turn is not None and step is not None:
                key = (turn, step)
        elif event_type == "compaction/summary":
            auxiliary = _normalize_dsh_usage(data.get("usage"))
            if auxiliary is not None:
                totals = _add_usage(totals, auxiliary, source="dsh_session_events")
                samples += 1
            elif data.get("usage") is not None:
                malformed = True
            continue

        if usage_value is None:
            continue
        normalized = _normalize_dsh_usage(usage_value)
        if normalized is None or key is None:
            malformed = True
            continue
        replace_or_add(key, normalized)

    if samples == 0:
        return _unavailable("dsh_session_events", "no_provider_usage_events")
    totals["samples"] = samples
    totals["exact"] = not malformed
    if malformed:
        totals["lower_bound"] = True
        totals["warning"] = "malformed_dsh_usage_event"
    return totals


def collect_dsh_invocation_usage(
    root: Path,
    before: dict[str, tuple[int, int]],
    *,
    process_settled: bool,
) -> dict[str, Any]:
    after = dsh_session_snapshot(root)
    new_paths = sorted(set(after) - set(before))
    modified_existing = sorted(
        path for path in set(after) & set(before) if after[path] != before[path]
    )
    if not new_paths and not modified_existing:
        return _unavailable(
            "dsh_session_events", "no_new_dsh_session_artifact", session_count=0
        )
    total = zero_usage("dsh_session_events")
    parsed = 0
    errors: list[str] = []
    for raw_path in new_paths:
        path = Path(raw_path)
        try:
            usage = parse_dsh_session_events(_read_dsh_log(path))
        except (OSError, UnicodeDecodeError, RuntimeError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not usage.get("available"):
            errors.append(f"{path}: {usage.get('error', 'usage unavailable')}")
            continue
        total = _add_usage(total, usage, source="dsh_session_events")
        if not usage.get("exact"):
            errors.append(f"{path}: inexact usage")
        parsed += 1
    if parsed == 0:
        return _unavailable(
            "dsh_session_events",
            "unable_to_parse_new_dsh_sessions",
            session_count=len(new_paths),
            errors=errors,
        )
    exact = bool(process_settled) and not errors and not modified_existing
    total["exact"] = exact
    total["session_count"] = parsed
    total["session_artifacts"] = new_paths
    if modified_existing:
        errors.append(
            "existing DSH session artifacts changed; append-only delta was not attributed"
        )
    if not exact:
        total["lower_bound"] = True
        total["warning"] = "partial_dsh_usage"
        if errors:
            total["errors"] = errors
    return total


def usage_ledger_path(project: Path) -> Path:
    return Path(project).resolve() / "runtime" / "executor_token_usage.jsonl"


def usage_summary_path(project: Path) -> Path:
    return Path(project).resolve() / "runtime" / "executor_token_usage_summary.json"


def _contract_version(contract_path: Path) -> int | None:
    name = Path(contract_path).name
    return int(name[1:]) if name.startswith("v") and name[1:].isdigit() else None


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _aggregate_records(records: list[dict[str, Any]], version: int) -> dict[str, Any]:
    selected = [record for record in records if record.get("contract_version") == version]
    totals = {field: 0 for field in USAGE_FIELDS}
    exact = True
    unavailable = 0
    inexact = 0
    for record in selected:
        usage = record.get("usage")
        if not isinstance(usage, dict) or not usage.get("available"):
            exact = False
            unavailable += 1
            continue
        for field in USAGE_FIELDS:
            value = _count(usage.get(field))
            if value is not None:
                totals[field] += value
        if not usage.get("exact"):
            exact = False
            inexact += 1
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return {
        "contract_version": version,
        "contract": f"v{version}",
        "invocations": len(selected),
        "exact_invocations": len(selected) - unavailable - inexact,
        "inexact_invocations": inexact,
        "unavailable_invocations": unavailable,
        "exact": exact,
        "lower_bound": not exact,
        **totals,
    }


def contract_executor_usage(project: Path, version: int) -> dict[str, Any]:
    """Recompute one Contract version's Executor usage from the append-only ledger."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("Contract version must be a positive integer")
    project = Path(project).resolve()
    return _aggregate_records(_read_ledger(usage_ledger_path(project)), version)


def record_executor_usage(
    project: Path,
    contract_path: Path,
    *,
    task: str,
    execution_round: int,
    retry_kind: str,
    status: str,
    reason: str | None,
    log_path: str | None,
    usage: dict[str, Any],
) -> dict[str, Any]:
    version = _contract_version(contract_path)
    if version is None:
        raise ValueError(f"cannot derive Contract version from {contract_path}")
    project = Path(project).resolve()
    ledger = usage_ledger_path(project)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "recorded_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "contract_version": version,
        "contract": f"v{version}",
        "task": task,
        "execution_round": execution_round,
        "retry_kind": retry_kind,
        "status": status,
        "reason": reason,
        "log_path": log_path,
        "usage": usage,
    }
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    records = _read_ledger(ledger)
    versions = sorted({
        int(item["contract_version"])
        for item in records
        if isinstance(item.get("contract_version"), int)
    })
    contracts = {f"v{item}": _aggregate_records(records, item) for item in versions}
    summary = {
        "schema_version": 1,
        "updated_at": record["recorded_at"],
        "contracts": contracts,
    }
    summary_path = usage_summary_path(project)
    temporary = summary_path.with_name(summary_path.name + ".tmp")
    temporary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, summary_path)
    return {
        "invocation": usage,
        "contract_total": contracts[f"v{version}"],
        "ledger_path": str(ledger),
        "summary_path": str(summary_path),
    }
