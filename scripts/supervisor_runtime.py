from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
import functools
import logging
from pathlib import Path
from typing import Callable

import sys
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_registry
from typing import Any

TASK_ID_RE = re.compile(r"^T-\d{3,}$")
TRANSITION_DECISIONS = frozenset({"quality_rework", "pass", "blocked", "waiting_planner"})


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workflow_state_path(project: Path) -> Path:
    return Path(project).resolve() / "runtime" / "workflow_state.json"


def workflow_state_sha256(project: Path) -> str | None:
    path = workflow_state_path(project)
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _stage_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _replace_staged(staged: list[tuple[Path, Path]], after_replace: Callable[[Path], None] | None = None, before_replace: Callable[[Path], None] | None = None) -> None:
    try:
        for temp, target in staged:
            if before_replace is not None:
                before_replace(target)
            os.replace(temp, target)
            if after_replace is not None:
                after_replace(target)
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)


def _write_transaction_journal(path: Path, record: dict[str, Any]) -> None:
    """Write sidecar evidence atomically; no Review content or credentials."""
    text = _json_text(record)
    temp = _stage_text(path, text)
    try:
        # Protect journal against abrupt MCP process death after a file replace.
        with temp.open("r+b") as inp:
            os.fsync(inp.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _serialized_transition(fn):
    """Prevent simultaneous MCP calls from racing on one workflow state."""
    @functools.wraps(fn)
    def wrapped(project: Path, *args: Any, **kwargs: Any):
        project = Path(project).resolve()
        # Shared file lock also covers separate MCP server processes.
        lock = project / "runtime" / "supervisor-transition.lock"
        with workflow_registry._locked(lock):
            return fn(project, *args, **kwargs)
    return wrapped


def _task_order(contract: Path) -> list[str]:
    text = (Path(contract) / "tasks.md").read_text(encoding="utf-8")
    return re.findall(r"(?m)^\s*#+\s*(T-\d{3,})\b", text)


def _contract_version(contract: Path) -> int:
    metadata_path = Path(contract) / "metadata.json"
    if metadata_path.is_file():
        metadata = _read_json(metadata_path)
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if isinstance(version, int) and version >= 1:
            return version
    match = re.fullmatch(r"v(\d+)", Path(contract).name)
    if not match:
        raise ValueError(f"cannot determine Contract version from {contract}")
    return int(match.group(1))


def _effective_current_task(state: dict[str, Any], order: list[str]) -> str | None:
    current = state.get("current_task")
    if isinstance(current, str) and current in order:
        return current
    completed = state.get("last_completed_task")
    if completed is None:
        return order[0] if order else None
    if completed in order:
        index = order.index(completed) + 1
        return order[index] if index < len(order) else None
    return None


def _retry_state(project: Path, version: int, task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    path = Path(project) / "runtime" / "executor_attempts.json"
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {
            "execution_round": 1,
            "initial_attempted": False,
            "quality_retries_used": 0,
            "abnormal_retries_used": 0,
        }
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        return None
    tasks = value.get("tasks")
    if not isinstance(tasks, dict):
        return None
    state = tasks.get(f"v{version}:{task_id}")
    return dict(state) if isinstance(state, dict) else {
        "execution_round": 1,
        "initial_attempted": False,
        "quality_retries_used": 0,
        "abnormal_retries_used": 0,
    }


def _workspace_snapshot(repository: Path | None) -> dict[str, Any] | None:
    if repository is None:
        return None
    repository = Path(repository).resolve()
    try:
        head = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {"available": False}
    dirty_paths = sorted({
        line[3:].strip().replace("\\", "/")
        for line in status.splitlines()
        if len(line) >= 4 and line[3:].strip()
    })
    return {
        "available": True,
        "head": head or None,
        "dirty_paths": dirty_paths,
        "status_sha256": _sha256_bytes(status.encode("utf-8")),
    }


def _repository_from_project(project: Path) -> Path | None:
    path = Path(project) / "runtime" / "project.json"
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    raw = value.get("repository") if isinstance(value, dict) else None
    return Path(str(raw)).expanduser().resolve() if raw else None


def _artifact_paths(project: Path, task_id: str | None) -> dict[str, dict[str, Any]]:
    if not task_id:
        return {}
    root = Path(project) / "developing" / "artifacts" / task_id
    result: dict[str, dict[str, Any]] = {}
    for name in ("executor-packet.md", "plan.md", "coding.md", "review.md", "result.md"):
        path = root / name
        result[name] = {"path": str(path), "exists": path.is_file()}
    return result


def supervisor_snapshot(
    project: Path,
    *,
    contract: Path | None = None,
    repository: Path | None = None,
) -> dict[str, Any]:
    project = Path(project).resolve()
    state_path = workflow_state_path(project)
    state = _read_json(state_path)
    if not isinstance(state, dict):
        raise ValueError("workflow_state.json must contain an object")
    version = state.get("contract_version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("workflow_state.contract_version must be a positive integer")
    contract_path = Path(contract).resolve() if contract is not None else project / "contract" / f"v{version}"
    order = _task_order(contract_path)
    current_task = _effective_current_task(state, order)
    task_path = project / "developing" / "tasks" / f"{current_task}.md" if current_task else None
    task_markdown = task_path.read_text(encoding="utf-8") if task_path is not None and task_path.is_file() else None
    resume_path = project / "runtime" / "supervisor_resume.json"
    try:
        resume = _read_json(resume_path)
    except (OSError, json.JSONDecodeError):
        resume = None
    repository_path = Path(repository).resolve() if repository is not None else _repository_from_project(project)
    workspace = _workspace_snapshot(repository_path)
    drift = None
    if isinstance(resume, dict) and isinstance(workspace, dict) and workspace.get("available"):
        prior = resume.get("workspace")
        if isinstance(prior, dict) and prior.get("available"):
            drift = (
                prior.get("head") != workspace.get("head")
                or prior.get("status_sha256") != workspace.get("status_sha256")
            )
    return {
        "schema_version": 1,
        "project": str(project),
        "workflow_state_sha256": workflow_state_sha256(project),
        "contract_version": version,
        "contract_path": str(contract_path),
        "status": state.get("status"),
        "execution_owner": state.get("execution_owner", "executor"),
        "persisted_current_task": state.get("current_task"),
        "current_task": current_task,
        "last_completed_task": state.get("last_completed_task"),
        "last_stage": state.get("last_stage"),
        "task_order": order,
        "task_path": str(task_path) if task_path is not None else None,
        "task_markdown": task_markdown,
        "retry": _retry_state(project, version, current_task),
        "artifacts": _artifact_paths(project, current_task),
        "resume_capsule": resume,
        "workspace": workspace,
        "workspace_drift_since_boundary": drift,
    }


def _resume_capsule(
    *,
    contract_version: int,
    completed_task: str,
    state: dict[str, Any],
    state_text: str,
    review_path: Path,
    result_path: Path,
    workspace: dict[str, Any] | None,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract_version": contract_version,
        "completed_task": completed_task,
        "current_task": state.get("current_task"),
        "status": state.get("status"),
        "execution_owner": state.get("execution_owner", "executor"),
        "workflow_state_sha256": _sha256_bytes(state_text.encode("utf-8")),
        "review_path": str(review_path),
        "result_path": str(result_path),
        "workspace": workspace,
        "created_at": timestamp,
    }


@_serialized_transition
def commit_supervisor_transition(
    project: Path,
    contract: Path,
    task_id: str,
    decision: str,
    review_markdown: str,
    *,
    result_markdown: str | None = None,
    expected_state_sha256: str | None = None,
    repository: Path | None = None,
) -> dict[str, Any]:
    project = Path(project).resolve()
    contract = Path(contract).resolve()
    task_id = str(task_id).strip()
    decision = str(decision).strip()
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id must be a T-### identifier")
    if decision not in TRANSITION_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(TRANSITION_DECISIONS)}")
    if not isinstance(review_markdown, str) or not review_markdown.strip():
        raise ValueError("review_markdown must be non-empty")
    if decision == "pass" and (not isinstance(result_markdown, str) or not result_markdown.strip()):
        raise ValueError("result_markdown is required for pass")

    state_path = workflow_state_path(project)
    current_hash = workflow_state_sha256(project)
    if expected_state_sha256 is not None and expected_state_sha256 != current_hash:
        raise ValueError(
            "workflow_state_conflict: expected_state_sha256 does not match current workflow state"
        )
    state = _read_json(state_path)
    if not isinstance(state, dict):
        raise ValueError("workflow_state.json must contain an object")
    version = _contract_version(contract)
    if state.get("contract_version") != version:
        raise ValueError(
            f"Contract version mismatch: state v{state.get('contract_version')} != transition v{version}"
        )
    order = _task_order(contract)
    if task_id not in order:
        raise ValueError(f"{task_id} is not defined by {contract / 'tasks.md'}")
    effective_current = _effective_current_task(state, order)
    if effective_current != task_id:
        raise ValueError(
            f"task boundary mismatch: workflow expects {effective_current!r}, transition requested {task_id!r}"
        )
    if state.get("status") in {"workflow_passed", "failed"}:
        raise ValueError(f"cannot transition terminal workflow status {state.get('status')!r}")

    timestamp = _now()
    new_state = dict(state)
    history = new_state.get("supervisor_transition_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "contract_version": version,
        "task": task_id,
        "decision": decision,
        "previous_status": state.get("status"),
        "changed_at": timestamp,
    })
    new_state["supervisor_transition_history"] = history
    new_state["current_task"] = task_id
    new_state["last_supervisor_decision"] = decision
    new_state["updated_at"] = timestamp

    artifact_dir = project / "developing" / "artifacts" / task_id
    review_path = artifact_dir / "review.md"
    result_path = artifact_dir / "result.md"
    review_text = review_markdown.rstrip() + "\n"

    if decision == "quality_rework":
        new_state["status"] = "ready"
        new_state["last_stage"] = "supervisor_quality_rework"
    elif decision in {"blocked", "waiting_planner"}:
        new_state["status"] = decision
        new_state["last_stage"] = f"supervisor_{decision}"
    else:
        index = order.index(task_id)
        next_task = order[index + 1] if index + 1 < len(order) else None
        new_state["last_completed_task"] = task_id
        new_state["current_task"] = next_task
        new_state["attempt"] = 0
        new_state["status"] = "ready" if next_task is not None else "workflow_passed"
        new_state["last_stage"] = "task_passed" if next_task is not None else "workflow_passed"
        new_state.pop("runtime_failure", None)
        new_state.pop("retry_exhaustion", None)

        scoped = new_state.get("scoped_supervisor_takeover")
        if isinstance(scoped, dict) and scoped.get("task") == task_id and scoped.get("return_owner") == "executor":
            owner_history = new_state.get("execution_owner_history")
            if not isinstance(owner_history, list):
                owner_history = []
            previous_owner = new_state.get("execution_owner", "supervisor")
            reason = f"scoped Supervisor takeover for {task_id} completed; returned construction to E"
            owner_history.append({
                "owner": "executor",
                "previous_owner": previous_owner,
                "reason": reason,
                "task": next_task,
                "changed_at": timestamp,
            })
            new_state["execution_owner"] = "executor"
            new_state["execution_owner_reason"] = reason
            new_state["execution_owner_updated_at"] = timestamp
            new_state["execution_owner_history"] = owner_history
            new_state.pop("scoped_supervisor_takeover", None)

    new_state["last_review_path"] = str(review_path)
    staged: list[tuple[Path, Path]] = []
    staged.append((_stage_text(review_path, review_text), review_path))

    capsule = None
    if decision == "pass":
        result_text = str(result_markdown).rstrip() + "\n"
        new_state["last_result_path"] = str(result_path)
        staged.append((_stage_text(result_path, result_text), result_path))
        repository_path = Path(repository).resolve() if repository is not None else _repository_from_project(project)
        workspace = _workspace_snapshot(repository_path)
        state_text = _json_text(new_state)
        capsule = _resume_capsule(
            contract_version=version,
            completed_task=task_id,
            state=new_state,
            state_text=state_text,
            review_path=review_path,
            result_path=result_path,
            workspace=workspace,
            timestamp=timestamp,
        )
        latest_resume = project / "runtime" / "supervisor_resume.json"
        history_resume = project / "runtime" / "resume" / f"{task_id}.json"
        capsule_text = _json_text(capsule)
        staged.append((_stage_text(history_resume, capsule_text), history_resume))
        staged.append((_stage_text(latest_resume, capsule_text), latest_resume))

    state_text = _json_text(new_state)
    # Stage the authoritative state last; it is the commit point. Other files
    # cannot be part of one OS-level atomic rename, so preserve write-ahead
    # evidence for partial failures and process termination.
    try:
        staged.append((_stage_text(state_path, state_text), state_path))
    except Exception:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
        raise

    transaction_id = "st-" + uuid.uuid4().hex
    journal_path = project / "runtime" / "supervisor-transactions" / f"{transaction_id}.json"
    journal = {
        "schema_version": 1,
        "id": transaction_id,
        "task": task_id,
        "decision": decision,
        "contract_version": version,
        "phase": "prepared",
        "state_sha_before": current_hash,
        "state_sha_target": _sha256_bytes(state_text.encode("utf-8")),
        "intended_paths": [
            {"path": target.relative_to(project).as_posix(),
             "sha256": _sha256_bytes(temp.read_bytes())}
            for temp, target in staged
        ],
        "applied_paths": [],
        "updated_at": _now(),
    }
    try:
        _write_transaction_journal(journal_path, journal)
    except Exception:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
        raise

    applied: list[str] = []

    def before_replace(path: Path) -> None:
        journal["phase"] = "applying"
        journal["current_path"] = path.relative_to(project).as_posix()
        journal["updated_at"] = _now()
        _write_transaction_journal(journal_path, journal)

    def after_replace(path: Path) -> None:
        applied.append(path.relative_to(project).as_posix())
        journal["phase"] = "applying"
        journal["applied_paths"] = list(applied)
        journal["updated_at"] = _now()
        _write_transaction_journal(journal_path, journal)

    try:
        _replace_staged(staged, after_replace=after_replace, before_replace=before_replace)
    except Exception as error:
        journal["phase"] = "partial" if applied else "failed"
        journal["failure_type"] = type(error).__name__
        journal["failed_path"] = journal.get("current_path")
        journal["updated_at"] = _now()
        try:
            _write_transaction_journal(journal_path, journal)
        except OSError:
            # The original error is more useful than a second journal failure.
            logging.getLogger(__name__).exception(
                "Could not record partial Supervisor commit %s", transaction_id)
        raise

    journal["phase"] = "committed"
    journal["updated_at"] = _now()
    try:
        _write_transaction_journal(journal_path, journal)
    except OSError:
        # State is committed and the prepared/applying journal still permits
        # reconciliation. Do not report an erroneous commit failure to the S.
        logging.getLogger(__name__).exception(
            "Commit %s succeeded but final journal update failed", transaction_id)
    workflow_registry.safe_sync_workflow(project)
    return {
        "transaction_id": transaction_id,
        "mutation_status": "committed_state",
        "status": "transition_committed",
        "decision": decision,
        "contract_version": version,
        "task": task_id,
        "workflow_status": new_state.get("status"),
        "current_task": new_state.get("current_task"),
        "last_completed_task": new_state.get("last_completed_task"),
        "execution_owner": new_state.get("execution_owner", "executor"),
        "review_path": str(review_path),
        "result_path": str(result_path) if decision == "pass" else None,
        "workflow_state_sha256": _sha256_bytes(state_text.encode("utf-8")),
        "resume_capsule": capsule,
    }
