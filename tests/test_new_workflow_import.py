"""Regression coverage for explicit independent workflow bootstrap.

These tests exercise only the deterministic PSC Runtime helper with isolated
temporary repositories. They never invoke an Executor or product code.
"""
from __future__ import annotations

import json

import pytest

from conftest import build_bundle_text, contract_versions, project_dir, run_cli, write_external_bundle


def _projects(tmp_path):
    return sorted(path for path in (tmp_path / "runtime_root").iterdir() if path.is_dir())


def _complete_workflow(project):
    state_path = project / "runtime" / "workflow_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "workflow_passed"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def test_new_project_bootstraps_independent_v1_after_completed_workflow(tmp_path, tmp_repo, tmp_runtime):
    first = write_external_bundle(tmp_path, build_bundle_text(version=1), name="workflow-a.md")
    assert run_cli("import-bundle", str(first), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime)).returncode == 0
    workflow_a = project_dir(tmp_path)
    _complete_workflow(workflow_a)

    second = write_external_bundle(tmp_path, build_bundle_text(version=1), name="workflow-b.md")
    proc = run_cli(
        "import-bundle", str(second), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--new-project-id", "Workflow-B",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    workflow_b = next(path for path in _projects(tmp_path) if path != workflow_a)
    assert result["status"] == "imported"
    assert contract_versions(workflow_a) == [1]
    assert contract_versions(workflow_b) == [1]
    assert result["materialized_path"] == str(workflow_b / "contract" / "v1")
    manifest = json.loads((workflow_b / "runtime" / "project.json").read_text(encoding="utf-8"))
    state = json.loads((workflow_b / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == "Workflow-B"
    assert manifest["repository"] == str(tmp_repo.resolve())
    assert state["status"] == "initialized"
    assert state["contract_version"] == 1


def test_existing_and_unknown_project_selection_fail_closed(tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(version=1), name="first.md")
    assert run_cli(
        "import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--new-project-id", "Workflow-A",
    ).returncode == 0
    workflow_a = project_dir(tmp_path)
    before = {str(path.relative_to(workflow_a)): path.read_bytes() for path in workflow_a.rglob("*") if path.is_file()}
    other = write_external_bundle(tmp_path, build_bundle_text(version=1), name="other.md")

    unknown = run_cli(
        "import-bundle", str(other), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--project-id", "unknown-id",
    )
    assert unknown.returncode == 2
    assert "project_id_not_found" in unknown.stderr
    assert _projects(tmp_path) == [workflow_a]

    existing = run_cli(
        "import-bundle", str(other), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--new-project-id", "Workflow-A",
    )
    assert existing.returncode == 2
    assert "project_id_exists" in existing.stderr
    assert _projects(tmp_path) == [workflow_a]
    assert before == {str(path.relative_to(workflow_a)): path.read_bytes() for path in workflow_a.rglob("*") if path.is_file()}


def test_selector_conflict_and_path_traversal_create_nothing(helper, tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="bundle.md")
    with pytest.raises(ValueError, match="project_selection_conflict"):
        helper.import_bundle(bundle, tmp_repo, tmp_runtime, project_id="A", new_project_id="B")

    conflict = run_cli(
        "import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--project-id", "A", "--new-project-id", "B",
    )
    assert conflict.returncode == 2
    assert "project_selection_conflict" in conflict.stderr
    assert _projects(tmp_path) == []

    proc = run_cli(
        "import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--new-project-id", "../outside",
    )
    assert proc.returncode == 2
    assert "must not contain '..'" in proc.stderr
    assert _projects(tmp_path) == []
    assert not (tmp_path / "outside").exists()


def test_invalid_new_workflow_import_rolls_back_staging(tmp_path, tmp_repo, tmp_runtime):
    invalid = write_external_bundle(tmp_path, build_bundle_text(first_line="# invalid"), name="invalid.md")
    proc = run_cli(
        "import-bundle", str(invalid), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime),
        "--new-project-id", "Workflow-B",
    )
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert _projects(tmp_path) == []
    assert not list((tmp_path / "runtime_root").glob(".workflow-stage-*"))
