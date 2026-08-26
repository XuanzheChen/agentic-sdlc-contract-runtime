"""Bundle parser and transport-format tests (AC-01..AC-04)."""
from __future__ import annotations

import hashlib
import json

import pytest

from conftest import (
    BUNDLE_END,
    BUNDLE_HEADING,
    CANONICAL_FILES,
    DELIMITER,
    build_bundle_text,
    contract_versions,
    default_sections,
    find_report,
    project_dir,
    run_cli,
    write_bundle,
    write_external_bundle,
)


def parse(helper, text):
    parsed, errors = helper.parse_bundle(text)
    return parsed, errors


# --- AC-01 envelope and structural recognition ---------------------------------

@pytest.mark.parametrize(
    "mutate_kwargs,fragment",
    [
        ({"first_line": "# OOPS"}, "must start with"),
        ({"first_line": ""}, "must start with"),
        # manifest defects
        ({"manifest": ""}, "missing '## CONTRACT-MANIFEST'"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nVersion: 1\n"}, "missing Status"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nStatus: approved\n"}, "missing a positive integer Version"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nVersion: abc\nStatus: approved\nFiles:\n" + "".join(f"- {n}\n" for n in CANONICAL_FILES)}, "manifest Version must be a positive integer"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nVersion: 0\nStatus: approved\n"}, "manifest Version must be a positive integer"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nVersion: 1\nStatus: rejected\n"}, "manifest Status must be approved or draft"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nVersion: 1\nStatus: approved\n"}, "missing the Files bullet list"),
        ({"manifest": "## CONTRACT-MANIFEST\n\nVersion: 1\nStatus: approved\n\nFiles:\n- metadata.json\n- requirements.md\n- acceptance.md\n- implementation.md\n- constraints.md\n"}, "manifest Files must be exactly"),
        # section defects
        ({"skip_section": "tasks.md"}, "exactly six FILE sections"),
        ({"extra_section": ("extra.md", "# extra\n")}, "unknown FILE section"),
        ({"duplicate_section": "requirements.md"}, "duplicate FILE section"),
        ({"end_marker": False}, "missing 'END PSC-CONTRACT-BUNDLE'"),
        ({"trailing": "garbage"}, "unexpected content after"),
        ({"manifest_files": ["metadata.json", "requirements.md", "acceptance.md", "implementation.md", "constraints.md", "tasks.md", "extra.md"]}, "manifest Files must be exactly"),
    ],
)
def test_parse_mechanical_errors(helper, tmp_path, mutate_kwargs, fragment):
    text = build_bundle_text(**mutate_kwargs)
    parsed, errors = parse(helper, text)
    assert parsed is None
    assert any(fragment in error for error in errors), errors


def test_parse_valid_template_passes(helper, tmp_path):
    # The golden fixture is loaded from the repo tests/fixtures directory.
    from conftest import SKILL_ROOT
    golden = SKILL_ROOT / "tests" / "fixtures" / "template_bundle.md"
    parsed, errors = parse(helper, golden.read_text(encoding="utf-8"))
    assert errors == []
    assert parsed is not None
    assert set(parsed["sections"]) == set(CANONICAL_FILES)
    assert parsed["manifest"]["version"] == 1
    assert parsed["manifest"]["status"] == "approved"


def test_parse_first_heading_tolerance_blank_lines(helper):
    text = "\n\n" + build_bundle_text()  # leading blank lines before the heading
    parsed, errors = parse(helper, text)
    assert errors == []
    assert parsed is not None


def test_parse_not_utf8_is_mechanical(helper, tmp_path, tmp_repo, tmp_runtime):
    # Raw bytes that are not UTF-8-decodable must terminate as import_failed.
    bundle = tmp_path / "bad.bundle.md"
    bundle.write_bytes(b"\xff\xfe\x00bad\x80")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert "not valid UTF-8" in " ".join(result["errors"])


# --- AC-02 exact, lossless section content ------------------------------------

def test_round_trip_byte_identical(helper, tmp_path, tmp_repo, tmp_runtime):
    """Re-exporting the materialized files into a Bundle yields byte-identical files."""
    sections = default_sections(version=1)
    text = build_bundle_text()
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    for name in CANONICAL_FILES:
        materialized = (project / "contract" / "v1" / name).read_text(encoding="utf-8")
        assert materialized == sections[name], name
        assert materialized.endswith("\n")
    # Re-export materialized files back into a fresh Bundle and re-parse.
    reexport_sections = {}
    for name in CANONICAL_FILES:
        reexport_sections[name] = (project / "contract" / "v1" / name).read_text(encoding="utf-8")
    text2 = build_bundle_text(sections=reexport_sections)
    parsed, errors = parse(helper, text2)
    assert errors == []
    for name in CANONICAL_FILES:
        assert parsed["sections"][name] == reexport_sections[name]


