"""Boundary and deterministic-verification tests (AC-21..AC-24): Executor
boundary, frozen export-prompt hash, documentation updates, helper CLI surface,
and the AC-24 spot end-to-end flow.

All tests run against tmp_path-only fixtures, never invoke an Executor, and
never touch product source.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

from conftest import (
    EXPORT_PROMPT_SHA256,
    SCRIPT,
    SKILL_ROOT,
    build_bundle_text,
    contract_versions,
    default_sections,
    find_report,
    project_dir,
    run_cli,
    write_external_bundle,
)

PINNED_REPORT_FIELDS = ["source", "copy_path", "sha256", "version", "import_time", "outcome", "materialized_path", "warnings", "status"]

IMPORT_FUNCTIONS = [
    "parse_bundle", "_parse_manifest", "_section_content", "_parse_metadata_content",
    "_check_contract", "validate_contract", "_import_metadata_checks",
    "_import_reference_checks", "_semantic_problems", "_write_escalation",
    "_provenance_copy", "_write_report", "_matching_reports", "_atomic_rename",
    "_materialize_contract", "_import_attempt", "import_bundle", "auto_import",
    "_associated_projects", "_has_usable_approved", "_pending_bundles",
    "_update_workflow_state", "_project_directory_name", "_sanitize_candidate",
]


def tree_hash(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _norm(text: str) -> str:
    """Collapse whitespace so wrapped documentation phrases still match."""
    return " ".join(text.split())


# --- AC-21 Executor boundary ------------------------------------------------------

def test_no_executor_invocation_in_helper(helper):
    """The import layer never invokes an Executor, an adapter, or any coding
    worker: no such calls exist in the helper, and the only subprocess usage is
    the bootstrap git baseline capture."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "invoke_executor" not in source
    tree = ast.parse(source)
    subprocess_calls: list[tuple[int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.current = ["<module>"]

        def visit_FunctionDef(self, node):
            self.current.append(node.name)
            self.generic_visit(node)
            self.current.pop()

        def visit_Call(self, node):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in ("run", "Popen", "call", "check_call", "check_output")
            ):
                subprocess_calls.append((node.lineno, self.current[-1]))
            self.generic_visit(node)

    Visitor().visit(tree)
    # Every subprocess invocation lives inside git_info (git baseline capture).
    assert subprocess_calls, "expected git_info subprocess calls"
    for lineno, function in subprocess_calls:
        assert function == "git_info", (lineno, function)
    # The import functions contain no subprocess AST nodes at all.
    for name in IMPORT_FUNCTIONS:
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                func = node.func
                assert not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                ), (name, node.lineno)
    # No import of any executor/adapter/coding-worker module.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] not in {"executor", "adapter", "agent"} for alias in node.names), node.lineno
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or node.module.split(".")[0] not in {"executor", "adapter", "agent"}, node.lineno


