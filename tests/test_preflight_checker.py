"""Offline, fully mocked tests for the PSC Preflight Checker (PC).

Every test in this module is deterministic and offline: no real Codex/DSH
process is ever launched. Checker subprocesses are replaced with an in-process
fake, and repository/Contract/project inputs are `tmp_path` fixtures.

Coverage: ALLOW, DENY, UNKNOWN, configuration mismatch, stale SHA evidence,
E-prompt injection, scope/bounded-evidence fail-closed behaviour, retry-budget
non-charging, Codex/DSH read-only command + patch construction, DSH composed
restriction verification, and backward compatibility with the existing Executor
path.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import preflight_checker as PC  # noqa: E402
import invoke_executor as IE  # noqa: E402
from adapters import codex as codex_adapter  # noqa: E402
from adapters import dsh as dsh_adapter  # noqa: E402

_MCP_SPEC = importlib.util.spec_from_file_location(
    "psc_executor_mcp_preflight", SKILL_ROOT / "scripts" / "psc_mcp_server.py"
)
assert _MCP_SPEC and _MCP_SPEC.loader
MCP = importlib.util.module_from_spec(_MCP_SPEC)
_MCP_SPEC.loader.exec_module(MCP)


# --------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------


class FakeCheckerProcess:
    """A minimal stand-in for the `subprocess` module used by PC."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, stdout="", stderr="", returncode=0, exc=None, hook=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.exc = exc
        self.hook = hook
        self.calls: list[tuple[list[str], dict]] = []

    def run(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if self.hook is not None:
            self.hook()
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            stdout=self.stdout, stderr=self.stderr, returncode=self.returncode
        )


def _codex_jsonl(report_text: str, *, usage: bool = True) -> str:
    lines = [json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": report_text}})]
    if usage:
        lines.append(json.dumps({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 100,
                "cache_write_input_tokens": 0,
                "output_tokens": 200,
                "reasoning_output_tokens": 50,
            },
        }))
    return "\n".join(lines) + "\n"


def _dsh_jsonl(report_text: str) -> str:
    return (
        json.dumps({"type": "session", "sessionId": "pc-session"}) + "\n"
        + json.dumps({"type": "final", "text": report_text}) + "\n"
    )


def _report(decision: str, findings=None) -> str:
    return json.dumps({
        "schema_version": 1,
        "decision": decision,
        "summary": f"{decision} summary",
        "findings": findings or [],
    })


def _blocking_finding(owner: str = "supervisor") -> dict:
    return {
        "id": "F-001",
        "severity": "blocking",
        "statement": "Allowed Scope does not cover the required change.",
        "evidence": ["T-001 Allowed Scope: src/example.py"],
        "resolution_owner": owner,
    }


def _executor_block(adapter: str, home: Path, executable: Path) -> dict:
    executor = {
        "adapter": adapter,
        "executable": str(executable),
        "executor_home": str(home),
        "config_source": "runtime",
        "approval_policy": "never",
        "sandbox": "workspace-write",
        "timeout": 1800,
        "maxTimeout": 7200,
        "smoke_timeout": 120,
        "routing": {
            "provider": "probe-provider",
            "model": "probe-model",
            "effort": "medium",
        },
    }
    if adapter == "dsh":
        executor["profile"] = "headless"
    return executor


def make_executor_home(tmp_path: Path, adapter: str) -> Path:
    home = tmp_path / f"{adapter}-home"
    home.mkdir(parents=True, exist_ok=True)
    if adapter == "dsh":
        profile = home / "profiles" / "headless"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "package.json").write_text('{"name":"psc-dsh-profile"}\n', encoding="utf-8")
        (profile / "cordis.patch.yml").write_text(
            "# profile overlay\n- id: tool-shell\n  disabled: false\n",
            encoding="utf-8",
        )
    return home


def make_config(
    tmp_path: Path,
    *,
    adapter: str = "codex",
    preflight: dict | None = None,
) -> dict:
    executable = tmp_path / f"fake-{adapter}.exe"
    executable.write_text("", encoding="utf-8")
    config = {
        "schema_version": 1,
        "runtime_root": str(tmp_path / "runtime_root"),
        "project_naming": "YYYYMMDD-{requirement}",
        "executor": _executor_block(adapter, make_executor_home(tmp_path, adapter), executable),
    }
    if preflight is not None:
        config["preflight"] = preflight
    return config


