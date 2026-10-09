"""Derived, crash-recoverable PSC workflow registry.

The authoritative sources remain runtime/workflow_state.json and project.json.
One atomic file per workflow avoids contention between different workflows.
Registry failure never changes task execution/retry semantics.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

TERMINAL = frozenset({"workflow_passed", "failed"})
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def registry_root(runtime_root: Path) -> Path:
    """Keep index outside the workflow-discovery directory."""
    root = Path(runtime_root).resolve()
    return root.parent / ".psc-index" / root.name


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, ensure_ascii=False, sort_keys=True, indent=2)
            out.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock files are intentionally persistent to avoid races during lock removal.
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"registry lock timed out: {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _paths(project: Path) -> tuple[Path, Path, Path, Path]:
    project = Path(project).resolve()
    if not _NAME.fullmatch(project.name) or project.name.startswith("."):
        raise ValueError(f"invalid PSC workflow directory name: {project.name}")
    registry = registry_root(project.parent)
    return (
        registry / "active" / f"{project.name}.json",
        registry / "recent" / f"{project.name}.json",
        registry / "locks" / f"{project.name}.lock",
        project / "runtime" / "workflow_closure.json",
    )


def sync_workflow(project: Path) -> dict[str, Any] | None:
    """Rebuild exactly one registry entry from authoritative workflow files."""
    project = Path(project).resolve()
    if project.name.startswith(".workflow-stage-"):
        return None
    active, recent, lock, closure = _paths(project)
    with _locked(lock):
        state_path = project / "runtime" / "workflow_state.json"
        manifest_path = project / "runtime" / "project.json"
        if not state_path.is_file() or not manifest_path.is_file():
            # Incomplete bootstrap is not an active workflow.
            active.unlink(missing_ok=True)
            return None
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(manifest, dict):
            raise ValueError("workflow state or manifest must contain a JSON object")
        status = str(state.get("status", "uninitialized"))
        completed = status in TERMINAL
        explicitly_closed = closure.is_file()
        if explicitly_closed:
            closed_data = json.loads(closure.read_text(encoding="utf-8"))
            if not isinstance(closed_data, dict) or closed_data.get("closed") is not True:
                raise ValueError(f"invalid closure marker: {closure}")
        record = {
            "schema_version": 1,
            "workflow_id": project.name,
            "project_id": str(manifest.get("project_id", project.name)),
            "repository": str(manifest.get("repository", "")),
            "project_path": str(project),
            "workflow_status": status,
            "current_task": state.get("current_task"),
            "contract_version": state.get("contract_version"),
            "executor_progress_path": str(project / "runtime" / "executor-progress.json"),
            "state_updated_at": state.get("updated_at"),
            "registered_at": str(manifest.get("created_at") or state.get("updated_at") or _now()),
            "indexed_at": _now(),
            "lifecycle": "closed" if explicitly_closed else ("finished" if completed else "active"),
        }
        destination = recent if (completed or explicitly_closed) else active
        _atomic_json(destination, record)
        if destination == recent:
            with _locked(active.parent.parent / "locks" / "recent.lock"):
                _atomic_json(active.parent.parent / "last.json", record)
        other = active if destination == recent else recent
        other.unlink(missing_ok=True)
        return record


def safe_sync_workflow(project: Path) -> None:
    """Best-effort side effect; a registry fault must not fail Executor/transition."""
    try:
        sync_workflow(project)
    except Exception as error:
        try:
            index = registry_root(Path(project).resolve().parent)
            index.mkdir(parents=True, exist_ok=True)
            with (index / "registry-errors.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps({"at": _now(), "project": str(project),
                                      "error": str(error)}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def close_workflow(project: Path, reason: str, *, expected_state_sha256: str | None = None) -> dict[str, Any]:
    """Explicit lifecycle close: never delete contracts, logs or attempt ledgers."""
    import hashlib
    project = Path(project).resolve()
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("close_workflow requires a non-empty reason")
    active, recent, lock, marker = _paths(project)
    state_path = project / "runtime" / "workflow_state.json"
    if not state_path.is_file():
        raise ValueError(f"workflow state not found: {state_path}")
    raw = state_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_state_sha256 is not None and expected_state_sha256 != digest:
        raise ValueError("workflow_state_conflict: expected state hash differs")
    state = json.loads(raw)
    if state.get("status") in {"executor_running", "supervisor_running"}:
        raise ValueError("cannot close a workflow while task execution is running")
    # Do not silently close while a live Executor is still reporting progress.
    progress_path = project / "runtime" / "executor-progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            if progress.get("status") == "running":
                recent_time = progress.get("last_heartbeat_at") or progress.get("last_executor_event_at")
                if recent_time:
                    stamp = dt.datetime.fromisoformat(str(recent_time).replace("Z", "+00:00"))
                    age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
                    if age < 120:
                        raise ValueError("cannot close a workflow with a recently live Executor")
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            raise ValueError(f"cannot verify Executor progress before closing: {exc}") from exc
    if marker.is_file():
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing.get("closed") is True:
            safe_sync_workflow(project)
            return {"status": "already_closed", "project": str(project), "registry": str(recent)}
    _atomic_json(marker, {
        "schema_version": 1, "closed": True, "closed_at": _now(),
        "reason": reason, "workflow_state_sha256": digest,
    })
    safe_sync_workflow(project)
    return {"status": "closed", "project": str(project), "registry": str(recent)}


def reopen_for_contract_activation(project: Path) -> None:
    """Only called after a successful *higher-version* approved contract activation."""
    _, _, _, marker = _paths(project)
    marker.unlink(missing_ok=True)
    safe_sync_workflow(project)


def reconcile_registry(runtime_root: Path) -> dict[str, Any]:
    """Rare explicit repair scan, not a 30-second monitor hot path."""
    root = Path(runtime_root).resolve()
    index = registry_root(root)
    repaired, errors = [], []
    known: set[str] = set()
    if root.is_dir():
        for project in root.iterdir():
            if not project.is_dir() or not _NAME.fullmatch(project.name):
                continue
            if not (project / "runtime" / "project.json").is_file():
                continue
            known.add(project.name)
            try:
                record = sync_workflow(project)
                if record is not None:
                    repaired.append(record["workflow_id"])
            except (OSError, ValueError, TypeError, TimeoutError) as error:
                errors.append(f"{project.name}: {error}")
    for group in ("active", "recent"):
        folder = index / group
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            if path.stem not in known:
                try:
                    path.unlink()
                except OSError as error:
                    errors.append(f"{path}: {error}")
    return {"status": "reconciled" if not errors else "partial",
            "index_root": str(index), "indexed": len(repaired), "errors": errors}
