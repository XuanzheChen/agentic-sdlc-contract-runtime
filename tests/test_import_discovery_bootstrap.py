"""Entry points, startup discovery, bootstrap, and version-change handoff tests
(AC-16..AC-20).

All tests run against tmp_path-only fixtures, never invoke an Executor, and
never touch product source.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from conftest import (
    build_bundle_text,
    contract_versions,
    default_sections,
    find_report,
    project_dir,
    run_cli,
    write_bundle,
    write_external_bundle,
)

PINNED_STATE_KEYS = {
    "schema_version", "contract_version", "current_task", "status",
    "attempt", "last_completed_task", "last_stage", "updated_at",
}


def make_workflow(runtime_root, repo, project_name="manual"):
    """Create a workflow project with no Contract (no usable Approved
    Contract), the scenario startup discovery targets."""
    project = runtime_root / project_name
    (project / "contract" / "imports").mkdir(parents=True)
    (project / "runtime").mkdir(parents=True)
    manifest = {"project_id": project_name, "repository": str(repo), "created_at": "2026-08-25T00:00:00+00:00", "baseline_commit": None, "baseline_status": ""}
    (project / "runtime" / "project.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    state = {"schema_version": 1, "contract_version": 1, "current_task": None, "status": "initialized", "attempt": 0, "last_completed_task": None, "last_stage": None, "updated_at": "2026-08-25T00:00:00+00:00"}
    (project / "runtime" / "workflow_state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return project


def tree_hash(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- AC-16 explicit target selection ---------------------------------------------

def test_explicit_import_single_project(helper, tmp_path, tmp_repo, tmp_runtime):
    """After bootstrap, a single associated workflow receives the import."""
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    project = project_dir(tmp_path)
    v2 = write_external_bundle(tmp_path, build_bundle_text(version=2), name="v2.md")
    proc = run_cli("import-bundle", str(v2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert contract_versions(project) == [1, 2]
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["contract_version"] == 2


def test_project_selection_required_no_guess(helper, tmp_path, tmp_repo, tmp_runtime):
    """Several associated workflows without --project-id: no import, no report,
    no guess; exit 2."""
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "first")
    assert proc.returncode == 0, proc.stdout
    project = project_dir(tmp_path)
    # Create a second workflow via the existing bootstrap helper.
    proc = run_cli("bootstrap", str(project / "contract" / "v1"), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "second")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    second = tmp_path / "runtime_root" / "second"
    assert (second / "runtime" / "project.json").is_file()
    reports_before = len(list((project / "contract" / "imports" / "reports").glob("import-*.json")))
    copies_before = len(list((project / "contract" / "imports").glob("*.bundle.md")))

    v2 = write_external_bundle(tmp_path, build_bundle_text(version=2), name="v2.md")
    proc = run_cli("import-bundle", str(v2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "project_selection_required"
    assert any("--project-id" in e for e in result["errors"])
    # No import attempt: no new report, no provenance copy, no new version.
    assert len(list((project / "contract" / "imports" / "reports").glob("import-*.json"))) == reports_before
    assert len(list((project / "contract" / "imports").glob("*.bundle.md"))) == copies_before
    assert contract_versions(project) == [1]
    assert contract_versions(second) == [1]


def test_project_selection_explicit_project_id(helper, tmp_path, tmp_repo, tmp_runtime):
    """--project-id selects the explicit target (and must resolve or fail)."""
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "first")
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    proc = run_cli("bootstrap", str(project / "contract" / "v1"), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "second")
    assert proc.returncode == 0
    second = tmp_path / "runtime_root" / "second"

    v2 = write_external_bundle(tmp_path, build_bundle_text(version=2), name="v2.md")
    proc = run_cli("import-bundle", str(v2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "second")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert str(tmp_path / "runtime_root" / "second" / "contract" / "v2") == result["materialized_path"]
    assert contract_versions(second) == [1, 2]
    assert contract_versions(project) == [1]  # untouched

    # Unknown --project-id fails with exit 2 and no import (error on stderr,
    # consistent with the existing helper's configuration-error handling).
    v3 = write_external_bundle(tmp_path, build_bundle_text(version=3), name="v3.md")
    proc = run_cli("import-bundle", str(v3), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "ghost")
    assert proc.returncode == 2
    assert "does not resolve" in proc.stderr
    assert contract_versions(second) == [1, 2]


def test_explicit_import_never_requires_bundle_inside_repo(helper, tmp_path, tmp_repo, tmp_runtime):
    """The Bundle path is never required to be inside the repository."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="outside.md")
    assert not str(bundle).startswith(str(tmp_repo.resolve()))
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "imported"
    assert contract_versions(project_dir(tmp_path)) == [1]


# --- AC-17 auto-discovery ---------------------------------------------------------

