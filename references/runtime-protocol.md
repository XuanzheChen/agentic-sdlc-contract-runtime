# PSC runtime protocol

## Project layout

Persistent records live below `runtime_root`, one directory per request:

```text
{project}/
  contract/v1/...
  contract/imports/<sha16>.bundle.md
  contract/imports/reports/import-<sha12>-<UTC timestamp>-<NNN>.json
  developing/tasks/T-001.md
  developing/artifacts/T-001/{plan.md,coding.md,review.md,result.md}
  review/escalation-001.md
  runtime/project.json
  runtime/workflow_state.json
  logs/executor/T-001-attempt-01.log
```

`project.json` stores `project_id`, absolute `repository`, `created_at`, and a
baseline commit when Git is available (otherwise a recorded baseline status or
tree fingerprint). `workflow_state.json` is the durable scheduler record and
must include `schema_version`, `contract_version`, `current_task`, `status`,
`attempt`, `last_completed_task`, `last_stage`, and `updated_at`.

`contract/imports/` holds provenance copies of imported `PSC-CONTRACT-BUNDLE`
files: every external Bundle is **copied** (never moved or deleted) there before
validation, and every retained copy is byte-identical to the source. Exactly one
import report JSON object is written under `contract/imports/reports/` per
attempt.

## States and transitions

Use only these states:

`initialized -> ready -> executor_running -> supervisor_review -> task_passed ->`
`ready` (next task) `-> workflow_passed`.

`supervisor_review -> retry_required -> executor_running` is the repair path.
Missing authority or a contract conflict goes to `waiting_planner`; unresolved
retry limits go to `blocked`; unsafe or unrecoverable runtime failures go to
`failed`; incompatible external changes go to `workspace_drift`. Terminal
states are `workflow_passed`, `blocked`, `failed`, and `waiting_planner` until a
new approved Contract or explicit resolution is recorded.

After every transition write state atomically (temporary file plus replace),
record an ISO-8601 timestamp, and retain command output as evidence. Never use
conversation memory as state.

## Task execution ownership and handoff

Execution ownership is sticky across task boundaries until explicitly changed.
The default is `executor`. Persist every change through
`set-execution-owner`; record owner, previous owner, reason, current task, and
timestamp in workflow state/history.

Retry budgets are scoped to a **Task execution round**, identified by its
Contract-version/Task key plus an `execution_round` counter. Every Task begins
with round 1 and fresh `quality_rework=0/3` and `abnormal_retry=0/3`
usage. Exhaustion of one Task can never consume, reset, or block another Task.

When either E retry budget for the current task is exhausted, immediately set
`workflow_state.status=blocked`, persist a `retry_exhaustion` marker containing
Contract version, Task ID, budget type, usage, limit, and the two permitted user
decisions, and stop all task scheduling. The fact that the other retry budget
still has capacity does not permit further E dispatch; exhaustion creates a
task-level user decision point.

The only two atomic resolutions are:

- `reset-and-continue-executor`: start a new execution round for the exact
  blocked `vN:T-###`. Increment `execution_round`, set
  `initial_attempted=false`, reset **both** `quality_retries_used` and
  `abnormal_retries_used` to zero, preserve every other Task's state, set owner
  to `executor`, clear the marker, and return the same Task to `ready`.
- `switch-to-supervisor`: preserve both E budgets, set owner to `supervisor`,
  clear the marker, and return the same task to `ready`.

Use `resolve-retry-exhaustion` for this decision. Generic
`set-execution-owner` must refuse to modify a workflow blocked on
`retry_exhaustion`, preventing an owner change from bypassing the required user
choice. Outside this block, normal S/E handoff remains available, including a
later user-directed handoff back to E at a task boundary. Ordinary handoff never resets retry counters. Only an explicit user decision to
continue a retry-exhausted Task with E starts a new round and refreshes both
budgets.

The MCP Executor boundary must refuse dispatch while
`execution_owner=supervisor`; this prevents conversation-only or accidental
routing from bypassing the durable owner state. Never change owner while an
execution is actively running.

## Executor prompt transport and deterministic launch failures

Executor prompt size must be independent of OS argv limits. Codex receives the
full prompt over stdin using its `exec -` sentinel. DSH receives only a short
bootstrap argv while the full prompt lives in a temporary runtime-owned UTF-8
workspace file. Delete any such transport file before the post-execution Git
snapshot so it cannot be reported as a product change.

Classify Windows `WinError 206` and equivalent `ENAMETOOLONG` process-launch
failures as `launch_transport_failed` with `retryable=false`. E did not run,
so this failure consumes neither quality-rework nor abnormal-retry budget and
does not consume the round's initial attempt. Immediately set
`workflow_state.status=blocked` with a `runtime_failure` marker; MCP must
refuse another dispatch of that task while the marker is active.

After repairing the Skill/adapter/runtime, clear this block only with
`resolve-runtime-failure --project <project> --reason <repair evidence>`. The
resolver restores the same task to `ready` while preserving
`execution_round`, both retry counters, and execution owner. Generic owner
handoff must not bypass an active `runtime_failure`.

## Discovery, resume, and drift

Reload runtime configuration every run. Search all immediate project directories
under `runtime_root`; match `runtime/project.json.repository` to the resolved
repository. Ignore malformed or unrelated projects but report them. One active
match resumes automatically; multiple active matches require user selection.
Before starting or retrying a task, compare the stored baseline/last verified
repository state with current HEAD, status, and task files. Record harmless drift;
for drift affecting the task or assumptions, set `workspace_drift` and stop.