def write_runtime_config(tmp_path: Path, config: dict, name: str = "runtime.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


CONTRACT_FILES = {
    "metadata.json": json.dumps({
        "schema_version": 1,
        "version": 1,
        "status": "approved",
        "created_by": "external-planner",
        "created_at": "2026-08-25T12:00:00+08:00",
        "supersedes": None,
        "workflow_policy": {"restart": "all"},
    }, indent=2) + "\n",
    "requirements.md": "# Requirements\n\n## REQ-001\n\nImplement one.\n",
    "acceptance.md": "# Acceptance\n\n## AC-001\n\nCovers REQ-001.\n",
    "implementation.md": "# Implementation\n\n## T-001\n\nEdit src/example.py.\n",
    "constraints.md": "# Constraints\n\nC-001: no network access.\n",
    "tasks.md": (
        "# Tasks\n\n## T-001\n\nTitle: Task one\n\nAllowed Scope:\n- src/example.py\n\n"
        "Forbidden Scope:\n- none\n\nRequired Verification:\n- pytest\n"
    ),
}


def make_project(tmp_path: Path, *, with_review: bool = False) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    (project / "runtime").mkdir(parents=True, exist_ok=True)
    contract = project / "contract" / "v1"
    contract.mkdir(parents=True, exist_ok=True)
    for name, text in CONTRACT_FILES.items():
        (contract / name).write_text(text, encoding="utf-8")
    task = project / "developing" / "tasks" / "T-001.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text(
        "# T-001\n\nGoal: implement one.\n\nAllowed Scope:\n- src/example.py\n\n"
        "Forbidden Scope:\n- none\n",
        encoding="utf-8",
    )
    review = project / "review" / "T-001.md"
    if with_review:
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("Decision: quality_rework.\n", encoding="utf-8")
    return project, task, contract


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "example.py").write_text("print('in scope')\n", encoding="utf-8")
    (repo / "outside").mkdir(parents=True, exist_ok=True)
    (repo / "outside" / "secret.py").write_text("print('out of scope')\n", encoding="utf-8")
    return repo


def run_pc(
    config: dict,
    repository: Path,
    project: Path | None,
    task: Path,
    contract: Path,
    review: Path | None = None,
    *,
    fake: FakeCheckerProcess | None = None,
    monkeypatch=None,
    adapter: str = "codex",
    report_text: str | None = None,
) -> dict:
    if fake is None:
        payload = report_text if report_text is not None else _report("ALLOW")
        stdout = _dsh_jsonl(payload) if adapter == "dsh" else _codex_jsonl(payload)
        fake = FakeCheckerProcess(stdout=stdout)
    monkeypatch.setattr(PC, "subprocess", fake)
    return PC.run_preflight_check(
        config, repository, project, task, contract, review
    )


# --------------------------------------------------------------------------
# Command / patch construction (Codex + DSH read-only)
# --------------------------------------------------------------------------


def test_codex_preflight_command_forces_read_only_and_never(tmp_path):
    executor = _executor_block("codex", tmp_path / "home", tmp_path / "codex.exe")
    executor["sandbox"] = "danger-full-access"
    executor["approval_policy"] = "on-request"

    command = codex_adapter.build_preflight_command(
        "codex", executor, "-", output_schema=Path("schema.json")
    )

    assert command[0] == "codex"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "danger-full-access" not in command
    assert "on-request" not in command
    assert "workspace-write" not in command
    # Same routing as the Executor.
    assert command[command.index("--model") + 1] == "probe-model"
    assert 'model_provider="probe-provider"' in command
    assert 'model_reasoning_effort="medium"' in command
    # Same exec framing as E, plus the strict report schema.
    assert command[command.index("--output-schema") + 1] == "schema.json"
    assert command[-1] == "-"
    assert command[command.index("exec") + 1] == "--json"


def test_codex_preflight_command_falls_back_to_legacy_runtime_routing(tmp_path):
    executor = _executor_block("codex", tmp_path / "home", tmp_path / "codex.exe")
    executor.pop("routing")
    executor.update({"provider": "legacy", "model": "legacy-model", "effort": "high"})

    command = codex_adapter.build_preflight_command("codex", executor, "-")

    assert command[command.index("--model") + 1] == "legacy-model"
    assert 'model_provider="legacy"' in command
    assert 'model_reasoning_effort="high"' in command


def test_dsh_preflight_command_places_patch_before_app_flags(tmp_path):
    executor = _executor_block("dsh", tmp_path / "home", tmp_path / "dsh.exe")

    command = dsh_adapter.build_preflight_command(
        "dsh", executor, "bootstrap", patch_path=Path("restrictions.yml")
    )

    assert command == [
        "dsh", "--profile", "headless", "--patch", "restrictions.yml", "--json", "bootstrap",
    ]
    assert command.index("--patch") < command.index("--json")


def test_dsh_preflight_command_requires_profile_and_patch(tmp_path):
    executor = _executor_block("dsh", tmp_path / "home", tmp_path / "dsh.exe")
    with pytest.raises(ValueError):
        dsh_adapter.build_preflight_command("dsh", executor, "p", patch_path=None)
    executor.pop("profile")
    with pytest.raises(ValueError):
        dsh_adapter.build_preflight_command(
            "dsh", executor, "p", patch_path=Path("p.yml")
        )


def test_dsh_preflight_patch_disables_every_dangerous_capability(tmp_path):
    executor = _executor_block("dsh", tmp_path / "home", tmp_path / "dsh.exe")

    patch = PC.build_dsh_preflight_patch(executor)
    states = PC.parse_patch_disabled_states(patch)

    for capability, patch_id, _description in PC.DSH_RESTRICTIONS:
        assert states.get(patch_id) is True, capability
    assert states.get("session-title-llm") is True
    # Routing is preserved from the Executor configuration.
    assert 'provider: "probe-provider"' in patch
    assert 'model: "probe-model"' in patch
    assert 'reasoningEffort: "medium"' in patch