def test_auto_discovery_single(helper, tmp_path, tmp_repo, tmp_runtime):
    """No usable Approved Contract + exactly one pending Bundle -> imported."""
    project = make_workflow(tmp_path / "runtime_root", tmp_repo)
    pending = write_bundle(tmp_path, build_bundle_text(), name="pending-src.md")
    imports = project / "contract" / "imports"
    pending_copy = imports / "pending-src.md"
    pending_copy.write_bytes(pending.read_bytes())
    assert not any((project / "contract").glob("v*"))
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert contract_versions(project) == [1]
    assert result["source"] == str(pending_copy)
    report = find_report(project / "contract" / "imports" / "reports", result["sha256"], "imported")
    assert report is not None
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "initialized"


def test_auto_discovery_ambiguous(helper, tmp_path, tmp_repo, tmp_runtime):
    """Two pending Bundles: no import, no guess, no report; exit 2."""
    project = make_workflow(tmp_path / "runtime_root", tmp_repo)
    imports = project / "contract" / "imports"
    for name, version in (("a.md", 1), ("b.md", 1)):
        text = build_bundle_text(version=version)
        (imports / name).write_text(text, encoding="utf-8")
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "multiple_pending_bundles"
    assert contract_versions(project) == []
    assert not list((project / "contract" / "imports" / "reports").glob("import-*.json"))


def test_auto_discovery_skipped_with_approved(helper, tmp_path, tmp_repo, tmp_runtime):
    """A usable Approved Contract exists: pending Bundles are not auto-imported."""
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    pending = write_bundle(tmp_path, build_bundle_text(version=2), name="pending-v2.md")
    (project / "contract" / "imports" / "pending-v2.md").write_bytes(pending.read_bytes())
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["status"] == "skipped_approved"
    assert contract_versions(project) == [1]  # no v2 materialized


