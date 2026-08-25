"""Shared fixtures for the PSC Contract Bundle import test suite.

All tests are offline-deterministic, run against ``tmp_path`` fixtures only,
and never invoke an Executor or touch product source.

The default pytest temp area may be sandbox-restricted, so this module pins
``PYTEST_DEBUG_TEMPROOT`` (honoured by pytest's TempPathFactory) to a
repository-local scratch directory under ``temp/`` before any session fixture
is created. Nothing in this module edits product source.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts" / "psc_runtime.py"
EXPORT_PROMPT = SKILL_ROOT / "prompts" / "contract-export.md"

# Load the deterministic helper into this process for direct unit-level calls
# (parser, validation splits, injected-failure tests); CLI tests additionally
# run it as a fresh subprocess via run_cli().
_SPEC = importlib.util.spec_from_file_location("psc_runtime_helper", str(SCRIPT))
assert _SPEC is not None and _SPEC.loader is not None
_HELPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPER)

# The sandbox denies access to directories created with mode=0o700 (the mode
# pytest's default TempPathFactory uses), so this suite pins its own temp root
# (a repository-local scratch directory under ``temp/``) and installs a
# session-scoped ``tmp_path_factory`` that creates directories with default
# ACLs. Pytest's TempPathFactory honours PYTEST_DEBUG_TEMPROOT for the default
# root; the factory override below removes the 0o700 modes entirely.
_TEMP_ROOT = SKILL_ROOT / "temp" / "pytest-bundle-import"
_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_TEMP_ROOT)


def _make_numbered_dir(root: Path, prefix: str) -> Path:
    for i in range(1000):
        candidate = root / f"{prefix}{i}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"cannot create a numbered temp directory under {root}")


class _BundleImportTempPathFactory:
    """A minimal stand-in for pytest's TempPathFactory without 0o700 modes."""

    def __init__(self, basetemp: Path) -> None:
        self._basetemp = basetemp
        self._retention_policy = 'all'

    def getbasetemp(self) -> Path:
        return self._basetemp

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        basename = os.path.normpath(basename)
        base = self.getbasetemp()
        if (base / basename).resolve().parent != base:
            raise ValueError(f"{basename} is not a normalized and relative path")
        if not numbered:
            path = base / basename
            path.mkdir()
            return path
        return _make_numbered_dir(base, prefix=basename)


@pytest.fixture(scope="session")
def tmp_path_factory() -> _BundleImportTempPathFactory:
    return _BundleImportTempPathFactory(_TEMP_ROOT)

# --- Bundle template constants (per prompts/contract-export.md) ------------

DELIMITER = "=" * 50
BUNDLE_HEADING = "# PSC-CONTRACT-BUNDLE"
BUNDLE_END = "END PSC-CONTRACT-BUNDLE"
CANONICAL_FILES = [
    "metadata.json",
    "requirements.md",
    "acceptance.md",
    "implementation.md",
    "constraints.md",
    "tasks.md",
]

EXPORT_PROMPT_SHA256 = "6ca1cd0ae6842e2558e4d721721cf2b3416400b88f77d29e347e30e81e5b0dd1"

