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
  runtime/executor_token_usage.jsonl
  runtime/executor_token_usage_summary.json
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

## Harness-neutral Preflight performance

Use a deterministic empty PC cwd, with a separate ephemeral schema/patch
directory for every check. A contaminated cwd is UNKNOWN and blocks E;
tokens spent by a completed model call must still be recorded. Place static
admission rules and frozen Contract constraints ahead of dynamic identities,
evidence and runtime facts. Use deterministic native source ranking and
complete-line bounded excerpts; metadata-only omitted candidates are also
bound by SHA. Never extrapolate a missing implementation from a truncation.
This is a per-attempt model check (never replay cached ALLOW). Provider
token limits and reasoning settings are not modified. Prompt structural
metrics are diagnostic proxies, not provider cache measurements. See
preflight-performance.md.

## Preflight persistence and admission publication

Only the native, finalized admission result with matching evidence may launch
Executor. A per-run Checker report and independent provider ledger are
unfinalized until latest publication succeeds. Persisted Checker ALLOW is not
runtime ALLOW. A stale latest.json with a different run_id does not authorize
the newest attempt. Native Supervisor snapshots expose preflight_publication
to correlate these sources. Runtime-ledger recovery facts are bound into the
Preflight prompt and revalidated. Windows atomic publishing fails closed with
detailed attempts and optional ReplaceFileW (existing destinations only).
See preflight-persistence.md for failure handling, environment limitations,
and rollback.

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

## Supervisor two-level repair decision

The Supervisor autonomously classifies each verification finding **before**
committing `quality_rework` or starting another Executor attempt. Exactly two
repair levels exist:

| Level | Finding | Authorized repair path |
| --- | --- | --- |
| S0 | Non-semantic housekeeping of permitted auxiliary/scratch artifacts, such as an exact move of a misfiled transient script with original bytes and audit evidence preserved | Supervisor directly fixes and verifies locally; no E dispatch, Preflight, smoke, owner change, or E retry charge |
| S1 | Any product-code micro-fix, test/config/product-doc semantic change, substantive deliverable correction, or normal development rework | Executor through native MCP and quality-rework review; Supervisor does not implement S1 in an Executor-owned workflow |

Supervisor alone judges the level. Unclear or mixed issues default to S1,
not a convenient S0 label. An item is **not** S0 merely because it is trivial,
under `temp/`, untracked, or can be fixed by moving a file. Confirm Contract
Allowed/Forbidden Scope, repository policy and the exact paths/byte hashes;
do not rewrite original proof, freeze-controlled evidence, source, tests,
authoritative Contract/Task, or runtime state. Preserve original logs, receipts,
and provenance, then put the S0 correction and targeted independent checks
into the normal Supervisor Review. S0 may proceed with
`execution_owner=executor` only because it is **not** product implementation.
Acceptance and PASS still require the complete task evidence and the official
native `psc_commit_supervisor_transition` (with a fresh state SHA).

S0 is **never** concurrent with running/possibly running E: a cancelled or
disconnected MCP invocation is not proof of subprocess termination. Reconcile
the official attempt, process and lock status first. Do not edit while native
state is blocked or in partial/unknown mutation. A successful S0 needs no
quality rework and never changes E counters; if it fails verification or needs
semantic work, classify S1 and use the normal rejection/Executor route.

Existing user-authorized `execution_owner=supervisor` takeover is separate
from S0/S1 triage; no discretionary tier classification changes ownership or
authorizes S1 implementation in an Executor-owned workflow. Retry exhaustion
continues to require the existing explicit user decision, without auto-takeover
or budget reset. There is no S2 classification.

## Native recovery of missing retry-exhaustion marker

An Executor's real quality/abnormal retry counters in
runtime/executor_attempts.json are the authority; State retry_exhaustion is a
derived user decision marker, never an extra retry allowance. The last quality
rework may execute successfully but fail Supervisor acceptance: in that case
Supervisor native quality_rework or blocked transition publishes status=blocked
and the exhausted-budget marker in the same workflow State commit. This avoids
having to dispatch a fourth Executor solely to create the marker.

For legacy blocked State where retry_exhaustion is absent, call
psc_recover_retry_exhaustion_marker with project, current workflow State SHA
and optionally the current attempts ledger SHA. Equivalent deterministic CLI:

python scripts/psc_runtime.py recover-retry-exhaustion-marker --project <workflow> --expected-state-sha256 <sha256> --expected-ledger-sha256 <sha256>

The runtime holds executor-dispatch.lock, validates State CAS, owner=executor,
status=blocked, allowed blocked-stage provenance, no competing runtime_failure,
active Approved Contract and current uncompleted Task, exact versioned attempt
record, nonzero genuine initial execution and exhausted retry limit. Pending-only
Contract continuity must also verify. It atomically commits only the marker,
with source State and ledger hashes. No retry counters, execution round, owner,
Review, Preflight, Contract or product files are touched.

