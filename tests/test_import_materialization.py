"""Materialization safety tests (AC-11..AC-13): atomic failure behavior,
immutability and same-version conflicts, and SHA-256 idempotency.

All tests run against tmp_path-only fixtures, never invoke an Executor, and
never touch product source.
"""
from __future__ import annotations

import hashlib
import json

from conftest import (
    build_bundle_text,
    contract_versions,
    default_sections,
    project_dir,
    run_cli,
    sha256_bytes,
    write_external_bundle,
)


def hash_tree(root) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _residue(project):
    contract_dir = project / "contract"
    residue = []
    if contract_dir.is_dir():
        for p in contract_dir.rglob("*"):
            if p.is_dir() and p.name.startswith(".import-stage-"):
                residue.append(str(p))
            if p.is_file() and (p.name.endswith(".tmp") or p.name.endswith(".partial")):
                residue.append(str(p))
    return residue


# --- AC-11 atomic failure behavior: no partial Contract version -----------------

def test_no_partial_on_mechanical_failure(helper, tmp_path, tmp_repo, tmp_runtime):
    """Every mechanical failure leaves no vN and no staging residue."""
    malformed = build_bundle_text(first_line="# WRONG HEADING")
    bundle = write_external_bundle(tmp_path, malformed, name="malformed.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "atomic")
    assert proc.returncode == 2
    project = project_dir(tmp_path)
    assert contract_versions(project) == []
    assert _residue(project) == []


def test_no_partial_on_injected_rename_failure(helper, tmp_path, tmp_repo, tmp_runtime):
    """A deterministic rename failure leaves no partial vN and no residue (AC-11b)."""

    def boom(src, dst):
        raise OSError("injected rename failure")

    helper._atomic_rename = boom
    try:
        bundle = write_external_bundle(tmp_path, build_bundle_text(), name="rename-fail.md")
        result = helper.import_bundle(bundle, tmp_repo, tmp_runtime, project_id="fail-rename")
    finally:
        del helper._atomic_rename
    assert result["status"] == "import_failed"
    assert contract_versions(project_dir(tmp_path)) == []
    assert _residue(project_dir(tmp_path)) == []
    # The failed attempt still produces a report and a provenance copy.
    reports = list((project_dir(tmp_path) / "contract" / "imports" / "reports").glob("import-*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["status"] == "import_failed"


def test_no_partial_on_injected_write_failure(helper, tmp_path, tmp_repo, tmp_runtime, monkeypatch):
    """A deterministic staging-write failure leaves no partial vN and no residue."""
    from pathlib import Path

    original_write_text = Path.write_text

    def failing_write(path, *args, **kwargs):
        if ".import-stage-" in str(path) and path.name == "requirements.md":
            raise OSError("injected staging write failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="write-fail.md")
    result = helper.import_bundle(bundle, tmp_repo, tmp_runtime, project_id="fail-write")
    assert result["status"] == "import_failed"
    project = project_dir(tmp_path)
    assert contract_versions(project) == []
    assert _residue(project) == []


def test_success_has_exactly_one_complete_vN(helper, tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="ok.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "success")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    project = project_dir(tmp_path)
    assert contract_versions(project) == [1]
    v1 = project / "contract" / "v1"
    assert sorted(p.name for p in v1.iterdir()) == sorted(
        ["metadata.json", "requirements.md", "acceptance.md", "implementation.md", "constraints.md", "tasks.md"]
    )
    assert _residue(project) == []


# --- AC-12 immutability and same-version conflicts -------------------------------

def test_version_conflict_immutable(helper, tmp_path, tmp_repo, tmp_runtime):
    """Same version, different content: version_conflict, exit 2, vN byte-identical."""
    bundle1 = write_external_bundle(tmp_path, build_bundle_text(), name="v1-a.md")
    proc1 = run_cli("import-bundle", str(bundle1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "conflict")
    assert proc1.returncode == 0, proc1.stdout
    project = project_dir(tmp_path)
    before = hash_tree(project / "contract" / "v1")

    sections = default_sections()
    sections["requirements.md"] = sections["requirements.md"] + "\n## REQ-099\n\nDifferent requirement.\n"
    bundle2 = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="v1-b.md")
    proc2 = run_cli("import-bundle", str(bundle2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "conflict")
    assert proc2.returncode == 2
    result = json.loads(proc2.stdout)
    assert result["status"] == "version_conflict"
    assert "already exists with different content" in " ".join(result["errors"])
    after = hash_tree(project / "contract" / "v1")
    assert before == after
    assert contract_versions(project) == [1]
    # The conflicting attempt still records a report (with the conflict status).
    reports = list((project / "contract" / "imports" / "reports").glob("import-*.json"))
    statuses = [json.loads(p.read_text(encoding="utf-8"))["status"] for p in reports]
    assert "version_conflict" in statuses
    assert statuses.count("imported") == 1


def test_free_version_materialized_exactly_beside_existing(helper, tmp_path, tmp_repo, tmp_runtime):
    """A Bundle declaring a free version materializes exactly v<declared>; other
    versions remain untouched and no renumbering occurs."""
    sections_v1 = default_sections(version=1)
    bundle_v1 = write_external_bundle(tmp_path, build_bundle_text(sections=sections_v1, version=1), name="v1.md")
    proc = run_cli("import-bundle", str(bundle_v1), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "freeversion")
    assert proc.returncode == 0, proc.stdout

    # Import a v2 Bundle (different content, higher version).
    sections_v2 = default_sections(version=2, status="approved")
    sections_v2["requirements.md"] = sections_v2["requirements.md"] + "\n## REQ-010\n\nRequirement ten.\n"
    bundle_v2 = write_external_bundle(tmp_path, build_bundle_text(sections=sections_v2, version=2), name="v2.md")
    proc = run_cli("import-bundle", str(bundle_v2), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "freeversion")
    assert proc.returncode == 0, proc.stdout

    # Now a v1-declaring Bundle that was never imported before (different bytes
    # from the first v1) must materialize as v3? No: it declares v1 -> conflict.
    # Instead use a genuinely free version: v1 was taken, v2 taken, so v3.
    sections_v3 = default_sections(version=3, status="approved")
    bundle_v3 = write_external_bundle(tmp_path, build_bundle_text(sections=sections_v3, version=3), name="v3.md")
    proc = run_cli("import-bundle", str(bundle_v3), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "freeversion")
    assert proc.returncode == 0, proc.stdout

    project = project_dir(tmp_path)
    assert contract_versions(project) == [1, 2, 3]
    assert (project / "contract" / "v3" / "metadata.json").is_file()
    # v1/v2 untouched by the v3 import.
    v1meta = json.loads((project / "contract" / "v1" / "metadata.json").read_text(encoding="utf-8"))
    v2meta = json.loads((project / "contract" / "v2" / "metadata.json").read_text(encoding="utf-8"))
    assert v1meta["version"] == 1 and v2meta["version"] == 2

    # A lower-numbered free version is never renumbered: import v4, then v0.5 is
    # impossible; instead verify that a declared v4 lands exactly at v4.
    sections_v4 = default_sections(version=4, status="approved")
    bundle_v4 = write_external_bundle(tmp_path, build_bundle_text(sections=sections_v4, version=4), name="v4.md")
    proc = run_cli("import-bundle", str(bundle_v4), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "freeversion")
    assert proc.returncode == 0, proc.stdout
    assert contract_versions(project) == [1, 2, 3, 4]


def test_existing_vN_never_modified_by_failed_reimport(helper, tmp_path, tmp_repo, tmp_runtime):
    """A malformed re-import targeting an existing project leaves vN untouched."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="ok.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "immutable")
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    before = hash_tree(project / "contract" / "v1")
    bad = write_external_bundle(tmp_path, build_bundle_text(end_marker=False), name="bad.md")
    proc = run_cli("import-bundle", str(bad), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "immutable")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["status"] == "import_failed"
    assert hash_tree(project / "contract" / "v1") == before


# --- AC-13 SHA-256 idempotency (decidable from disk state alone) ----------------

def test_same_hash_idempotent_two_processes(helper, tmp_path, tmp_repo, tmp_runtime):
    """Importing identical bytes twice in two separate processes: first
    'imported', second 'already_imported'; vN hashes unchanged."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="idem.md")
    proc1 = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "idem")
    assert proc1.returncode == 0
    result1 = json.loads(proc1.stdout)
    assert result1["status"] == "imported"
    project = project_dir(tmp_path)
    sha = sha256_bytes(bundle.read_bytes())
    assert result1["sha256"] == sha
    before = hash_tree(project / "contract" / "v1")

    proc2 = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "idem")
    assert proc2.returncode == 0
    result2 = json.loads(proc2.stdout)
    assert result2["status"] == "already_imported"
    assert result2["sha256"] == sha
    assert result2["version"] == 1
    assert hash_tree(project / "contract" / "v1") == before
    # No scheduling side effects: workflow state untouched by the second import.
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "initialized"
    assert state["contract_version"] == 1
    # Both attempts have reports; the decision is decidable from reports+contract.
    reports = sorted((project / "contract" / "imports" / "reports").glob("import-*.json"))
    assert len(reports) == 2
    statuses = [json.loads(r.read_text(encoding="utf-8"))["status"] for r in reports]
    assert sorted(statuses) == ["already_imported", "imported"]
    # A same-version different-bytes Bundle is version_conflict, proving the two
    # outcomes are distinct (already covered above; asserted here end-to-end).
    sections = default_sections()
    sections["implementation.md"] = "# changed\n"
    other = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="other.md")
    proc3 = run_cli("import-bundle", str(other), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "idem")
    assert proc3.returncode == 2
    assert json.loads(proc3.stdout)["status"] == "version_conflict"


def test_successful_report_but_missing_vN_is_loud_failure(helper, tmp_path, tmp_repo, tmp_runtime):
    """Provenance inconsistency (success report, no vN) is import_failed, never a
    silent 'imported'."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="inconsistent.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "inconsistent")
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    # Simulate a supervisor-side loss of the materialized version (e.g. external
    # deletion); the reports remain.
    import shutil
    shutil.rmtree(project / "contract" / "v1")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "inconsistent")
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("materialized contract version is missing" in e for e in result["errors"]), result["errors"]