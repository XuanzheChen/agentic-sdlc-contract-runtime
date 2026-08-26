"""Provenance and report tests (AC-14, AC-15): source preservation (copy, never
move or delete) and exactly one import report per attempt with pinned fields,
decidable from disk state alone.

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
    find_report,
    project_dir,
    run_cli,
    write_external_bundle,
)


def _report_paths(project) -> list:
    reports_dir = project / "contract" / "imports" / "reports"
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("import-*.json"))


def _load_reports(project) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in _report_paths(project)]


PINNED_FIELDS = ["source", "copy_path", "sha256", "version", "import_time", "outcome", "materialized_path", "warnings", "status"]


def assert_pinned_fields(report: dict) -> None:
    for field in PINNED_FIELDS:
        assert field in report, field
    assert report["outcome"]["type"] in {"valid", "escalated", "failed"}
    assert report["status"] in {"imported", "already_imported", "import_failed", "version_conflict"}
    assert isinstance(report["warnings"], list)
    assert isinstance(report["import_time"], str)


# --- AC-14 source preservation ---------------------------------------------------

def test_source_preserved_on_success(helper, tmp_path, tmp_repo, tmp_runtime):
    text = build_bundle_text()
    bundle = write_external_bundle(tmp_path, text)
    original_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    # Source file untouched at its original path.
    assert bundle.is_file()
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == original_hash
    # Exactly one byte-identical copy under contract/imports/.
    project = project_dir(tmp_path)
    copies = [p for p in (project / "contract" / "imports").iterdir() if p.is_file() and p.name.endswith(".bundle.md")]
    assert len(copies) == 1
    assert hashlib.sha256(copies[0].read_bytes()).hexdigest() == original_hash
    assert str(copies[0]) == result["copy_path"]
    assert result["sha256"] == original_hash


def test_source_preserved_on_failure(helper, tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(end_marker=False), name="bad-src.md")
    original_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert bundle.is_file()
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == original_hash
    project = project_dir(tmp_path)
    copies = [p for p in (project / "contract" / "imports").iterdir() if p.is_file() and p.name.endswith(".bundle.md")]
    assert len(copies) == 1
    assert hashlib.sha256(copies[0].read_bytes()).hexdigest() == original_hash


def test_source_preserved_on_conflict(helper, tmp_path, tmp_repo, tmp_runtime):
    first = write_external_bundle(tmp_path, build_bundle_text(), name="first.md")
    proc = run_cli("import-bundle", str(first), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    sections = default_sections()
    sections["implementation.md"] = "# different\n"
    second = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="second.md")
    second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
    proc = run_cli("import-bundle", str(second), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["status"] == "version_conflict"
    assert second.is_file()
    assert hashlib.sha256(second.read_bytes()).hexdigest() == second_hash
    project = project_dir(tmp_path)
    copies = [p for p in (project / "contract" / "imports").iterdir() if p.is_file() and p.name.endswith(".bundle.md")]
    assert len(copies) == 2  # both attempts preserved as provenance
    assert {hashlib.sha256(c.read_bytes()).hexdigest() for c in copies} == {hashlib.sha256(first.read_bytes()).hexdigest(), second_hash}


def test_copy_reused_never_overwritten(helper, tmp_path, tmp_repo, tmp_runtime):
    """Re-importing the same bytes reuses the provenance copy; copies are never
    overwritten or deleted."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="reuse.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    copies = [p for p in (project / "contract" / "imports").iterdir() if p.is_file() and p.name.endswith(".bundle.md")]
    assert len(copies) == 1
    copy_before = copies[0].read_bytes()
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "already_imported"
    copies = [p for p in (project / "contract" / "imports").iterdir() if p.is_file() and p.name.endswith(".bundle.md")]
    assert len(copies) == 1
    assert copies[0].read_bytes() == copy_before


# --- AC-15 import reports for every attempt --------------------------------------