def test_test_suite_never_invokes_executor(helper):
    """The automated tests never invoke an Executor or an adapter."""
    tests_dir = SKILL_ROOT / "tests"
    for path in sorted(tests_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        # No call syntax to any executor-invoking interface (the token is
        # assembled so this negative assertion does not match itself).
        assert ("invoke_" + "executor(") not in text, path
        # The only subprocess the suite spawns is the deterministic helper
        # itself (run_cli in conftest); no test module spawns anything.
        if path.name == "conftest.py":
            continue
        for call in re.findall(r"subprocess\.(?:run|Popen|call|check_call|check_output)\s*\(", text):
            raise AssertionError(f"unexpected subprocess use in {path.name}: {call}")


# --- AC-22 frozen export prompt and documentation --------------------------------

def test_export_prompt_hash_unchanged(helper):
    prompt = SKILL_ROOT / "prompts" / "contract-export.md"
    actual = hashlib.sha256(prompt.read_bytes()).hexdigest()
    assert actual == EXPORT_PROMPT_SHA256
    # The documented delimiter lines are exactly 50 '=' characters.
    for lineno, line in enumerate(prompt.read_text(encoding="utf-8").splitlines(), 1):
        if re.fullmatch(r"=+", line):
            assert len(line) == 50, (lineno, len(line))


def test_skill_documentation_updated(helper):
    skill = _norm((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
    required_phrases = [
        # deterministic helper surface and statuses
        "import-bundle", "auto-import", "already_imported", "import_failed", "version_conflict",
        # startup discovery
        "pending Bundles", "contract/imports/", "single-pending-Bundle rule",
        "any normal Contract loading",
        # failure/escalation semantics
        "waiting_planner", "review/escalation-NNN.md", "draft awaiting approval",
        # provenance
        "contract/imports/reports/", "sole basis for restart idempotency",
        # bootstrap naming
        "metadata.project_name",
        # strict role boundary and export prompt documentation
        "External Planner Contract Export Prompt", "not a Supervisor Runtime Prompt",
        "prompts/contract-export.md",
    ]
    for phrase in required_phrases:
        assert phrase in skill, phrase


def test_runtime_protocol_documentation_updated(helper):
    doc = _norm((SKILL_ROOT / "references" / "runtime-protocol.md").read_text(encoding="utf-8"))
    for phrase in [
        "contract/imports/", "contract/imports/reports/",
        "import-bundle", "auto-import",
        "imported", "already_imported", "import_failed", "version_conflict",
        "Startup order", "user-provided", "pending Bundles", "waiting_planner",
        "review/escalation-NNN.md", "draft awaiting approval",
        "External Planner Contract Export Prompt",
    ]:
        assert phrase in doc, phrase


def test_contract_schema_documentation_updated(helper):
    doc = (SKILL_ROOT / "references" / "contract-schema.md").read_text(encoding="utf-8")
    for phrase in [
        "Contract Bundle transport", "draft", "approved",
        "AC -> REQ", "C-###", "UNRESOLVED", "already_imported", "version_conflict",
    ]:
        assert phrase in doc, phrase


def test_prompts_contract_export_is_documented_as_external_planner_prompt(helper):
    """AC-22 item 6: the export prompt is the External Planner Contract Export
    Prompt, not a Supervisor Runtime Prompt (documented in SKILL.md and both
    updated references)."""
    skill = _norm((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
    assert "it is copied to an external Planner to emit a Bundle and is **not a Supervisor Runtime Prompt**" in skill
    protocol = _norm((SKILL_ROOT / "references" / "runtime-protocol.md").read_text(encoding="utf-8"))
    assert "External Planner Contract Export Prompt" in protocol
    schema = _norm((SKILL_ROOT / "references" / "contract-schema.md").read_text(encoding="utf-8"))
    assert "External Planner Contract Export Prompt" in schema


# --- AC-24 helper surface ----------------------------------------------------------

def test_import_bundle_help_documents_all_flags(helper):
    proc = run_cli("import-bundle", "--help")
    assert proc.returncode == 0
    assert "bundle_path" in proc.stdout
    for flag in ("--repository", "--runtime-config", "--project-id"):
        assert flag in proc.stdout, flag


def test_auto_import_help_documents_all_flags(helper):
    proc = run_cli("auto-import", "--help")
    assert proc.returncode == 0
    for flag in ("--repository", "--runtime-config", "--project-id"):
        assert flag in proc.stdout, flag


def test_validate_contract_and_discover_surface(helper, tmp_path, tmp_repo, tmp_runtime):
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="surface.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    project = project_dir(tmp_path)
    vcheck = run_cli("validate-contract", str(project / "contract" / "v1"), "--repository", str(tmp_repo))
    assert vcheck.returncode == 0
    assert json.loads(vcheck.stdout)["valid"] is True
    disc = json.loads(run_cli("discover", "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime)).stdout)
    assert disc["projects"][0]["active"] is True
    assert disc["projects"][0]["approved_versions"] == [1]


# --- AC-24 spot end-to-end (fresh tmp runtime root + fixture repository) ----------

def test_spot_end_to_end(helper, tmp_path, tmp_repo, tmp_runtime):
    # (a) valid approved bundle -> exit 0, status imported, contract/v1 complete,
    #     provenance copy, report with all pinned fields, manifest + state.
    bundle = write_external_bundle(tmp_path, build_bundle_text(), name="e2e.md")
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "imported"
    assert result["version"] == 1
    project = project_dir(tmp_path)
    v1 = project / "contract" / "v1"
    assert contract_versions(project) == [1]
    for name in ["metadata.json", "requirements.md", "acceptance.md", "implementation.md", "constraints.md", "tasks.md"]:
        assert (v1 / name).is_file(), name
    copies = [p for p in (project / "contract" / "imports").iterdir() if p.is_file() and p.name.endswith(".bundle.md")]
    assert len(copies) == 1
    report = find_report(project / "contract" / "imports" / "reports", result["sha256"], "imported")
    assert report is not None
    for field in PINNED_REPORT_FIELDS:
        assert field in report, field
    manifest = json.loads((project / "runtime" / "project.json").read_text(encoding="utf-8"))
    assert Path(manifest["repository"]).resolve() == tmp_repo.resolve()
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "initialized"
    v1_before = tree_hash(v1)

    # (b) same bytes again -> exit 0, already_imported, v1 hashes unchanged.
    proc = run_cli("import-bundle", str(bundle), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "already_imported"
    assert tree_hash(v1) == v1_before

    # (c) different v1 bundle -> exit 2, version_conflict, v1 hashes unchanged.
    sections = default_sections()
    sections["implementation.md"] = "# different implementation\n"
    other = write_external_bundle(tmp_path, build_bundle_text(sections=sections), name="e2e-other.md")
    proc = run_cli("import-bundle", str(other), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["status"] == "version_conflict"
    assert tree_hash(v1) == v1_before

    # (d) draft bundle -> exit 0, workflow_state.status waiting_planner, no task
    #     artifacts, no executor invocation.
    draft = write_external_bundle(tmp_path, build_bundle_text(version=2, status="draft"), name="e2e-draft.md")
    proc = run_cli("import-bundle", str(draft), "--repository", str(tmp_repo), "--runtime-config", str(tmp_runtime))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "imported"
    state = json.loads((project / "runtime" / "workflow_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "waiting_planner"
    task_records = list((project / "developing" / "tasks").glob("T-*.md")) if (project / "developing" / "tasks").exists() else []
    # Only v1's bootstrap task records exist; the draft adds none.
    assert len(task_records) == 2