Next obtain a **new** native Supervisor Snapshot and explicitly select
resolve-retry-exhaustion --project <workflow> --decision
switch-to-supervisor-for-current-task (or another permitted choice).
The scoped option keeps the exhausted Executor round intact and returns owner
to E at the next Task boundary only. If ledger/State drifts between recovery
and decision, the recovery-derived marker is rejected. Repeating recovery
with stale State SHA fails closed. Never use set-execution-owner to bypass an
exhausted ledger without a marker; generic ownership handoff is blocked.

## Task execution ownership and handoff

Execution ownership is normally sticky across task boundaries until explicitly
changed. The default is `executor`. Persist every change through
`set-execution-owner`; record owner, previous owner, reason, current task, and
timestamp in workflow state/history. A retry-exhaustion
`switch-to-supervisor-for-current-task` decision is the sole scoped exception:
persist `scoped_supervisor_takeover` with the exact Contract/Task and
`return_owner=executor`, then return ownership automatically at the next Task
boundary.

Retry budgets are scoped to a **Task execution round**, identified by its
Contract-version/Task key plus an `execution_round` counter. Every Task begins
with round 1 and fresh `quality_rework=0/3` and `abnormal_retry=0/3`
usage. Exhaustion of one Task can never consume, reset, or block another Task.

When either E retry budget for the current task is exhausted, immediately set
`workflow_state.status=blocked`, persist a `retry_exhaustion` marker containing
Contract version, Task ID, budget type, usage, limit, and the three permitted user
decisions, and stop all task scheduling. The fact that the other retry budget
still has capacity does not permit further E dispatch; exhaustion creates a
task-level user decision point.

The three atomic resolutions are:

- `reset-and-continue-executor`: start a new execution round for the exact
  blocked `vN:T-###`. Increment `execution_round`, set
  `initial_attempted=false`, reset **both** `quality_retries_used` and
  `abnormal_retries_used` to zero, preserve every other Task's state, set owner
  to `executor`, clear the marker, and return the same Task to `ready`.
- `switch-to-supervisor-for-current-task`: preserve the exhausted E round and
  both counters, set owner to `supervisor`, clear the retry block, and persist a
  scoped takeover marker for exactly this Task. After S completes the Task and
  workflow state reaches the next Task boundary, run
  `finish-scoped-supervisor-takeover`; ownership returns to E automatically.
  No retry counter is reset. The next Task is fresh because retry state is keyed
  independently by `vN:T-###`.
- `switch-to-supervisor`: preserve both E budgets, set owner to `supervisor`
  with sticky scope, clear the marker, and return the same task to `ready`.

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

### Codex semantic smoke fingerprint

When `config_source=executor_home`, Codex `config.toml` is fingerprinted by
parsed, canonical TOML semantics rather than raw file bytes. Codex may persist
project-trust bookkeeping while PSC runs an isolated smoke. Exclude only
`projects` entries whose path is a sibling of the product repository and whose
basename exactly matches `psc-executor-smoke-<32 lowercase hex>`.

Every other `projects` entry and every other Codex config value remains part
of the fingerprint. Therefore model/provider/reasoning/sandbox changes and
trust changes for real projects still invalidate a prior smoke. A stale
fingerprint must never be repaired by copying the current fingerprint into the
smoke artifact; rerun the real smoke instead.

## Deterministic Supervisor MCP operations

The direct PSC MCP namespace owns deterministic runtime operations in addition to
the blocking Executor transport. `psc_supervisor_snapshot` is the preferred
startup/resume read: it returns workflow status, effective current Task, retry
state, artifact paths, current state SHA-256, latest resume capsule, workspace
boundary drift, and compact Executor health without injecting full runtime files
into S context.

Supervisor decisions are persisted through
`psc_commit_supervisor_transition`. The caller supplies the exact Task,
decision, Supervisor review content, terminal result content when passing, and
the `expected_state_sha256` obtained from the snapshot. A state-hash mismatch
fails closed. Artifact files are staged first and `workflow_state.json` is
replaced last, making workflow state the commit point. A `pass` advances to the
next Contract Task (or `workflow_passed`), completes any current-task-only
Supervisor takeover by returning ownership to E, and writes both
`runtime/supervisor_resume.json` and `runtime/resume/T-###.json`.

Normal `psc_invoke_executor` performs Executor readiness internally. If the
stored smoke is missing or stale, the MCP process runs a real same-adapter smoke
before the Task attempt. Smoke failure is pre-dispatch and consumes no Executor
retry budget. Manual CLI smoke remains a diagnostic/recovery entrypoint, not a
normal Supervisor lifecycle step.