def test_dsh_composed_restriction_verification_is_last_writer_wins(tmp_path):
    home = make_executor_home(tmp_path, "dsh")
    patch_path = tmp_path / "restrictions.yml"
    patch_path.write_text(PC.build_dsh_preflight_patch(
        _executor_block("dsh", home, tmp_path / "dsh.exe")
    ), encoding="utf-8")

    verified = PC.verify_dsh_tool_restrictions(patch_path, home, "headless")

    assert verified["verified"] is True
    assert len(verified["restrictions"]) == len(PC.DSH_RESTRICTIONS)
    assert verified["composed_sha256"]


def test_dsh_restriction_verification_fails_closed_on_missing_profile_patch(tmp_path):
    home = make_executor_home(tmp_path, "dsh")
    patch_path = tmp_path / "restrictions.yml"
    patch_path.write_text(PC.build_dsh_preflight_patch(
        _executor_block("dsh", home, tmp_path / "dsh.exe")
    ), encoding="utf-8")
    (home / "profiles" / "headless" / "cordis.patch.yml").unlink()

    with pytest.raises(PC.PreflightVerificationError):
        PC.verify_dsh_tool_restrictions(patch_path, home, "headless")


def test_dsh_preflight_check_fails_closed_when_restrictions_unverifiable(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, adapter="dsh")
    fake = FakeCheckerProcess(stdout=_dsh_jsonl(_report("ALLOW")))

    def unverifiable(*args, **kwargs):
        raise PC.PreflightVerificationError(
            "composed DSH configuration leaves shell / command execution enabled"
        )

    monkeypatch.setattr(PC, "verify_dsh_tool_restrictions", unverifiable)

    result = run_pc(
        config, repo, project, task, contract,
        fake=fake, monkeypatch=monkeypatch, adapter="dsh",
    )

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_DSH_UNVERIFIABLE
    assert result["blocks_executor"] is True
    assert fake.calls == []


def test_dsh_missing_profile_patch_is_fail_closed_end_to_end(tmp_path, monkeypatch):
    """A missing profile patch fails closed before any checker process starts.

    The Executor-home fingerprint already reads the profile overlay, so the
    degraded configuration is rejected at evidence-gathering time. Either way E
    never launches and the result blocks.
    """
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, adapter="dsh")
    Path(
        config["executor"]["executor_home"], "profiles", "headless", "cordis.patch.yml"
    ).unlink()
    fake = FakeCheckerProcess(stdout=_dsh_jsonl(_report("ALLOW")))

    result = run_pc(
        config, repo, project, task, contract,
        fake=fake, monkeypatch=monkeypatch, adapter="dsh",
    )

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["blocks_executor"] is True
    assert fake.calls == []


# --------------------------------------------------------------------------
# ALLOW
# --------------------------------------------------------------------------


def test_allow_returns_decision_and_binds_task_contract_review_configuration(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path, with_review=True)
    review = project / "review" / "T-001.md"
    config = make_config(tmp_path)

    result = run_pc(config, repo, project, task, contract, review, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_ALLOW
    assert result["decision"] == PC.DECISION_ALLOW
    assert result["blocks_executor"] is False
    assert result["enforced"] is True
    bindings = result["bindings"]
    assert bindings["task_id"] == "T-001"
    assert bindings["contract_version"] == 1
    assert bindings["review_sha256"] == PC._sha256_text("Decision: quality_rework.\n")
    assert bindings["configuration_sha256"] == IE.executor_config_fingerprint(config, repo)
    assert bindings["task_sha256"] and bindings["contract_packet_sha256"]
    assert bindings["contract_files_sha256"]
    assert result["revalidation"]["matched"] is True


def test_allow_writes_report_under_project_runtime_only(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)

    result = run_pc(config, repo, project, task, contract, monkeypatch=monkeypatch)

    report_path = Path(result["report_path"]).resolve()
    assert report_path.is_file()
    assert report_path.parent == (project / "runtime" / "preflight").resolve()
    assert Path(result["latest_path"]).is_file()
    assert Path(result["log_path"]).is_file()
    stored = json.loads(report_path.read_text(encoding="utf-8"))
    assert stored["status"] == PC.STATUS_ALLOW
    assert stored["checked_at"]
    # Nothing was written into the product repository.
    assert not (repo / "runtime").exists()
    assert not (repo / "preflight").exists()


def test_allow_records_checker_usage_separately_from_executor_usage(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)

    result = run_pc(config, repo, project, task, contract, monkeypatch=monkeypatch)

    ledger = project / "runtime" / "preflight_token_usage.jsonl"
    summary = project / "runtime" / "preflight_token_usage_summary.json"
    assert ledger.is_file()
    assert summary.is_file()
    # The Executor ledger and retry ledger are untouched by a PC call.
    assert not (project / "runtime" / "executor_token_usage.jsonl").exists()
    assert not (project / "runtime" / "executor_attempts.json").exists()

    record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert record["contract_version"] == 1
    assert record["decision"] == "ALLOW"
    assert record["usage"]["total_tokens"] == 1200
    assert record["usage"]["elapsed_seconds"] is not None
    stored = json.loads(summary.read_text(encoding="utf-8"))
    assert stored["ledger"] == "preflight_token_usage.jsonl"
    assert stored["contracts"]["v1"]["checker_invocations"] == 1
    assert result["checker_usage"]["checker_contract_total"]["checker_invocations"] == 1
    assert result["token_usage"]["elapsed_seconds"] >= 0.0


def test_allow_runs_from_isolated_empty_workspace_outside_repository(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))

    run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    _command, kwargs = fake.calls[0]
    cwd = Path(kwargs["cwd"]).resolve()
    assert cwd != repo.resolve()
    assert repo.resolve() not in cwd.parents
    assert not cwd.exists()  # temporary workspace cleaned after PC
    # Codex receives the whole prompt on stdin, never as an oversized argument.
    assert kwargs["input"].startswith("# PSC Preflight Checker")
    assert fake.calls[0][0][-1] == "-"
    assert str(repo.resolve()) not in " ".join(fake.calls[0][0])


