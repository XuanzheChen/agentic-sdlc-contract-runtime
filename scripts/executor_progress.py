"""Best-effort Executor progress observation; never a source of attempt outcomes."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

_SECRET = (
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
ProgressCallback = Callable[[dict[str, Any]], None]


def _clean(value: Any, limit: int = 180) -> str:
    text = _CONTROL.sub(" ", str(value)).strip()
    for pattern in _SECRET:
        text = pattern.sub(r"\1[REDACTED]" if pattern.groups else "[REDACTED]", text)
    return text[:limit]


def format_elapsed_time(seconds: float) -> str:
    """Render a duration for humans; keep numeric seconds for machine consumers."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours} h {minutes} m {secs} s"


def _target(tool: str, args: Any) -> str:
    """Only allowlist path hints; never forward arbitrary commands/tool results."""
    if not isinstance(args, dict):
        return ""
    for field in ("path", "file_path", "filePath", "filename"):
        path = args.get(field)
        if isinstance(path, str) and path:
            return _clean(path, 120)
    command = args.get("command") or args.get("cmd")
    if isinstance(command, str):
        if re.search(r"\bpytest\b", command):
            return "pytest"
        if re.search(r"\b(?:npm|pnpm)\s+test\b", command):
            return "package tests"
    return ""


def summarize_event(adapter: str, event: Any) -> tuple[str, str, int, int] | None:
    """Return (kind, safe human summary, steps_delta, tool_delta)."""
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind in ("tool_call", "item.started"):
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        if kind == "item.started" and item.get("type") not in ("command_execution", "file_change", "mcp_tool_call"):
            return None
        tool = _clean(item.get("tool") or item.get("name") or item.get("type") or "tool", 48)
        hint = _target(tool, item.get("input") or item.get("arguments"))
        return "tool_call", f"{tool}: {hint}" if hint else tool, 0, 1
    if kind == "status":
        phase = event.get("phase")
        if phase == "step_end":
            return "step_end", "Model step completed", 1, 0
        if phase == "step_start":
            return "step_start", "Model step started", 0, 0
        if phase in ("turn_end", "turn_start"):
            return "phase", _clean(phase), 0, 0
    if kind == "turn.completed":
        return "step_end", "Codex turn completed", 1, 0
    if kind == "final":
        return "finalizing", "Executor produced final response", 0, 0
    if kind == "error":
        return "error", "Executor reported an error", 0, 0
    return None


class ProgressRecorder:
    """Atomically publish a latest snapshot and bounded, redacted event history."""

    def __init__(self, project: Path, task: str, retry_kind: str, adapter: str,
                 model: str, callback: ProgressCallback | None = None) -> None:
        self.run_id = uuid.uuid4().hex
        root = Path(project) / "runtime"
        self.status_path = root / "executor-progress.json"
        self.events_path = root / "executor-progress" / f"{self.run_id}.jsonl"
        self.callback = callback
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_event = self.started
        self.state: dict[str, Any] = {
            "run_id": self.run_id, "status": "running",
            "task": _clean(task), "retry_kind": _clean(retry_kind),
            "adapter": adapter, "model": _clean(model), "steps": 0,
            "tool_calls": 0, "sequence": 0,
            "started_at": self._now(), "last_executor_event_at": None,
            "last_heartbeat_at": None, "last_activity": "Executor starting",
        }
        self.emit("started", f"Executor started · {adapter} / {_clean(model)}", activity=False)

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def _publish(self, kind: str, summary: str, *, activity: bool) -> None:
        with self.lock:
            if activity:
                self.last_event = time.monotonic()
                self.state["last_executor_event_at"] = self._now()
            self.state["sequence"] += 1
            elapsed = max(0.0, time.monotonic() - self.started)
            silence = max(0.0, time.monotonic() - self.last_event)
            self.state["elapsed_seconds"] = round(elapsed, 1)
            self.state["elapsed_display"] = format_elapsed_time(elapsed)
            self.state["seconds_since_executor_event"] = round(silence, 1)
            self.state["last_event_age_display"] = format_elapsed_time(silence)
            self.state["last_activity"] = _clean(summary)
            now = self._now()
            if kind == "heartbeat":
                self.state["last_heartbeat_at"] = now
            snapshot = dict(self.state)
            event = {"at": now, "kind": kind, "message": _clean(summary),
                     "sequence": snapshot["sequence"], "steps": snapshot["steps"],
                     "tool_calls": snapshot["tool_calls"]}
            try:
                self.events_path.parent.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                temporary = self.status_path.with_name(self.status_path.name + f".{self.run_id}.tmp")
                temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary, self.status_path)
            except OSError:
                # Progress artifacts are observational and must not fail an attempt.
                pass
        if self.callback is not None:
            try:
                self.callback(snapshot)
            except Exception:
                pass

    def emit(self, kind: str, summary: str, *, activity: bool = True) -> None:
        self._publish(kind, summary, activity=activity)

    def accept_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict):
            return
        with self.lock:
            self.last_event = time.monotonic()
            self.state["last_executor_event_at"] = self._now()
        summarized = summarize_event(str(self.state["adapter"]), event)
        if summarized is None:
            return
        kind, summary, step_delta, tool_delta = summarized
        with self.lock:
            self.state["steps"] += step_delta
            self.state["tool_calls"] += tool_delta
        # Start boundaries are frequent and less useful than completed steps.
        if kind != "step_start":
            self.emit(kind, summary)

    def heartbeat(self) -> None:
        with self.lock:
            silence = format_elapsed_time(time.monotonic() - self.last_event)
            steps = self.state["steps"]
            tools = self.state["tool_calls"]
        self.emit("heartbeat", f"Still running · {steps} steps · {tools} tools · last event {silence} ago", activity=False)

    def finish(self, status: str, reason: str | None = None) -> None:
        with self.lock:
            self.state["status"] = status
            self.state["reason"] = _clean(reason) if reason else None
            self.state["finished_at"] = self._now()
        self.emit("finished", f"Executor {status}" + (f" · {_clean(reason)}" if reason else ""), activity=False)


def run_streaming(command: list[str], *, cwd: str, env: dict[str, str],
                  input: str | None, timeout: float,
                  recorder: ProgressRecorder) -> SimpleNamespace:
    """Popen with concurrent draining, bounded in-flight memory, and timeout.

    The entire stdout/stderr are reconstructed at completion for the existing
    parsers and accounting; progress is an independent best-effort side channel.
    """
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    assert process.stdout is not None and process.stderr is not None

    def drain(pipe: Any, handle: Any, on_line: Callable[[str], None] | None) -> None:
        try:
            for line in pipe:
                handle.write(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass

    def write_stdin() -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(input or "")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file, \
         tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
        readers = [
            threading.Thread(target=drain, args=(process.stdout, stdout_file, recorder.accept_line), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr_file, None), daemon=True),
        ]
        for reader in readers:
            reader.start()
        if input is not None:
            threading.Thread(target=write_stdin, daemon=True).start()
        deadline = time.monotonic() + timeout
        next_heartbeat = time.monotonic() + 30
        timed_out = False
        try:
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    process.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() >= next_heartbeat and process.poll() is None:
                    recorder.heartbeat()
                    next_heartbeat = time.monotonic() + 30
        finally:
            if timed_out and process.poll() is None:
                process.kill()
            process.wait()
            for reader in readers:
                reader.join(timeout=5)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout, stderr = stdout_file.read(), stderr_file.read()
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=process.returncode)