def test_auto_discovery_mechanical_defect(helper, tmp_path, tmp_repo, tmp_runtime):
    """A pending Bundle with a mechanical defect: import_failed report, no
    workflow-state change, no waiting_planner."""
    project = make_workflow(tmp_path / "runtime_root", tmp_repo)
    (project / "contract" / "imports" / "bad.md").write_text(
        build_bundle_text(end_marker=False), encoding="utf-8")
    state_before = (project / "runtime" / "workflow_state.json").read_bytes()
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert contract_versions(project) == []
    reports = list((project / "contract" / "imports" / "reports").glob("import-*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["status"] == "import_failed"
    assert (project / "runtime" / "workflow_state.json").read_bytes() == state_before
    review = project / "review"
    assert not review.exists() or not list(review.glob("escalation-*.md"))


def test_auto_discovery_already_imported_skipped(helper, tmp_path, tmp_repo, tmp_runtime):
    """A Bundle with a successful import record is no longer pending."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="once.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    copies_before = len(list((project / "contract" / "imports").glob("*.bundle.md")))
    reports_before = len(list((project / "contract" / "imports" / "reports").glob("import-*.json")))
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    # A usable Approved Contract now exists, so startup discovery stops at the
    # approved gate: nothing is re-imported and no new artifacts appear.
    assert result["status"] == "skipped_approved"
    assert copies_before == len(list((project / "contract" / "imports").glob("*.bundle.md")))
    assert reports_before == len(list((project / "contract" / "imports" / "reports").glob("import-*.json")))


def test_auto_discovery_ignores_reports_subdirectory(helper, tmp_path, tmp_repo, tmp_runtime):
    """Discovery never treats files under contract/imports/reports/ as pending."""
    project = make_workflow(tmp_path / "runtime_root", tmp_repo)
    reports_dir = project / "contract" / "imports" / "reports"
    reports_dir.mkdir()
    (reports_dir / "import-not-a-bundle.json").write_text("{}", encoding="utf-8")
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "no_pending"
    assert contract_versions(project) == []


# --- AC-18 startup order (behavioral: import precedes Contract selection) ---------

def test_startup_sequence_pending_import_precedes_selection(helper, tmp_path, tmp_repo, tmp_runtime):
    project = make_workflow(tmp_path / "runtime_root", tmp_repo)
    pending = write_bundle(tmp_path, build_bundle_text(), name="startup-src.md")
    (project / "contract" / "imports" / "startup-src.md").write_bytes(pending.read_bytes())
    # Before import: no approved contract is discoverable.
    disc = json.loads(run_cli("discover", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime)).stdout)
    assert disc["projects"][0]["approved_versions"] == []
    # Startup import happens before Contract selection: after auto-import the
    # same discover call sees the imported version as approved and usable.
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "imported"
    disc = json.loads(run_cli("discover", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime)).stdout)
    project_entry = [p for p in disc["projects"] if p["path"] == str(project)][0]
    assert project_entry["active"] is True
    assert project_entry["approved_versions"] == [1]


def test_startup_sequence_explicit_path_version_selected(helper, tmp_path, tmp_repo, tmp_runtime):
    """A user-provided explicit Bundle path is handled before normal Contract
    loading: the explicit Bundle's version is the one selected afterward."""
    project = make_workflow(tmp_path / "runtime_root", tmp_repo)
    explicit = write_external_bundle(tmp_path, build_bundle_text(version=2), name="explicit-v2.md")
    proc = run_cli("import-bundle", str(explicit), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "imported"
    assert contract_versions(project) == [2]
    # A different pending v1 Bundle under imports must not be auto-imported now
    # that a usable Approved Contract (v2) exists.
    pending = write_bundle(tmp_path, build_bundle_text(version=1), name="pending-v1.md")
    (project / "contract" / "imports" / "pending-v1.md").write_bytes(pending.read_bytes())
    proc = run_cli("auto-import", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "skipped_approved"
    assert contract_versions(project) == [2]
    disc = json.loads(run_cli("discover", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime)).stdout)
    assert disc["projects"][0]["approved_versions"] == [2]


# --- AC-19 bootstrap from an explicit Bundle --------------------------------------

def test_approved_bootstrap_full_layout(helper, tmp_path, tmp_repo, tmp_runtime):
    sections = default_sections(version=1)
    metadata = json.loads(sections["metadata.json"])
    metadata["project_name"] = "metaproject"
    sections["metadata.json"] = json.dumps(metadata, indent=2) + "\n"
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="bootstrap.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    project = project_dir(tmp_path)
    # Naming embeds the project_name candidate (project_naming template).
    assert "metaproject" in project.name

    # Normal workflow layout.
    for rel in ["contract", "developing/tasks", "developing/artifacts", "review", "runtime", "logs/executor"]:
        assert (project / rel).is_dir(), rel
    # Project manifest: project_id is the raw chosen naming candidate.
    manifest = json.loads((project / "runtime" / "project.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == "metaproject"
    assert Path(manifest["repository"]).resolve() == tmp_repo.resolve()
    assert manifest["created_at"]
    assert "baseline_commit" in manifest and "baseline_status" in manifest
    # Workflow state (approved bootstrap -> initialized).
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert set(state.keys()) == PINNED_STATE_KEYS
    assert state["schema_version"] == 1
    assert state["contract_version"] == 1
    assert state["current_task"] is None
    assert state["status"] == "initialized"
    assert state["attempt"] == 0
    assert state["last_completed_task"] is None
    assert state["last_stage"] == "bootstrap"
    assert state["updated_at"]
    # validate-contract passes on the materialized vN; discover lists active.
    vcheck = run_cli("validate-contract", str(project / "contract" / "v1"), "--repository", str(tmp_repo))
    assert vcheck.returncode == 0
    assert json.loads(vcheck.stdout)["valid"] is True
    disc = json.loads(run_cli("discover", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime)).stdout)
    assert disc["projects"][0]["active"] is True
    assert disc["projects"][0]["approved_versions"] == [1]


def test_approved_bootstrap_repository_name_fallback_and_determinism(helper, tmp_path, tmp_repo, tmp_runtime):
    """Without project_name, the repository basename is the naming candidate;
    same inputs produce the same directory name."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="fallback.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    assert "repo" in project.name  # repository directory name fallback
    # Same inputs -> same name, in a fresh runtime root.
    import shutil
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    config = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    config["runtime_root"] = str(fresh)
    (tmp_path / "runtime.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    fresh_projects = [p for p in fresh.iterdir() if p.is_dir()]
    assert len(fresh_projects) == 1
    assert fresh_projects[0].name == project.name


def test_draft_bootstrap_never_schedules(helper, tmp_path, tmp_repo, tmp_runtime):
    """A draft Bundle imports but cannot start coding: waiting_planner, no task
    artifacts, no executor."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(status="draft"), name="draft.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert result["version"] == 1
    project = project_dir(tmp_path)
    assert contract_versions(project) == [1]
    assert (project / "contract" / "v1" / "metadata.json").is_file()
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "waiting_planner"
    assert state["current_task"] is None
    # Hold reason recorded in the report (no new state key).
    report = find_report(project / "contract" / "imports" / "reports", result["sha256"], "imported")
    assert report is not None
    assert any("draft awaiting approval" in w for w in report["warnings"])
    # No task artifacts created.
    assert list((project / "developing" / "tasks").glob("T-*.md")) == []
    assert list((project / "developing" / "artifacts").glob("T-*")) == []
    # No escalation for a clean draft.
    assert list((project / "review").glob("escalation-*.md")) == []
    # Mechanical validation still passes.
    vcheck = run_cli("validate-contract", str(project / "contract" / "v1"), "--repository", str(tmp_repo))
    assert json.loads(vcheck.stdout)["valid"] is True


def test_draft_with_unresolved_records_escalation(helper, tmp_path, tmp_repo, tmp_runtime):
    sections = default_sections(status="draft")
    sections["implementation.md"] = sections["implementation.md"] + "\nUNRESOLVED: choose a storage backend.\n"
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections, status="draft"), name="draft-unresolved.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert result.get("escalated") is True
    project = project_dir(tmp_path)
    esc = list((project / "review").glob("escalation-*.md"))
    assert len(esc) == 1
    esc_text = esc[0].read_text(encoding="utf-8")
    assert "waiting_planner" in esc_text
    assert "UNRESOLVED" in esc_text
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "waiting_planner"
    report = find_report(project / "contract" / "imports" / "reports", result["sha256"], "imported")
    assert report["outcome"]["type"] == "escalated"
    assert any("UNRESOLVED marker" in r for r in report["outcome"]["reasons"])


def test_draft_import_into_existing_workflow_holds(helper, tmp_path, tmp_repo, tmp_runtime):
    """A later draft into an existing workflow also never schedules."""
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    tasks_before = sorted(p.name for p in (project / "developing" / "tasks").glob("T-*.md")) if (project / "developing" / "tasks").exists() else []
    draft = write_external_bundle(tmp_path, build_bundle_text(version=2, status="draft"), name="draft-v2.md")
    proc = run_cli("import-bundle", str(draft), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "imported"
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "waiting_planner"
    assert state["contract_version"] == 2
    assert sorted(p.name for p in (project / "developing" / "tasks").glob("T-*.md")) == tasks_before


# --- AC-20 version-change handoff -------------------------------------------------

def test_version_change_policy_handoff(helper, tmp_path, tmp_repo, tmp_runtime):
    """Newer approved version over an existing workflow: imported; v1
    byte-identical; state reflects only the documented key set; policy shape
    declared in v2 metadata; nothing scheduled by the importer."""
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    v1_before = tree_hash(project / "contract" / "v1")
    state_before_keys = set(json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8")).keys())

    sections = default_sections(version=2, status="approved")
    sections["requirements.md"] = sections["requirements.md"] + "\n## REQ-010\n\nRequirement ten.\n"
    sections["acceptance.md"] = sections["acceptance.md"] + "\n## AC-010\n\nCovers REQ-010.\n"
    tasks = sections["tasks.md"]
    tasks = tasks.replace(
        "Acceptance:\n- AC-002\n",
        "Acceptance:\n- AC-002\n- AC-010\n\n",
    )
    sections["tasks.md"] = tasks
    meta = json.loads(sections["metadata.json"])
    meta["version"] = 2
    meta["workflow_policy"] = {"restart": "pending_only"}
    sections["metadata.json"] = json.dumps(meta, indent=2) + "\n"
    v2 = write_external_bundle(tmp_path, build_bundle_text(sections=sections, version=2), name="v2.md")
    proc = run_cli("import-bundle", str(v2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert contract_versions(project) == [1, 2]
    # v1 untouched.
    assert tree_hash(project / "contract" / "v1") == v1_before
    # The declared policy is preserved verbatim in the imported v2 metadata.
    v2_meta = json.loads((project / "contract" / "v2" / "metadata.json").read_text(encoding="utf-8"))
    assert v2_meta["workflow_policy"] == {"restart": "pending_only"}
    # State: only the documented key set; contract_version bumped; no new
    # invalidation system, no marker artifacts.
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert set(state.keys()) == state_before_keys == PINNED_STATE_KEYS
    assert state["schema_version"] == 1
    assert state["contract_version"] == 2
    assert state["status"] == "initialized"  # no scheduling, no interim status
    assert state["last_stage"] == "import"
    contract_entries = sorted(p.name for p in (project / "contract").iterdir() if p.is_dir())
    assert sorted(contract_entries) == ["imports", "v1", "v2"]
    # No task artifacts were created or modified by the import.
    assert len(list((project / "developing" / "tasks").glob("T-*.md"))) == 2


def test_version_change_lower_version_leaves_highest(helper, tmp_path, tmp_repo, tmp_runtime):
    """Importing a lower free version materializes v<declared> but keeps
    contract_version at the highest."""
    v2 = write_external_bundle(tmp_path, build_bundle_text(version=2), name="v2-first.md")
    proc = run_cli("import-bundle", str(v2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    # v1 (different content from v2) is a free version and materializes exactly.
    v1 = write_external_bundle(tmp_path, build_bundle_text(version=1), name="v1-later.md")
    proc = run_cli("import-bundle", str(v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "imported"
    assert contract_versions(project) == [1, 2]
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["contract_version"] == 2  # stays at the highest