def test_evidence_bundle_is_bounded_to_task_scope_and_configuration(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, preflight={"include_files": ["outside/secret.py"]})

    result = run_pc(config, repo, project, task, contract, monkeypatch=monkeypatch)

    paths = [item["path"] for item in result["evidence_manifest"]]
    assert paths == ["outside/secret.py", "src/example.py"]
    categories = {item["path"]: item["category"] for item in result["evidence_manifest"]}
    assert categories["src/example.py"] == "allowed_scope"
    assert categories["outside/secret.py"] == "configured"
    # Unrelated repository files never enter the bundle.
    assert "README.md" not in paths


def test_configured_include_files_cannot_escape_the_repository(tmp_path):
    config = make_config(tmp_path, preflight={"include_files": ["../../etc/passwd"]})
    with pytest.raises(PC.PreflightConfigurationError):
        PC.preflight_settings(config)

    config = make_config(tmp_path, preflight={"include_files": ["C:\\Windows\\win.ini"]})
    with pytest.raises(PC.PreflightConfigurationError):
        PC.preflight_settings(config)


def test_previous_review_evidence_is_truncated_and_hashed(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    review = project / "review" / "T-001.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("R" * (PC.MAX_REVIEW_CHARS + 50), encoding="utf-8")
    config = make_config(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))

    run_pc(config, repo, project, task, contract, review, fake=fake, monkeypatch=monkeypatch)

    prompt = fake.calls[0][1]["input"]
    assert "[... review truncated by the runtime ...]" in prompt
    assert "R" * (PC.MAX_REVIEW_CHARS + 1) not in prompt


# --------------------------------------------------------------------------
# DENY / UNKNOWN / fail-closed
# --------------------------------------------------------------------------


def test_deny_decision_blocks_executor_and_reports_resolution_owner(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    payload = _report("DENY", [_blocking_finding("planner")])

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, report_text=payload,
    )

    assert result["status"] == PC.STATUS_DENIED
    assert result["decision"] == PC.DECISION_DENY
    assert result["blocks_executor"] is True
    assert result["resolution_owner"] == "planner"
    assert result["findings"][0]["evidence"] == ["T-001 Allowed Scope: src/example.py"]
    assert result["revalidation"] is None  # never revalidated for a DENY


def test_deny_prefers_runtime_owner_over_supervisor(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    findings = [_blocking_finding("supervisor"), dict(_blocking_finding("runtime"), id="F-002")]

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, report_text=_report("DENY", findings),
    )

    assert result["resolution_owner"] == "runtime"


def test_unknown_on_invalid_checker_response(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, report_text="I think this is fine, no JSON here.",
    )

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["decision"] == PC.DECISION_UNKNOWN
    assert result["reason"] == PC.REASON_INVALID_RESPONSE
    assert result["blocks_executor"] is True


def test_unknown_on_empty_checker_response(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, report_text="",
    )

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_EMPTY_RESPONSE


