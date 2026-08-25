"""Mechanical validation classification (AC-05..AC-07) and semantic
escalation (AC-08) tests for the Bundle importer."""
from __future__ import annotations

import json

import pytest

from conftest import (
    build_bundle_text,
    contract_versions,
    default_sections,
    find_report,
    project_dir,
    run_cli,
    sha256_bytes,
    write_external_bundle,
)


def metadata_bundle(tmp_path, name="bundle.md", *, version=1, status="approved", **overrides):
    sections = default_sections(version=version, status=status)
    meta = json.loads(sections["metadata.json"])
    meta.update(overrides)
    sections["metadata.json"] = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    # The manifest keeps a valid Version/Status so invalid-metadata fixtures
    # fail on the metadata checks (and manifest<->metadata disagreement),
    # while valid fixtures (e.g. version=3) agree with the manifest.
    manifest_kwargs = {}
    if isinstance(version, int) and version >= 1:
        manifest_kwargs["version"] = version
    if status in {"approved", "draft"}:
        manifest_kwargs["status"] = status
    return write_external_bundle(tmp_path, build_bundle_text(sections=sections, **manifest_kwargs), name=name)


def mutated_sections(mutators: dict) -> dict:
    """Return sections with a callable applied to one artifact.

    The artifact names contain dots (``requirements.md``), so the mapping is
    passed as an explicit dictionary rather than keyword arguments.
    """
    sections = default_sections()
    for key, fn in mutators.items():
        sections[key] = fn(sections[key])
    return sections


# --- AC-05 metadata validation (mechanical -> import_failed) -------------------

@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"schema_version": 2}, "metadata.schema_version must be 1"),
        ({"version": 0}, "metadata.version must be a positive integer"),
        ({"version": "1"}, "metadata.version must be a positive integer"),
        ({"status": "superseded"}, "must be draft or approved"),
        ({"status": "rejected"}, "must be draft or approved"),
        ({"status": "unknown"}, "must be draft or approved"),
        ({"status": None}, "must be draft or approved"),
        ({"created_by": ""}, "metadata.created_by is required"),
        ({"created_at": ""}, "metadata.created_at is required"),
        ({"api_key": "sk-test"}, "credential-like metadata key is forbidden"),
        ({"token": "abc"}, "credential-like metadata key is forbidden"),
        ({"supersedes": -1}, "metadata.supersedes must be null or a positive integer strictly less than version"),
        ({"supersedes": 1}, "metadata.supersedes must be null or a positive integer strictly less than version"),
        ({"supersedes": 2}, "metadata.supersedes must be null or a positive integer strictly less than version"),
        ({"supersedes": "1"}, "metadata.supersedes must be null or a positive integer strictly less than version"),
    ],
)
def test_invalid_metadata(helper, tmp_path, tmp_repo, tmp_runtime, overrides, fragment):
    kwargs = dict(overrides)
    if "version" in kwargs and kwargs["version"] in ("1", 0):
        kwargs = {"version": kwargs["version"]}
    if "supersedes" in kwargs and kwargs["supersedes"] == 1:
        # version defaults to 1; supersedes must be strictly less => conflict
        pass
    bundle = metadata_bundle(tmp_path, name="invalid-metadata.md", **kwargs)
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "meta")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any(fragment in e for e in result["errors"]), result["errors"]
    assert contract_versions(project_dir(tmp_path)) == []


def test_invalid_metadata_workflow_policy_shapes(helper, tmp_path, tmp_repo, tmp_runtime):
    cases = [
        ({"workflow_policy": "restart"}, "must be an object"),
        ({"workflow_policy": {"bogus": 1}}, "unknown keys"),
        ({"workflow_policy": {"restart": "sometimes"}}, "must be 'all' or 'pending_only'"),
        ({"workflow_policy": {"invalidate_from_task": "T-999"}}, "must be a T-### that resolves to an existing task"),
        ({"workflow_policy": {"invalidate_from_task": "AC-001"}}, "must be a T-### that resolves to an existing task"),
    ]
    for overrides, fragment in cases:
        bundle = metadata_bundle(tmp_path, name="policy.md", **overrides)
        proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "policy")
        assert proc.returncode == 2, proc.stdout
        result = json.loads(proc.stdout)
        assert result["status"] == "import_failed"
        assert any(fragment in e for e in result["errors"]), result["errors"]


def test_valid_workflow_policy_and_supersedes_accepted(helper, tmp_path, tmp_repo, tmp_runtime):
    bundle = metadata_bundle(
        tmp_path, name="valid-meta.md",
        version=3, status="approved",
        supersedes=2,
        workflow_policy={"invalidate_from_task": "T-001"},
    )
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "validmeta")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert result["version"] == 3