Supervisor review is diff-first: use changed paths and a small-context unified
diff before opening whole files; run the Contract-required verification and
expand reads only where the evidence requires it. Already injected Skill text and
large reference documents must not be redundantly dumped into the conversation.

## Pending-only task execution continuity

\`activate_contract\` stages a per-task pending-only retry inheritance
transaction in the authoritative Executor attempt ledger before changing
workflow_state.contract_version. The same OS-level lock serializes Contract
activation, official \`psc_recover_task_continuity\` recovery, E dispatch and
explicit retry-exhaustion reset. Source retry entries are immutable, and target
records bind original source state, exact task identity and Contract/task SHA
in the \`continuity\` map. Incomplete or drifted provenance blocks the
native Snapshot, manual Preflight and E dispatch before paid model calls.
A previously attempted task cannot be dispatched as initial after upgrade;
legal remaining retry budgets and later explicit user resets retain independent
accounting. \`restart: all\` and other strategies remain unchanged.
Current-vN PC authorization is always required. See task-execution-continuity.md.

## Supervisor transition error recovery

All registered MCP Python tool-body exceptions are translated to structured error IDs and persisted traceback logs. Supervisor commits are serialized, stage artifacts first, write a write-ahead journal under runtime/supervisor-transactions, and replace workflow State last. On failure, do not assume rollback of Review/Result/Resume artifacts. Read psc_transition_diagnostics and a fresh psc_supervisor_snapshot before recovery; never directly change State or charge Executor retries. See mcp-diagnostics.md.

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

## Executor process environment isolation

Supervisor and Executor process identity are separate even when the operating
system child process starts from a copy of the parent environment. Before every
smoke or normal Executor launch, preserve ordinary launch infrastructure such as
`PATH`, temp-directory variables, and proxies, but remove Supervisor credential
and Codex-session overrides: at minimum `OPENAI_API_KEY`, `CODEX_API_KEY`,
`CODEX_CI`, `CODEX_SESSION_ID`, and `CODEX_THREAD_ID`. Then set only the
configured independent `CODEX_HOME` or `DSH_HOME` for the selected adapter.

For Codex this guarantees that credentials under the configured Executor home
(for example `auth.json`) are authoritative and cannot be silently shadowed by a
Supervisor `OPENAI_API_KEY`. Sanitization applies identically to semantic smoke
and real task invocation because both use the same invocation boundary.

## Executor health

First runtime initialization is an explicit user wizard and does not infer Executor values from the Supervisor session. Executor static validation plus a real same-adapter smoke invocation are required before Ready. The smoke uses a temporary workspace, checks a marker file independently, stores a secret-free `executor-smoke.json`, and is invalidated by security/behavior-significant Executor configuration changes. Normal MCP dispatch reloads configuration and automatically refreshes a missing or stale smoke inside the MCP runtime before the real Task attempt; smoke failure fails closed and never falls back to the Supervisor.

## Executor token accounting

PSC records only Executor usage; Supervisor usage is intentionally out of
scope. Each real E invocation reads provider/harness telemetry and appends one
immutable row to `runtime/executor_token_usage.jsonl`, including Contract
version, Task ID, execution round, retry kind, outcome, log path, and normalized
usage.

Normalized fields are `input_tokens`, `uncached_input_tokens`,
`cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, and `total_tokens`. Reasoning is a subset of
output and is never added twice. For Codex, `input_tokens` is the provider's
total prompt input and cache buckets are subsets; for DSH, its disjoint
uncached/cache-read/cache-write buckets are normalized into the same total-input
field.

