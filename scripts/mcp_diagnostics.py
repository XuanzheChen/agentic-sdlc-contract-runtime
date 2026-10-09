"""Structured diagnostics for PSC MCP tool failures.

Keep MCP stdout protocol-clean, never persist raw tool arguments (especially
inline Review Markdown), and never imply a failed mutation was rolled back.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("psc.mcp")
_SECRET = re.compile(
    r"(?i)((?:api[_-]?key|authorization|bearer|access[_-]?token|secret|password)"
    r"\s*[:=]\s*['\"]?)([^\s,'\"}]+)"
)
_SAFE_TOOLS = frozenset({"psc_supervisor_snapshot", "psc_preflight_check",
    "psc_ensure_executor_ready", "psc_progress_probe", "psc_transition_diagnostics"})
_MUTATING = frozenset({"psc_commit_supervisor_transition", "psc_close_workflow",
    "psc_reconcile_workflow_registry", "psc_invoke_executor"})
_MAX_DIAGNOSTIC_BYTES = 100_000


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _scrub(value: str) -> str:
    return _SECRET.sub(r"\1[REDACTED]", value)


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _artifacts(project: Path, task_id: str | None) -> dict[str, str | None]:
    """Conservative before/after fingerprints of *known* Supervisor outputs."""
    result: dict[str, str | None] = {
        "workflow_state": _hash(project / "runtime" / "workflow_state.json"),
        "supervisor_resume": _hash(project / "runtime" / "supervisor_resume.json"),
    }
    if task_id and re.fullmatch(r"T-\d{3,}", task_id):
        root = project / "developing" / "artifacts" / task_id
        for name in ("review.md", "result.md"):
            result[name] = _hash(root / name)
        result["resume_history"] = _hash(project / "runtime" / "resume" / f"{task_id}.json")
    return result


def _latest_journal(project: Path) -> dict[str, Any] | None:
    folder = project / "runtime" / "supervisor-transactions"
    try:
        files = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except (OSError, ValueError):
        return None
    for path in files[:16]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return {key: doc.get(key) for key in
                    ("id", "phase", "failed_path", "current_path", "state_sha_before", "state_sha_target", "updated_at")}
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def classify_exception(exc: BaseException, tool: str) -> tuple[str, str, bool, str]:
    message = str(exc).lower()
    if "workflow_state_conflict" in message or "expected_state_sha256" in message:
        return ("STATE_SHA_CONFLICT", "validate_state", False,
                "Call psc_supervisor_snapshot again; verify the task and retry with its current state SHA.")
    if "contract version mismatch" in message or "task boundary mismatch" in message:
        return ("WORKFLOW_BOUNDARY_MISMATCH", "validate_state", False,
                "Re-read the Supervisor snapshot and approved Contract; do not force a state update.")
    if isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and getattr(exc, "winerror", None) in (5, 32)):
        return ("FILESYSTEM_ACCESS_DENIED", "filesystem", False,
                "Check the MCP process identity, file ACL and concurrent file handles; avoid blind retries.")
    if isinstance(exc, FileNotFoundError):
        return ("FILE_NOT_FOUND", "filesystem", False,
                "Verify paths in the active project and approved Contract.")
    if isinstance(exc, TimeoutError) or isinstance(exc, asyncio.TimeoutError):
        return ("TOOL_TIMEOUT", "execution", False,
                "Inspect the diagnostic log and current snapshot before retrying.")
    if isinstance(exc, json.JSONDecodeError):
        return ("INVALID_JSON", "parse_state", False,
                "Inspect the affected source file; do not overwrite it automatically.")
    if isinstance(exc, (ValueError, TypeError)):
        return ("TOOL_INPUT_OR_STATE_INVALID", "validate_input_or_state", False,
                "Review the diagnostic error summary, tool schema, and latest workflow snapshot.")
    if isinstance(exc, OSError):
        return ("FILESYSTEM_ERROR", "filesystem", False,
                "Check the diagnostic log and file state before retrying.")
    return ("INTERNAL_TOOL_ERROR", "unknown", False,
            "Inspect the diagnostic traceback; do not assume the operation had no side effects.")


def _mutation_status(tool: str, before: dict[str, str | None] | None,
                     after: dict[str, str | None] | None,
                     journal: dict[str, Any] | None) -> str:
    if tool in _SAFE_TOOLS:
        return "not_applicable"
    if before is None or after is None:
        return "unknown"
    changed = [key for key in set(before) | set(after) if before.get(key) != after.get(key)]
    if not changed:
        if journal and journal.get("phase") in {"prepared", "applying", "partial"}:
            return "unknown"
        return "no_monitored_mutation"
    if tool == "psc_commit_supervisor_transition":
        if (journal and journal.get("state_sha_target") == after.get("workflow_state")
                and journal.get("state_sha_before") != after.get("workflow_state")):
            return "committed_state"
        if before.get("workflow_state") == after.get("workflow_state"):
            return "partial_mutation"
        return "unknown"
    return "unknown"


def _diagnostics_directory(project: Path | None) -> Path:
    if project is not None and project.is_dir() and (project / "runtime").is_dir():
        return project / "runtime" / "mcp-diagnostics"
    custom = os.environ.get("PSC_MCP_DIAGNOSTIC_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / ".psc-mcp" / "diagnostics"


def _write_record(record: dict[str, Any], project: Path | None) -> str | None:
    candidates = [_diagnostics_directory(project)]
    fallback = _diagnostics_directory(None)
    if fallback not in candidates:
        candidates.append(fallback)
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"mcp-errors-{dt.datetime.now(dt.timezone.utc):%Y%m%d}.jsonl"
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            # On Windows append is atomic for single writes within a process only;
            # lock-free append is sufficient for this local diagnostic journal.
            with path.open("a", encoding="utf-8") as out:
                out.write(line + "\n")
                out.flush()
                os.fsync(out.fileno())
            return str(path)
        except (OSError, ValueError):
            continue
    return None


def diagnose_failure(tool: str, exc: BaseException, *,
                     project: Path | None = None, task_id: str | None = None,
                     before: dict[str, str | None] | None = None,
                     previous_journal_id: str | None = None) -> dict[str, Any]:
    error_id = "mcp-" + uuid.uuid4().hex[:16]
    code, phase, retryable, advice = classify_exception(exc, tool)
    after = _artifacts(project, task_id) if project is not None else None
    journal = _latest_journal(project) if project is not None and tool == "psc_commit_supervisor_transition" else None
    if journal is not None and journal.get("id") == previous_journal_id:
        # A prior unfinished transaction is not evidence that this invocation
        # wrote anything. Its separate journal remains available to S.
        journal = None
    if journal is not None and journal.get("failed_path"):
        phase = "replace_state" if str(journal["failed_path"]).endswith("/workflow_state.json") else "replace_artifact"
    mutation = _mutation_status(tool, before, after, journal)
    if tool in _MUTATING and mutation not in ("not_applicable", "no_monitored_mutation"):
        retryable = False
        advice = "Mutation may have persisted. Inspect psc_transition_diagnostics and the current snapshot; do not replay blindly."
    summary = _scrub(str(exc))[:900] or type(exc).__name__
    frames = _scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    record: dict[str, Any] = {
        "schema_version": 1, "id": error_id, "at": _timestamp(), "tool": tool,
        "error_code": code, "exception_type": type(exc).__name__,
        "failure_phase": phase, "message": summary,
        "mutation_status": mutation,
        "state_sha_before": before.get("workflow_state") if before else None,
        "state_sha_after": after.get("workflow_state") if after else None,
        "monitored_artifacts_before": before, "monitored_artifacts_after": after,
        "transition_journal": journal,
        "traceback": frames[-_MAX_DIAGNOSTIC_BYTES:],
    }
    path = _write_record(record, project)
    if path is None:
        LOGGER.error("PSC tool error %s %s: %s\n%s", error_id, tool, summary, frames)
    else:
        LOGGER.error("PSC tool error %s %s (%s); log=%s", error_id, tool, code, path)
    return {
        "status": "tool_error", "reason": code, "error_code": code, "error_id": error_id,
        "tool": tool, "exception_type": type(exc).__name__,
        "failure_phase": phase, "message": summary,
        "mutation_status": mutation, "state_sha_before": record["state_sha_before"],
        "state_sha_after": record["state_sha_after"], "retryable": retryable,
        "next_action": advice, "diagnostic_log_path": path,
        "errors": [summary], "executor_attempt_charged": False if tool != "psc_invoke_executor" else None,
    }


def _context(fn: Callable[..., Any], args: tuple[Any, ...],
             kwargs: dict[str, Any]) -> tuple[Path | None, str | None]:
    """Extract *only* diagnostic routing fields; never log user-provided content."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
    except (TypeError, ValueError):
        return None, None
    raw = bound.get("project")
    project: Path | None = None
    if isinstance(raw, (str, Path)) and str(raw).strip():
        try:
            path = Path(raw).expanduser().resolve()
            if path.is_dir():
                project = path
        except (OSError, ValueError):
            pass
    task = bound.get("task_id")
    if task is None and isinstance(bound.get("task"), (str, Path)):
        match = re.search(r"T-\d{3,}", Path(bound["task"]).name)
        task = match.group(0) if match else None
    return project, str(task) if task is not None else None


