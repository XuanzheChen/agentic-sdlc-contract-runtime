#!/usr/bin/env python3
"""Deterministic, artifact-only helpers for the PSC Contract runtime.

This helper validates Contracts, performs optional project bootstrap, activates
an already-materialized Contract, and
provides the deterministic PSC Contract Bundle import layer (``import-bundle``
and startup ``auto-import``). It never invokes a coding worker, an Executor, or
an adapter, and never edits product source files.

The Bundle importer consumes the single-Markdown transport format documented in
``prompts/contract-export.md`` (the External Planner Contract Export Prompt),
validates it strictly, and materializes immutable ``contract/vN/`` versions
atomically with provenance copies and per-attempt reports under
``contract/imports/``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import copytree, copyfile, rmtree
from typing import Any

REQ_RE = re.compile(r"\bREQ-(\d{3,})\b")
AC_RE = re.compile(r"\bAC-(\d{3,})\b")
TASK_RE = re.compile(r"\bT-(\d{3,})\b")
REQUIRED = ("requirements.md", "acceptance.md", "implementation.md", "constraints.md", "tasks.md", "metadata.json")
TERMINAL = {"workflow_passed", "blocked", "failed"}
EXECUTION_OWNERS = frozenset({"executor", "supervisor"})
SECRET_KEYS = {"api_key", "apikey", "password", "passwd", "token", "access_token", "secret", "cookie", "private_key"}

BUNDLE_HEADING = "# PSC-CONTRACT-BUNDLE"
BUNDLE_END = "END PSC-CONTRACT-BUNDLE"
CANONICAL_FILES = ["metadata.json", "requirements.md", "acceptance.md", "implementation.md", "constraints.md", "tasks.md"]
CANONICAL_SET = frozenset(CANONICAL_FILES)
IMPORT_STATUSES = frozenset({"draft", "approved"})
REPORT_SUCCESS_STATUSES = frozenset({"imported", "already_imported"})
TASK_DECLARATION_LABELS = (
    "Dependencies",
    "Allowed Scope",
    "Forbidden Scope",
    "Implementation Notes",
    "Required Verification",
)
EXIT0_IMPORT = frozenset({"imported", "already_imported"})
EXIT0_AUTO = frozenset({"imported", "already_imported", "no_pending", "skipped_approved"})


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(value, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def ids(text: str, pattern: re.Pattern[str]) -> set[str]:
    namespace = "REQ" if pattern is REQ_RE else "AC" if pattern is AC_RE else "T"
    return {f"{namespace}-{n}" for n in pattern.findall(text)}


def headings(text: str, pattern: re.Pattern[str]) -> set[str]:
    # Require IDs to be headings or fenced task labels, while references may be inline.
    prefix = "REQ" if pattern is REQ_RE else "AC" if pattern is AC_RE else "T"
    return {f"{prefix}-{n}" for n in re.findall(rf"^\s*#+\s*{prefix}-(\d{{3,}})\b", text, re.M)}


def heading_matches(text: str, prefix: str) -> list[str]:
    return [f"{prefix}-{n}" for n in re.findall(rf"^\s*#+\s*{prefix}-(\d{{3,}})\b", text, re.M)]


def task_blocks(task_text: str) -> list[tuple[str, str]]:
    """Return each formally-defined task and its exact Markdown block."""
    matches = list(re.finditer(r"^\s*#+\s*(T-\d{3,})\b", task_text, re.M))
    return [
        (match.group(1), task_text[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(task_text)])
        for index, match in enumerate(matches)
    ]


def task_field_values(block: str, label: str) -> set[str]:
    match = re.search(rf'(?im)^{re.escape(label)}:\s*\n((?:\s*[-*]\s*.*\n?)*)', block)
    if not match:
        return set()
    return {line.strip()[1:].strip().replace('\\', '/') for line in match.group(1).splitlines() if line.strip().startswith(('-', '*')) and line.strip()[1:].strip().lower() != 'none'}


def constraint_definitions(constraints_text: str) -> list[str]:
    """Find formal constraint declarations, not incidental C-### references."""
    matches: list[str] = []
    for line in constraints_text.splitlines():
        heading = re.match(r"^\s*#+\s*(C-\d{3,})\b", line)
        labelled = re.match(r"^\s*(C-\d{3,})\s*:", line)
        if heading:
            matches.append(heading.group(1))
        elif labelled:
            matches.append(labelled.group(1))
    return matches


def _workflow_policy_checks(metadata: Any, contents: dict[str, str], errors: list[str]) -> None:
    if not isinstance(metadata, dict):
        return
    supersedes = metadata.get("supersedes")
    version = metadata.get("version")
    if supersedes is not None:
        if not isinstance(supersedes, int) or supersedes < 1 or (isinstance(version, int) and supersedes >= version):
            errors.append(f"metadata.supersedes must be null or a positive integer strictly less than version (got: {supersedes!r})")
    workflow_policy = metadata.get("workflow_policy")
    if not isinstance(workflow_policy, dict):
        errors.append("metadata.workflow_policy must be an object with exactly one invalidation strategy")
        return
    strategy_keys = set(workflow_policy)
    if strategy_keys not in ({"restart"}, {"invalidate_from_task"}):
        errors.append("metadata.workflow_policy must contain exactly one of restart or invalidate_from_task")
        return
    if "restart" in workflow_policy and workflow_policy["restart"] not in {"all", "pending_only"}:
        errors.append(f"metadata.workflow_policy.restart must be 'all' or 'pending_only' (got: {workflow_policy['restart']!r})")
    if "invalidate_from_task" in workflow_policy:
        value = workflow_policy["invalidate_from_task"]
        tasks = set(heading_matches(contents.get("tasks.md", ""), "T"))
        if not (isinstance(value, str) and re.fullmatch(r"T-\d{3,}", value) and value in tasks):
            errors.append(f"metadata.workflow_policy.invalidate_from_task must be a T-### that resolves to an existing task (got: {value!r})")


