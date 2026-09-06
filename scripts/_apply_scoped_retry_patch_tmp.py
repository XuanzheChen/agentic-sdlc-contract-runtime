from __future__ import annotations

import re
from pathlib import Path


def load(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def save(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def must_replace(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 match, found {count}")
    return text.replace(old, new, 1)


def must_sub(text: str, pattern: str, repl: str, label: str, flags: int = 0) -> str:
    new, count = re.subn(pattern, repl, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"{label}: expected 1 regex match, found {count}")
    return new


def patch_runtime() -> None:
    path = "scripts/psc_runtime.py"
    s = load(path)
    if '"switch-to-supervisor-for-current-task"' in s:
        return

    s = must_replace(
        s,
        'RETRY_EXHAUSTION_DECISIONS = frozenset({\n    "reset-and-continue-executor",\n    "switch-to-supervisor",\n})',
        'RETRY_EXHAUSTION_DECISIONS = frozenset({\n    "reset-and-continue-executor",\n    "switch-to-supervisor-for-current-task",\n    "switch-to-supervisor",\n})',
        "decision set",
    )

    s = must_replace(
        s,
        '    reset-and-continue-executor starts a fresh Executor execution round for the\n'
        '    exact Contract-version/Task key: both retry budgets return to zero and the\n'
        '    new round has no initial attempt yet. switch-to-supervisor preserves the\n'
        '    exhausted E round as history and hands the blocked task to S.\n',
        '    reset-and-continue-executor starts a fresh Executor execution round for the\n'
        '    exact Contract-version/Task key: both retry budgets return to zero and the\n'
        '    new round has no initial attempt yet. switch-to-supervisor-for-current-task\n'
        '    preserves the exhausted E round and gives S only this Task, with a durable\n'
        '    handback to E at the next Task boundary. switch-to-supervisor preserves the\n'
        '    exhausted E round and gives S sticky ownership until another explicit handoff.\n',
        "resolver docstring",
    )

    s = must_replace(
        s,
        '        raise ValueError(\n            "decision must be reset-and-continue-executor or switch-to-supervisor"\n        )',
        '        raise ValueError(\n            "decision must be reset-and-continue-executor, "\n            "switch-to-supervisor-for-current-task, or switch-to-supervisor"\n        )',
        "resolver validation",
    )

    s = must_replace(
        s,
        '    reset_budget: str | None = None\n    reset_budgets: list[str] = []\n    execution_round: int | None = None\n',
        '    reset_budget: str | None = None\n    reset_budgets: list[str] = []\n    execution_round: int | None = None\n    scoped_takeover: dict[str, Any] | None = None\n',
        "resolver locals",
    )

    s = must_replace(
        s,
        '    else:\n        owner = "supervisor"\n        reason = f"user switched blocked {task_id} from E to S"\n\n    history = state.get("execution_owner_history")',
        '    elif decision == "switch-to-supervisor-for-current-task":\n'
        '        owner = "supervisor"\n'
        '        scoped_takeover = {\n'
        '            "contract_version": version,\n'
        '            "task": task_id,\n'
        '            "scope": "current_task",\n'
        '            "return_owner": "executor",\n'
        '            "created_at": timestamp,\n'
        '            "reason": "retry_exhaustion_scoped_supervisor_takeover",\n'
        '        }\n'
        '        reason = (\n'
        '            f"user switched blocked {task_id} from E to S for this Task only; "\n'
        '            "ownership returns to E at the next Task boundary"\n'
        '        )\n'
        '    else:\n'
        '        owner = "supervisor"\n'
        '        reason = f"user switched blocked {task_id} from E to S with sticky ownership"\n\n'
        '    if scoped_takeover is not None:\n'
        '        state["scoped_supervisor_takeover"] = scoped_takeover\n'
        '    else:\n'
        '        state.pop("scoped_supervisor_takeover", None)\n\n'
        '    history = state.get("execution_owner_history")',
        "resolver owner branch",
    )

    s = must_replace(
        s,
        '        "new_execution_round": execution_round,\n        "resolved_owner": owner,\n        "resolved_at": timestamp,\n',
        '        "new_execution_round": execution_round,\n'
        '        "resolved_owner": owner,\n'
        '        "execution_owner_scope": (\n'
        '            "current_task" if scoped_takeover is not None\n'
        '            else "sticky" if owner == "supervisor"\n'
        '            else "executor"\n'
        '        ),\n'
        '        "return_owner_after_task": (\n'
        '            scoped_takeover["return_owner"] if scoped_takeover is not None else None\n'
        '        ),\n'
        '        "resolved_at": timestamp,\n',
        "resolution history metadata",
    )

    s = must_replace(
        s,
        '        "execution_round": execution_round,\n        "execution_owner": owner,\n        "workflow_status": "ready",\n    }\n\n\ndef resolve_runtime_failure',
        '        "execution_round": execution_round,\n'
        '        "execution_owner": owner,\n'
        '        "execution_owner_scope": (\n'
        '            "current_task" if scoped_takeover is not None\n'
        '            else "sticky" if owner == "supervisor"\n'
        '            else "executor"\n'
        '        ),\n'
        '        "return_owner_after_task": (\n'
        '            scoped_takeover["return_owner"] if scoped_takeover is not None else None\n'
        '        ),\n'
        '        "workflow_status": "ready",\n'
        '    }\n\n\n'
        'def finish_scoped_supervisor_takeover(project: Path, task_id: str) -> dict[str, Any]:\n'
        '    """Return construction ownership to E after a scoped S-only Task completes.\n\n'
        '    The exhausted E round is preserved for audit. The next Task already owns an\n'
        '    independent retry key and therefore naturally starts with fresh E budgets.\n'
        '    """\n'
        '    project = Path(project).resolve()\n'
        '    task_id = str(task_id or "").strip()\n'
        '    if not re.fullmatch(r"T-\\d{3,}", task_id):\n'
        '        raise ValueError("task must be a T-### identifier")\n'
        '    state_path = project / "runtime" / "workflow_state.json"\n'
        '    if not state_path.is_file():\n'
        '        raise ValueError(f"workflow state not found: {state_path}")\n'
        '    state = load_json(state_path)\n'
        '    if not isinstance(state, dict):\n'
        '        raise ValueError(f"invalid workflow state: {state_path}")\n'
        '    marker = state.get("scoped_supervisor_takeover")\n'
        '    if not isinstance(marker, dict) or marker.get("task") != task_id:\n'
        '        raise ValueError(f"no scoped Supervisor takeover is active for {task_id}")\n'
        '    if state.get("execution_owner", "executor") != "supervisor":\n'
        '        raise ValueError("scoped Supervisor takeover requires execution_owner=supervisor")\n'
        '    if state.get("status") in {"executor_running", "supervisor_running", "blocked"}:\n'
        '        raise ValueError("cannot finish scoped Supervisor takeover while execution is running or blocked")\n'
        '    boundary_reached = (\n'
        '        state.get("status") in {"task_passed", "workflow_passed"}\n'
        '        or state.get("last_completed_task") == task_id\n'
        '        or state.get("current_task") != task_id\n'
        '    )\n'
        '    if not boundary_reached:\n'
        '        raise ValueError(\n'
        '            f"{task_id} has not reached a completed Task boundary; "\n'
        '            "persist terminal task evidence/state before returning ownership to E"\n'
        '        )\n'
        '    timestamp = now()\n'
        '    history = state.get("execution_owner_history")\n'
        '    if not isinstance(history, list):\n'
        '        history = []\n'
        '    reason = f"scoped Supervisor takeover for {task_id} completed; returned construction to E"\n'
        '    history.append({\n'
        '        "owner": "executor",\n'
        '        "previous_owner": "supervisor",\n'
        '        "reason": reason,\n'
        '        "task": task_id,\n'
        '        "changed_at": timestamp,\n'
        '    })\n'
        '    state = dict(state)\n'
        '    state["execution_owner"] = "executor"\n'
        '    state["execution_owner_reason"] = reason\n'
        '    state["execution_owner_updated_at"] = timestamp\n'
        '    state["execution_owner_history"] = history\n'
        '    state.pop("scoped_supervisor_takeover", None)\n'
        '    state["last_stage"] = "scoped_supervisor_takeover_completed"\n'
        '    state["updated_at"] = timestamp\n'
        '    dump_json(state_path, state)\n'
        '    return {\n'
        '        "status": "scoped_supervisor_takeover_completed",\n'
        '        "task": task_id,\n'
        '        "execution_owner": "executor",\n'
        '        "workflow_status": state.get("status"),\n'
        '        "retry_counters_changed": False,\n'
        '        "execution_round_changed": False,\n'
        '    }\n\n\n'
        'def resolve_runtime_failure',
        "finish scoped helper",
    )

    # Generic owner changes are sticky and cancel a scoped takeover marker.
    set_start = s.index('def set_execution_owner(')
    set_end = s.index('\n\nRETRY_EXHAUSTION_DECISIONS', set_start)
    block = s[set_start:set_end]
    block = must_replace(
        block,
        '    state["execution_owner"] = owner\n',
        '    state.pop("scoped_supervisor_takeover", None)\n    state["execution_owner"] = owner\n',
        "generic handoff clears scoped marker",
    )
    s = s[:set_start] + block + s[set_end:]

    s = must_replace(
        s,
        '    resolve.add_argument("--decision", choices=sorted(RETRY_EXHAUSTION_DECISIONS), required=True, help="reset exhausted task budget and continue with E, or switch the task to S")\n',
        '    resolve.add_argument("--decision", choices=sorted(RETRY_EXHAUSTION_DECISIONS), required=True, help="continue with a fresh E round, give only this Task to S, or give S sticky ownership")\n'
        '    finish_scoped = sub.add_parser("finish-scoped-supervisor-takeover", help="return construction ownership to E after the scoped S-only Task reaches a task boundary")\n'
        '    finish_scoped.add_argument("--project", type=Path, required=True, help="workflow project directory")\n'
        '    finish_scoped.add_argument("--task", required=True, help="completed scoped Task ID (T-###)")\n',
        "CLI parser",
    )

    s = must_replace(
        s,
        '        if args.command == "resolve-runtime-failure":\n            result = resolve_runtime_failure(args.project, args.reason)\n',
        '        if args.command == "finish-scoped-supervisor-takeover":\n'
        '            result = finish_scoped_supervisor_takeover(args.project, args.task)\n'
        '            print(json.dumps(result, indent=2, ensure_ascii=False))\n'
        '            return 0\n'
        '        if args.command == "resolve-runtime-failure":\n'
        '            result = resolve_runtime_failure(args.project, args.reason)\n',
        "CLI dispatch",
    )

    save(path, s)


def patch_mcp() -> None:
    path = "scripts/psc_mcp_server.py"
    s = load(path)
    if '"switch-to-supervisor-for-current-task"' in s:
        return

    s = must_replace(
        s,
        '                "explicit user decision: reset this task-local exhausted "\n'
        '                "budget and continue with E, or switch execution to S."\n',
        '                "explicit user decision: reset both task-local budgets and "\n'
        '                "continue with E, switch only this Task to S and return the next "\n'
        '                "Task to E, or switch execution to S with sticky ownership."\n',
        "budget block message",
    )

    s = must_replace(
        s,
        '        "decision_required": [\n            "reset-and-continue-executor",\n            "switch-to-supervisor",\n        ],\n',
        '        "decision_required": [\n            "reset-and-continue-executor",\n            "switch-to-supervisor-for-current-task",\n            "switch-to-supervisor",\n        ],\n',
        "MCP decision list",
    )

    helper = '''def _restore_executor_after_scoped_supervisor_boundary(\n    project: Path,\n    task_path: Path,\n) -> bool:\n    """Auto-return ownership when S's scoped Task has already advanced."""\n    path = _workflow_state_path(project)\n    try:\n        state = json.loads(path.read_text(encoding="utf-8"))\n    except (OSError, json.JSONDecodeError):\n        return False\n    if not isinstance(state, dict) or state.get("execution_owner") != "supervisor":\n        return False\n    marker = state.get("scoped_supervisor_takeover")\n    if not isinstance(marker, dict) or marker.get("return_owner") != "executor":\n        return False\n    scoped_task = marker.get("task")\n    requested_task = _task_id_from_path(task_path)\n    current_task = state.get("current_task")\n    if not (isinstance(scoped_task, str) and requested_task != scoped_task and current_task == requested_task):\n        return False\n    timestamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()\n    history = state.get("execution_owner_history")\n    if not isinstance(history, list):\n        history = []\n    reason = (\n        f"scoped Supervisor takeover for {scoped_task} ended at Task boundary; "\n        f"returned construction to E for {requested_task}"\n    )\n    history.append({\n        "owner": "executor",\n        "previous_owner": "supervisor",\n        "reason": reason,\n        "task": requested_task,\n        "changed_at": timestamp,\n    })\n    state = dict(state)\n    state["execution_owner"] = "executor"\n    state["execution_owner_reason"] = reason\n    state["execution_owner_updated_at"] = timestamp\n    state["execution_owner_history"] = history\n    state.pop("scoped_supervisor_takeover", None)\n    state["last_stage"] = "scoped_supervisor_takeover_completed"\n    state["updated_at"] = timestamp\n    _write_workflow_state(project, state)\n    return True\n\n\n'''
    s = must_replace(s, 'def _mark_nonretryable_runtime_blocked(\n', helper + 'def _mark_nonretryable_runtime_blocked(\n', "MCP auto-return helper")

    s = must_replace(
        s,
        '    project_path = Path(project)\n    task_path = Path(task)\n    contract_path = Path(contract)\n    if _workflow_execution_owner(project_path) == "supervisor":\n',
        '    project_path = Path(project)\n    task_path = Path(task)\n    contract_path = Path(contract)\n'
        '    _restore_executor_after_scoped_supervisor_boundary(project_path, task_path)\n'
        '    if _workflow_execution_owner(project_path) == "supervisor":\n',
        "MCP owner check",
    )

    save(path, s)


def patch_docs() -> None:
    skill = load("SKILL.md")
    skill = must_replace(skill, "Offer exactly two resolution choices for that blocked task:", "Offer exactly three resolution choices for that blocked task:", "skill choice count")
    old = '''2. `switch-to-supervisor`: do not reset either E budget; atomically set\n   execution owner to `supervisor`, clear the block, and let S continue the\n   same task.\n\nUse `python scripts/psc_runtime.py resolve-retry-exhaustion --project <project>\n--decision <reset-and-continue-executor|switch-to-supervisor>` only after the\nuser explicitly chooses.'''
    new = '''2. `switch-to-supervisor-for-current-task`: preserve the exhausted E round and\n   both retry counters as history, set execution owner to `supervisor`, and\n   persist `scoped_supervisor_takeover` for this Task only. S completes and\n   verifies this same Task. After the Task reaches its terminal pass boundary\n   and workflow state advances, automatically run\n   `python scripts/psc_runtime.py finish-scoped-supervisor-takeover --project <project> --task <T-###>`\n   without another user decision. This returns construction ownership to E.\n   The completed Task's exhausted E counters are not reset; the next Task has\n   its own independent retry key and therefore naturally starts with fresh E\n   budgets.\n3. `switch-to-supervisor`: preserve both E budgets and atomically set execution\n   owner to `supervisor` with sticky scope, so S owns this and subsequent Tasks\n   until a later explicit handoff.\n\nUse `python scripts/psc_runtime.py resolve-retry-exhaustion --project <project>\n--decision <reset-and-continue-executor|switch-to-supervisor-for-current-task|switch-to-supervisor>`\nonly after the user explicitly chooses.'''
    skill = must_replace(skill, old, new, "skill choices")
    skill = must_replace(
        skill,
        'states without the field default to `executor`. The selected owner applies to\nthe current task and remains sticky for subsequent tasks until explicitly\nchanged.',
        'states without the field default to `executor`. Ordinary owner selection is\nsticky across subsequent tasks. The retry-exhaustion decision\n`switch-to-supervisor-for-current-task` is the deliberate exception: it persists\n`scoped_supervisor_takeover` for exactly the blocked Task and carries an\nautomatic `return_owner=executor` instruction for the next Task boundary.',
        "skill owner scope",
    )
    skill = must_replace(
        skill,
        'it must still obey the immutable Contract, Allowed/Forbidden Scope, perform\nindependent verification, and write the same review/result evidence expected\nfrom the normal workflow. It must not call `psc_invoke_executor` until an\nexplicit handoff changes the owner back to `executor`. Returning ownership to E through an ordinary handoff does not\nreset either E retry budget.',
        'it must still obey the immutable Contract, Allowed/Forbidden Scope, perform\nindependent verification, and write the same review/result evidence expected\nfrom the normal workflow. It must not call `psc_invoke_executor` for the scoped\ncurrent Task. For `scoped_supervisor_takeover`, once S completes that Task and\nadvances workflow state to the next Task boundary, it must immediately run\n`finish-scoped-supervisor-takeover` without asking the user again; MCP also\nrestores E automatically if the next Task is dispatched after state has already\nadvanced. A sticky `switch-to-supervisor` still requires an explicit later\nhandoff before E may resume. Returning ownership to E through an ordinary or\nscoped handoff does not reset the completed Task's E retry budget.',
        "skill handback",
    )
    save("SKILL.md", skill)

    protocol = load("references/runtime-protocol.md")
    protocol = must_replace(protocol, "Contract version, Task ID, budget type, usage, limit, and the two permitted user\ndecisions", "Contract version, Task ID, budget type, usage, limit, and the three permitted user\ndecisions", "protocol choice count")
    protocol = must_replace(protocol, "The only two atomic resolutions are:", "The three atomic resolutions are:", "protocol resolution count")
    protocol = must_replace(
        protocol,
        '- `switch-to-supervisor`: preserve both E budgets, set owner to `supervisor`,\n  clear the marker, and return the same task to `ready`.',
        '- `switch-to-supervisor-for-current-task`: preserve the exhausted E round and\n  both counters, set owner to `supervisor`, clear the retry block, and persist a\n  scoped takeover marker for exactly this Task. After S completes the Task and\n  workflow state reaches the next Task boundary, run\n  `finish-scoped-supervisor-takeover`; ownership returns to E automatically.\n  No retry counter is reset. The next Task is fresh because retry state is keyed\n  independently by `vN:T-###`.\n- `switch-to-supervisor`: preserve both E budgets, set owner to `supervisor`\n  with sticky scope, clear the marker, and return the same task to `ready`.',
        "protocol choices",
    )
    protocol = must_replace(
        protocol,
        'Execution ownership is sticky across task boundaries until explicitly changed.\nThe default is `executor`. Persist every change through\n`set-execution-owner`; record owner, previous owner, reason, current task, and\ntimestamp in workflow state/history.',
        'Execution ownership is normally sticky across task boundaries until explicitly\nchanged. The default is `executor`. Persist every change through\n`set-execution-owner`; record owner, previous owner, reason, current task, and\ntimestamp in workflow state/history. A retry-exhaustion\n`switch-to-supervisor-for-current-task` decision is the sole scoped exception:\npersist `scoped_supervisor_takeover` with the exact Contract/Task and\n`return_owner=executor`, then return ownership automatically at the next Task\nboundary.',
        "protocol owner scope",
    )
    save("references/runtime-protocol.md", protocol)


def patch_tests() -> None:
    path = "tests/test_runtime_hardening.py"
    s = load(path)
    marker = "def test_scoped_supervisor_retry_resolution_preserves_budget_and_returns_to_e"
    if marker not in s:
        s += r'''


def test_scoped_supervisor_retry_resolution_preserves_budget_and_returns_to_e(helper, tmp_path):
    project = tmp_path / 'project'
    state_path = _write_retry_exhaustion_fixture(project, task='T-001', version=5, budget='quality_rework')
    attempts_path = project / 'runtime' / 'executor_attempts.json'
    original = {
        'schema_version': 2,
        'tasks': {
            'v5:T-001': {'execution_round': 3, 'initial_attempted': True, 'quality_retries_used': 3, 'abnormal_retries_used': 2},
            'v5:T-002': {'execution_round': 1, 'initial_attempted': False, 'quality_retries_used': 0, 'abnormal_retries_used': 0},
        },
        'legacy_unclassified_attempts': {},
    }
    attempts_path.write_text(json.dumps(original), encoding='utf-8')

    result = helper.resolve_retry_exhaustion(project, 'switch-to-supervisor-for-current-task')
    assert result['execution_owner'] == 'supervisor'
    assert result['execution_owner_scope'] == 'current_task'
    assert result['return_owner_after_task'] == 'executor'
    assert result['execution_round'] is None
    assert json.loads(attempts_path.read_text(encoding='utf-8')) == original

    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['scoped_supervisor_takeover']['task'] == 'T-001'
    state['status'] = 'ready'
    state['last_completed_task'] = 'T-001'
    state['current_task'] = 'T-002'
    state_path.write_text(json.dumps(state), encoding='utf-8')

    finished = helper.finish_scoped_supervisor_takeover(project, 'T-001')
    assert finished['execution_owner'] == 'executor'
    assert finished['retry_counters_changed'] is False
    assert finished['execution_round_changed'] is False
    assert json.loads(attempts_path.read_text(encoding='utf-8')) == original
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['execution_owner'] == 'executor'
    assert 'scoped_supervisor_takeover' not in state
'''
        save(path, s)

    path = "tests/test_executor_mcp.py"
    s = load(path)
    marker = "def test_retry_exhaustion_offers_scoped_supervisor_third_choice"
    if marker not in s:
        s += r'''


def test_retry_exhaustion_offers_scoped_supervisor_third_choice(tmp_path):
    project = tmp_path / 'project'
    task = tmp_path / 'T-020.md'
    task.write_text('# T-020\n', encoding='utf-8')
    contract = tmp_path / 'contract' / 'v6'
    contract.mkdir(parents=True)
    _write_workflow_owner(project, 'executor')
    _write_retry_state(project, 'v6:T-020', round_number=1, initial=True, quality=3, abnormal=0)
    marker = MCP._mark_retry_exhaustion_blocked(
        project, contract, task, budget='quality_rework', used=3, limit=3,
        reason='quality_rework_limit_reached',
    )
    assert marker['decision_required'] == [
        'reset-and-continue-executor',
        'switch-to-supervisor-for-current-task',
        'switch-to-supervisor',
    ]


def test_mcp_auto_returns_scoped_supervisor_ownership_at_next_task_boundary(tmp_path):
    project = tmp_path / 'project'
    runtime = project / 'runtime'
    runtime.mkdir(parents=True)
    state_path = runtime / 'workflow_state.json'
    state_path.write_text(json.dumps({
        'schema_version': 1,
        'contract_version': 6,
        'current_task': 'T-002',
        'status': 'ready',
        'last_completed_task': 'T-001',
        'execution_owner': 'supervisor',
        'execution_owner_history': [],
        'scoped_supervisor_takeover': {
            'contract_version': 6,
            'task': 'T-001',
            'scope': 'current_task',
            'return_owner': 'executor',
        },
    }), encoding='utf-8')
    next_task = tmp_path / 'T-002.md'
    next_task.write_text('# T-002\n', encoding='utf-8')
    assert MCP._restore_executor_after_scoped_supervisor_boundary(project, next_task)
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['execution_owner'] == 'executor'
    assert 'scoped_supervisor_takeover' not in state
'''
        save(path, s)


if __name__ == "__main__":
    patch_runtime()
    patch_mcp()
    patch_docs()
    patch_tests()
    print("scoped retry patch applied")