def test_repository_mismatch_fails(helper, tmp_path, tmp_repo, tmp_runtime):
    other = tmp_path / "other-repo"
    other.mkdir()
    bundle = metadata_bundle(tmp_path, name="repo-mismatch.md", repository=str(other))
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "repomismatch")
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("Contract repository mismatch" in e for e in result["errors"])


# --- AC-06 stable IDs, references, dependencies (mechanical) -------------------

def make_tasks(tasks_text: str):
    sections = default_sections()
    sections["tasks.md"] = tasks_text
    return sections


DUPLICATE_REQ = mutated_sections({"requirements.md": lambda t: t + "\n## REQ-001\n\nDuplicate requirement.\n"})
DUPLICATE_AC = mutated_sections({"acceptance.md": lambda t: t + "\n## AC-001\n\nDuplicate criterion.\n"})
DUPLICATE_T = mutated_sections({"tasks.md": lambda t: t + "\n## T-001\n\nDuplicate task.\n"})
DUPLICATE_C = mutated_sections({"constraints.md": lambda t: t + "\nC-001: duplicate constraint.\n"})


@pytest.mark.parametrize(
    "name,sections,fragment",
    [
        ("dup-req", DUPLICATE_REQ, "duplicate requirement IDs"),
        ("dup-ac", DUPLICATE_AC, "duplicate acceptance IDs"),
        ("dup-t", DUPLICATE_T, "duplicate task IDs"),
        ("dup-c", DUPLICATE_C, "duplicate constraint IDs"),
    ],
)
def test_duplicate_ids(helper, tmp_path, tmp_repo, tmp_runtime, name, sections, fragment):
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name=f"{name}.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "dupid")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any(fragment in e for e in result["errors"]), result["errors"]


UNKNOWN_TASK_REQ = make_tasks(
    default_sections()["tasks.md"] + "\n## T-099\n\nRequirements:\n- REQ-999\n\nAcceptance:\n- AC-001\n\nDependencies:\n- None\n"
)
UNKNOWN_TASK_AC = make_tasks(
    default_sections()["tasks.md"] + "\n## T-099\n\nRequirements:\n- REQ-001\n\nAcceptance:\n- AC-999\n\nDependencies:\n- None\n"
)
UNKNOWN_TASK_TDEP = make_tasks(
    default_sections()["tasks.md"] + "\n## T-099\n\nRequirements:\n- REQ-001\n\nAcceptance:\n- AC-001\n\nDependencies: T-999\n"
)
UNKNOWN_AC_REQ = mutated_sections({"acceptance.md": lambda t: t + "\n## AC-099\n\nCovers REQ-999.\n"})
SELF_CYCLE = make_tasks(
    default_sections()["tasks.md"].replace("Dependencies:\n- T-001\n\nAllowed Scope:\n- scope two", "Dependencies: T-002\n\nAllowed Scope:\n- scope two")
    .replace("## T-001\n\nTitle: Task one\n\nGoal: Implement one.\n\nRequirements:\n- REQ-001\n\nAcceptance:\n- AC-001\n\nDependencies:\n- None", "## T-001\n\nTitle: Task one\n\nGoal: Implement one.\n\nRequirements:\n- REQ-001\n\nAcceptance:\n- AC-001\n\nDependencies: T-001")
)
TWO_TASK_CYCLE = make_tasks(
    default_sections()["tasks.md"].replace("Dependencies:\n- T-001\n\nAllowed Scope:\n- scope two", "Dependencies: T-001\n\nAllowed Scope:\n- scope two")
    .replace("## T-001\n\nTitle: Task one\n\nGoal: Implement one.\n\nRequirements:\n- REQ-001\n\nAcceptance:\n- AC-001\n\nDependencies:\n- None", "## T-001\n\nTitle: Task one\n\nGoal: Implement one.\n\nRequirements:\n- REQ-001\n\nAcceptance:\n- AC-001\n\nDependencies: T-002")
)


@pytest.mark.parametrize(
    "name,sections,fragments",
    [
        ("unknown-task-req", UNKNOWN_TASK_REQ, ["task references unknown REQ-999"]),
        ("unknown-task-ac", UNKNOWN_TASK_AC, ["task references unknown AC-999"]),
        ("unknown-task-tdep", UNKNOWN_TASK_TDEP, ["depends on unknown T-999"]),
        ("unknown-ac-req", UNKNOWN_AC_REQ, ["acceptance references unknown REQ-999"]),
        ("self-cycle", SELF_CYCLE, ["cyclic task dependency involving T-001"]),
        ("two-task-cycle", TWO_TASK_CYCLE, ["cyclic task dependency involving"]),
    ],
)
def test_unresolved_references_and_cycles(helper, tmp_path, tmp_repo, tmp_runtime, name, sections, fragments):
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name=f"{name}.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "refs")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    for fragment in fragments:
        assert any(fragment in e for e in result["errors"]), (fragment, result["errors"])
    assert contract_versions(project_dir(tmp_path)) == []