def secret_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in SECRET_KEYS:
                found.append(f"{path}.{key}" if path else str(key))
            found.extend(secret_keys(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(secret_keys(child, f"{path}[{i}]"))
    return found


def git_info(repository: Path) -> tuple[str | None, str]:
    try:
        head = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(repository), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        return head or None, status
    except (OSError, subprocess.CalledProcessError):
        digest = hashlib.sha256()
        for path in sorted(repository.rglob("*")):
            if path.is_file() and ".git" not in path.parts:
                digest.update(str(path.relative_to(repository)).encode())
                digest.update(str(path.stat().st_mtime_ns).encode())
        return None, digest.hexdigest()


def _check_contract(metadata: Any, contents: dict[str, str], repository: Path | None = None) -> dict[str, Any]:
    """Shared mechanical validation core.

    Holds the complete existing Contract validation logic (metadata, stable
    IDs, references, task-block labels, dependency acyclicity) so the public
    ``validate_contract`` wrapper and the Bundle import path exercise exactly
    the same checks with identical messages. Bundle import extensions are
    applied separately on the import path only.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if isinstance(metadata, dict):
        if metadata.get("schema_version") != 1:
            errors.append("metadata.schema_version must be 1")
        version = metadata.get("version")
        if not isinstance(version, int) or version < 1:
            errors.append("metadata.version must be a positive integer")
        if metadata.get("status") not in {"draft", "approved", "superseded", "rejected"}:
            errors.append("metadata.status must be draft, approved, superseded, or rejected")
        for key in ("created_by", "created_at"):
            if not metadata.get(key):
                errors.append(f"metadata.{key} is required")
        found_secrets = secret_keys(metadata)
        if found_secrets:
            errors.extend(f"credential-like metadata key is forbidden: {key}" for key in found_secrets)
        _workflow_policy_checks(metadata, contents, errors)
    req_matches = heading_matches(contents.get("requirements.md", ""), "REQ")
    ac_matches = heading_matches(contents.get("acceptance.md", ""), "AC")
    task_matches = heading_matches(contents.get("tasks.md", ""), "T")
    reqs, acs, tasks = set(req_matches), set(ac_matches), set(task_matches)
    if not reqs:
        errors.append("requirements.md has no REQ-### headings")
    if not acs:
        errors.append("acceptance.md has no AC-### headings")
    if not tasks:
        errors.append("tasks.md has no T-### headings")
    for label, matches in (("requirement", req_matches), ("acceptance", ac_matches), ("task", task_matches)):
        if len(matches) != len(set(matches)):
            errors.append(f"duplicate {label} IDs")
    task_text = contents.get("tasks.md", "")
    referenced_reqs = ids(task_text, REQ_RE)
    referenced_acs = ids(task_text, AC_RE)
    referenced_tasks = ids(task_text, TASK_RE)
    for ref in sorted(referenced_reqs - reqs):
        errors.append(f"task references unknown {ref}")
    for ref in sorted(referenced_acs - acs):
        errors.append(f"task references unknown {ref}")
    for ref in sorted(referenced_tasks - tasks):
        errors.append(f"task references unknown {ref}")
    referenced_reqs_in_acceptance = ids(contents.get("acceptance.md", ""), REQ_RE)
    for ref in sorted(referenced_reqs_in_acceptance - reqs):
        errors.append(f"acceptance references unknown {ref}")
    c_matches = constraint_definitions(contents.get("constraints.md", ""))
    if len(c_matches) != len(set(c_matches)):
        errors.append("duplicate constraint IDs")
    # Every task needs explicit contractual coverage. A task heading starts a
    # block that ends at the next task heading.
    task_blocks = list(re.finditer(r"^\s*#+\s*(T-\d{3,})\b", task_text, re.M))
    for index, match in enumerate(task_blocks):
        block_end = task_blocks[index + 1].start() if index + 1 < len(task_blocks) else len(task_text)
        block = task_text[match.start():block_end]
        task_id = match.group(1)
        if not re.search(r"requirements?\s*:", block, re.I) or not re.search(r"acceptance(?: criteria)?\s*:", block, re.I):
            errors.append(f"{task_id} must declare Requirements and Acceptance references")
        overlap = task_field_values(block, 'Allowed Scope') & task_field_values(block, 'Forbidden Scope')
        if overlap:
            errors.append(task_id + ' has overlapping Allowed Scope and Forbidden Scope: ' + ', '.join(sorted(overlap)))
    if repository is not None:
        repository = repository.resolve()
        declared = metadata.get("repository") if isinstance(metadata, dict) else None
        if declared and Path(str(declared)).resolve() != repository:
            errors.append(f"Contract repository mismatch: {declared} != {repository}")
    # Dependency graph: inspect the lines after a task heading. The documented
    # task schema lists dependencies both inline ("Dependencies: T-001") and as
    # a bullet list ("Dependencies:" followed by "- T-001" lines), so the
    # collector follows the label line through its bullet-list continuation.
    graph: dict[str, set[str]] = {task: set() for task in tasks}
    current: str | None = None
    in_deps = False
    for line in task_text.splitlines():
        match = re.search(rf"^\s*#+\s*(T-\d{{3,}})\b", line)
        if match:
            current = match.group(1)
            in_deps = False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if in_deps:
            if not stripped:
                continue
            if re.match(r"^[-*]\s+", stripped) or re.fullmatch(r"T-\d{3,}", stripped):
                graph[current].update(TASK_RE.findall(line))
                continue
            in_deps = False
            # fall through: this line may itself be a Dependencies label
        if re.search(r"dependencies?\s*:", line, re.I):
            graph[current].update(TASK_RE.findall(line))
            in_deps = True
    for task, deps in graph.items():
        if task in deps:
            errors.append(f"task dependency self-cycle: {task}")
        for dep in list(deps):
            dep_id = f"T-{dep}" if dep.isdigit() else dep
            if dep_id not in tasks:
                errors.append(f"{task} depends on unknown {dep_id}")
            else:
                graph[task].discard(dep)
                graph[task].add(dep_id)
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            errors.append(f"cyclic task dependency involving {node}")
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, set()):
            if dep in graph:
                visit(dep)
        visiting.remove(node); visited.add(node)
    for task in tasks:
        visit(task)
    return {"valid": not errors, "errors": errors, "warnings": warnings, "metadata": metadata, "ids": {"requirements": sorted(reqs), "acceptance": sorted(acs), "tasks": sorted(tasks)}}


def validate_contract_mechanical(contract_dir: Path, repository: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    contract_dir = contract_dir.resolve()
    if not contract_dir.is_dir():
        return {"valid": False, "errors": [f"contract directory not found: {contract_dir}"], "warnings": []}
    missing = [name for name in REQUIRED if not (contract_dir / name).is_file()]
    if missing:
        errors.extend(f"missing required file: {name}" for name in missing)
    metadata: dict[str, Any] = {}
    if not (contract_dir / "metadata.json").is_file():
        return {"valid": False, "errors": errors, "warnings": warnings}
    try:
        metadata = load_json(contract_dir / "metadata.json")
        if not isinstance(metadata, dict):
            errors.append("metadata.json must contain an object")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid metadata.json: {exc}")
    contents: dict[str, str] = {}
    for name in REQUIRED:
        if name.endswith(".md") and (contract_dir / name).is_file():
            try:
                contents[name] = (contract_dir / name).read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"cannot read {name}: {exc}")
    check = _check_contract(metadata, contents, repository)
    return {
        "valid": not errors and check["valid"],
        "errors": errors + check["errors"],
        "warnings": warnings + check["warnings"],
        "contract": str(contract_dir),
        "metadata": metadata,
        "ids": check["ids"],
    }


def validate_contract_semantics(contents: dict[str, str], metadata: Any) -> list[str]:
    status = metadata.get('status') if isinstance(metadata, dict) else ''
    return _semantic_problems(contents, str(status))


def validate_contract(contract_dir: Path, repository: Path | None = None) -> dict[str, Any]:
    mechanical = validate_contract_mechanical(contract_dir, repository)
    contents: dict[str, str] = {}
    if mechanical.get('contract'):
        root = Path(mechanical['contract'])
        for name in REQUIRED:
            if name.endswith('.md') and (root / name).is_file():
                contents[name] = (root / name).read_text(encoding='utf-8')
    semantic_errors = validate_contract_semantics(contents, mechanical.get('metadata')) if mechanical['valid'] else []
    result = dict(mechanical)
    result['mechanical_valid'] = mechanical['valid']
    result['semantic_valid'] = not semantic_errors
    result['semantic_errors'] = semantic_errors
    result['valid'] = mechanical['valid'] and not semantic_errors
    result['errors'] = list(mechanical['errors']) + semantic_errors
    return result


EXECUTOR_REQUIRED_FIELDS = ('adapter', 'executable', 'executor_home', 'provider', 'model', 'effort', 'approval_policy', 'sandbox', 'timeout')
SUPPORTED_ADAPTERS = frozenset({'codex', 'dsh'})
CONFIG_SOURCES = frozenset({'runtime', 'executor_home'})
APPROVAL_POLICIES = frozenset({'untrusted', 'on-request', 'never'})
SANDBOX_MODES = frozenset({'read-only', 'workspace-write', 'danger-full-access'})


def runtime_configuration_requirements(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ['runtime.json must be an object']
    missing = [key for key in ('runtime_root', 'project_naming', 'executor') if not value.get(key)]
    executor = value.get('executor')
    if not isinstance(executor, dict):
        return missing + ['executor must be an object']
    config_source = executor.get('config_source', 'runtime')
    if config_source not in CONFIG_SOURCES:
        missing.append('executor.config_source must be runtime or executor_home')
    if executor.get('adapter') not in SUPPORTED_ADAPTERS:
        missing.append('executor.adapter must be codex or dsh')
    required_fields = EXECUTOR_REQUIRED_FIELDS
    if config_source == 'executor_home' or executor.get('adapter') == 'dsh':
        required_fields = tuple(key for key in required_fields if key not in {'provider', 'model', 'effort'})
    for key in required_fields:
        if key == 'approval_policy' and 'approval' in executor:
            continue
        if executor.get(key) in (None, ''):
            missing.append(f'executor.{key}')
    if executor.get('adapter') == 'dsh' and not executor.get('profile'):
        missing.append('executor.profile')
    return missing


def _shared_executor_home(path: Path, executor_home: Path) -> bool:
    supervisor_home = os.environ.get('CODEX_HOME')
    try:
        effective_supervisor_home = (
            Path(supervisor_home).expanduser().resolve()
            if supervisor_home
            else (Path.home() / '.codex').resolve()
        )
        if executor_home == effective_supervisor_home:
            return True
    except OSError:
        pass
    repository = path.parent.parent.resolve() if path.parent.name == '.agentic-sdlc' else None
    return repository is not None and executor_home in {repository / '.codex', repository / '.codex-local'}


def runtime_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"runtime config not found: {path}")
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("runtime.json must be an object with schema_version 1")
    if secret_keys(value):
        raise ValueError("runtime.json contains credential-like keys")
    missing = runtime_configuration_requirements(value)
    if missing:
        raise ValueError('configuration_required: provide explicit values for ' + ', '.join(missing))
    value = dict(value)
    executor = dict(value['executor'])
    value['executor'] = executor
    executor.setdefault('config_source', 'runtime')
    if executor['config_source'] not in CONFIG_SOURCES:
        raise ValueError('executor.config_source must be runtime or executor_home')
    if executor.get('adapter') not in SUPPORTED_ADAPTERS:
        raise ValueError('executor.adapter must be codex or dsh')
    if 'approval_policy' not in executor:
        legacy = executor.get('approval')
        if legacy in APPROVAL_POLICIES:
            executor['approval_policy'] = legacy
        else:
            raise ValueError('configuration_required: executor.approval_policy must be explicitly selected; legacy approval is ambiguous')
    if executor['approval_policy'] not in APPROVAL_POLICIES:
        raise ValueError('executor.approval_policy must be untrusted, on-request, or never')
    if executor.get('sandbox') not in SANDBOX_MODES:
        raise ValueError('executor.sandbox must be read-only, workspace-write, or danger-full-access')
    for key in ('timeout',):
        if not isinstance(executor.get(key), int) or executor[key] <= 0:
            raise ValueError(f'executor.{key} must be a positive integer')
    if 'maxTimeout' in executor:
        if not isinstance(executor.get('maxTimeout'), int) or executor['maxTimeout'] <= 0:
            raise ValueError('executor.maxTimeout must be a positive integer')
        if executor['maxTimeout'] < executor['timeout']:
            raise ValueError('executor.maxTimeout must be greater than or equal to executor.timeout')
    else:
        # Backward compatibility for existing runtime.json files. New
        # initializations should explicitly collect maxTimeout; an old config
        # without it simply keeps the previous fixed-timeout behavior.
        executor['maxTimeout'] = executor['timeout']
    executor.setdefault('smoke_timeout', 120)
    if not isinstance(executor['smoke_timeout'], int) or executor['smoke_timeout'] <= 0:
        raise ValueError('executor.smoke_timeout must be a positive integer')
    if executor.get('approvals_reviewer') is not None:
        if executor['approval_policy'] != 'on-request' or executor['approvals_reviewer'] != 'auto_review' or executor['sandbox'] != 'workspace-write':
            raise ValueError('approvals_reviewer=auto_review requires approval_policy=on-request and sandbox=workspace-write')
    home = Path(str(executor['executor_home'])).expanduser().resolve()
    if _shared_executor_home(path.resolve(), home) and executor.get('allow_shared_executor_home') is not True:
        raise ValueError('configuration_required: executor_home shares the Supervisor environment; set allow_shared_executor_home only after explicit user confirmation')
    return value


def discover(repository: Path, config_path: Path) -> dict[str, Any]:
    config = runtime_config(config_path)
    root = Path(config["runtime_root"]).expanduser().resolve()
    projects: list[dict[str, Any]] = []
    if root.is_dir():
        for project in sorted(p for p in root.iterdir() if p.is_dir()):
            manifest_path = project / "runtime" / "project.json"
            state_path = project / "runtime" / "workflow_state.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = load_json(manifest_path)
            except (OSError, json.JSONDecodeError):
                continue
            if Path(str(manifest.get("repository", ""))).resolve() != repository.resolve():
                continue
            state = load_json(state_path) if state_path.is_file() else None
            status = state.get("status") if isinstance(state, dict) else "uninitialized"
            approved: list[int] = []
            for version_dir in (project / "contract").glob("v*"):
                if not version_dir.is_dir():
                    continue
                try:
                    candidate = validate_contract(version_dir, repository)
                    metadata = candidate.get("metadata", {})
                    if candidate["valid"] and metadata.get("status") == "approved":
                        approved.append(int(metadata["version"]))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
            projects.append({"path": str(project), "project": manifest, "status": status, "active": status not in TERMINAL, "approved_versions": sorted(approved)})
    return {"repository": str(repository.resolve()), "runtime_root": str(root.resolve()), "projects": projects, "active": [p for p in projects if p["active"]]}


def _make_project_layout(project: Path) -> None:
    (project / "contract").mkdir(parents=True, exist_ok=True)
    (project / "developing" / "tasks").mkdir(parents=True, exist_ok=True)
    (project / "developing" / "artifacts").mkdir(parents=True, exist_ok=True)
    (project / "review").mkdir(parents=True, exist_ok=True)
    (project / "runtime").mkdir(parents=True, exist_ok=True)
    (project / "logs" / "executor").mkdir(parents=True, exist_ok=True)


def _write_task_records(project: Path, task_text: str) -> None:
    task_matches = list(re.finditer(r"^\s*#+\s*(T-\d{3,})\b", task_text, re.M))
    for index, match in enumerate(task_matches):
        end = task_matches[index + 1].start() if index + 1 < len(task_matches) else len(task_text)
        task_id = match.group(1)
        task_file = project / "developing" / "tasks" / f"{task_id}.md"
        task_file.write_text(task_text[match.start():end].strip() + "\n", encoding="utf-8")
        (project / "developing" / "artifacts" / task_id).mkdir(parents=True, exist_ok=True)


def _write_project_manifest(project: Path, repository: Path, project_id: str) -> dict[str, Any]:
    head, status = git_info(repository)
    manifest = {"project_id": project_id, "repository": str(repository.resolve()), "created_at": now(), "baseline_commit": head, "baseline_status": status}
    dump_json(project / "runtime" / "project.json", manifest)
    return manifest


def _write_workflow_state(project: Path, version: int, state_status: str, last_stage: str) -> dict[str, Any]:
    timestamp = now()
    state = {
        "schema_version": 1,
        "contract_version": version,
        "current_task": None,
        "status": state_status,
        "attempt": 0,
        "last_completed_task": None,
        "last_stage": last_stage,
        "execution_owner": "executor",
        "execution_owner_reason": "default",
        "execution_owner_updated_at": timestamp,
        "execution_owner_history": [
            {"owner": "executor", "reason": "default", "changed_at": timestamp}
        ],
        "updated_at": timestamp,
    }
    dump_json(project / "runtime" / "workflow_state.json", state)
    return state


def bootstrap(contract_dir: Path, repository: Path, config_path: Path, project_id: str | None = None) -> dict[str, Any]:
    config = runtime_config(config_path)
    check = validate_contract(contract_dir, repository)
    if not check["valid"]:
        raise ValueError("Contract validation failed: " + "; ".join(check["errors"]))
    if check["metadata"].get("status") != "approved":
        raise ValueError("bootstrap requires an approved Contract")
    root = Path(config["runtime_root"]).expanduser().resolve()
    meta = check["metadata"]
    project_id = project_id or str(meta.get("project_id") or f"{dt.datetime.now().strftime('%Y%m%d')}-psc")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project_id):
        raise ValueError("project id must contain only letters, digits, dot, underscore, or hyphen")
    project = (root / project_id).resolve()
    try:
        project.relative_to(root)
    except ValueError as exc:
        raise ValueError("project id resolves outside runtime_root") from exc
    if project.exists():
        raise ValueError(f"project already exists: {project}")
    project.mkdir(parents=True)
    _make_project_layout(project)
    copytree(contract_dir, project / "contract" / f"v{meta['version']}")
    task_text = (contract_dir / "tasks.md").read_text(encoding="utf-8")
    _write_task_records(project, task_text)
    manifest = _write_project_manifest(project, repository, project_id)
    state = _write_workflow_state(project, meta["version"], "initialized", "bootstrap")
    return {"project": str(project), "manifest": manifest, "state": state, "contract": check}


# ---------------------------------------------------------------------------
# Contract Bundle parsing (FR-1, AC-01..AC-04)
# ---------------------------------------------------------------------------

def _is_delimiter(line: str) -> bool:
    return bool(re.fullmatch(r"=+", line.strip()))


def _section_content(lines: list[str]) -> str:
    """Exact, lossless section body extraction.

    The lines between the section's closing delimiter pair and the next
    delimiter; leading/trailing blank lines are template separators and are
    removed; interior lines are preserved verbatim; exactly one trailing LF is
    guaranteed (appended if missing).
    """
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    end = len(lines)
    while end > start and not lines[end - 1].strip():
        end -= 1
    body = "\n".join(lines[start:end])
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def _parse_manifest(lines: list[str], errors: list[str]) -> dict[str, Any]:
    manifest: dict[str, Any] = {"version": None, "status": None, "files": []}
    in_files = False
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^version\s*:\s*(.+)$", stripped, re.I)
        if m:
            value = m.group(1).strip()
            if re.fullmatch(r"[1-9]\d*", value):
                manifest["version"] = int(value)
            else:
                errors.append(f"manifest Version must be a positive integer (got: {value!r})")
            in_files = False
            continue
        m = re.match(r"^status\s*:\s*(.+)$", stripped, re.I)
        if m:
            value = m.group(1).strip().lower()
            if value in {"approved", "draft"}:
                manifest["status"] = value
            else:
                errors.append(f"manifest Status must be approved or draft (got: {value!r})")
            in_files = False
            continue
        if re.match(r"^files\s*:?\s*$", stripped, re.I):
            in_files = True
            continue
        if in_files:
            m = re.match(r"^-\s*(.+)$", stripped)
            if m:
                manifest["files"].append(m.group(1).strip())
                continue
            in_files = False
    if manifest["version"] is None:
        errors.append("manifest is missing a positive integer Version")
    if manifest["status"] is None:
        errors.append("manifest is missing Status (approved or draft)")
    if not manifest["files"]:
        errors.append("manifest is missing the Files bullet list")
    elif set(manifest["files"]) != CANONICAL_SET:
        errors.append(f"manifest Files must be exactly: {', '.join(CANONICAL_FILES)}")
    return manifest


def parse_bundle(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse the PSC-CONTRACT-BUNDLE transport format.

    Returns ``(parsed, [])`` with ``parsed = {"manifest", "sections"}``, or
    ``(None, errors)`` when any structural violation is detected. All failures
    are mechanical parser errors -> the importer terminates with
    ``import_failed`` and materializes nothing.
    """
    errors: list[str] = []
    text = text.replace("\r\n", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]  # tolerate a UTF-8 BOM before the Bundle heading
    lines = text.split("\n")
    # Envelope: first non-blank line must be the Bundle heading.
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != BUNDLE_HEADING:
        errors.append(f"bundle must start with {BUNDLE_HEADING!r} as its first non-blank line")
        return None, errors
    # Manifest block sits between the heading and the first FILE delimiter.
    first_delim = None
    for i in range(idx + 1, len(lines)):
        if _is_delimiter(lines[i]):
            first_delim = i
            break
    if first_delim is None:
        errors.append("bundle must contain a FILE section delimiter")
        return None, errors
    pre = lines[idx + 1:first_delim]
    manifest_idx = None
    for i, line in enumerate(pre):
        if line.strip() == "## CONTRACT-MANIFEST":
            manifest_idx = i
            break
    if manifest_idx is None:
        errors.append("missing '## CONTRACT-MANIFEST' section")
        manifest: dict[str, Any] = {"version": None, "status": None, "files": []}
    else:
        manifest = _parse_manifest(pre[manifest_idx + 1:], errors)
    # FILE sections.
    sections: dict[str, str] = {}
    i = first_delim
    end_seen = False
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if not _is_delimiter(lines[i]):
            errors.append(f"unexpected content outside a FILE section: {lines[i].strip()!r}")
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            errors.append(f"bundle ends before the {BUNDLE_END!r} marker")
            break
        stripped = lines[j].strip()
        m = re.match(r"^FILE:\s*(\S+)\s*$", stripped)
        if m:
            name = m.group(1)
            if name not in CANONICAL_SET:
                errors.append(f"unknown FILE section: {name}")
            elif name in sections:
                errors.append(f"duplicate FILE section: {name}")
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k >= len(lines) or not _is_delimiter(lines[k]):
                errors.append(f"FILE section {name} must be delimited by '=' lines")
                i = k
                continue
            body_end = k + 1
            while body_end < len(lines) and not _is_delimiter(lines[body_end]):
                body_end += 1
            if body_end >= len(lines):
                errors.append(f"FILE section {name} is not terminated by a delimiter")
                break
            sections[name] = _section_content(lines[k + 1:body_end])
            i = body_end
            continue
        if stripped == BUNDLE_END:
            i = j + 1
            while i < len(lines):
                s = lines[i].strip()
                if s and not _is_delimiter(lines[i]):
                    errors.append(f"unexpected content after {BUNDLE_END}: {s!r}")
                    break
                i += 1
            end_seen = True
            break
        errors.append(f"expected 'FILE: <name>' or {BUNDLE_END!r} after delimiter, got: {stripped!r}")
        i = j
    if not end_seen:
        errors.append(f"missing {BUNDLE_END!r} marker")
    if len(sections) != 6:
        errors.append(f"bundle must contain exactly six FILE sections (got {len(sections)})")
    elif set(sections) != CANONICAL_SET:
        errors.append("bundle FILE sections must be exactly the six canonical artifacts (metadata.json, requirements.md, acceptance.md, implementation.md, constraints.md, tasks.md)")
    if errors:
        return None, errors
    return {"manifest": manifest, "sections": sections}, []


def _parse_metadata_content(content: str, errors: list[str]) -> Any:
    try:
        value = json.loads(content)
        if not isinstance(value, dict):
            errors.append("metadata.json must contain an object")
        return value
    except json.JSONDecodeError as exc:
        errors.append(f"invalid metadata.json: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Bundle import extensions on top of the shared validation core (AC-05..AC-06)
# ---------------------------------------------------------------------------

def _import_metadata_checks(metadata: Any, contents: dict[str, str], errors: list[str]) -> None:
    if not isinstance(metadata, dict):
        return
    status = metadata.get("status")
    if status not in IMPORT_STATUSES:
        errors.append(f"metadata.status must be draft or approved for import (got: {status!r})")
    version = metadata.get("version")
    supersedes = metadata.get("supersedes")
    if supersedes is not None:
        if not isinstance(supersedes, int) or supersedes < 1 or (isinstance(version, int) and supersedes >= version):
            errors.append(f"metadata.supersedes must be null or a positive integer strictly less than version (got: {supersedes!r})")
    workflow_policy = metadata.get("workflow_policy")
    if workflow_policy is not None:
        if not isinstance(workflow_policy, dict):
            errors.append("metadata.workflow_policy must be an object")
        else:
            unknown = sorted(set(workflow_policy) - {"restart", "invalidate_from_task"})
            if unknown:
                errors.append(f"metadata.workflow_policy contains unknown keys: {', '.join(unknown)}")
            if "restart" in workflow_policy and workflow_policy["restart"] not in {"all", "pending_only"}:
                errors.append(f"metadata.workflow_policy.restart must be 'all' or 'pending_only' (got: {workflow_policy['restart']!r})")
            if "invalidate_from_task" in workflow_policy:
                value = workflow_policy["invalidate_from_task"]
                tasks = set(heading_matches(contents.get("tasks.md", ""), "T"))
                if not (isinstance(value, str) and re.fullmatch(r"T-\d{3,}", value) and value in tasks):
                    errors.append(f"metadata.workflow_policy.invalidate_from_task must be a T-### that resolves to an existing task (got: {value!r})")


def _import_reference_checks(contents: dict[str, str], errors: list[str]) -> None:
    reqs = set(heading_matches(contents.get("requirements.md", ""), "REQ"))
    acceptance_text = contents.get("acceptance.md", "")
    referenced_in_acceptance = ids(acceptance_text, REQ_RE)
    for ref in sorted(referenced_in_acceptance - reqs):
        errors.append(f"acceptance references unknown {ref}")
    constraints_text = contents.get("constraints.md", "")
    c_matches = constraint_definitions(constraints_text)
    if len(c_matches) != len(set(c_matches)):
        errors.append("duplicate constraint IDs")


def _unresolved_locations(contents: dict[str, str]) -> list[str]:
    locations: list[str] = []
    for name in ("requirements.md", "acceptance.md", "implementation.md", "constraints.md", "tasks.md"):
        for lineno, line in enumerate(contents.get(name, "").splitlines(), 1):
            if "UNRESOLVED" in line:
                locations.append(f"{name}:{lineno}")
    return locations


def _semantic_problems(contents: dict[str, str], status: str) -> list[str]:
    """Detect completeness/contradiction conditions (AC-08); never repair them."""
    problems: list[str] = []
    reqs = set(heading_matches(contents.get("requirements.md", ""), "REQ"))
    acs = set(heading_matches(contents.get("acceptance.md", ""), "AC"))
    task_text = contents.get("tasks.md", "")
    referenced_reqs_in_acceptance = ids(contents.get("acceptance.md", ""), REQ_RE)
    referenced_acs_in_tasks = ids(task_text, AC_RE)
    for req in sorted(reqs - referenced_reqs_in_acceptance):
        problems.append(f"uncovered requirement {req}: not referenced by any acceptance criterion in acceptance.md")
    for ac in sorted(acs - referenced_acs_in_tasks):
        problems.append(f"uncovered acceptance criterion {ac}: not referenced by any task in tasks.md")
    task_blocks = list(re.finditer(r"^\s*#+\s*(T-\d{3,})\b", task_text, re.M))
    for index, match in enumerate(task_blocks):
        block_end = task_blocks[index + 1].start() if index + 1 < len(task_blocks) else len(task_text)
        block = task_text[match.start():block_end]
        task_id = match.group(1)
        missing = [label for label in TASK_DECLARATION_LABELS if not re.search(rf"{re.escape(label)}\s*:", block, re.I)]
        if missing:
            problems.append(f"task {task_id} lacks implementation-critical declaration: {', '.join(missing)}")
    if status == "approved":
        for location in _unresolved_locations(contents):
            problems.append(f"blocking UNRESOLVED marker in approved contract at {location}")
    return problems


def _decision_for(problem: str) -> str:
    if problem.startswith("uncovered requirement"):
        return "Add an acceptance criterion referencing this requirement in a new Contract version, or explicitly drop the requirement."
    if problem.startswith("uncovered acceptance criterion"):
        return "Assign this acceptance criterion to an implementing task in a new Contract version, or explicitly drop it."
    if problem.startswith("task "):
        return "Complete the task definition with the missing implementation-critical declaration lines in a new Contract version."
    if problem.startswith("blocking UNRESOLVED"):
        return "Resolve the UNRESOLVED marker before execution; the Contract cannot start until the decision is recorded in a new Contract version."
    if problem.startswith("UNRESOLVED marker"):
        return "Review the listed UNRESOLVED items and record the decisions in a new Contract version."
    return "Record the decision that resolves this problem in a new Contract version."


def _write_escalation(project: Path, version: int, sha: str, status: str, problems: list[str]) -> Path:
    review_dir = project / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (review_dir / f"escalation-{n:03d}.md").exists():
        n += 1
    path = review_dir / f"escalation-{n:03d}.md"
    lines = [
        "# Escalation — waiting_planner",
        "",
        "- Status: waiting_planner",
        f"- Contract version: v{version}",
        f"- Bundle status: {status}",
        f"- Bundle SHA-256: {sha}",
        f"- Import time: {now()}",
        "",
        "## Problems (concrete evidence)",
        "",
    ]
    lines.extend(f"{i}. {problem}" for i, problem in enumerate(problems, 1))
    lines.extend(["", "## Planner decision required", ""])
    lines.extend(f"- {_decision_for(problem)}" for problem in problems)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Provenance, reports, idempotency (FR-4, AC-14..AC-15, AC-13)
# ---------------------------------------------------------------------------

def _provenance_copy(src: Path, project: Path, sha: str) -> tuple[Path, list[str]]:
    imports_dir = project / "contract" / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    src_resolved = src.resolve()
    if src_resolved.is_relative_to(imports_dir.resolve()):
        # Bundles already residing under contract/imports/ (auto-discovery)
        # are already their own provenance copy.
        return src_resolved, []
    copy_path = imports_dir / f"{sha[:16]}.bundle.md"
    if copy_path.exists():
        if copy_path.read_bytes() == src_resolved.read_bytes():
            return copy_path, [f"reusing existing byte-identical provenance copy: {copy_path.name}"]
        raise ValueError(f"provenance copy path already exists with different content: {copy_path}")
    copyfile(src_resolved, copy_path)
    return copy_path, []


def _write_report(
    project: Path,
    source: Path,
    copy_path: Path | None,
    sha: str,
    version: int | None,
    status: str,
    outcome_type: str,
    warnings: list[str],
    errors: list[str] | None = None,
    reasons: list[str] | None = None,
    materialized_path: Path | None = None,
) -> Path:
    reports_dir = project / "contract" / "imports" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"import-{sha[:12]}-{stamp}"
    counter = 1 + len(list(reports_dir.glob(f"{prefix}-*.json")))
    path = reports_dir / f"{prefix}-{counter:03d}.json"
    report = {
        "source": str(source),
        "copy_path": str(copy_path) if copy_path is not None else None,
        "sha256": sha,
        "version": version,
        "import_time": now(),
        "outcome": {
            "type": outcome_type,
            "reasons": list(reasons or []),
            "errors": list(errors or []),
        },
        "materialized_path": str(materialized_path) if materialized_path is not None else None,
        "warnings": list(warnings),
        "status": status,
    }
    if any(part.startswith(".workflow-stage-") for part in project.parts):
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    else:
        dump_json(path, report)
    return path


def _matching_reports(project: Path, sha: str, version: int | None = None, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    """Idempotency/pending decisions are decidable from disk state alone."""
    reports_dir = project / "contract" / "imports" / "reports"
    if not reports_dir.is_dir():
        return []
    hits: list[dict[str, Any]] = []
    for path in sorted(reports_dir.glob("import-*.json")):
        try:
            report = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict) or report.get("sha256") != sha:
            continue
        if version is not None and report.get("version") != version:
            continue
        if statuses is not None and report.get("status") not in statuses:
            continue
        hits.append(report)
    return hits


# ---------------------------------------------------------------------------
# Materialization (FR-3, AC-11..AC-12)
# ---------------------------------------------------------------------------

_STAGING_COUNTER = 0


def _atomic_rename(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _materialize_contract(project: Path, version: int, sections: dict[str, str]) -> Path:
    contract_dir = project / "contract"
    # Create the staging directory with plain mkdir: tempfile.mkdtemp applies a
    # restrictive mode that some sandboxes deny with EACCES on entry writes.
    global _STAGING_COUNTER
    _STAGING_COUNTER += 1
    staging = contract_dir / f".import-stage-{os.getpid()}-{_STAGING_COUNTER}"
    staging.mkdir()
    target = contract_dir / f"v{version}"
    try:
        for name in CANONICAL_FILES:
            (staging / name).write_text(sections[name], encoding="utf-8", newline="\n")
        for name in CANONICAL_FILES:
            if (staging / name).read_bytes() != sections[name].encode("utf-8"):
                raise RuntimeError(f"staging content mismatch for {name}")
        if target.exists():
            raise ValueError(f"contract version already exists: {target}")
        _atomic_rename(staging, target)
        staging = None  # renamed into place; nothing to clean up
    finally:
        if staging is not None and staging.exists():
            rmtree(staging, ignore_errors=True)
    return target


# ---------------------------------------------------------------------------
# Target selection, discovery, and the single import pipeline
# (FR-5, FR-6, AC-16..AC-20)
# ---------------------------------------------------------------------------

def _associated_projects(root: Path, repository: Path) -> list[Path]:
    projects: list[Path] = []
    if root.is_dir():
        for project in sorted(p for p in root.iterdir() if p.is_dir()):
            manifest_path = project / "runtime" / "project.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = load_json(manifest_path)
            except (OSError, json.JSONDecodeError):
                continue
            if Path(str(manifest.get("repository", ""))).resolve() == repository:
                projects.append(project)
    return projects


def _project_matches(project: Path, project_id: str) -> bool:
    """A project is selected by --project-id when it equals the workflow
    directory name or the manifest project_id (the raw chosen candidate)."""
    if project.name == project_id:
        return True
    manifest_path = project / "runtime" / "project.json"
    if manifest_path.is_file():
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            return False
        return manifest.get("project_id") == project_id
    return False


def _has_usable_approved(project: Path, repository: Path) -> bool:
    contract_dir = project / "contract"
    if not contract_dir.is_dir():
        return False
    for version_dir in sorted(contract_dir.glob("v*")):
        if not version_dir.is_dir():
            continue
        try:
            check = validate_contract(version_dir, repository)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if check["valid"] and check.get("metadata", {}).get("status") == "approved":
            return True
    return False


def _pending_bundles(project: Path) -> list[Path]:
    imports_dir = project / "contract" / "imports"
    if not imports_dir.is_dir():
        return []
    pending: list[Path] = []
    for path in sorted(p for p in imports_dir.iterdir() if p.is_file()):
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if _matching_reports(project, sha, None, REPORT_SUCCESS_STATUSES):
            continue  # already imported; retained as provenance, no longer pending
        pending.append(path)
    return pending


def _sanitize_candidate(candidate: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip(".-")
    name = re.sub(r"^[^A-Za-z0-9]+", "", name)
    name = name[:64]
    if not name:
        raise ValueError(f"cannot derive a valid project directory name from candidate: {candidate!r}")
    return name


def _project_directory_name(config: dict[str, Any], candidate: str) -> str:
    sanitized = _sanitize_candidate(candidate)
    naming = config.get("project_naming")
    if isinstance(naming, str) and ('YYYYMMDD' in naming or ('{' in naming and '}' in naming)):
        date = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')
        name = naming.replace('YYYYMMDD', date)
        for token in ("{date}", "{candidate}", "{project_name}", "{requirement}"):
            name = name.replace(token, date if token == "{date}" else sanitized)
        name = _sanitize_candidate(name)
    else:
        name = sanitized
    return name


def _result(
    status: str,
    sha: str | None,
    version: int | None,
    source: Path | None,
    copy_path: Path | None,
    materialized_path: Path | None,
    report_path: Path | None,
    warnings: list[str],
    errors: list[str],
    escalated: bool = False,
) -> dict[str, Any]:
    result = {
        "status": status,
        "sha256": sha,
        "version": version,
        "source": str(source) if source is not None else None,
        "copy_path": str(copy_path) if copy_path is not None else None,
        "materialized_path": str(materialized_path) if materialized_path is not None else None,
        "report_path": str(report_path) if report_path is not None else None,
        "warnings": warnings,
        "errors": errors,
    }
    if escalated:
        result["escalated"] = True
    return result


def _update_workflow_state(project: Path, version: int, new_status: str | None) -> dict[str, Any]:
    state_path = project / "runtime" / "workflow_state.json"
    if not state_path.is_file():
        return _write_workflow_state(project, version, new_status or "initialized", "import")
    state = load_json(state_path)
    if not isinstance(state, dict):
        raise ValueError(f"invalid workflow state: {state_path}")
    state = dict(state)
    if new_status is not None:
        state["status"] = new_status
    state.setdefault("execution_owner", "executor")
    state.setdefault("execution_owner_reason", "legacy_default")
    state.setdefault("execution_owner_updated_at", state.get("updated_at") or now())
    state.setdefault("execution_owner_history", [])
    state["last_stage"] = "import"
    state["updated_at"] = now()
    dump_json(state_path, state)
    return state


def set_execution_owner(project: Path, owner: str, reason: str) -> dict[str, Any]:
    """Atomically hand task execution between Executor and Supervisor."""
    project = Path(project).resolve()
    if owner not in EXECUTION_OWNERS:
        raise ValueError("execution owner must be executor or supervisor")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("execution owner handoff requires a non-empty reason")
    state_path = project / "runtime" / "workflow_state.json"
    if not state_path.is_file():
        raise ValueError(f"workflow state not found: {state_path}")
    state = load_json(state_path)
    if not isinstance(state, dict):
        raise ValueError(f"invalid workflow state: {state_path}")
    if state.get("status") in {"executor_running", "supervisor_running"}:
        raise ValueError("cannot change execution owner while a task execution is running")
    if state.get("status") == "blocked" and isinstance(state.get("retry_exhaustion"), dict):
        raise ValueError(
            "retry exhaustion requires resolve-retry-exhaustion; generic owner "
            "handoff cannot bypass the user decision point"
        )
    previous = state.get("execution_owner", "executor")
    timestamp = now()
    history = state.get("execution_owner_history")
    if not isinstance(history, list):
        history = []
    if previous != owner:
        history.append({
            "owner": owner,
            "previous_owner": previous,
            "reason": reason,
            "task": state.get("current_task"),
            "changed_at": timestamp,
        })
    state["execution_owner"] = owner
    state["execution_owner_reason"] = reason
    state["execution_owner_updated_at"] = timestamp
    state["execution_owner_history"] = history
    state["last_stage"] = "execution_owner_handoff"
    state["updated_at"] = timestamp
    dump_json(state_path, state)
    return {
        "status": "owner_changed" if previous != owner else "owner_unchanged",
        "previous_owner": previous,
        "execution_owner": owner,
        "reason": reason,
        "current_task": state.get("current_task"),
        "workflow_status": state.get("status"),
    }


RETRY_EXHAUSTION_DECISIONS = frozenset({
    "reset-and-continue-executor",
    "switch-to-supervisor",
})


def _executor_attempts_path(project: Path) -> Path:
    return Path(project) / "runtime" / "executor_attempts.json"


def _load_executor_attempt_state(project: Path) -> dict[str, Any]:
    path = _executor_attempts_path(project)
    try:
        value = load_json(path)
    except (OSError, json.JSONDecodeError):
        return {
            "schema_version": 2,
            "tasks": {},
            "legacy_unclassified_attempts": {},
        }
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise ValueError(
            "retry exhaustion resolution requires executor_attempts.json schema_version 2"
        )
    tasks = value.get("tasks")
    legacy = value.get("legacy_unclassified_attempts")
    if not isinstance(tasks, dict) or not isinstance(legacy, dict):
        raise ValueError("invalid executor retry state")
    return value


def resolve_retry_exhaustion(project: Path, decision: str) -> dict[str, Any]:
    """Atomically resolve one blocked task's exhausted E budget.

    reset-and-continue-executor resets only the exhausted budget for the exact
    Contract-version/Task key and returns execution to E. switch-to-supervisor
    preserves all E retry counters and hands the blocked task to S.
    """
    project = Path(project).resolve()
    if decision not in RETRY_EXHAUSTION_DECISIONS:
        raise ValueError(
            "decision must be reset-and-continue-executor or switch-to-supervisor"
        )
    state_path = project / "runtime" / "workflow_state.json"
    if not state_path.is_file():
        raise ValueError(f"workflow state not found: {state_path}")
    state = load_json(state_path)
    if not isinstance(state, dict):
        raise ValueError(f"invalid workflow state: {state_path}")
    marker = state.get("retry_exhaustion")
    if state.get("status") != "blocked" or not isinstance(marker, dict):
        raise ValueError("workflow is not blocked on an Executor retry exhaustion decision")

    task_id = marker.get("task")
    version = marker.get("contract_version")
    budget = marker.get("budget")
    if not (
        isinstance(task_id, str)
        and re.fullmatch(r"T-\d{3,}", task_id)
        and isinstance(version, int)
        and version >= 1
        and budget in {"quality_rework", "abnormal_retry"}
    ):
        raise ValueError("invalid retry_exhaustion marker")

    key = f"v{version}:{task_id}"
    timestamp = now()
    state = dict(state)
    previous_owner = state.get("execution_owner", "executor")
    reset_budget: str | None = None

    if decision == "reset-and-continue-executor":
        retry_state = _load_executor_attempt_state(project)
        task_retry = retry_state["tasks"].get(key)
        if not isinstance(task_retry, dict):
            raise ValueError(f"retry state missing for blocked task {key}")
        task_retry = dict(task_retry)
        field = (
            "quality_retries_used"
            if budget == "quality_rework"
            else "abnormal_retries_used"
        )
        task_retry[field] = 0
        retry_state["tasks"] = dict(retry_state["tasks"])
        retry_state["tasks"][key] = task_retry
        dump_json(_executor_attempts_path(project), retry_state)
        owner = "executor"
        reset_budget = budget
        reason = f"user reset task-local {budget} budget and continued with E"
    else:
        owner = "supervisor"
        reason = f"user switched blocked {task_id} from E to S"

    history = state.get("execution_owner_history")
    if not isinstance(history, list):
        history = []
    if previous_owner != owner:
        history.append({
            "owner": owner,
            "previous_owner": previous_owner,
            "reason": reason,
            "task": task_id,
            "changed_at": timestamp,
        })

    resolution_history = state.get("retry_exhaustion_history")
    if not isinstance(resolution_history, list):
        resolution_history = []
    resolution_history.append({
        **marker,
        "decision": decision,
        "reset_budget": reset_budget,
        "resolved_owner": owner,
        "resolved_at": timestamp,
    })

    state["execution_owner"] = owner
    state["execution_owner_reason"] = reason
    state["execution_owner_updated_at"] = timestamp
    state["execution_owner_history"] = history
    state["retry_exhaustion_history"] = resolution_history
    state.pop("retry_exhaustion", None)
    state["status"] = "ready"
    state["current_task"] = task_id
    state["last_stage"] = "retry_exhaustion_resolved"
    state["updated_at"] = timestamp
    dump_json(state_path, state)

    return {
        "status": "retry_exhaustion_resolved",
        "decision": decision,
        "contract_version": version,
        "task": task_id,
        "exhausted_budget": budget,
        "reset_budget": reset_budget,
        "execution_owner": owner,
        "workflow_status": "ready",
    }


def _rebuild_task_records(project: Path, task_text: str) -> list[str]:
    tasks_dir = project / 'developing' / 'tasks'
    tasks_dir.mkdir(parents=True, exist_ok=True)
    for record in tasks_dir.glob('T-*.md'):
        record.unlink()
    _write_task_records(project, task_text)
    return [task_id for task_id, _ in task_blocks(task_text)]


def _highest_approved_contract(project: Path, repository: Path) -> tuple[int, Path, dict[str, Any]] | None:
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for version_dir in (project / 'contract').glob('v*'):
        if not version_dir.is_dir():
            continue
        check = validate_contract(version_dir, repository)
        metadata = check.get('metadata', {})
        if check['valid'] and metadata.get('status') == 'approved':
            candidates.append((int(metadata['version']), version_dir, check))
    return max(candidates, key=lambda item: item[0]) if candidates else None


def activate_contract(project: Path, repository: Path) -> dict[str, Any]:
    project = project.resolve()
    state_path = project / 'runtime' / 'workflow_state.json'
    if not state_path.is_file():
        raise ValueError(f'workflow state not found: {state_path}')
    state = load_json(state_path)
    if not isinstance(state, dict) or not isinstance(state.get('contract_version'), int):
        raise ValueError(f'invalid workflow state: {state_path}')
    highest = _highest_approved_contract(project, Path(repository).resolve())
    if highest is None:
        raise ValueError('no semantically valid Approved Contract is available for activation')
    version, contract_dir, check = highest
    effective = state['contract_version']
    if version <= effective:
        return {'status': 'no_activation', 'effective_contract_version': effective, 'highest_approved_version': version}
    metadata = check['metadata']
    policy = metadata['workflow_policy']
    task_text = (contract_dir / 'tasks.md').read_text(encoding='utf-8')
    active_tasks = _rebuild_task_records(project, task_text)
    state['current_task'] = None
    state['attempt'] = 0
    state.setdefault('execution_owner', 'executor')
    state.setdefault('execution_owner_reason', 'legacy_default')
    state.setdefault('execution_owner_updated_at', state.get('updated_at') or now())
    state.setdefault('execution_owner_history', [])
    if policy.get('restart') == 'all':
        state['last_completed_task'] = None
    elif 'invalidate_from_task' in policy:
        target = policy['invalidate_from_task']
        index = active_tasks.index(target)
        state['current_task'] = target
        state['last_completed_task'] = active_tasks[index - 1] if index else None
    state['status'] = 'ready'
    state['last_stage'] = 'contract_activation'
    state['updated_at'] = now()
    state['contract_version'] = version
    dump_json(state_path, state)
    return {'status': 'activated', 'previous_contract_version': effective, 'contract_version': version, 'workflow_policy': policy, 'active_tasks': active_tasks}


def _import_attempt(
    src: Path,
    sha: str,
    text: str | None,
    decode_error: str | None,
    parsed: dict[str, Any] | None,
    parse_errors: list[str],
    target: Path,
    repo: Path,
    bootstrapping: bool,
    bootstrap_id: str | None = None,
) -> dict[str, Any]:
    """Run one import attempt into an existing (or freshly laid-out) project.

    Order of operations: provenance copy -> mechanical validation ->
    idempotency/conflict decision -> atomic materialization -> semantic check
    -> workflow-state -> report. Mechanical errors never touch
    ``workflow_state.json`` and never create a partial ``contract/vN``.
    """
    copy_path, copy_warnings = _provenance_copy(src, target, sha)
    warnings: list[str] = list(copy_warnings)
    errors: list[str] = []
    version: int | None = None
    contents: dict[str, str] = {}

    if decode_error is not None:
        errors.append(f"bundle is not valid UTF-8: {decode_error}")
        report = _write_report(target, src, copy_path, sha, None, "import_failed", "failed", warnings, errors)
        return _result("import_failed", sha, None, src, copy_path, None, report, warnings, errors)
    if parsed is None:
        errors.extend(parse_errors)
        report = _write_report(target, src, copy_path, sha, None, "import_failed", "failed", warnings, errors)
        return _result("import_failed", sha, None, src, copy_path, None, report, warnings, errors)

    version = parsed["manifest"]["version"]
    sections = parsed["sections"]
    contents = {name: sections[name] for name in REQUIRED}
    metadata = _parse_metadata_content(sections["metadata.json"], errors)
    if isinstance(metadata, dict):
        if version != metadata.get("version"):
            errors.append(f"manifest Version {version} disagrees with metadata.version {metadata.get('version')!r}")
        manifest_status = parsed["manifest"]["status"]
        metadata_status = str(metadata.get("status", "")).lower()
        if metadata_status != manifest_status:
            errors.append(f"manifest Status {manifest_status!r} disagrees with metadata.status {metadata.get('status')!r}")
    core = _check_contract(metadata, contents, repo)
    errors.extend(core["errors"])
    warnings.extend(core["warnings"])
    _import_metadata_checks(metadata, contents, errors)
    _import_reference_checks(contents, errors)

    if errors:
        report = _write_report(target, src, copy_path, sha, version, "import_failed", "failed", warnings, errors)
        return _result("import_failed", sha, version, src, copy_path, None, report, warnings, errors)

    declared_version = version
    contract_dir = target / "contract"
    vdir = contract_dir / f"v{declared_version}"
    if _matching_reports(target, sha, declared_version, REPORT_SUCCESS_STATUSES):
        if not vdir.is_dir():
            errors.append("successful import report exists but the materialized contract version is missing (provenance inconsistency)")
            report = _write_report(target, src, copy_path, sha, declared_version, "import_failed", "failed", warnings, errors)
            return _result("import_failed", sha, declared_version, src, copy_path, None, report, warnings, errors)
        warnings.append(f"bundle already imported as v{declared_version}")
        report = _write_report(target, src, copy_path, sha, declared_version, "already_imported", "valid", warnings)
        return _result("already_imported", sha, declared_version, src, copy_path, vdir, report, warnings, [])
    if vdir.is_dir():
        errors.append(f"contract version v{declared_version} already exists with different content")
        report = _write_report(target, src, copy_path, sha, declared_version, "version_conflict", "failed", warnings, errors)
        return _result("version_conflict", sha, declared_version, src, copy_path, None, report, warnings, errors)

    try:
        materialized = _materialize_contract(target, declared_version, sections)
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"materialization failed: {exc}")
        report = _write_report(target, src, copy_path, sha, declared_version, "import_failed", "failed", warnings, errors)
        return _result("import_failed", sha, declared_version, src, copy_path, None, report, warnings, errors)

    status = parsed["manifest"]["status"]
    problems = _semantic_problems(contents, status)
    escalated = False
    if status == "draft":
        warnings.append("draft awaiting approval")
        problems.extend(f"UNRESOLVED marker in {loc}" for loc in _unresolved_locations(contents))
    if problems:
        escalated = True
        _write_escalation(target, declared_version, sha, status, problems)
    state_status = "initialized" if (status == "approved" and not escalated) else "waiting_planner"
    if bootstrapping:
        _write_project_manifest(target, repo, bootstrap_id or target.name)
        _write_workflow_state(target, declared_version, state_status, "bootstrap")
        _write_task_records(target, contents["tasks.md"]) if state_status == "initialized" else None
    else:
        _update_workflow_state(target, declared_version, state_status if (escalated or status == "draft") else None)
    if escalated:
        report = _write_report(target, src, copy_path, sha, declared_version, "imported", "escalated", warnings, reasons=problems, materialized_path=materialized)
        return _result("imported", sha, declared_version, src, copy_path, materialized, report, warnings, [], escalated=True)
    report = _write_report(target, src, copy_path, sha, declared_version, "imported", "valid", warnings, materialized_path=materialized)
    return _result("imported", sha, declared_version, src, copy_path, materialized, report, warnings, [])


def _validate_project_id(value: str, option: str = "project id") -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) or value in {".", ".."} or ".." in value:
        raise ValueError(f"{option} must contain only safe path-segment characters and must not contain '..'")


def _rebase_bootstrap_paths(result: dict[str, Any], staging: Path, target: Path) -> dict[str, Any]:
    """Rewrite staged report and result paths before the workflow is renamed."""
    staging_text = str(staging)
    target_text = str(target)
    report_path = Path(result["report_path"]) if result.get("report_path") else None
    if report_path is not None and report_path.is_file():
        report = load_json(report_path)
        if isinstance(report, dict):
            for key in ("copy_path", "materialized_path"):
                value = report.get(key)
                if isinstance(value, str) and value.startswith(staging_text):
                    report[key] = target_text + value[len(staging_text):]
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    for key in ("copy_path", "materialized_path", "report_path"):
        value = result.get(key)
        if isinstance(value, str) and value.startswith(staging_text):
            result[key] = target_text + value[len(staging_text):]
    return result


def _new_workflow_stage(root: Path, target: Path) -> Path:
    """Create an undiscoverable workflow staging directory under runtime_root."""
    global _STAGING_COUNTER
    _STAGING_COUNTER += 1
    stage = root / f".workflow-stage-{os.getpid()}-{_STAGING_COUNTER}"
    if stage.exists() or target.exists():
        raise ValueError(f"workflow directory already exists: {target}")
    stage.mkdir()
    _make_project_layout(stage)
    return stage


def _workflow_id_exists(root: Path, project_id: str) -> bool:
    """Return whether any valid workflow manifest already uses this id."""
    if not root.is_dir():
        return False
    for project in root.iterdir():
        manifest_path = project / "runtime" / "project.json"
        if not project.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict) and manifest.get("project_id") == project_id:
            return True
    return False


def import_bundle(
    bundle_path: Path,
    repository: Path,
    config_path: Path,
    project_id: str | None = None,
    new_project_id: str | None = None,
) -> dict[str, Any]:
    """Deterministic explicit Bundle import entry point (AC-16, AC-19)."""
    if project_id is not None and new_project_id is not None:
        raise ValueError("project_selection_conflict: --project-id and --new-project-id are mutually exclusive")
    config = runtime_config(config_path)
    repo = Path(repository).resolve()
    src = Path(bundle_path)
    if not src.is_file():
        raise ValueError(f"bundle file not found: {src}")
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text: str | None = None
    decode_error: str | None = None
    try:
        text = raw.decode("utf-8")
        text = text.replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        decode_error = str(exc)
    parsed: dict[str, Any] | None = None
    parse_errors: list[str] = []
    metadata: dict[str, Any] | None = None
    if text is not None:
        parsed, parse_errors = parse_bundle(text)
        if parsed is not None:
            try:
                value = json.loads(parsed["sections"]["metadata.json"])
                if isinstance(value, dict):
                    metadata = value
            except (KeyError, json.JSONDecodeError):
                metadata = None
    root = Path(config["runtime_root"]).expanduser().resolve()
    projects = _associated_projects(root, repo)
    if project_id is not None:
        _validate_project_id(project_id)
    if new_project_id is not None:
        _validate_project_id(new_project_id, "new project id")
    target: Path | None = None
    bootstrapping = False
    staged_bootstrap = False
    staging: Path | None = None
    bootstrap_id: str | None = None
    if projects:
        matches = [p for p in projects if _project_matches(p, project_id)] if project_id else []
        if new_project_id is not None:
            if _workflow_id_exists(root, new_project_id):
                raise ValueError(f"project_id_exists: --new-project-id already names an existing workflow: {new_project_id}")
            bootstrap_id = new_project_id
            name = _project_directory_name(config, new_project_id)
            target = (root / name).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError("project directory resolves outside runtime_root") from exc
            staged_bootstrap = True
            bootstrapping = True
        elif len(projects) == 1 and (project_id is None or matches):
            target = projects[0]
        elif matches:
            target = matches[0]
        elif project_id is not None:
            raise ValueError(f"project_id_not_found: --project-id does not resolve to an existing workflow for this repository: {project_id}")
        else:
            return _result(
                "project_selection_required", sha, None, src, None, None, None, [],
                ["multiple workflows are associated with this repository; pass --project-id to select one"],
            )
    else:
        if project_id is not None:
            raise ValueError(f"project_id_not_found: --project-id does not resolve to an existing workflow for this repository: {project_id}")
        candidate = str(new_project_id) if new_project_id is not None else (metadata or {}).get("project_name") or repo.name
        bootstrap_id = str(candidate)
        name = _project_directory_name(config, str(candidate))
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("project directory resolves outside runtime_root") from exc
        if target.exists() and new_project_id is not None:
            raise ValueError(f"project directory already exists: {target}")
        if new_project_id is not None:
            staged_bootstrap = True
        bootstrapping = True
    assert target is not None
    if staged_bootstrap:
        try:
            staging = _new_workflow_stage(root, target)
            result = _import_attempt(src, sha, text, decode_error, parsed, parse_errors, staging, repo, True, bootstrap_id=bootstrap_id)
            if result["status"] not in REPORT_SUCCESS_STATUSES and result["status"] != "imported":
                rmtree(staging, ignore_errors=True)
                return result
            result = _rebase_bootstrap_paths(result, staging, target)
            _atomic_rename(staging, target)
            return result
        except (OSError, RuntimeError, ValueError) as exc:
            if staging is not None and staging.exists():
                rmtree(staging, ignore_errors=True)
            raise ValueError(f"new workflow bootstrap failed: {exc}") from exc
    target.mkdir(parents=True, exist_ok=True)
    _make_project_layout(target)
    return _import_attempt(src, sha, text, decode_error, parsed, parse_errors, target, repo, bootstrapping, bootstrap_id=bootstrap_id)


def auto_import(repository: Path, config_path: Path, project_id: str | None = None) -> dict[str, Any]:
    """Startup auto-discovery entry point (FR-5, AC-17)."""
    config = runtime_config(config_path)
    repo = Path(repository).resolve()
    root = Path(config["runtime_root"]).expanduser().resolve()
    projects = _associated_projects(root, repo)
    if not projects:
        return _result("no_pending", None, None, None, None, None, None, ["no workflow exists for this repository"], [])
    if any(_has_usable_approved(project, repo) for project in projects):
        return _result(
            "skipped_approved", None, None, None, None, None, None,
            ["a usable Approved Contract exists; pending Bundles are not auto-imported (explicit import remains available)"], [],
        )
    if project_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project_id):
        raise ValueError("project id must contain only letters, digits, dot, underscore, or hyphen")
    if len(projects) == 1 and (project_id is None or _project_matches(projects[0], project_id)):
        target = projects[0]
    elif project_id is not None:
        matches = [p for p in projects if _project_matches(p, project_id)]
        if not matches:
            raise ValueError(f"--project-id does not resolve to an existing workflow for this repository: {project_id}")
        target = matches[0]
    else:
        return _result(
            "project_selection_required", None, None, None, None, None, None, [],
            ["multiple workflows are associated with this repository; pass --project-id to select one"],
        )
    pending = _pending_bundles(target)
    if len(pending) > 1:
        return _result(
            "multiple_pending_bundles", None, None, None, None, None, None, [],
            [f"{len(pending)} pending Bundles in contract/imports/ require user selection: " + ", ".join(p.name for p in pending)],
        )
    if not pending:
        return _result("no_pending", None, None, None, None, None, None, ["no pending Bundle in contract/imports/"], [])
    src = pending[0]
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text: str | None = None
    decode_error: str | None = None
    try:
        text = raw.decode("utf-8")
        text = text.replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        decode_error = str(exc)
    parsed: dict[str, Any] | None = None
    parse_errors: list[str] = []
    if text is not None:
        parsed, parse_errors = parse_bundle(text)
    return _import_attempt(src, sha, text, decode_error, parsed, parse_errors, target, repo, False)


def main() -> int:
    parser = argparse.ArgumentParser(description="PSC Contract/runtime helper")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate-contract", help="validate an immutable contract/vN directory")
    val.add_argument("contract_dir", type=Path, help="path to the contract/vN directory to validate")
    val.add_argument("--repository", type=Path, help="repository path the Contract is associated with")
    disc = sub.add_parser("discover", help="list workflow projects associated with a repository")
    disc.add_argument("--repository", type=Path, required=True, help="repository path to match against runtime/project.json")
    disc.add_argument("--runtime-config", type=Path, required=True, help="path to .agentic-sdlc/runtime.json")
    boot = sub.add_parser("bootstrap", help="bootstrap a new workflow from an approved contract/vN directory")
    boot.add_argument("contract_dir", type=Path, help="path to an approved Contract directory to copy")
    boot.add_argument("--repository", type=Path, required=True, help="repository path the Contract is associated with")
    boot.add_argument("--runtime-config", type=Path, required=True, help="path to .agentic-sdlc/runtime.json")
    boot.add_argument("--project-id", help="explicit project id for the new workflow")
    imp = sub.add_parser("import-bundle", help="import a PSC-CONTRACT-BUNDLE markdown file deterministically")
    imp.add_argument("bundle_path", type=Path, help="path to the PSC-CONTRACT-BUNDLE file (may be outside the repository)")
    imp.add_argument("--repository", type=Path, required=True, help="repository path the Contract is associated with")
    imp.add_argument("--runtime-config", type=Path, required=True, help="path to .agentic-sdlc/runtime.json")
    imp.add_argument("--project-id", help="select an existing workflow project id")
    imp.add_argument("--new-project-id", help="explicitly create a new independent workflow project id; mutually exclusive with --project-id")
    ai = sub.add_parser("auto-import", help="startup auto-discovery: import a single pending Bundle from contract/imports/ when no usable Approved Contract exists")
    ai.add_argument("--repository", type=Path, required=True, help="repository path to match against runtime/project.json")
    ai.add_argument("--runtime-config", type=Path, required=True, help="path to .agentic-sdlc/runtime.json")
    ai.add_argument("--project-id", help="explicit target workflow project id when several workflows are associated")
    activate = sub.add_parser("activate-contract", help="activate the highest valid Approved Contract and rebuild the effective task queue")
    activate.add_argument("--project", type=Path, required=True, help="workflow project directory")
    activate.add_argument("--repository", type=Path, required=True, help="repository path to validate against the Contract")
    owner = sub.add_parser("set-execution-owner", help="persistently hand task execution between Executor and Supervisor")
    owner.add_argument("--project", type=Path, required=True, help="workflow project directory")
    owner.add_argument("--owner", choices=sorted(EXECUTION_OWNERS), required=True, help="new task execution owner")
    owner.add_argument("--reason", required=True, help="auditable reason for the handoff")
    resolve = sub.add_parser("resolve-retry-exhaustion", help="resolve a blocked task-local Executor retry budget")
    resolve.add_argument("--project", type=Path, required=True, help="workflow project directory")
    resolve.add_argument("--decision", choices=sorted(RETRY_EXHAUSTION_DECISIONS), required=True, help="reset exhausted task budget and continue with E, or switch the task to S")
    args = parser.parse_args()
    try:
        if args.command == "validate-contract":
            result = validate_contract(args.contract_dir, args.repository)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("valid", True) else 2
        if args.command == "discover":
            result = discover(args.repository, args.runtime_config)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "bootstrap":
            result = bootstrap(args.contract_dir, args.repository, args.runtime_config, args.project_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "import-bundle":
            result = import_bundle(args.bundle_path, args.repository, args.runtime_config, args.project_id, args.new_project_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] in EXIT0_IMPORT else 2
        if args.command == "activate-contract":
            result = activate_contract(args.project, args.repository)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "set-execution-owner":
            result = set_execution_owner(args.project, args.owner, args.reason)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "resolve-retry-exhaustion":
            result = resolve_retry_exhaustion(args.project, args.decision)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        result = auto_import(args.repository, args.runtime_config, args.project_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] in EXIT0_AUTO else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