def guard_tool(tool_name: str):
    """Decorate the callable *inside* @server.tool; supports sync/async and
    preserves its exact signature for SDK schema generation."""
    def decorate(fn: Callable[..., Any]):
        def run_sync(*args: Any, **kwargs: Any):
            project, task = _context(fn, args, kwargs)
            before = _artifacts(project, task) if project is not None and tool_name in _MUTATING else None
            prior = _latest_journal(project) if project is not None and tool_name == "psc_commit_supervisor_transition" else None
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - tool boundary must return usable errors
                return diagnose_failure(tool_name, exc, project=project, task_id=task, before=before,
                                        previous_journal_id=prior.get("id") if prior else None)

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any):
            project, task = _context(fn, args, kwargs)
            before = _artifacts(project, task) if project is not None and tool_name in _MUTATING else None
            prior = _latest_journal(project) if project is not None and tool_name == "psc_commit_supervisor_transition" else None
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                return diagnose_failure(tool_name, exc, project=project, task_id=task, before=before,
                                        previous_journal_id=prior.get("id") if prior else None)

        if inspect.iscoroutinefunction(fn):
            return run_async
        return functools.wraps(fn)(run_sync)
    return decorate


def transition_diagnostics(project: Path, *, limit: int = 10) -> dict[str, Any]:
    """Read-only diagnosis of Supervisor transaction journals and error IDs."""
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError("project must be an existing directory")
    if type(limit) is not int or not (1 <= limit <= 50):
        raise ValueError("limit must be between 1 and 50")
    journals = project / "runtime" / "supervisor-transactions"
    transactions: list[dict[str, Any]] = []
    for path in sorted(journals.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                transactions.append({key: data.get(key) for key in (
                    "id", "phase", "state_sha_before", "state_sha_target",
                    "updated_at", "task", "decision", "applied_paths")})
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    errors: list[dict[str, Any]] = []
    root = project / "runtime" / "mcp-diagnostics"
    for path in sorted(root.glob("mcp-errors-*.jsonl"), reverse=True):
        try:
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                item = json.loads(line)
                if item.get("tool") == "psc_commit_supervisor_transition":
                    errors.append({key: item.get(key) for key in ("id", "at", "tool", "error_code",
                        "failure_phase", "mutation_status", "state_sha_before", "state_sha_after")})
                    if len(errors) >= limit:
                        break
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if len(errors) >= limit:
            break
    return {"status": "ok", "project": str(project),
        "workflow_state_sha256": _hash(project / "runtime" / "workflow_state.json"),
        "transactions": transactions, "errors": errors}