def test_unknown_on_timeout(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    exc = subprocess.TimeoutExpired(cmd=["codex"], timeout=1)
    fake = FakeCheckerProcess(exc=exc)

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_TIMEOUT
    assert result["blocks_executor"] is True


def test_unknown_on_process_failure_exit_code(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")), returncode=2)

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_PROCESS_FAILED
    assert result["blocks_executor"] is True


def test_unknown_on_spawn_failure(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    fake = FakeCheckerProcess(exc=OSError("ENOENT"))

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_LAUNCH_FAILED


@pytest.mark.parametrize("payload,expected", [
    (_report("ALLOW", [_blocking_finding()]), "ALLOW must not contain blocking findings"),
    (_report("DENY", []), "DENY requires at least one blocking finding"),
    (json.dumps({"schema_version": 1, "decision": "MAYBE", "summary": "s", "findings": []}), "ALLOW or DENY"),
    (json.dumps({"schema_version": 2, "decision": "ALLOW", "summary": "s", "findings": []}), "schema_version"),
    (json.dumps({"schema_version": 1, "decision": "ALLOW", "summary": "", "findings": []}), "summary"),
    (json.dumps({"schema_version": 1, "decision": "ALLOW", "summary": "s", "findings": [
        {"id": "F-001", "severity": "advisory", "statement": "x", "evidence": [], "resolution_owner": "supervisor"}
    ]}), "evidence"),
    (json.dumps({"schema_version": 1, "decision": "ALLOW", "summary": "s", "findings": [
        {"id": "F-001", "severity": "advisory", "statement": "x", "evidence": ["e"], "resolution_owner": "user"}
    ]}), "resolution_owner"),
    (json.dumps({"schema_version": 1, "decision": "ALLOW", "summary": "s", "findings": [],
                 "extra": True}), "exactly"),
])
def test_checker_report_schema_is_strict(payload, expected):
    value, error = PC._parse_report_text(payload, allow_wrapped=False)
    assert value is None
    assert expected in error


def test_invalid_report_blocks_executor_end_to_end(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    payload = _report("ALLOW", [_blocking_finding()])

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, report_text=payload,
    )

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_INVALID_RESPONSE
    assert result["blocks_executor"] is True


def test_allow_with_no_findings_is_valid():
    value, error = PC._parse_report_text(_report("ALLOW"), allow_wrapped=False)
    assert error is None
    assert value["decision"] == "ALLOW"


def test_advisory_findings_are_allowed_on_allow():
    payload = _report("ALLOW", [{
        "id": "F-001",
        "severity": "advisory",
        "statement": "Consider adding a negative test.",
        "evidence": ["acceptance.md AC-001"],
        "resolution_owner": "supervisor",
    }])
    value, error = PC._parse_report_text(payload, allow_wrapped=False)
    assert error is None
    assert value["findings"][0]["severity"] == "advisory"


# --------------------------------------------------------------------------
# Configuration mismatch / explicit disable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("block", [
    {"enabled": "yes"},
    {"timeout": 0},
    {"timeout": "600"},
    {"max_files": 0},
    {"include_scope_files": "true"},
    {"include_files": "src/example.py"},
    {"unknown_key": True},
])
def test_invalid_preflight_configuration_fails_closed(tmp_path, block):
    config = make_config(tmp_path, preflight=block)
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)

    result = PC.run_preflight_check(config, repo, project, task, contract)

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_INVALID_CONFIG
    assert result["blocks_executor"] is True


def test_default_preflight_configuration_is_enabled():
    settings = PC.preflight_settings({
        "executor": {"adapter": "codex", "timeout": 1800},
    })
    assert settings == {
        "enabled": True,
        "timeout": 1800,
        "include_scope_files": True,
        "include_files": [],
        "max_files": PC.MAX_INCLUDED_FILES,
    }


def test_preflight_timeout_is_bounded_by_executor_timeout():
    settings = PC.preflight_settings({"executor": {"adapter": "codex", "timeout": 30}})
    assert settings["timeout"] == PC.DEFAULT_TIMEOUT_FLOOR
    settings = PC.preflight_settings({"executor": {"adapter": "codex", "timeout": 99999}})
    assert settings["timeout"] == PC.DEFAULT_TIMEOUT_CEILING
    settings = PC.preflight_settings({
        "executor": {"adapter": "codex", "timeout": 1800},
        "preflight": {"timeout": 45},
    })
    assert settings["timeout"] == 45


def test_explicit_disable_is_the_only_bypass(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, preflight={"enabled": False})
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("DENY", [_blocking_finding()])))

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_DISABLED
    assert result["enforced"] is False
    assert result["blocks_executor"] is False
    assert fake.calls == []


