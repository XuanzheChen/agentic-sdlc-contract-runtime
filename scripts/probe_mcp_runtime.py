#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


MIN_VERSION = (3, 10)


def _resolve_executable(value: str | Path) -> Path | None:
    raw = str(value)
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve() if candidate.is_file() else None
    found = shutil.which(raw)
    return Path(found).resolve() if found else None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return False


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _python_probe(executable: Path) -> dict[str, Any]:
    code = r'''
import json
import ssl
import sys

mcp_error = None
try:
    from mcp.server import MCPServer
    mcp_available = True
except Exception as exc:
    mcp_available = False
    mcp_error = f"{type(exc).__name__}: {exc}"

print(json.dumps({
    "version": list(sys.version_info[:3]),
    "version_string": sys.version.split()[0],
    "ssl_available": True,
    "openssl_version": ssl.OPENSSL_VERSION,
    "mcp_available": mcp_available,
    "mcp_error": mcp_error,
}))
'''
    try:
        completed = subprocess.run(
            [str(executable), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "probe_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stderr": "",
        }
    if completed.returncode != 0:
        return {
            "probe_ok": False,
            "error": "python_probe_failed",
            "stderr": completed.stderr.strip(),
        }
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return {
            "probe_ok": False,
            "error": "python_probe_invalid_json",
            "stderr": completed.stderr.strip(),
        }
    payload["probe_ok"] = True
    return payload


def _pip_probe(executable: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(executable), "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"pip_available": False, "pip_error": f"{type(exc).__name__}: {exc}"}
    return {
        "pip_available": completed.returncode == 0,
        "pip_version": completed.stdout.strip() if completed.returncode == 0 else None,
        "pip_error": completed.stderr.strip() if completed.returncode != 0 else None,
    }


def probe_mcp_runtime(
    python: str | Path,
    *,
    repository: Path | None = None,
    project_python: str | Path | None = None,
) -> dict[str, Any]:
    executable = _resolve_executable(python)
    if executable is None:
        return {
            "status": "invalid",
            "reason": "python_not_found",
            "python": str(python),
            "independent": False,
            "ready": False,
            "installable": False,
        }

    independent = True
    independence_reason = None
    if repository is not None and _inside(executable, Path(repository)):
        independent = False
        independence_reason = "python_inside_repository"
    if project_python is not None:
        project_executable = _resolve_executable(project_python)
        if project_executable is not None and _same_path(executable, project_executable):
            independent = False
            independence_reason = "same_as_project_python"

    python_probe = _python_probe(executable)
    pip_probe = _pip_probe(executable)

    version = tuple(python_probe.get("version", []))
    version_ok = len(version) >= 2 and version[:2] >= MIN_VERSION
    ssl_ok = bool(python_probe.get("ssl_available")) and python_probe.get("probe_ok", False)
    pip_ok = bool(pip_probe.get("pip_available"))
    mcp_ok = bool(python_probe.get("mcp_available"))

    installable = independent and version_ok and ssl_ok and pip_ok
    ready = installable and mcp_ok

    if not independent:
        status, reason = "rejected", independence_reason
    elif not python_probe.get("probe_ok"):
        status, reason = "invalid", "python_or_ssl_probe_failed"
    elif not version_ok:
        status, reason = "invalid", "python_too_old"
    elif not pip_ok:
        status, reason = "invalid", "pip_unavailable"
    elif not mcp_ok:
        status, reason = "install_required", "mcp_not_installed"
    else:
        status, reason = "ready", None

    return {
        "status": status,
        "reason": reason,
        "python": str(executable),
        "independent": independent,
        "ready": ready,
        "installable": installable,
        "python_version": python_probe.get("version_string"),
        "openssl_version": python_probe.get("openssl_version"),
        "pip_available": pip_ok,
        "mcp_available": mcp_ok,
        "mcp_error": python_probe.get("mcp_error"),
        "probe_error": python_probe.get("error"),
        "probe_stderr": python_probe.get("stderr"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe an independent Python runtime for the PSC Executor MCP server."
    )
    parser.add_argument("--python", required=True, help="Candidate Python executable or PATH command.")
    parser.add_argument(
        "--repository",
        type=Path,
        help="Reject a candidate interpreter located inside the product repository.",
    )
    parser.add_argument(
        "--project-python",
        help="Reject the candidate when it is the same interpreter as the project Python.",
    )
    args = parser.parse_args()

    result = probe_mcp_runtime(
        args.python,
        repository=args.repository,
        project_python=args.project_python,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