def test_task_missing_requirements_acceptance_labels(helper, tmp_path, tmp_repo, tmp_runtime):
    sections = default_sections()
    sections["tasks.md"] = sections["tasks.md"].replace("Requirements:\n- REQ-002\n\n", "")
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="no-labels.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "nolabels")
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed"
    assert any("must declare Requirements and Acceptance references" in e for e in result["errors"])


@pytest.mark.parametrize(
    "name,sections",
    [
        ("self-cycle", SELF_CYCLE),
        ("two-task-cycle", TWO_TASK_CYCLE),
    ],
)
def test_dependency_cycle(helper, tmp_path, tmp_repo, tmp_runtime, name, sections):
    """Self and two-task dependency cycles are mechanical failures (AC-06)."""
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name=f"{name}.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "cycles")
    assert proc.returncode == 2, name
    result = json.loads(proc.stdout)
    assert result["status"] == "import_failed", name
    assert any("cyclic task dependency" in e or "self-cycle" in e for e in result["errors"]), result["errors"]
    assert contract_versions(project_dir(tmp_path)) == []


# --- AC-08 semantic completeness escalation (waiting_planner) ------------------

def assert_escalated(tmp_path, tmp_repo, proc, result, sections, project, reasons_substrings):
    assert proc.returncode == 0, f"escalated import must exit 0: {proc.stdout}{proc.stderr}"
    assert result["status"] == "imported"
    assert result.get("escalated") is True
    # Immutable artifact preserved and byte-identical.
    v1 = project / "contract" / "v1"
    assert v1.is_dir()
    from conftest import CANONICAL_FILES
    for name in CANONICAL_FILES:
        assert (v1 / name).read_text(encoding="utf-8") == sections[name], name
    # Escalation file + waiting_planner state.
    esc = sorted((project / "review").glob("escalation-*.md"))
    assert len(esc) == 1
    esc_text = esc[0].read_text(encoding="utf-8")
    assert "waiting_planner" in esc_text
    assert "Contract version: v1" in esc_text
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "waiting_planner"
    # Report outcome escalated with reasons.
    report = find_report(project / "contract" / "imports" / "reports", result["sha256"], "imported")
    assert report is not None
    assert report["outcome"]["type"] == "escalated"
    for fragment in reasons_substrings:
        assert any(fragment in r for r in report["outcome"]["reasons"]), (fragment, report["outcome"]["reasons"])
        assert any(fragment in esc_text for fragment in report["outcome"]["reasons"]), fragment
    # Mechanical validation still passes on the materialized artifact.
    vcheck = run_cli("validate-contract", str(v1), "--repository", str(tmp_repo))
    assert vcheck.returncode == 0
    assert json.loads(vcheck.stdout)["valid"] is True


def _missing_definition_sections() -> dict:
    sections = default_sections()
    sections["tasks.md"] = sections["tasks.md"].replace("Required Verification:\n- verify one\n", "")
    return sections


@pytest.mark.parametrize(
    "name,build_sections,reasons_substrings",
    [
        (
            "uncovered-requirement",
            lambda: mutated_sections({"requirements.md": lambda t: t + "\n## REQ-003\n\nUncovered requirement.\n"}),
            ["uncovered requirement REQ-003"],
        ),
        (
            "uncovered-acceptance",
            lambda: mutated_sections({"acceptance.md": lambda t: t + "\n## AC-003\n\nCovers REQ-001.\n"}),
            ["uncovered acceptance criterion AC-003"],
        ),
        (
            "missing-task-definition",
            _missing_definition_sections,
            ["task T-001 lacks implementation-critical declaration"],
        ),
        (
            "approved-unresolved",
            lambda: mutated_sections({"implementation.md": lambda t: t + "\nUNRESOLVED: pick a parser library.\n"}),
            ["blocking UNRESOLVED marker in approved contract"],
        ),
    ],
    ids=["uncovered-requirement", "uncovered-acceptance", "missing-task-definition", "approved-unresolved"],
)
def test_completeness_escalation(helper, tmp_path, tmp_repo, tmp_runtime, name, build_sections, reasons_substrings):
    """All four AC-08 semantic conditions escalate: artifact preserved
    immutable, escalation file, waiting_planner, exit 0."""
    sections = build_sections()
    bundle = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name=f"esc-{name}.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime), "--project-id", "esc")
    result = json.loads(proc.stdout)
    assert_escalated(tmp_path, tmp_repo, proc, result, sections, project_dir(tmp_path), reasons_substrings)