def test_configuration_binding_changes_when_routing_changes(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    first = run_pc(config, repo, project, task, contract, monkeypatch=monkeypatch)
    config["executor"]["routing"]["model"] = "different-model"
    second = run_pc(config, repo, project, task, contract, monkeypatch=monkeypatch)

    assert first["bindings"]["configuration_sha256"] != second["bindings"]["configuration_sha256"]


# --------------------------------------------------------------------------
# Stale SHA evidence / fresh revalidation
# --------------------------------------------------------------------------


def test_stale_scope_file_hash_invalidates_a_fresh_allow(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)

    def mutate_after_allow():
        (repo / "src" / "example.py").write_text("print('changed mid-check')\n", encoding="utf-8")

    fake = FakeCheckerProcess(
        stdout=_codex_jsonl(_report("ALLOW")), hook=mutate_after_allow
    )

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_STALE_EVIDENCE
    assert result["blocks_executor"] is True
    assert result["revalidation"]["matched"] is False
    assert "evidence_sha256" in result["revalidation"]["changed"]


def test_stale_task_hash_invalidates_a_fresh_allow(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    fake = FakeCheckerProcess(
        stdout=_codex_jsonl(_report("ALLOW")),
        hook=lambda: task.write_text("# T-001\n\nChanged.\n", encoding="utf-8"),
    )

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["reason"] == PC.REASON_STALE_EVIDENCE
    assert "task_sha256" in result["revalidation"]["changed"]


def test_deny_is_not_revalidated_and_has_no_fresh_hashes(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    fake = FakeCheckerProcess(
        stdout=_codex_jsonl(_report("DENY", [_blocking_finding()])),
        hook=lambda: (repo / "src" / "example.py").write_text("changed\n", encoding="utf-8"),
    )

    result = run_pc(config, repo, project, task, contract, fake=fake, monkeypatch=monkeypatch)

    assert result["status"] == PC.STATUS_DENIED
    assert result["revalidation"] is None


def test_revalidate_evidence_reports_each_changed_binding(tmp_path):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    bundle = PC.gather_evidence(config, repo, task, contract)

    matched = PC.revalidate_evidence(
        bundle["bindings"], config, repo, task, contract
    )
    assert matched["matched"] is True and matched["changed"] == []

    (contract / "acceptance.md").write_text("# Acceptance\n\nchanged\n", encoding="utf-8")
    (repo / "src" / "example.py").write_text("changed\n", encoding="utf-8")
    stale = PC.revalidate_evidence(bundle["bindings"], config, repo, task, contract)

    assert stale["matched"] is False
    assert "contract_files_sha256" in stale["changed"]
    assert "evidence_sha256" in stale["changed"]


# --------------------------------------------------------------------------
# DSH transport details
# --------------------------------------------------------------------------


def test_dsh_preflight_uses_isolated_prompt_transport_and_runtime_patch(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, adapter="dsh")
    fake = FakeCheckerProcess(stdout=_dsh_jsonl(_report("ALLOW")))

    result = run_pc(
        config, repo, project, task, contract,
        fake=fake, monkeypatch=monkeypatch, adapter="dsh",
    )

    assert result["status"] == PC.STATUS_ALLOW
    command, kwargs = fake.calls[0]
    assert command[1:3] == ["--profile", "headless"]
    assert command.index("--patch") < command.index("--json")
    assert command[-1] == "-"
    assert "PSC Preflight Checker" in kwargs["input"]
    assert str(repo.resolve()) not in command[-1]
    assert kwargs["cwd"] != str(repo.resolve())
    assert "PSC Preflight Checker" in kwargs["input"]
    assert result["dsh_tool_restrictions"]["verified"] is True


def test_dsh_accepts_wrapped_final_json(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, adapter="dsh")
    wrapped = "Here is my decision:\n" + _report("DENY", [_blocking_finding()]) + "\nDone."

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, adapter="dsh", report_text=wrapped,
    )

    assert result["status"] == PC.STATUS_DENIED
    assert result["decision"] == "DENY"


def test_codex_does_not_accept_wrapped_json(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    wrapped = "Sure:\n" + _report("ALLOW") + "\n"

    result = run_pc(
        config, repo, project, task, contract,
        monkeypatch=monkeypatch, report_text=wrapped,
    )

    assert result["status"] == PC.STATUS_UNKNOWN
    assert result["reason"] == PC.REASON_INVALID_RESPONSE


def test_dsh_child_env_uses_same_home_and_strips_supervisor_credentials(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path, adapter="dsh")
    monkeypatch.setenv("OPENAI_API_KEY", "supervisor-secret")
    monkeypatch.setenv("DSH_HOME", "supervisor-dsh-home")
    fake = FakeCheckerProcess(stdout=_dsh_jsonl(_report("ALLOW")))

    run_pc(
        config, repo, project, task, contract,
        fake=fake, monkeypatch=monkeypatch, adapter="dsh",
    )

    env = fake.calls[0][1]["env"]
    assert "OPENAI_API_KEY" not in env
    assert Path(env["DSH_HOME"]).resolve() == Path(
        config["executor"]["executor_home"]
    ).resolve()


# --------------------------------------------------------------------------
# E prompt injection
# --------------------------------------------------------------------------


def test_allow_injects_compact_verified_facts_into_executor_prompt(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path, with_review=True)
    review = project / "review" / "T-001.md"
    config = make_config(tmp_path)

    result = run_pc(config, repo, project, task, contract, review, monkeypatch=monkeypatch)
    facts = PC.verified_facts(result)

    assert facts["decision"] == "ALLOW"
    assert facts["task_id"] == "T-001"
    assert facts["contract_version"] == 1
    assert facts["task_sha256"] == result["bindings"]["task_sha256"]
    assert facts["evidence_sha256"] == result["bindings"]["evidence_sha256"]
    assert facts["verified_files"][0]["path"] == "src/example.py"

    prompt = IE._executor_prompt(
        task, "packet", review, preflight_facts=facts
    )
    assert "## Verified Preflight Facts" in prompt
    assert "Treat them as authoritative" in prompt
    assert result["bindings"]["task_sha256"] in prompt
    assert result["bindings"]["configuration_sha256"] in prompt
    assert "src/example.py sha256=" in prompt
    # The authoritative clause is explicit about hash drift.
    assert "no longer matches its recorded hash" in prompt


def test_executor_prompt_without_facts_has_no_preflight_section(tmp_path):
    prompt = IE._executor_prompt("T-001 task", "packet", None)
    assert "## Verified Preflight Facts" not in prompt


def test_invoke_executor_from_paths_forwards_preflight_facts(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    runtime_path = write_runtime_config(tmp_path, make_config(tmp_path))
    captured: dict = {}

    def fake_executor_call(*args, **kwargs):
        captured.update(kwargs)
        return {"status": "completed", "reason": None, "artifact_paths": {}}

    monkeypatch.setattr(IE, "invoke_executor", fake_executor_call)

    facts = {"task_sha256": "abc", "decision": "ALLOW"}
    result = IE.invoke_executor_from_paths(
        repository=repo,
        runtime_config=runtime_path,
        project=project,
        task_path=task,
        contract_path=contract,
        preflight_facts=facts,
    )

    assert captured["preflight_facts"] == facts
    assert result["status"] == "completed"


# --------------------------------------------------------------------------
# MCP gate: enforcement, no budget charge, compatibility
# --------------------------------------------------------------------------


def _mcp_project(tmp_path: Path) -> tuple[dict, dict]:
    repo = make_repo(tmp_path)
    project, task, contract = make_project(tmp_path)
    config = make_config(tmp_path)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    runtime_path = write_runtime_config(cfg_dir, config)
    (project / "runtime").mkdir(parents=True, exist_ok=True)
    (project / "runtime" / "workflow_state.json").write_text(json.dumps({
        "schema_version": 1,
        "contract_version": 1,
        "current_task": "T-001",
        "status": "ready",
        "attempt": 0,
        "last_completed_task": None,
        "last_stage": "test",
        "execution_owner": "executor",
        "updated_at": "2026-08-28T00:00:00+00:00",
    }), encoding="utf-8")
    return {
        "repository": str(repo),
        "runtime_config": str(runtime_path),
        "project": str(project),
        "task": str(task),
        "contract": str(contract),
    }, config


def test_mcp_entrypoint_denies_executor_when_preflight_denies(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    project = Path(paths["project"])
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("DENY", [_blocking_finding()])))
    monkeypatch.setattr(PC, "subprocess", fake)

    called = {"executor": 0}

    def fake_invoke(**kwargs):
        called["executor"] += 1
        return {"status": "completed", "reason": None, "log_path": "executor.log"}

    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert called["executor"] == 0
    assert result["status"] == "preflight_denied"
    assert result["retryable"] is False
    assert result["workflow_status"] == "blocked"
    assert result["preflight"]["decision"] == "DENY"
    assert result["preflight"]["resolution_owner"] == "supervisor"
    # No Executor attempt happened, so no retry budget was charged.
    assert result["retry_policy"]["charged_budget"] is None
    assert result["retry_policy"]["initial_attempted"] is False
    assert result["retry_policy"]["quality_retries_used"] == 0
    assert result["retry_policy"]["abnormal_retries_used"] == 0
    assert not (project / "runtime" / "executor_attempts.json").exists()
    assert not (project / "runtime" / "executor_token_usage.jsonl").exists()
    # The checker still recorded its own independent usage.
    assert (project / "runtime" / "preflight_token_usage.jsonl").is_file()


def test_mcp_entrypoint_fails_closed_when_preflight_cannot_decide(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl("not a report"))
    monkeypatch.setattr(PC, "subprocess", fake)
    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: pytest.fail("E must not launch after a failed preflight"),
    )

    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert result["status"] == "preflight_unknown"
    assert result["reason"] == PC.REASON_INVALID_RESPONSE
    assert result["retry_policy"]["charged_budget"] is None


def test_mcp_entrypoint_gates_even_when_supervisor_skips_separate_check(tmp_path, monkeypatch):
    """No MCP parameter can bypass PC: the gate runs inside the dispatch call."""
    paths, _config = _mcp_project(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("DENY", [_blocking_finding("runtime")])))
    monkeypatch.setattr(PC, "subprocess", fake)
    launched = {"count": 0}

    def fake_invoke(**kwargs):
        launched["count"] += 1
        return {"status": "completed", "reason": None, "log_path": "executor.log"}

    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    for kwargs in ({}, {"retry_kind": "quality_rework"}, {"previous_review": None}):
        result = MCP._invoke_executor_impl(**paths, **kwargs)
        assert result["status"] in {"preflight_denied", "preflight_unknown"}

    assert launched["count"] == 0


def test_mcp_entrypoint_injects_facts_and_preserves_executor_path(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))
    monkeypatch.setattr(PC, "subprocess", fake)
    observed = {}

    def fake_invoke(**kwargs):
        observed.update(kwargs)
        return {
            "status": "completed",
            "reason": None,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": "executor.log",
            "executor_config_sha256": "sha",
            "errors": [],
        }

    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_invoke)

    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert result["status"] == "completed"
    assert result["preflight"]["status"] == PC.STATUS_ALLOW
    facts = observed["preflight_facts"]
    assert facts["decision"] == "ALLOW"
    assert facts["task_sha256"] == result["preflight"]["bindings"]["task_sha256"]
    # Existing retry semantics are untouched for a real E attempt.
    assert result["retry_policy"]["initial_attempted"] is True
    assert result["retry_policy"]["charged_budget"] is None