`runtime/executor_token_usage_summary.json` projects the ledger separately per
Contract `vN`. Every MCP invocation result exposes both the current invocation
usage and current `vN` cumulative total. Supervisor must display the complete
normalized breakdown after every E invocation and again for the current
Contract at `workflow_passed`: `input_tokens`,
`uncached_input_tokens`, `cached_input_tokens`,
`cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, and `total_tokens`. A total-only report is not
compliant. Reasoning is a subset of output and is informational rather than an
additional term in `total_tokens`. Missing provider telemetry is never
converted to zero: the aggregate becomes `exact=false` and tracks
`inexact_invocations` / `unavailable_invocations`.

Codex usage comes from `codex exec --json` `turn.completed.usage`. DSH usage
comes from newly persisted session logs under the configured
`executor_home/sessions`, applying DSH's same-attempt replacement semantics,
provider retry boundaries, compaction usage, and all newly created child
sessions. PSC disables DSH's automatic model-backed session-title plugin for
disposable E runs because that auxiliary request does not expose durable usage
and is not required for development.

## Artifact ownership

Executor owns the semantic content of `plan.md` and `coding.md` and returns it
as a structured completion object. The invocation layer persists that exact
content under the current task artifact directory and retains a raw executor
log; the Executor does not directly write the runtime root. Supervisor owns
`review.md`, `result.md`, state, escalations, and verification evidence.
`result.md` is written only at a terminal task state and lists Contract/Task
IDs, acceptance outcomes, attempts, tests, and modified files.

## Preflight Checker gate

Every Executor attempt dispatched through the PSC MCP runtime is preceded by a
read-only Preflight Check (PC). The gate lives inside `psc_invoke_executor`
itself, so a Supervisor cannot reach E by skipping a separate
`psc_preflight_check` call. `psc_preflight_check` is an optional companion tool
for explicit repair-and-recheck and for inspecting a blocked dispatch; it never
launches E.

Order of operations for one dispatch:

```text
path arguments -> ownership -> runtime block -> retry budget classification
-> Preflight Check (PC) -> Executor attempt -> usage/retry accounting
```

PC returns a structured report with `decision` `ALLOW` or `DENY`, non-empty
`summary`, and `findings` carrying `evidence` and a `resolution_owner`
(`runtime`, `supervisor`, or `planner`). An `ALLOW` must not contain blocking
findings; a `DENY` must contain at least one. Everything else -- invalid JSON,
schema violation, non-zero exit, timeout, spawn failure, unverifiable DSH
restrictions, or stale evidence -- is `UNKNOWN`.

Runtime enforcement:

- `ALLOW` proceeds to E and the runtime injects the compact verified facts into
  the E prompt (`## Verified Preflight Facts`). Those facts are authoritative for
  the attempt unless a listed hash no longer matches.
- `DENY` returns `preflight_denied` and launches no Executor.
- `UNKNOWN` returns `preflight_unknown` and launches no Executor.
- For a finalized, ledger-matched DENY with a blocking Supervisor-owned
  missing-context finding, S may invoke the native
  `psc_submit_preflight_evidence` interface. It accepts an existing,
  task/Contract/review-referenced, repository-relative UTF-8 file (not an
  approval created by S), exact finding ID and current State SHA. The
  append-only `runtime/preflight/submitted_evidence/vN/T-###/` receipt is
  validated and its bytes are SHA-bound on each check. It never rewrites the
  prior report, State, Contract, approval, Executor attempt or usage ledger.
  After submission S must run a **fresh** `psc_preflight_check`; only that
  finalized decision has current standing, and normal dispatch checks again.
  Changed files, corrupt receipts and omitted submitted evidence fail closed.
  Runtime/Planner-owned findings, missing external permission and S1 product
  defects cannot be repaired by inventing an evidence receipt.
- Both blocked results are `retryable=false`, set `workflow_status=blocked`, and
  charge **no** Executor retry budget. Supervisor repairs the reported finding
  and dispatches again; the next dispatch re-runs PC with fresh hashes.
- If the runtime configuration cannot be loaded at all, the check is recorded as
  `not_evaluated`; E still cannot launch because readiness/configuration checks
  fail closed first.

### Proactive Supervisor evidence selection

Before the first PC check, S may inspect/replace the Task-scoped PC candidate
list via `psc_get_preflight_evidence_selection` and
`psc_set_preflight_evidence_selection`. The setter receives ordered
`include_paths`, optional `exclude_paths` (only optional configured or
historically submitted candidates), a verbatim Task/Contract excerpt
`task_basis`, explicit `reason`, and optimistic State/list revision SHAs.
A DENY is **not** necessary. This is a replace-all selection; omitting paths
removes them from the selection while preserving history. Priority files are
seen first under the bounded PC source budget; excluded optional sources are
disclosed as hashed metadata rather than disappearing silently.

Native runtime persists Task/Contract-SHA-bound immutable revisions under
`runtime/preflight/evidence_selection/vN/T-###/` and atomically moves only
the latest selection pointer. PC re-hashes both included and excluded sources
before and after its independent model decision, and binds the revision SHA
to the admission evidence. A changed file or corrupted revision yields
UNKNOWN, never reuse of historical ALLOW. S cannot exclude a concrete Task
Allowed Scope file, change immutable Contract/Task artifacts, create
approvals, modify a PC verdict or bypass PC. Missing external scientific
authorization remains a blocking fact regardless of evidence-list changes.

PC has independent accounting. Checker token usage and elapsed time are written
only to `runtime/preflight_token_usage.jsonl` and
`runtime/preflight_token_usage_summary.json`, and the decision report stays under
`runtime/preflight/` in the PSC project.
