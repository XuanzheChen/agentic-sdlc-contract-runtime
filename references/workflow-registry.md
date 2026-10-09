# PSC Workflow Registry

The registry is **derived**, not authoritative. The source of truth remains
`runtime/workflow_state.json`, `runtime/project.json`, and Executor's
`runtime/executor-progress.json`.

For a runtime root `<repository>/.agentic-sdlc/developing`, the registry lives in
`<repository>/.agentic-sdlc/.psc-index/developing/` (outside the workflow
directory) and contains:

- `active/<workflow-directory-name>.json`: registered non-terminal workflows,
  including blocked/waiting_planner; workflow active **does not mean E running**.
- `recent/<workflow-directory-name>.json`: completed/explicitly closed workflows.
- `last.json`: lightweight pointer to most recently indexed completed/closed workflow.
- `locks/`: persistent per-workflow advisory lock files for cross-process writes.

Entries identify the project, workflow status and current task, and the path to
Executor's progress snapshot. Every update is written via temporary file and
`os.replace`. Multiple workflows update independent entry files.

## Lifecycle rules

- Successful `bootstrap` and explicit new-workflow import register the
  **final** project path only after bootstrap has materialized. Temporary
  `.workflow-stage-*` directories are never indexed.
- Normal workflow-state updates, including Supervisor review and MPC
  retry/runtime-block changes, reconcile the corresponding index entry.
- `blocked` and `waiting_planner` remain registered for explicit recovery.
- `workflow_passed` or `failed` transitions move entries to `recent/`.
- `close-workflow --project ... --reason ...` creates the durable
  `runtime/workflow_closure.json` marker and unregisters the active entry.
  It cannot close during an active workflow execution or a recently live E
  heartbeat. No contracts, logs, progress or token records are deleted.
- Successfully activating a **higher-version** approved contract removes the
  explicit closure marker, registering that workflow again.

## Repair / CLI / MCP

```bash
python scripts/psc_runtime.py reconcile-registry --runtime-root <runtime_root>
python scripts/psc_runtime.py close-workflow --project <workflow_path> --reason "Archived by user"
```

For an optimistic close, supply `--expected-state-sha256`. MCP exposes
`psc_close_workflow` and `psc_reconcile_workflow_registry`.

**Index errors are observational:** failures are recorded in
`registry-errors.jsonl`, without altering Executor attempt, retry or
Supervisor transition outcomes. Run `reconcile-registry` after crash/upgrade
or when repairing missing indices; it scans all workflows intentionally, not
every GUI refresh. Index snapshots are not process liveness guarantees: check
Executor heartbeat separately. No dashboard may treat index contents as
authoritative workflow transitions.

## GUI companion

The independent Windows GUI companion is maintained separately from the Skill
in [psc-executor-monitor](https://github.com/XuanzheChen/psc-executor-monitor).
It reads `.psc-index` when present, with legacy full-directory scan fallback
only for installations not yet exposing the registry.