def test_mcp_success_attaches_ready_to_display_e_pc_usage_report(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    monkeypatch.setattr(
        PC, "subprocess", FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW"))),
    )

    def fake_executor(**kwargs):
        return {
            "status": "completed", "reason": None,
            "log_path": "executor.log",
            "token_usage": {
                "available": True, "exact": True, "source": "fixture",
                "input_tokens": 1_000, "uncached_input_tokens": 20,
                "cached_input_tokens": 980, "cache_write_input_tokens": 0,
                "output_tokens": 5, "reasoning_output_tokens": 2,
                "total_tokens": 1_005, "elapsed_seconds": 1037.28,
            },
        }

    monkeypatch.setattr(MCP.executor_runtime, "invoke_executor_from_paths", fake_executor)
    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert result["executor_usage"]["contract_total"]["invocations"] == 1
    pc = result["usage_report"]["checker_workflow_total"]
    assert pc["checker_invocations"] == 1
    assert pc["exact"] is True
    message = result["usage_report"]["markdown"]
    assert "本次 E 耗时 0 h 17 m 17 s" in message
    assert "本次缓存命中率 98.0%，输出/输入比 0.5%" in message
    assert "| E 累计（v1，1 次） | PC 累计（1 次） |" in message
    assert "| 总 Token | 1,005 | 1,005 | 1,200 |" in message
    assert "累计比率" not in message


def test_psc_preflight_check_tool_never_launches_executor(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    fake = FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))
    monkeypatch.setattr(PC, "subprocess", fake)
    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: pytest.fail("psc_preflight_check must not launch E"),
    )

    result = MCP.preflight_check_tool(**paths)

    assert result["status"] == PC.STATUS_ALLOW
    assert result["decision"] == "ALLOW"
    assert "stdout" not in result
    assert result["report_sha256"]
    assert result["token_usage"]["total_tokens"] == 1200