def test_report_per_attempt_on_success(helper, tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="reports.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    reports = _load_reports(project)
    assert len(reports) == 1
    report = reports[0]
    assert_pinned_fields(report)
    assert report["status"] == "imported"
    assert report["outcome"]["type"] == "valid"
    assert report["version"] == 1
    assert report["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert report["materialized_path"] == str(project / "contract" / "v1")
    assert report["copy_path"] is not None
    assert report["source"] == str(bundle)


def test_report_per_attempt_escalated_and_failed(helper, tmp_path, tmp_repo, tmp_runtime):
    # Escalated import: report with outcome escalated + reasons.
    sections = default_sections()
    sections["requirements.md"] = sections["requirements.md"] + "\n## REQ-099\n\nUncovered.\n"
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="esc.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    reports = _load_reports(project)
    assert len(reports) == 1
    assert reports[0]["status"] == "imported"
    assert reports[0]["outcome"]["type"] == "escalated"
    assert any("uncovered requirement REQ-099" in r for r in reports[0]["outcome"]["reasons"])

    # Failed import into the same project: second report for the same attempt set.
    bad = write_external_bundle(tmp_path, build_bundle_text(end_marker=False), name="bad.md")
    proc = run_cli("import-bundle", str(bad), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    reports = _load_reports(project)
    assert len(reports) == 2  # exactly one report per attempt
    failed = [r for r in reports if r["status"] == "import_failed"]
    assert len(failed) == 1
    assert_pinned_fields(failed[0])
    assert failed[0]["outcome"]["type"] == "failed"
    assert failed[0]["materialized_path"] is None


def test_restart_idempotency_decision_from_disk_alone(helper, tmp_path, tmp_repo, tmp_runtime):
    """A fresh process can decide 'already_imported' from reports + contract/
    alone — demonstrated by re-running the import as a separate process and by
    re-reading reports in this (fresh) test process."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="restart.md")
    proc1 = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc1.returncode == 0
    project = project_dir(tmp_path)
    # Re-import in a fresh process.
    proc2 = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc2.returncode == 0
    assert json.loads(proc2.stdout)["status"] == "already_imported"
    # The decision matches what reports + contract/ imply.
    reports = _load_reports(project)
    assert len(reports) == 2
    assert sum(1 for r in reports if r["status"] in {"imported", "already_imported"}) == 2
    v1 = project / "contract" / "v1"
    assert v1.is_dir()
    for name in ["metadata.json", "requirements.md", "acceptance.md"]:
        assert (v1 / name).is_file()
    # Report filenames are deterministic and unique per attempt.
    names = [p.name for p in _report_paths(project)]
    assert len(names) == len(set(names))
    assert all(n.startswith("import-") and n.endswith(".json") for n in names)


def test_report_available_for_every_outcome(helper, tmp_path, tmp_repo, tmp_runtime):
    """Success, conflict, and failure attempts all leave exactly one report with
    the pinned fields and the correct terminal status."""
    first = write_external_bundle(tmp_path, build_bundle_text(), name="outcome1.md")
    proc = run_cli("import-bundle", str(first), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    assert find_report(project / "contract" / "imports" / "reports", hashlib.sha256(first.read_bytes()).hexdigest(), "imported") is not None

    sections = default_sections()
    sections["implementation.md"] = "# changed\n"
    conflict = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="outcome2.md")
    proc = run_cli("import-bundle", str(conflict), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["status"] == "version_conflict"
    assert find_report(project / "contract" / "imports" / "reports", hashlib.sha256(conflict.read_bytes()).hexdigest(), "version_conflict") is not None

    bad = write_external_bundle(tmp_path, build_bundle_text(end_marker=False), name="outcome3.md")
    proc = run_cli("import-bundle", str(bad), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    assert find_report(project / "contract" / "imports" / "reports", hashlib.sha256(bad.read_bytes()).hexdigest(), "import_failed") is not None