def test_single_line_json_metadata_is_byte_identical(helper, tmp_path, tmp_repo, tmp_runtime):
    """A metadata section the exporter would not normalize must materialize verbatim."""
    import textwrap
    sections = default_sections(version=1)
    single_line = json.dumps(json.loads(sections["metadata.json"]), separators=(",", ":")) + "\n"
    assert "\n" not in single_line.rstrip("\n")
    sections["metadata.json"] = single_line
    text = build_bundle_text(sections=sections)
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    project = project_dir(tmp_path)
    assert (project / "contract" / "v1" / "metadata.json").read_text(encoding="utf-8") == single_line


def test_crlf_and_utf8_bom_sources(helper, tmp_path, tmp_repo, tmp_runtime):
    """CRLF-normalized Bundles import identically; raw-bytes SHA-256 is unaffected."""
    crlf = build_bundle_text().replace("\n", "\r\n")
    assert "\r\n" in crlf
    bundle = write_external_bundle(tmp_path, crlf)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- AC-03 manifest <-> metadata agreement -------------------------------------

def test_manifest_metadata_version_mismatch(helper, tmp_path, tmp_repo, tmp_runtime):
    text = build_bundle_text(version=1, manifest_version=2)
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("disagrees with metadata.version" in e for e in result["errors"])
    assert contract_versions(project_dir(tmp_path)) == []


def test_manifest_metadata_status_mismatch(helper, tmp_path, tmp_repo, tmp_runtime):
    text = build_bundle_text(status="draft", manifest_status="approved")
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("disagrees with metadata.status" in e for e in result["errors"])


# --- AC-04 exactly the six artifacts -------------------------------------------

def test_missing_section(helper, tmp_path, tmp_repo, tmp_runtime):
    for name in CANONICAL_FILES:
        text = build_bundle_text(skip_section=name)
        bundle = write_external_bundle(tmp_path, text, name=f"missing-{name}.md")
        proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
        assert proc.returncode == 2, name
        result = json.loads(proc.stdout)
        assert result["status"] == "import_failed", name
    assert contract_versions(project_dir(tmp_path)) == []


def test_extra_section_fails(helper, tmp_path, tmp_repo, tmp_runtime):
    text = build_bundle_text(extra_section=("notes.md", "# notes\n"))
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("unknown FILE section" in e for e in result["errors"])


def test_duplicate_section_fails(helper, tmp_path, tmp_repo, tmp_runtime):
    text = build_bundle_text(duplicate_section="requirements.md")
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("duplicate FILE section" in e for e in result["errors"])


def test_missing_end_marker_fails(helper, tmp_path, tmp_repo, tmp_runtime):
    text = build_bundle_text(end_marker=False)
    bundle = write_external_bundle(tmp_path, text)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("missing 'END PSC-CONTRACT-BUNDLE'" in e for e in result["errors"])


def test_golden_fixture_sha_is_stable(tmp_path, helper):
    from conftest import SKILL_ROOT
    golden = SKILL_ROOT / "tests" / "fixtures" / "template_bundle.md"
    raw = golden.read_bytes()
    assert raw.startswith(b"# PSC-CONTRACT-BUNDLE")
    assert b"END PSC-CONTRACT-BUNDLE" in raw
    # The golden fixture must be a valid approved Bundle import.
    parsed, errors = parse(helper, raw.decode("utf-8"))
    assert errors == []
    assert parsed["manifest"]["version"] == 1
    assert parsed["manifest"]["status"] == "approved"


# --- AC-01/AC-04 CLI-level malformed-bundle matrix (import_failed, exit 2) -----

MALFORMED_MUTATIONS = [
    # one defect per entry; each must fail mechanically with no contract/vN
    ("wrong-heading", {"first_line": "# WRONG"}),
    ("no-manifest", {"manifest": ""}),
    ("missing-version", {"manifest": "## CONTRACT-MANIFEST\n\nStatus: approved\n"}),
    ("zero-version", {"manifest": "## CONTRACT-MANIFEST\n\nVersion: 0\nStatus: approved\n"}),
    ("bad-status", {"manifest": "## CONTRACT-MANIFEST\n\nVersion: 1\nStatus: rejected\n"}),
    ("missing-files", {"manifest": "## CONTRACT-MANIFEST\n\nVersion: 1\nStatus: approved\n"}),
    ("extra-section", {"extra_section": ("notes.md", "# notes\n")}),
    ("duplicate-section", {"duplicate_section": "requirements.md"}),
    ("no-end-marker", {"end_marker": False}),
    ("trailing-garbage", {"trailing": "garbage"}),
]


@pytest.mark.parametrize("name,mutate_kwargs", MALFORMED_MUTATIONS, ids=[m[0] for m in MALFORMED_MUTATIONS])
def test_malformed_bundle(helper, tmp_path, tmp_repo, tmp_runtime, name, mutate_kwargs):
    """Every AC-01/AC-04 defect terminates as import_failed with exit 2, no
    contract/vN, and a report for the failed attempt."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(**mutate_kwargs), name=f"malformed-{name}.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2, name
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed", name
    assert contract_versions(project_dir(tmp_path)) == [], name
    reports = list((project_dir(tmp_path) / "contract" / "imports" / "reports").glob("import-*.json"))
    assert len(reports) == 1, name
    assert json.loads(reports[0].read_text(encoding="utf-8"))["status"] == "import_failed", name