def test_psc_preflight_check_tool_rejects_inline_markdown(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    monkeypatch.setattr(
        PC, "subprocess", FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))
    )

    result = MCP.preflight_check_tool(
        **{**paths, "task": "# T-001\n\ninline markdown, not a path"}
    )

    assert result["status"] == "invalid_mcp_arguments"
    assert result["retryable"] is False


def test_runtime_config_unavailable_is_not_evaluated_and_not_a_fail_open(tmp_path, monkeypatch):
    """Legacy/direct callers without a loadable runtime config keep old behaviour.

    E cannot launch under an unreadable runtime configuration anyway: both the
    readiness gate and `invoke_executor` fail closed before launch. So this is
    not a fail-open path for the checker's own decisions.
    """
    paths, _config = _mcp_project(tmp_path)
    paths["runtime_config"] = str(tmp_path / "does-not-exist.json")
    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: {"status": "executor_unavailable",
                          "reason": "invalid_runtime_config", "log_path": None},
    )

    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert result["status"] == "executor_unavailable"
    assert "preflight" not in result


def test_preflight_gate_fails_closed_on_runtime_error(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("checker crashed")

    monkeypatch.setattr(MCP.preflight_runtime, "run_preflight_check", boom)

    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert result["status"] == "preflight_unknown"
    assert result["reason"] == "preflight_runtime_error"
    assert result["retryable"] is False


def test_explicit_disable_preserves_legacy_executor_dispatch(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    disabled = make_config(tmp_path, preflight={"enabled": False})
    write_runtime_config(Path(paths["runtime_config"]).parent, disabled)
    monkeypatch.setattr(
        PC,
        "subprocess",
        FakeCheckerProcess(exc=AssertionError("checker must not run when disabled")),
    )
    monkeypatch.setattr(
        MCP.executor_runtime,
        "invoke_executor_from_paths",
        lambda **kwargs: {
            "status": "completed",
            "reason": None,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "changed_paths": [],
            "scope_violations": [],
            "artifact_paths": {},
            "log_path": "executor.log",
            "executor_config_sha256": "sha",
            "errors": [],
        },
    )

    result = MCP._invoke_executor_impl(**paths, retry_kind="initial")

    assert result["status"] == "completed"
    assert result["retry_policy"]["initial_attempted"] is True
    assert "preflight" not in result


def test_compact_preflight_result_omits_raw_transcript(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    monkeypatch.setattr(
        PC, "subprocess", FakeCheckerProcess(stdout=_codex_jsonl(_report("ALLOW")))
    )

    compact = MCP.preflight_check_tool(**paths)

    assert set(compact) <= {
        "status", "decision", "reason", "enforced", "blocks_executor", "task_id",
        "contract_version", "adapter", "report_sha256", "summary", "findings",
        "resolution_owner", "bindings", "revalidation", "report_path", "latest_path",
        "log_path", "elapsed_seconds", "token_usage", "checker_usage",
        "dsh_tool_restrictions", "errors",
    }
    assert "command" not in compact
    assert "report" not in compact


# --------------------------------------------------------------------------
# Documentation / wiring compatibility
# --------------------------------------------------------------------------


def test_configure_supervisor_expected_tools_include_preflight_check():
    import configure_supervisor_mcp as CFG

    assert "mcp__agentic_sdlc_executor__psc_preflight_check" in CFG.EXPECTED_TOOLS


def test_preflight_never_writes_to_executor_ledgers_or_attempt_counters(tmp_path, monkeypatch):
    paths, _config = _mcp_project(tmp_path)
    project = Path(paths["project"])
    monkeypatch.setattr(
        PC, "subprocess", FakeCheckerProcess(stdout=_codex_jsonl(_report("DENY", [_blocking_finding()])))
    )

    MCP.preflight_check_tool(**paths)

    runtime_dir = project / "runtime"
    assert not (runtime_dir / "executor_token_usage.jsonl").exists()
    assert not (runtime_dir / "executor_token_usage_summary.json").exists()
    assert not (runtime_dir / "executor_attempts.json").exists()