## Escalation and version changes

Write `review/escalation-NNN.md` with status `waiting_planner`, Contract/task,
problem, concrete evidence, and the exact Planner decision required. Do not
attempt to infer an answer. On seeing a newer Approved Contract, stop scheduling
and apply only its declared `workflow_policy` (`restart: all`,
`restart: pending_only`, or `invalidate_from_task: T-###`). Keep prior versions.

## Bundle import

The Supervisor consumes a `PSC-CONTRACT-BUNDLE` only through the deterministic
importer (`import-bundle`, plus startup `auto-import`) in `scripts/psc_runtime.py`.
The Bundle format is documented only by `prompts/contract-export.md` (the
External Planner Contract Export Prompt); the Supervisor never parses or imports
a Bundle in any other way, and the Executor never parses, imports, or receives a
Bundle.

**Startup order** (artifact-driven): reload `.agentic-sdlc/runtime.json` ->
identify repository and workflow -> discover materialized Contracts **and
pending Bundles** -> import if needed (a user-provided Bundle path is handled
first, then the single-pending-Bundle rule) -> select the highest Approved
Contract -> load `runtime/workflow_state.json` -> inspect the repository ->
resume/start. A user-provided path is read/copied/imported before normal
Contract loading. `contract/imports/` is scanned only when no usable Approved
Contract exists (usable = passes `validate-contract` with `status: approved`); a
pending Bundle is a regular file directly under `contract/imports/` (the
`reports/` subdirectory is ignored) with no successful import report; exactly
one pending Bundle is imported, two or more require user selection and are never
guessed.

**Statuses and reports.** A terminal status is recorded per attempt in
`contract/imports/reports/import-<sha12>-<UTC timestamp>-<NNN>.json`:
`imported`, `already_imported`, `import_failed`, `version_conflict`. Report
fields: `source`, `copy_path`, `sha256`, `version`, `import_time` (ISO-8601
UTC), `outcome` (`valid` | `escalated` with reasons | `failed` with errors),
`materialized_path` (or `null`), `warnings`, `status`. Reports plus `contract/`
are the sole basis for restart idempotency: the same SHA-256 for the same
declared version is `already_imported`, while a different SHA-256 for an
existing version is `version_conflict` and leaves `vN` byte-identical.

**Failure and escalation semantics.** Mechanical errors (malformed Bundle,
invalid metadata, manifest-metadata disagreement, stable-ID/reference/
dependency violations) are `import_failed`: no `contract/vN`, no
`workflow_state.json` change, no `waiting_planner`. Semantic incompleteness or
contradiction (uncovered Requirement, Acceptance with no implementing task, a
task missing `Dependencies:`/`Allowed Scope:`/`Implementation Notes:`/
`Required Verification:`, or a blocking `UNRESOLVED` in an *approved* Bundle)
materializes the artifact as immutable evidence and writes
`review/escalation-NNN.md` plus `workflow_state.status = "waiting_planner"`; the
import itself exits 0. A draft Bundle materializes but never schedules:
`workflow_state.status = "waiting_planner"` with hold reason "draft awaiting
approval", plus an escalation file when it contains `UNRESOLVED`. The importer
never guesses, repairs, or promotes a draft; later approval goes through the
normal Contract-version/approval mechanism.

**Workflow selection and bootstrap.** One repository may own multiple
independent workflows, each with its own Contract namespace and runtime state.
`--project-id` is selection-only: it must resolve to an existing workflow
associated with the repository, otherwise the importer fails with
`project_id_not_found` and creates nothing. `--new-project-id` is the explicit
new-workflow operation, is mutually exclusive with `--project-id`, must not
name an existing workflow, and applies the configured `project_naming` rule.
New workflow imports are built below `runtime_root` in an undiscoverable staging
directory and atomically renamed into place only after validation and state
initialization; failed imports remove the stage. With no existing workflow, an
unselected import retains the compatibility bootstrap using
`metadata.project_name` or the repository directory name. Each workflow may
therefore independently contain `contract/v1`; version conflicts are checked
only within the selected workflow.

**Version changes.** Importing a newer approved version only materializes it and
records provenance. It must not update the existing effective
`runtime/workflow_state.json.contract_version`. The Supervisor detects highest
Approved > effective, then runs deterministic `activate-contract`: validate,
apply exactly one declared `workflow_policy` strategy, rebuild
`developing/tasks/` from the new `tasks.md`, preserve artifacts, and only then
write the new effective version. Bootstrap of the first Approved Contract is the
sole import-time exception. Prior `contract/vN/` versions are never modified or
renumbered.

## Executor health

First runtime initialization is an explicit user wizard and does not infer Executor values from the Supervisor session. Executor static validation plus a real same-adapter smoke invocation are required before Ready. The smoke uses a temporary workspace, checks a marker file independently, stores a secret-free `executor-smoke.json`, and is invalidated by any changed adapter, executable, home, provider, model, effort, approval policy, reviewer, or sandbox. Normal dispatch reloads configuration and refuses a missing or stale smoke result; it never falls back to the Supervisor.

## Artifact ownership

Executor owns the semantic content of `plan.md` and `coding.md` and returns it
as a structured completion object. The invocation layer persists that exact
content under the current task artifact directory and retains a raw executor
log; the Executor does not directly write the runtime root. Supervisor owns
`review.md`, `result.md`, state, escalations, and verification evidence.
`result.md` is written only at a terminal task state and lists Contract/Task
IDs, acceptance outcomes, attempts, tests, and modified files.