DEFAULT_METADATA = {
    "schema_version": 1,
    "version": 1,
    "status": "approved",
    "created_by": "external-planner",
    "created_at": "2026-08-25T12:00:00+08:00",
    "supersedes": None,
    "workflow_policy": {"restart": "all"},
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_sections(version: int = 1, status: str = "approved") -> dict[str, str]:
    """The six canonical artifact texts of a complete, valid Contract."""
    metadata = dict(DEFAULT_METADATA)
    metadata["version"] = version
    metadata["status"] = status
    return {
        "metadata.json": json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        "requirements.md": (
            "# Requirements\n\n"
            "## REQ-001\n\nRequirement one.\n\n"
            "## REQ-002\n\nRequirement two.\n"
        ),
        "acceptance.md": (
            "# Acceptance Criteria\n\n"
            "## AC-001\n\nCovers REQ-001.\n\n"
            "## AC-002\n\nCovers REQ-002.\n"
        ),
        "implementation.md": "# Implementation Recommendation\n\nREQUIRED: none.\n",
        "constraints.md": "# Constraints\n\nC-001: no network access.\n",
        "tasks.md": (
            "# Task Breakdown\n\n"
            "## T-001\n\n"
            "Title: Task one\n\n"
            "Goal: Implement one.\n\n"
            "Requirements:\n- REQ-001\n\n"
            "Acceptance:\n- AC-001\n\n"
            "Dependencies:\n- None\n\n"
            "Allowed Scope:\n- scope one\n\n"
            "Forbidden Scope:\n- none\n\n"
            "Implementation Notes:\n- note one\n\n"
            "Required Verification:\n- verify one\n\n"
            "## T-002\n\n"
            "Title: Task two\n\n"
            "Goal: Implement two.\n\n"
            "Requirements:\n- REQ-002\n\n"
            "Acceptance:\n- AC-002\n\n"
            "Dependencies:\n- T-001\n\n"
            "Allowed Scope:\n- scope two\n\n"
            "Forbidden Scope:\n- none\n\n"
            "Implementation Notes:\n- note two\n\n"
            "Required Verification:\n- verify two\n"
        ),
    }


def build_bundle_text(
    *,
    version: int = 1,
    status: str = "approved",
    sections: dict[str, str] | None = None,
    first_line: str = BUNDLE_HEADING,
    manifest: str | None = None,
    manifest_version: int | None = None,
    manifest_status: str | None = None,
    manifest_files: list[str] | None = None,
    section_order: list[str] | None = None,
    skip_section: str | None = None,
    extra_section: tuple[str, str] | None = None,
    duplicate_section: str | None = None,
    delimiter: str = DELIMITER,
    end_marker: bool = True,
    trailing: str = "",
    trailing_delimiter: bool = True,
    file_prefix: str = "FILE: ",
) -> str:
    """Emit a Bundle literally per the contract-export.md template.

    Every parameter defaults to the exact template shape; mutations are
    applied only when a test requests them.
    """
    body = dict(sections if sections is not None else default_sections(version, status))
    if manifest_version is None:
        manifest_version = version
    if manifest_status is None:
        manifest_status = status
    if manifest_files is None:
        manifest_files = list(CANONICAL_FILES)
    if manifest is None:
        manifest = (
            "## CONTRACT-MANIFEST\n"
            f"\nVersion: {manifest_version}\n"
            f"Status: {manifest_status}\n"
            "\nFiles:\n"
            + "".join(f"- {name}\n" for name in manifest_files)
        )
    if skip_section is not None:
        del body[skip_section]
    order = list(section_order if section_order is not None else CANONICAL_FILES)
    if skip_section is not None:
        order = [name for name in order if name != skip_section]
    if extra_section is not None:
        name, text = extra_section
        body[name] = text
        order.append(name)
    parts: list[str] = [first_line, "", manifest, ""]
    for name in order:
        parts.append(delimiter)
        parts.append(f"{file_prefix}{name}")
        parts.append(delimiter)
        parts.append("")
        parts.append(body[name])
        parts.append("")
    if duplicate_section is not None:
        # Re-emit the duplicated section header (and its body) before the end.
        parts.append(delimiter)
        parts.append(f"{file_prefix}{duplicate_section}")
        parts.append(delimiter)
        parts.append("")
        parts.append(body.get(duplicate_section, ""))
        parts.append("")
    if end_marker:
        parts.append(delimiter)
        parts.append(BUNDLE_END)
        if trailing_delimiter:
            parts.append(delimiter)
    if trailing:
        parts.append(trailing)
    return "\n".join(parts) + "\n"


def write_bundle(tmp_path: Path, text: str, name: str = "bundle.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the deterministic helper in a separate process (fresh, stateless)."""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def write_external_bundle(tmp_path: Path, text: str, name: str = "bundle.md") -> Path:
    """Write a Bundle at a path that is neither the repository nor the workflow."""
    external = tmp_path / "external"
    external.mkdir(exist_ok=True)
    path = external / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def project_dir(tmp_path: Path) -> Path:
    """The workflow project directory (exactly one) created under the runtime root."""
    runtime_root = tmp_path / "runtime_root"
    projects = sorted(p for p in runtime_root.iterdir() if p.is_dir())
    assert len(projects) == 1, f"expected exactly one project directory, got {projects}"
    return projects[0]


def make_runtime_config(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime_root"
    runtime_root.mkdir()
    executor_home = tmp_path / "executor-home"
    executor_home.mkdir()
    config = {
        "schema_version": 1,
        "runtime_root": str(runtime_root),
        "project_naming": "YYYYMMDD-{requirement}",
        "executor": {
            "adapter": "codex",
            "executable": sys.executable,
            "executor_home": str(executor_home),
            "provider": "probe",
            "model": "probe-model",
            "effort": "medium",
            "approval_policy": "never",
            "sandbox": "workspace-write",
            "timeout": 1800,
            "smoke_timeout": 120,
        },
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def helper():
    """The deterministic helper loaded into this process (direct calls)."""
    return _HELPER


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# fixture repository\n", encoding="utf-8")
    return repo


@pytest.fixture
def tmp_runtime(tmp_path: Path) -> Path:
    """A .agentic-sdlc/runtime.json pointing at a tmp runtime_root."""
    return make_runtime_config(tmp_path)


@pytest.fixture
def valid_bundle_text() -> str:
    return build_bundle_text()


def find_report(reports_dir: Path, sha: str, status: str) -> dict | None:
    """Find the (single) report for a sha with a given status, from disk."""
    if not reports_dir.is_dir():
        return None
    hits = []
    for path in reports_dir.glob("import-*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("sha256") == sha and report.get("status") == status:
            hits.append(report)
    return hits[0] if hits else None


def contract_versions(project: Path) -> list[int]:
    contract_dir = project / "contract"
    if not contract_dir.is_dir():
        return []
    versions = []
    for vdir in contract_dir.iterdir():
        m = re.fullmatch(r"v(\d+)", vdir.name)
        if m and vdir.is_dir():
            versions.append(int(m.group(1)))
    return sorted(versions)
