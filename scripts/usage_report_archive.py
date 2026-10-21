"""Immutable, verifiable per-invocation Executor usage reports.

Presentation receipts are independent from the authoritative E/PC token ledgers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


def _report_dir(project: Path) -> Path:
    return Path(project).resolve() / "runtime" / "executor-usage-reports"


def _validate_id(invocation_id: str) -> None:
    if not isinstance(invocation_id, str) or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise ValueError("invalid Executor invocation_id")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def persist_usage_report(
    project: Path, invocation_id: str,
    usage_report: dict[str, Any], *,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Commit report Markdown plus its checksum-bound JSON provenance snapshot."""
    _validate_id(invocation_id)
    markdown = usage_report.get("markdown")
    if not isinstance(markdown, str) or not markdown:
        raise ValueError("missing formatted usage report")
    folder = _report_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    md_path = folder / (invocation_id + ".md")
    json_path = folder / (invocation_id + ".json")
    sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": 1,
        "invocation_id": invocation_id,
        "markdown_sha256": sha,
        "usage_report": usage_report,
        "metadata": metadata,
    }
    if json_path.exists():
        previous = load_usage_report(project, invocation_id)
        if previous["usage_report"] != usage_report or previous["metadata"] != metadata:
            raise ValueError("conflicting immutable usage report")
    else:
        _atomic_write(md_path, markdown + "\n")
        _atomic_write(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "persisted",
        "invocation_id": invocation_id,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "sha256": sha,
    }


def load_usage_report(project: Path, invocation_id: str) -> dict[str, Any]:
    """Read-only recovery with checksum validation; never dispatches Executor."""
    _validate_id(invocation_id)
    folder = _report_dir(project)
    json_path = folder / (invocation_id + ".json")
    md_path = folder / (invocation_id + ".md")
    record = json.loads(json_path.read_text(encoding="utf-8"))
    report = record.get("usage_report")
    if (
        record.get("invocation_id") != invocation_id
        or record.get("schema_version") != 1
        or not isinstance(report, dict)
        or not isinstance(report.get("markdown"), str)
    ):
        raise ValueError("invalid usage report receipt")
    markdown = report["markdown"]
    sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    if sha != record.get("markdown_sha256"):
        raise ValueError("usage report checksum mismatch")
    if md_path.exists() and md_path.read_text(encoding="utf-8") != markdown + "\n":
        raise ValueError("usage report Markdown mirror mismatch")
    return {
        "status": "ok",
        "invocation_id": invocation_id,
        "usage_report": report,
        "metadata": record.get("metadata"),
        "sha256": sha,
    }
