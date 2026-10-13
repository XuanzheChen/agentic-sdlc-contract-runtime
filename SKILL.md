---
name: agentic-sdlc-contract-runtime
description: Run or author portable Contract-Driven Agentic SDLC (PSC) workflows from filesystem artifacts, with an independent Supervisor, disposable Executor adapters, immutable versioned Contracts, resumable state, retries, escalation, and evidence-based verification.
---

# Agentic SDLC Contract-Driven Runtime (PSC)

Use this skill for the PSC runtime or when the user explicitly asks to author a
PSC Contract. It is not a replacement for ordinary small coding work. The
runtime is deliberately artifact-first: Contract files, runtime state,
repository state, task artifacts, and verification evidence are the only durable
sources of truth. Never recover requirements from a previous conversation.

## Task-level Executor execution memory

The Executor remains a disposable independent process, but native invocation
archives a bounded, non-authoritative technical handoff for each launched
attempt. Same-task quality/abnormal retries receive up to four earlier compatible
attempt records, filtered by Task identity, Contract version, task text SHA and
Contract packet SHA. Their changed-file hashes are rechecked and any drift is
labeled stale. Do not rerun previously rejected approaches blindly or treat
past E claims as accepted work. The current Contract, fresh Preflight facts and
latest Supervisor Review always outrank memory. Old Contract-version memory
remains archived but cannot silently authorize or direct new-version work.

The optional Completion `handoff` contains implementation_state, decisions,
failed_approaches and next_steps; old Completion responses remain valid.
Missing memory must not block normal initial attempts, while corrupt memory
fails closed on retries. Archive persistence errors after a launched call
must preserve true usage/budget charging and cannot be reported as success.
Never copy raw transcripts, secrets or tool output into memory; no dedicated
LLM summarizer or shared Harness session is used.
See references/executor-adapters.md for the durable record protocol.

### Executor MCP progress (observational only)

`psc_invoke_executor` blocks through the entire Executor attempt even when its
DSH/Codex JSONL stream is projected into request-scoped MCP progress. Progress
events are only human observability: they MUST NOT change task ownership,
retry accounting, scope validation, usage accounting, or the final Supervisor
review. Do not feed progress back to the Supervisor via repeated polling turns,
and never forward thinking/tool-result contents into progress messages.

For UI-only diagnostics, `psc_progress_probe` emits five notifications over
20 seconds without launching E or mutating PSC workflow state. MCP clients may
accept notifications without rendering them in Desktop/TUI. For real attempts,
the fallback observer artifact is
`<project>/runtime/executor-progress.json` (and
`<project>/runtime/executor-progress/<run_id>.jsonl`). It is not a source of
truth for attempt completion; the final invoke result remains authoritative.



## Harness-neutral PC performance and cache semantics

PC must still run a fresh model check before every E attempt. It uses a
stable, empty, outside-repository current directory for cache-friendly
system-message prefixes; per-attempt schemas/patches are temporary and
independent. Never write files into the shared PC cwd. Contamination
must fail closed, while any already consumed model tokens remain charged
to the separate PC ledger. Universal rules and response schema, followed
by frozen Contract constraints, precede Task and dynamic SHA/review/ledger
facts. Repository file excerpts are deterministically prioritized and
bounded; missing excerpts cannot be treated as absent implementations.
Metadata-only omitted candidate files remain SHA-bound and must be
requested via Planner/Supervisor when essential to safe admission.
Provider/model/Effort remain exactly E's; do not add adapter-specific
max-output or reasoning overrides. See references/preflight-performance.md.

## Preflight publication failures (native runtime authority)

PC model output is not Executor admission. Use the current native result fields
checker_decision, checker_status, admission_status, admission_finalized, and
blocks_executor. Independent provider usage and a per-run Checker report are
unfinalized evidence before latest publication. Do not treat historical
ALLOW rows, a stale runtime/preflight/latest.json, or a past review as permission
to call E. If any key persistence phase fails, keep UNKNOWN and stop.
psc_supervisor_snapshot includes preflight_publication, which correlates the
latest provider run ID with the published admission. Do not infer success from
a mismatched run ID or the cached ALLOW counter. The checker gets fresh,
SHA-bound native ledger/recovery facts; outdated review claims cannot override
those specific verified facts, and none of them grant physical permissions.
For diagnosis, recovery, Windows limits and rollback see
references/preflight-persistence.md. Never auto-repair research workspace ACLs.

## First decision: select a mode from disk

On every activation, reload `.agentic-sdlc/runtime.json` (never cache it), locate
the current repository, and inspect the configured runtime root.

Startup is artifact-driven and import-aware, in this order: reload
`.agentic-sdlc/runtime.json` -> identify repository and workflow -> discover
materialized Contracts **and pending Bundles** -> import if needed (a
user-provided Bundle path first, then the single-pending-Bundle rule below) ->
select the highest Approved Contract -> load `runtime/workflow_state.json` ->
inspect the repository -> resume/start. If the user provides a
`PSC-CONTRACT-BUNDLE` file path, read/copy/import it **before** any normal
Contract loading. When no user path is given and no usable Approved Contract
exists, check `contract/imports/`: exactly one pending Bundle is imported
automatically; two or more candidates require user selection and are never
guessed.

Then select the mode:

1. If exactly one active project for this repository has a non-terminal
   `runtime/workflow_state.json`, enter Supervisor resume mode.
2. If several are active, ask the user to select a project directory; do not
   guess.
3. If no project is active but an associated project contains a highest-version
   `contract/vN/metadata.json` with `status: approved`, enter Supervisor
   bootstrap mode.
4. Enter Planner/Contract Author mode only when explicitly requested (for
   example, "create a project contract", "plan this requirement", or "act as
   Planner").
5. Otherwise do not code. Report that no valid Approved Contract was found and
   ask the user to import one (explicit Bundle path or a pending Bundle) or
   explicitly request Planner mode.

An explicitly supplied workflow directory may be used after the same validation.
Planner and Supervisor may be different sessions, applications, models, or
machines; Supervisor must work with zero Planner conversation context.

## Supervisor operating rules

The Supervisor is the runtime owner. At startup and before each task, load from
disk, in order: `runtime.json`, `runtime/project.json`, the highest approved
Contract, `runtime/workflow_state.json`, the current task, any prior review, and
repository state. Validate the Contract before dispatching an Executor. If a
Bundle needs importing (user-provided path, or a single pending Bundle when no
usable Approved Contract exists), run the import before selecting the highest
Approved Contract and before loading workflow state. Read
[`references/contract-schema.md`](references/contract-schema.md) for structural,
status, repository, ID, dependency, feasibility, and security checks.

Contracts are immutable. Use the highest Contract version whose metadata status
is `approved`; never use drafts and never overwrite a version. If a newer
approved version appears, stop scheduling and apply its explicit
`workflow_policy.restart`, `pending_only`, or `invalidate_from_task` policy.
Do not invent invalidation behavior or silently repair Contract semantics.

The Supervisor must independently inspect diffs, status, files, and important
test/build/lint/type-check output. Executor self-report is evidence, not proof.
Write `review.md` and, only for a terminal task state, `result.md`; preserve
attempts and state after every meaningful transition.

## Supervisor repair triage: S0 versus S1

Before declaring `quality_rework`, writing a rejection that requires another
Executor attempt, or launching `psc_invoke_executor`, the Supervisor **MUST**
classify each remaining finding as exactly one of two levels. Classification
is S's own judgment; routine S0 classification and repair do not require
asking the user to choose a tier.

- **S0 — organizational/housekeeping repair.** A narrowly identified,
  non-semantic correction such as relocating misfiled transient verification
  scripts, tidying permitted scratch paths, or fixing presentation/filing of
  non-authoritative auxiliary evidence. S **MAY and SHOULD fix S0 directly**,
  even with `execution_owner=executor`, without an Executor/Preflight/smoke
  call or an owner handoff. Confirm exact source/destination, authorization,
  unchanged bytes/hashes where applicable, and absence of unintended changes.
  Preserve historical evidence, original logs and truthful provenance; record
  the action, before/after paths and verification in the Supervisor Review.
  Only commit native `pass` when the entire Task already meets its Contract
  acceptance and all required independent verification passes. S0 is not a
  substitute for unfinished task implementation.
- **S1 — product micro-fix or normal development rework.** **Any** change to
  executable product code, tests, production configuration, product
  documentation/semantics, algorithms, APIs, outputs, acceptance behavior, or
  substantive deliverables is S1, however short or obvious. Any unresolved
  implementation or verification deficiency also remains S1. In the normal
  Executor-owned workflow S **MUST NOT implement S1 itself**: commit an
  evidence-backed `quality_rework` Review and use the normal native
  `psc_invoke_executor` path and existing retry budgets. Never relabel
  a semantic edit as S0 to avoid an Executor invocation.

For ambiguous or mixed findings, classify as S1 (or escalate for Contract
ambiguity); do not split off a purported S0 to silently make semantic edits.
The S0 privilege is restricted to non-authoritative housekeeping paths
permitted by the effective Contract, repository policy and user instructions:
no modification of product files, immutable Contract, approved Task,
Executor-owned `plan.md`/`coding.md`, authoritative runtime/ledger/state,
protected/frozen scientific evidence, or forbidden scope. A file's being small,
untracked, or located under `temp/` alone does not establish S0. No broad
deletion, evidence rewriting, permission changes, or executable verification
is justified solely by S0 classification.

**Concurrency and accounting:** S0 is forbidden while an Executor attempt is
running or its cancellation/completion and dispatch lock remain uncertain.
First reconcile the actual process, official snapshot/attempt outcome and any
partial state; do not treat an interrupted MCP client call as proof E stopped.
Never modify files concurrently with E or bypass an active native block.
S0 itself consumes **zero** E quality/abnormal retries and does not reset
budgets, impersonate an E completion, or edit workflow state by hand. If
independent verification fails after S0, follow the S1/review path.

The existing **explicitly user-authorized** `execution_owner=supervisor`
task-takeover mechanism is a separate durable owner mode, not a third repair
level, not authority granted by S0/S1 classification, and not a route for S to
unilaterally perform S1 in an Executor-owned task. Never switch owners solely
because an S1 fix looks easy. See
[references/runtime-protocol.md](references/runtime-protocol.md) for the
two-level decision boundary and evidence rules.

Retry accounting is split into two independent budgets within each Task
execution round. Every new Task starts with a fresh round, and every user-authorized
continuation of the same blocked Task starts a new round. The first Executor dispatch uses `retry_kind="initial"` and consumes no
retry budget. If a completed implementation is rejected by Supervisor
verification for implementation quality, acceptance failure, incomplete work, or
another code-quality reason, retry the same task with the review as input and
`retry_kind="quality_rework"`; at most three quality rework retries are
allowed. If the Executor attempt itself fails abnormally (including timeout/no
return, process failure after launch, ordinary spawn failure, invalid Executor
completion, or artifact persistence failure), retry with
`retry_kind="abnormal_retry"`; at most three abnormal retries are allowed.
A deterministic **pre-launch transport** failure such as Windows
`[WinError 206]` / `ENAMETOOLONG` is different: it means E did not run and
repeating the same launch cannot help. The invocation layer returns
`reason=launch_transport_failed`, `retryable=false`; do not consume either
retry budget, do not mark the round's initial attempt as used, and do not retry
the same launch. Persist a `runtime_failure` block and stop scheduling until
the runtime/adapter is repaired.
These budgets are independent. If a `quality_rework` dispatch itself ends in an
Executor abnormality, that attempt consumes the abnormal-retry budget only and
does not consume a quality-rework opportunity.

After the initial attempt, never use `retry_kind="initial"` again and never
guess the retry class: classify it from the immediately preceding outcome. If either task-local retry budget is exhausted, the current task must
immediately enter `blocked` and execution must stop for a user decision. Do not
consume the other budget, continue with another Executor retry class, auto-reset
a budget, auto-switch to S, or advance to the next task. Record the exact
Contract version, Task ID, exhausted budget, usage, limit, and evidence.

Offer exactly three resolution choices for that blocked task:

1. `reset-and-continue-executor`: treat the user's decision as starting a new
   execution round for that exact `vN:T-###`. Atomically reset **both**
   `quality_rework` and `abnormal_retry` usage to zero, reset the round's
   `initial_attempted` flag, increment `execution_round`, set execution owner
   to `executor`, clear the block, and continue the same task.
2. `switch-to-supervisor-for-current-task`: preserve the exhausted E round and
   both retry counters as history, set execution owner to `supervisor`, and
   persist `scoped_supervisor_takeover` for this Task only. S completes and
   verifies this same Task. After the Task reaches its terminal pass boundary
   and workflow state advances, automatically run
   `python scripts/psc_runtime.py finish-scoped-supervisor-takeover --project <project> --task <T-###>`
   without another user decision. This returns construction ownership to E.
   The completed Task's exhausted E counters are not reset; the next Task has
   its own independent retry key and therefore naturally starts with fresh E
   budgets.
3. `switch-to-supervisor`: preserve both E budgets and atomically set execution
   owner to `supervisor` with sticky scope, so S owns this and subsequent Tasks
   until a later explicit handoff.

Use `python scripts/psc_runtime.py resolve-retry-exhaustion --project <project>
--decision <reset-and-continue-executor|switch-to-supervisor-for-current-task|switch-to-supervisor>`
only after the user explicitly chooses. Other tasks always start with their own fresh execution round and are never
affected by exhausting or restarting this task. Retry limits are therefore
guards on one Task execution round, not lifetime quotas for the Task. On missing, contradictory,
unsafe, or impossible Contract information, stop and write
`review/escalation-NNN.md`, set `workflow_state.status` to `waiting_planner`,
and wait for a new Contract version or an explicit resolution artifact.

Task execution routing is durable and task-boundary aware.
`workflow_state.execution_owner` is `executor` or `supervisor`; legacy
states without the field default to `executor`. Ordinary owner selection is
sticky across subsequent tasks. The retry-exhaustion decision
`switch-to-supervisor-for-current-task` is the deliberate exception: it persists
`scoped_supervisor_takeover` for exactly the blocked Task and carries an
automatic `return_owner=executor` instruction for the next Task boundary. Use the deterministic helper
`python scripts/psc_runtime.py set-execution-owner --project <project> --owner <executor|supervisor> --reason <reason>`
for every handoff. Outside a retry-exhaustion block, a user may instruct S to take
over the current task, all remaining tasks, or to hand work back to E after a
task boundary; persist that choice before execution. During a retry-exhaustion
block, generic owner switching is forbidden: use only the atomic
`resolve-retry-exhaustion` decision path so owner change and optional
task-local budget reset cannot diverge. Do not switch owners while an execution
is running.

Only a separate explicit user-authorized owner handoff can enable the historical
Supervisor-owned implementation mode; S0/S1 classification alone never grants
product-implementation rights. In an ordinary Executor-owned task S is limited
to direct S0 housekeeping and S1 must go to Executor.

When `execution_owner=supervisor`, S may implement product code directly, but
it must still obey the immutable Contract, Allowed/Forbidden Scope, perform
independent verification, and write the same review/result evidence expected
from the normal workflow. It must not call `psc_invoke_executor` for the scoped
current Task. For `scoped_supervisor_takeover`, once S completes that Task and
advances workflow state to the next Task boundary, it must immediately run
`finish-scoped-supervisor-takeover` without asking the user again; MCP also
restores E automatically if the next Task is dispatched after state has already
advanced. A sticky `switch-to-supervisor` still requires an explicit later
handoff before E may resume. Returning ownership to E through an ordinary or
scoped handoff does not reset the completed Task E retry budget. The only reset path is the explicit
`reset-and-continue-executor` resolution. It starts a new execution round for
the blocked Contract/Task and refreshes **both** retry budgets together.

Read [`references/runtime-protocol.md`](references/runtime-protocol.md) for the
state machine, discovery, bootstrap, resume, drift, retry, escalation, and
artifact ownership rules. Read [`references/executor-adapters.md`](references/executor-adapters.md)
when invoking or changing a harness.

## P0: Cross-Contract task execution continuity

When a new Approved Contract uses \`restart: pending_only\`, native Runtime
atomically migrates the pending Tasks' actual Executor retry states before
activation. A workflow activated under older Runtime without migration must
use the native MCP \`psc_recover_task_continuity\` interface with the current
Snapshot SHA (and preferably ledger SHA), after explicit source/target Contract
and Task lineage verification. Do not patch executor_attempts.json by hand.
\`psc_supervisor_snapshot\` reports \`task_continuity\` and verified inherited
retry counts, or blocks when provenance is missing/drifted. Native
\`psc_preflight_check\` and \`psc_invoke_executor\` enforce continuity before
PC/E calls, including retry-kind and current-task order; historical v2 PC
ALLOW or DENY never authorizes a v3 run. A v3 T-007 that inherited an
initial execution cannot use retry_kind=initial. Never re-execute previously
native-PASS tasks merely due to version upgrade.
See references/task-execution-continuity.md for formal recovery and tests.

## Recovery of missing retry-exhaustion marker (native only)

Quality retry budget reaching its limit is decided at the Supervisor's
rejection boundary. Native Supervisor transition commits the exhaustion marker
with the rejected Review, not by requiring a fourth Executor call. For older
blocked workflows that lack retry_exhaustion, do not use generic owner handoff
or manually edit workflow_state.json. Call native MCP
psc_recover_retry_exhaustion_marker(project, expected_state_sha256,
expected_ledger_sha256?) with a freshly read Supervisor State SHA and optionally
the attempts ledger SHA. It strictly verifies a blocked workflow, active
Approved Contract, current unfinished Task, executor owner and exhausted
authoritative quality/abnormal retry counter before atomically restoring only
the marker. It never resets budgets or switches owner. Re-read native Snapshot,
then make the **separate authorized decision** using
resolve-retry-exhaustion --decision switch-to-supervisor-for-current-task.
Do not use Preflight or Executor to synthesize the missing marker. On any
mismatch, stop and escalate without bypass. See
references/runtime-protocol.md for invariants and recovery sequence.

## Native MCP errors and safe Supervisor recovery

All registered Python MCP tools return structured tool_error for tool-body exceptions. Inspect error_id, error_code, failure_phase, mutation_status, state_sha_before/after, diagnostic_log_path and next_action. Supervisor transitions have durable journals under runtime/supervisor-transactions with workflow State replaced last; cross-file writes are not atomic. When failed, call psc_transition_diagnostics(project) read-only and then psc_supervisor_snapshot(project); reconcile partial/unknown writes before dispatching E. Never edit workflow_state.json as a shortcut or charge Executor retry budgets for failed Supervisor commits. MCP pre-dispatch, startup and transport failures may require stderr or client transport logs. See references/mcp-diagnostics.md.


The direct PSC MCP namespace is a path-oriented control API. Do not infer an
argument's meaning from its generic `str` type or from similarly named content
returned by another tool. When an argument below is a path, pass the filesystem
path string itself, never the file contents. Prefer canonical paths returned by
`psc_supervisor_snapshot`; do not reconstruct or re-escape Windows paths when a
returned path can be reused.

- `psc_supervisor_snapshot`
  - `project`: PSC project directory path; required.
  - `repository`, `runtime_config`, and `contract`, when supplied, are
    filesystem paths.
  - The returned `task_path` is the canonical value for
    `psc_invoke_executor.task`. The returned `task_markdown` is Supervisor
    evidence only and **must never** be passed as `task`.
  - Artifact records expose `path` plus `exists`. For a rework dispatch,
    use the existing `review.md` artifact path as `previous_review`; do not
    copy the review body into that argument.
- `psc_invoke_executor`
  - `repository`: repository directory path. `.` is valid when the MCP
    server working directory is the repository root.
  - `runtime_config`: the active PSC runtime configuration path, normally
    `.agentic-sdlc/runtime.json`. Do not substitute an experiment, smoke, or
    temporary config merely because it also contains Executor settings.
  - `project`: active PSC project directory path.
  - `task`: **existing `T-###.md` task file path**. Never pass a task ID,
    task title, task instructions, or `task_markdown`.
  - `contract`: approved `contract/vN` directory path.
  - `previous_review`: existing Supervisor `review.md` file path, or
    `null` when no prior review exists. Never pass inline review Markdown.
  - `retry_kind`: `initial`, `quality_rework`, or `abnormal_retry`
    according to the persisted retry state.
- `psc_ensure_executor_ready`
  - `repository` and `runtime_config` are filesystem paths.
- `psc_get_preflight_evidence_selection` / `psc_set_preflight_evidence_selection`
  - Task/Contract-scoped read and compare-and-set replacement of ordered
    `include_paths` and optional `exclude_paths`. The setter requires
    verbatim `task_basis`, `reason`, State SHA and prior list revision SHA.
    No prerequisite PC DENY. No direct edits to binding SHA or past reports.
- `psc_submit_preflight_evidence`
  - Requires existing `project`, `repository`, `runtime_config`, `task`
    and Approved `contract` paths; `evidence_path` is repository-relative;
    `finding_id`, `reason`, `expected_state_sha256` are required.
    `previous_review`, if supplied, is an existing Review **path**.
    Appends one provenance receipt but never writes a prior PC decision.
- `psc_preflight_check`
  - Optional companion tool. `repository`, `runtime_config`, `project`,
    `task`, `contract`, and `previous_review` are the same literal paths as
    `psc_invoke_executor`.
  - Runs the read-only Preflight Checker only. It never launches E and never
    charges an Executor retry budget. Use it to repair-and-recheck, or to
    inspect why a dispatch was blocked. Normal `psc_invoke_executor` runs the
    same check itself, so skipping this tool does not weaken the gate.
- `psc_commit_supervisor_transition`
  - `project` and `contract` are paths; `task_id` is a stable task ID
    such as `T-002`.
  - `review_markdown` and optional `result_markdown` are deliberately
    **inline Markdown content**. This is intentionally different from
    `psc_invoke_executor.previous_review`, which is a **file path**.

Before each direct Executor dispatch, verify the path/content distinction above.
In particular, a snapshot can contain both `task_path` and `task_markdown`;
only `task_path` is valid for `psc_invoke_executor.task`. If a required path
is missing, stale, or ambiguous, refresh the snapshot or inspect the artifact
instead of guessing a replacement string.

Supervisor MCP exposure is harness-specific. In a Codex Supervisor, the PSC
namespace is configured as direct-only, so the raw `psc_*` tools may be absent
from Code Mode `ALL_TOOLS` / `functions.exec` discovery even while they are
correctly exposed as top-level model MCP tools. In a DSH Supervisor, the
official MCP client registers tools in the normal Harness tool registry with
server-qualified names such as
`mcp__agentic_sdlc_executor__psc_invoke_executor`. Never treat one harness's
discovery surface as evidence about the other. Do not probe a PSC tool by
wrapping it in another execution host or by intentionally sending guessed
arguments.

## Supervisor efficiency and deterministic runtime operations

Normal Supervisor work must keep judgment in S and move deterministic artifact/state
mutation into the PSC MCP runtime. This does **not** require E dispatch for
independently verified S0 housekeeping; S1 still uses native Executor dispatch.
Use the direct MCP tools as follows:

- `psc_supervisor_snapshot` at startup/resume and task boundaries instead of
  separately dumping `workflow_state.json`, retry ledgers, task files, Contract
  metadata, and Executor health through many shell calls. The compact snapshot
  includes a workflow-state SHA-256 for optimistic concurrency.
- `psc_commit_supervisor_transition` for `quality_rework`, `pass`,
  `blocked`, or `waiting_planner`. It writes Supervisor `review.md`, terminal
  `result.md`, workflow state, scoped-owner handback, and task-boundary resume
  capsules as one deterministic transaction with workflow state committed last.
  Pass `expected_state_sha256` from the snapshot; a stale state fails closed.
- `psc_ensure_executor_ready` only when an explicit readiness check is useful.
  Normal `psc_invoke_executor` performs the same readiness check automatically
  and runs a real smoke inside the MCP runtime when the stored smoke is missing
  or stale. Do not launch smoke through a Supervisor shell during normal work.
- `psc_get_preflight_evidence_selection` / `psc_set_preflight_evidence_selection`
  to inspect and proactively replace Task-scoped selected/optional-excluded
  evidence lists, including before the very first PC. Both preserve native
  SHA/provenance and **never** confer admission.
- `psc_submit_preflight_evidence` to register a present, referenced file
  after a finalized Supervisor-owned PC DENY, then explicitly run a new PC.
  The result is a receipt, never permission or an Executor dispatch.
- `psc_preflight_check` only for an explicit repair-and-recheck or to inspect a
  blocked dispatch. `psc_invoke_executor` enforces the same read-only Preflight
  Check before every attempt, so this tool is never a required precondition and
  never a bypass.

Supervisor verification remains independent and evidence-based, but evidence
acquisition is **diff-first and targeted-read**: start from Executor
`changed_paths`, inspect `git diff --stat` and a small-context unified diff for
those paths, run the Contract-required verification, and open whole source files
only when the diff/test evidence is insufficient. Do not re-read the full
`SKILL.md` when the skill loader has already injected it, and do not dump whole
reference documents by default.

At every successful Task boundary the transition tool writes
`runtime/supervisor_resume.json` plus immutable
`runtime/resume/T-###.json`. These compact capsules are the preferred context
checkpoint for a fresh Supervisor session; conversation history is not required
to resume safely.

## Executor boundary

Normal Supervisor task dispatch is **MCP-only**. The current Supervisor harness
determines the visible tool name, but not the workflow semantics:

- Codex Supervisor: call the direct top-level `psc_invoke_executor` tool. The
  Codex configuration must include `mcp__agentic_sdlc_executor` in
  `[features.code_mode].direct_only_tool_namespaces` so the long-running call
  bypasses the Code Mode cell host.
- DSH Supervisor: call
  `mcp__agentic_sdlc_executor__psc_invoke_executor` from DSH's normal tool
  registry. The companion tools use the same prefix, for example
  `mcp__agentic_sdlc_executor__psc_supervisor_snapshot`. DSH does not use the
  Codex `direct_only_tool_namespaces` setting.

A compliant dispatch is exactly one native MCP tool call in the active
Supervisor harness. Do not wrap it in `functions.exec`, JavaScript, a code-mode
cell, `exec_command`, `pwsh`, another shell, or a subagent. Do not call
`wait`, `write_stdin`, sleep loops, or repeated model turns to poll Executor
completion. If the required PSC MCP tool is not present in the current
Supervisor tool inventory, fail closed and report
`supervisor_mcp_unavailable`; refresh/fix the Supervisor MCP registration
before dispatching E.

**No silent compatibility fallback is permitted.** In normal Supervisor mode it
is forbidden to import `scripts/psc_mcp_server.py`, call
`invoke_executor_tool` / `_invoke_executor_impl` from Python, launch
`python scripts/invoke_executor.py invoke ...`, or otherwise reproduce the MCP
operation through a terminal. The public `invoke_executor_tool` compatibility
entrypoint itself fails closed with `mcp_transport_required` so an accidental
Python-import fallback cannot execute E. The shell `invoke` and `smoke`
commands remain manual/debug/CI entrypoints only; they are never a replacement
for missing Supervisor MCP exposure.

Normal MCP dispatch automatically performs the stale/missing Executor smoke
inside the MCP runtime before E. If the MCP dependency, server registration, or
tool exposure is missing, stop before any Executor attempt and surface the
configuration problem to the user.

### Preflight Checker gate

A read-only **Preflight Checker (PC)** runs before **every** Executor attempt,
inside `psc_invoke_executor` itself. PC uses the same adapter, executable,
executor home, provider, model, and reasoning effort as E, but it is a separate
read-only invocation with its own run id, timeout, log, and token/elapsed
accounting. It never edits the Executor home or `runtime.json`.

- Codex PC: forced `--sandbox read-only --ask-for-approval never`, isolated empty
  temporary working directory outside the repository, strict report schema via
  `--output-schema`, prompt on stdin.
- DSH PC: a command-line `--patch` overlay that disables shell, filesystem
  writes, code editing, MCP client tools, external tools, and dangerous tools,
  placed before the first app-owned flag. PSC verifies the composed overlay
  (profile layer first, runtime patch last) and **fails closed** with
  `dsh_tool_restrictions_unverifiable` when the restrictions cannot be proven.
- PC never gets free filesystem access. The runtime gathers a bounded,
  task-specific evidence bundle (Task file, task-scoped Contract packet,
  previous review, Contract binding, bounded Allowed-Scope/configured files)
  with deterministic SHA-256 hashes and transports it through a runtime-owned
  prompt.

PC returns `decision` `ALLOW` or `DENY` with a non-empty `summary` and
`findings` that each carry `evidence` and a `resolution_owner` of `runtime`,
`supervisor`, or `planner`. Invalid JSON, a schema violation, a non-zero exit, a
timeout, a spawn failure, or stale evidence is `UNKNOWN`. The runtime enforces
the decision:

- `ALLOW` proceeds and the compact verified facts are injected into the E prompt
  as `## Verified Preflight Facts`. Treat them as authoritative for that attempt
  unless a listed hash no longer matches, in which case report the discrepancy
  instead of proceeding.
- `DENY` returns `preflight_denied`; `UNKNOWN` returns `preflight_unknown`.
  Neither launches E, both are `retryable=false`, and **neither charges an
  Executor retry budget**.
- Before E launches, PC evidence is re-gathered and every hash revalidated. Any
  change invalidates the decision with `preflight_evidence_stale`.

### Supervisor authority: proactive Task evidence selection

**Before any PC call**, the Supervisor **MAY and SHOULD independently choose
the evidence files relevant to the active Task**. There is no requirement to
wait for a PC DENY or an Executor failure. The Supervisor owns the *candidate
list and its order*, not the bytes of the evidence, frozen scientific
approvals, SHA computation, or PC's independent admission decision.

Use `psc_get_preflight_evidence_selection(repository,project,task,contract)`
to inspect the current selection and its `revision_sha256`. Use native
`psc_set_preflight_evidence_selection` with:
`repository`, `runtime_config`, `project`, the existing Task file path
`task`, active Approved `contract` directory, `include_paths` (an ordered
replacement list of existing repository-relative files), `exclude_paths`
(optional configured/historical candidate files to omit), a verbatim
`task_basis` excerpt from the Task or its scoped Contract packet,
a factual `reason`, `expected_state_sha256` from Supervisor Snapshot, and
`expected_selection_sha256` from the get-selection result (null if absent).
Changing the list is an explicit **replace-all** operation: omit a previously
selected path to remove it, reorder paths to change priority, or pass empty
lists to restore native defaults. A stale State SHA or list revision fails
closed. Retain the full audit trail of immutable selection revisions.

A selection is scoped to the current Contract version and Task and requires
the exact Task/Contract evidence SHA. Only existing UTF-8 text files inside
the authorized product repository may be referenced: no symlink traversal,
credentials, runtime/private locations, or new source writes. A selected
source must be justified by a quote of the Task/Contract; the source filename
itself **need not be explicitly named in the Task**. This lets S
actively identify suitable files rather than merely collect previously named
ones. The selection cap and `preflight.max_files` remain enforced.
`exclude_paths` may omit **only optional configured or past submitted
candidate files**, never a concrete Task Allowed Scope file. Excluded paths
and their hashes stay visible as non-authoritative metadata to PC. Missing
required evidence or external authorization still blocks the Task.

The tool only updates audited runtime evidence selection (immutable
`runtime/preflight/evidence_selection/vN/T-###/revision-*.json` records
with an atomic `latest.json` pointer). Every PC call loads the latest
revision, rehashes selected and excluded sources, includes selected excerpts
in its bounded prompt, and binds the list SHA. An earlier PC ALLOW becomes
stale when the selection changes; **only a fresh PC evaluation with fresh
hash validation authorizes E**. The Supervisor MUST NOT use this to conceal
material contradictory evidence, bypass a Contract constraint, fabricate a
permit, rewrite a PC report, or disable PC. PC is an independent gate, not
an approval form whose outcome S can edit. This list operation does not alter
E retries, workflow state, Contract, approval files, or usage ledgers.

### Supervisor-to-PC evidence submission (native, bounded, legacy supplement)

When PC returns a **finalized** DENY with a blocking
`resolution_owner=supervisor` finding caused by an *existing* relevant
repository file missing from the PC evidence bundle, S **SHOULD** attempt
a safe evidence-only repair **before** escalating or asking E to do product
work. S must first distinguish missing context from a genuine missing
approval, missing scientific evidence, unauthorized work, Contract defect,
or S1 implementation work. Those cases remain blocked and follow their
respective Planner/E/runtime resolution paths.

Use the direct MCP `psc_submit_preflight_evidence` tool, supplying
`repository`, `runtime_config`, `project`, the **existing Task file**
`task`, the active Approved `contract` directory,
`evidence_path` (POSIX repository-relative UTF-8 source), the exact
Supervisor-owned blocking `finding_id`, a bounded `reason`, and
`expected_state_sha256` from the latest `psc_supervisor_snapshot`.
Optional `previous_review` is a Review **file path**, not inline Markdown.
Do not infer authorization from a similarly named file. The source must
already exist, be explicitly referenced by the current Task/Contract packet
or prior Review, be inside the repository, and not be a secret/runtime path.
Submissions are immutable, Task/Contract-specific receipts stored at
`runtime/preflight/submitted_evidence/vN/T-###/`; source bytes and
receipt provenance are SHA-bound and revalidated. The native runtime accepts
only a ledger-matched, published PC DENY for the same Task and a fresh
workflow-state SHA. The interface does **not** edit the Contract, Task,
original PC report, its latest publication, runtime configuration, State,
approval records, physical evidence or Executor retry counters.

After a successful `status=evidence_submitted`, call
`psc_preflight_check` with the **same** Task/Contract/review and examine its
*new* `admission_finalized`, `decision`, `bindings` and findings.
A receipt never means ALLOW. A subsequent normal Executor dispatch still
runs its own fresh PC and full SHA revalidation; only a finalized current
ALLOW may launch E. If the receipt cannot be validated, the referenced
file changes, the evidence is insufficient, or the required external approval
is absent, stop on DENY/UNKNOWN. Do not alter PC verdicts, fabricate approvals,
or change the user's `runtime.json` to make a check pass. This receipt-only
path consumes no E retry budget and is separate from S0/S1 business repairs.

Supervisor must repair the finding reported by `resolution_owner`
(`runtime` = runtime/adapter configuration, `supervisor` = in-session repair,
`planner` = Contract revision) and dispatch again; the next dispatch re-runs PC
with fresh hashes. Use the optional `psc_preflight_check` tool to recheck without
launching E. An explicit `preflight.enabled=false` in the user-owned
`runtime.json` is the only bypass, and it is a user decision, never a Supervisor
shortcut.

Checker token usage and elapsed time are recorded separately in
`runtime/preflight_token_usage.jsonl` and
`runtime/preflight_token_usage_summary.json`; the decision report stays under
`runtime/preflight/`. Do not merge PC usage into the Executor ledger or report
it as Executor usage. The PC ledger now also provides a `workflow_total`
across **all Contract versions in the same PSC project/workflow**, including
separate `psc_preflight_check` calls and denied/unknown checks. Count all
persisted checker invocations, including ones with unavailable provider usage.

**Mandatory per-E-return usage report:** Immediately after **every real E
invocation returns**, before discussing acceptance/rework/next steps, emit
`usage_report.markdown` from the `psc_invoke_executor` MCP result **verbatim**
as a Markdown table plus its leading duration sentence and accuracy
notes. Do not compose a replacement from raw `elapsed_seconds` values or
reformat it into floating-point seconds. The formatter uses the same
`x h x m x s` duration style as progress status and preserves raw seconds
only for machine consumers. When cumulative E duration is unavailable,
display `不可用`, not a guessed time. When the result lacks `usage_report`
because no E started or no provider usage was captured, do **not** invent
token counts; explain that a complete report is unavailable.

The required table has **five columns**: **指标**, **本次 E**,
**E 累计（当前 Contract vN，n 次）**, **PC 累计（n 次）**, and
**累计总计（E+PC）**. The PC column counts the **entire workflow**, not just
the current Contract. The combined column must sum E current-Contract cumulative
and PC whole-workflow cumulative **field by field**, without adding this E
invocation a second time. Explicitly preserve those different accumulation
scopes in the footnote; never imply this total includes E from earlier
Contract versions. An unavailable component produces an unavailable sum; an
inexact component produces a lower-bound sum (≥), never an exact number.

Each invocation's table is the **complete normalized breakdown**, not a
single-total summary. **Do not report only** `total_tokens`.
All four data columns have the same seven token rows:
`input_tokens`, `uncached_input_tokens`, `cached_input_tokens`,
`cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, and `total_tokens`. Reasoning is part of output,
never added to total again. Add **two further rows inside the table**:
**缓存命中率** (`cache_hit_rate`) and **输出/输入比** (`output_input_ratio`).
Render **both metrics in every data column**, as one-decimal percentages:
- 本次 E: use the exact invocation token counts.
- E 累计: use current-Contract cumulative E token counts.
- PC 累计: use workflow-wide PC token counts, including previous Contract versions.
- 累计总计（E+PC）: use the summed current-Contract E and workflow-wide
  PC counts, without double counting the current E invocation.

For each column, calculate `缓存命中率 = cached_input_tokens / input_tokens`
and `输出/输入比 = output_tokens / input_tokens`, using **aggregate
numerators and aggregate denominators**, never an unweighted average of
per-invocation percentages. Cached input includes only provider cache reads,
not cache writes. If input/output/cache counters are unavailable, usage is partial/incomplete,
or the input denominator is zero, render `不可用` for that column's ratios.
Unknown **reasoning** alone must NOT suppress those two ratios: render
reasoning as `N/A` (JSON `null`), but do not add any separate unmeasured
reasoning amount to output or total (effectively zero extra in ratio math).
If Claude only provides terminal `result.usage`, its counters and ratios
cover **main Agent only**; label the scope and do not claim a complete
Executor+Subagent total. If combining differently scoped components, label
the scope limitation rather than claiming whole-workflow exactness. Ratios are display
calculations only; keep PC/E ledgers independent, and retain precise raw
token totals. The opening sentence contains durations, not duplicate ratios.
**Do not output `累计比率未提供`**: the two rows themselves convey each
applicable ratio or a justified `不可用`.

The table is required for successful, failed, timed-out, and reworked E
invocations whenever E actually ran and accounting is returned; it is **not**
a status table for PC-only checks that blocked E before launch. If any E or
PC provider usage is missing/inexact, keep it marked as unavailable or a
lower bound (≥), preserving invocation counts and separate ledger identities.
Never present unavailable usage as exact zero. The report is **per invocation**,
not delayed until Contract completion.

Provider usage is persisted independently of conversation state in
`runtime/executor_token_usage.jsonl`; the per-Contract projection is
`runtime/executor_token_usage_summary.json`. Do not count Supervisor tokens,
smoke-only model calls, or tokenizer estimates in this ledger. For DSH, fold
current durable `assistant/message` and `assistant/attempt` embedded streams
(including append-only changes to existing Session artifacts); use headless
`--json` `step_end.usage` only as a fallback when durable provider usage is
unavailable. Missing usage is unavailable/inexact, never a synthetic zero.

The MCP tool returns compact execution metadata only. On failure it may include
bounded diagnostic tails of stderr/stdout for immediate diagnosis; full Executor
stdout/stderr always remain in the persisted executor log, and semantic
completion content remains in `plan.md` / `coding.md`. For a failed Executor,
inspect the bounded diagnostic first. If that is insufficient, read only the
relevant range or tail of `log_path`; do not load the entire raw log into
Supervisor context by default. Read task artifacts selectively during normal
verification instead of injecting the full Executor transcript.

Normal direct MCP dispatch consumes the tool's structured result directly. If a
manual/debug Code Mode wrapper is ever used outside normal dispatch, emit only
`r.structuredContent ?? r.content`; never serialize the entire wrapper object,
because that can duplicate `content` and `structuredContent` in Supervisor
context.

MCP configuration and Executor configuration are separate. The MCP server is a
stable transport/waiting layer. Executor adapter, executable, home, model,
provider, effort, profile, approval policy, sandbox, and Executor timeouts remain
owned by `.agentic-sdlc/runtime.json` and the independent Executor environment.
Reload `runtime.json` on every dispatch. Changing Executor configuration must
not require rewriting MCP registration unless the MCP server path/command or
`tool_timeout_sec` itself must change.

For a normal Executor task, any `subprocess.TimeoutExpired` result means the
Executor was successfully launched and remained under runtime control until its
deadline. Treat that as insufficient time budget even when no stdout/stderr,
semantic artifacts, or repository changes were returned. Atomically update
`executor.timeout` in `.agentic-sdlc/runtime.json` to
`min(timeout * 2, maxTimeout)` before the next retry. Never raise it above
`executor.maxTimeout`. Failures that occur before a normal Executor run
(`executor_unavailable`, invalid inputs/configuration, smoke failure, or
`spawn_failed`) do not increase the timeout. Explicit smoke-timeout overrides
never change the normal task timeout. Existing legacy runtime files without
`maxTimeout` retain the old fixed-timeout behavior; new initialization must
collect `maxTimeout >= timeout`.

Large Executor instructions must never be transported as a
single Windows command-line argument. The invocation layer owns prompt
transport:

- Codex: invoke with the stdin sentinel (`codex exec -`) and send the complete
  PSC prompt via subprocess stdin.
- Claude Code: invoke `claude -p` and send the complete PSC prompt over
  stdin; require a dedicated `CLAUDE_CONFIG_DIR` and no session persistence.
- DSH: write the complete prompt to a short-lived runtime-owned UTF-8 file under
  `.agentic-sdlc/runtime/executor-inputs/`, pass only a short bootstrap
  instruction in argv, and delete the transport file before changed-path
  accounting.

This transport is adapter infrastructure and must not change Contract semantics,
consume scope, or appear in product `changed_paths`.

Both MCP and CLI call the same logical
`invoke_executor(adapter, repository, task, contract,
previous_review, runtime_config, *, project)` interface. `project` is a
Supervisor-runtime context used only to resolve the current task artifact
location; adapter-specific path logic remains behind the invocation layer. Keep
Codex, Claude, DSH, OpenCode, or any future harness behind an adapter; changing
the adapter must not change PSC semantics. Each invocation is a fresh,
stateless worker. Give it only the current task, relevant REQ/AC sections,
constraints, implementation guidance, and previous Supervisor review. It may
inspect and modify the repository and add tests, but must not write Contract,
runtime, review, or result files.

When all tasks for an Approved Contract `vN` pass and the workflow enters
`workflow_passed`, the last real E invocation must already have displayed
its complete three-column E/PC token report. In the final Supervisor message
refer to that report and the resulting acceptance state; do **not** delay
the table until completion or print a duplicate. If a recovery path skipped
the per-invocation report, fetch the active E summary with
`python scripts/psc_runtime.py executor-usage --project <project>` and
the PC workflow summary from `runtime/preflight_token_usage_summary.json`,
then show the seven-field E/PC totals with the same accuracy warnings.
Never show cumulative ratios in the final report.

For normal task dispatch, the invocation layer first materializes
`developing/artifacts/T-###/executor-packet.md`, containing the current Task,
only its referenced Requirement/Acceptance sections, task-relevant implementation
recommendations when identifiable, and the global constraints. The Executor
receives this task-scoped packet instead of the full Contract. It then returns one
strictly structured completion object containing its plan, coding summary,
modified files, tests, risks, and unresolved issues. The invocation layer parses that object and
persists the Executor-owned semantic content as
`developing/artifacts/T-###/plan.md` and `coding.md`; it never invents or
rewrites the Executor's plan. An invalid response or failed process produces no
successful task artifacts. Smoke uses its separate marker-file protocol.

Never copy or expose credentials. `runtime.json` contains configuration only;
authentication remains in the selected Executor environment. The invocation
layer may inherit ordinary OS environment needed to launch a process, but it
must strip Supervisor authentication/session overrides before every smoke or
normal Executor launch. At minimum remove `OPENAI_API_KEY`, `CODEX_API_KEY`,
`CODEX_CI`, `CODEX_SESSION_ID`, and `CODEX_THREAD_ID`, then set the configured
Executor `CODEX_HOME`/`DSH_HOME`. A Codex Executor must therefore authenticate
from its independent Executor home (for example `auth.json`) rather than a
Supervisor process credential.
## Explicit Planner mode

Planner mode is an optional Contract authoring convenience, not a runtime
dependency. Planner may inspect the repository, clarify the request, and write a
new immutable `contract/vN/` containing `requirements.md`, `acceptance.md`,
`implementation.md`, `constraints.md`, `tasks.md`, and `metadata.json`. Include
stable IDs (`REQ-###`, `AC-###`, `T-###`), complete task scope/dependencies, and
all decisions needed by an independent Supervisor. Use `status: draft` until
the user explicitly approves; materialize a new `vN+1` with `status: approved`
and `supersedes: N` rather than modifying the draft. Planner must not
code, invoke Executors, or mutate Supervisor state. Avoid conversation-only
phrases such as "as discussed earlier" in an approved Contract.

For the exact portable Contract and runtime layouts, use the references above
and [`references/planner-contract.md`](references/planner-contract.md).

## Deterministic helper

The bundled `scripts/psc_runtime.py` performs read-only discovery and Contract
validation, and safe project bootstrap/state writes when explicitly requested:

```text
python scripts/psc_runtime.py validate-contract <contract-dir> --repository <path>
python scripts/psc_runtime.py discover --repository <path> --runtime-config <path>
python scripts/psc_runtime.py bootstrap <contract-dir> --repository <path> --runtime-config <path>
python scripts/psc_runtime.py import-bundle <bundle-path> --repository <path> --runtime-config <path> [--project-id <id> | --new-project-id <id>]
python scripts/psc_runtime.py auto-import --repository <path> --runtime-config <path> [--project-id <id>]
python scripts/psc_runtime.py resolve-runtime-failure --project <project> --reason "<repair evidence>"
python scripts/psc_runtime.py executor-usage --project <project> [--contract-version N]
```

For a `runtime_failure` block caused by
`launch_transport_failed` or another non-retryable runtime/adapter error,
repair/update the runtime first, then call `resolve-runtime-failure` with an
auditable repair reason. That command only clears the runtime block and restores
the same task to `ready`; it does **not** reset retry counters, change
`execution_round`, or change execution owner.

Use the script rather than reimplementing JSON/ID/dependency checks. It never
invokes an Executor and never edits product source. Inspect its `--help` output
for optional naming and baseline flags.
## Contract Bundle import

## Executor initialization, health, and dispatch

Before Executor initialization, establish the **independent MCP Python runtime**.
The MCP Python is PSC infrastructure and must not be implicitly borrowed from the
product repository, the currently activated virtualenv/conda environment, the
project interpreter selected by the IDE, or any other project-owned Python
environment. Never install the MCP SDK into a project Python environment merely
to make this Skill work.

Probe a user-approved candidate with:

```text
python scripts/probe_mcp_runtime.py --python <candidate-python> --repository <repository> [--project-python <project-python>]
```

A usable MCP runtime must satisfy all of the following:

- Python 3.10 or newer.
- `import ssl` succeeds and exposes a working OpenSSL runtime.
- `python -m pip --version` succeeds.
- The interpreter is independent of the product repository and is not the same
  interpreter as the known project Python.
- `from mcp.server import MCPServer` succeeds before MCP startup.

If the candidate is independent and passes Python/SSL/pip checks but the MCP SDK
is missing, report `install_required` and install `mcp>=2,<3` only into that
explicitly selected independent runtime. If the candidate is the project Python,
inside the product repository, lacks SSL, lacks pip, or is too old, reject it and
select/create another Python runtime. Do not repair, upgrade, or mutate the
project Python as part of PSC initialization.

The MCP runtime is transport configuration only and is separate from both the
product Python and the Executor runtime. Persist its exact executable path in
`.agentic-sdlc/runtime.json` as `mcp.python_interpreter`. Reuse that stable
MCP runtime across projects unless the user intentionally changes it. If an
existing legacy runtime lacks the `mcp` block, keep it compatible but record
the path after the user confirms/selects the MCP Python; never infer a missing
path from the active project interpreter or IDE.

Supervisor MCP registration is a separate, harness-specific initialization
branch. The **Supervisor harness is independent of `executor.adapter`**:
Codex may supervise a DSH Executor and DSH may supervise a Codex Executor.

### Codex Supervisor MCP initialization

Register the stdio server in the effective Supervisor Codex configuration with
the selected MCP Python as `command` and this Skill's
`scripts/psc_mcp_server.py` as the single argument:

```toml
[mcp_servers.agentic_sdlc_executor]
command = "<absolute-mcp-python>"
args = ["<absolute-skill-root>/scripts/psc_mcp_server.py"]
tool_timeout_sec = 3600

[features.code_mode]
direct_only_tool_namespaces = ["mcp__agentic_sdlc_executor"]
```

Preserve existing MCP servers and existing
`direct_only_tool_namespaces` entries; append rather than replace. Set
`tool_timeout_sec` at least as large as the maximum intended Executor MCP call.
After editing Codex configuration, start a refreshed Supervisor session and
require direct top-level exposure of `psc_invoke_executor` before normal
dispatch.

### DSH Supervisor MCP initialization

DSH Supervisor setup is an executable initialization step. Run:

```text
python scripts/configure_supervisor_mcp.py configure \
  --mcp-python <absolute-mcp-python> \
  --scope home
```

The default target is the Supervisor's home-level
`$DSH_HOME/cordis.patch.yml` (or `~/.dsh/cordis.patch.yml`). This is the
safe persistent layer for DSH Desktop because Electron exclusively owns
`$DSH_HOME/profiles/desktop`.

The bootstrapper writes a marker-managed **insert patch**:

```yaml
- insert:
    - id: mcp-agentic-sdlc-executor
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: agentic_sdlc_executor
        transport: stdio
        command: '<absolute-mcp-python>'
        args:
          - '<absolute-skill-root>/scripts/psc_mcp_server.py'
        toolCallTimeoutMs: 7200000
        failOnStartupError: true
```

A top-level entry beginning directly with
`- id: mcp-agentic-sdlc-executor` is **not equivalent** in a patch file: it
targets an already-existing row by id. Because the base DSH tree does not
provide that PSC row, such a legacy entry does not register the MCP client.
The bootstrapper detects that legacy shape and migrates it to the required
`- insert:` form while preserving unrelated rows.

For non-Desktop custom CLI profiles,
`--scope profile --profile <name>` is supported. The reserved
`--profile desktop` scope fails closed.

DSH builds its MCP tool registry at Harness startup. After creating, migrating,
or updating the patch, the currently running DSH session cannot gain the tools
in place. Stop normal scheduling and require a DSH restart. A fresh session
must expose:

```text
mcp__agentic_sdlc_executor__psc_supervisor_snapshot
mcp__agentic_sdlc_executor__psc_ensure_executor_ready
mcp__agentic_sdlc_executor__psc_invoke_executor
mcp__agentic_sdlc_executor__psc_preflight_check
mcp__agentic_sdlc_executor__psc_commit_supervisor_transition
```

If configuration was repaired in the current session, report
`supervisor_restart_required`. If a fresh session still lacks the tools,
report `supervisor_mcp_unavailable` and fail closed. Never substitute shell,
subagent, or direct-Python Executor dispatch.

Persistent configuration can be checked without editing it:

```text
python scripts/configure_supervisor_mcp.py check --scope home
```

This proves only that the managed patch exists; actual tool exposure must still
be verified in a freshly started DSH Supervisor session.


If `.agentic-sdlc/runtime.json` is absent, stop normal Supervisor startup and
run one explicit user-facing initialization wizard. It must explicitly collect:

- Supervisor Harness (`codex` or `dsh`) for MCP registration only; this is
  independent of `executor.adapter`
- For a DSH Supervisor, the active Supervisor DSH home/profile whose
  `cordis.patch.yml` will receive the MCP client entry
- MCP Python Runtime (independent PSC infrastructure runtime; persist exact
  path as `mcp.python_interpreter`)
- Runtime Root
- Project Naming Rule
- Executor Adapter
- Executor Executable
- Executor Home
- Config Source (`runtime` or `executor_home`)
- Provider
- Model
- Reasoning Effort
- Approval Policy (`approval_policy`)
- Sandbox Mode
- Timeout
- Max Timeout (`maxTimeout`, must be >= Timeout)
- Smoke Timeout

For every **new** initialization, Provider, Model, and Reasoning Effort are
explicit user-confirmed parameters. Persist them together under
`executor.routing`; Model and Reasoning Effort must never be inferred from the
Supervisor, `CODEX_HOME`, `DSH_HOME`, `CLAUDE_CONFIG_DIR`, or an existing project. When Config
Source is `executor_home`, the independent home still owns provider
definitions/endpoints/credentials and must be readable, but
`executor.routing` overrides the selected provider/model/effort for each PSC
Executor invocation without rewriting that home. Existing schema-version-1
runtime files without `executor.routing` remain compatible with the legacy
inheritance behavior. Runtime never edits Executor-home authentication files.

Never infer any Executor value from the Supervisor session, model, provider,
`CODEX_HOME`, project/global Codex configuration, IDE permission profile, or
conversation. Ask only missing or invalid values for an existing configuration.
The Supervisor and Executor are separate environments. Never copy
configuration, authentication, or credentials into the Executor home and never
change the Supervisor process environment. A shared Executor home is accepted
only when the user explicitly confirms it with
`allow_shared_executor_home: true`.

When `executor.routing` is present, `scripts/invoke_executor.py` applies
that route on every invocation regardless of Config Source. Codex receives
per-run `--model`, provider, and reasoning-effort overrides while keeping
`CODEX_HOME` isolated for provider definitions and authentication. DSH receives
a short-lived command-line `--patch` that overrides its
`agent-default-model` provider/model/reasoningEffort without modifying
`DSH_HOME`. Claude Code requires `config_source=executor_home` and an
explicit route; its provider field is an audit label only, while model and
effort are CLI overrides. Claude Code reads endpoint/auth exclusively from a
dedicated `CLAUDE_CONFIG_DIR` and the child scrubs inherited Claude/Anthropic
API environment variables. For `workspace-write`, the home must explicitly
enable native Claude sandbox on macOS/Linux/WSL2; PSC requires strict
sandbox failure and no unsandboxed retries there. On **native Windows**,
`workspace-write` explicitly selects `claude_native_windows_mode`:
`edit-only` for file editing without a shell, or `trusted-shell` for
automatic Bash/test execution with ordinary Windows account privileges.
The trusted shell is **not** OS-isolated; it is an explicit user opt-in,
and post-run scope auditing cannot prevent external effects. Neither choice
needs WSL2 or project migration. `acceptEdits` is not itself a sandbox. Claude Preflight disables built-in and MCP tools and customizations. See
[Claude Executor rules](references/claude-executor.md).
Legacy configurations without `executor.routing` preserve the
previous `runtime` versus `executor_home` behavior. When the optional
`approvals_reviewer: auto_review` mode is selected, the adapter first checks
CLI support and uses its dedicated `--approve-for-me` mode without also passing
conflicting approval or sandbox flags. Unsupported adapters fail explicitly and
never fall back to the Supervisor. The recommended disposable configuration is
`approval_policy: never` plus `sandbox: workspace-write`;
`danger-full-access` requires an explicit user choice. Warn when a user selects
`on-request` because a non-interactive Executor can block.

Normal E dispatch is intentionally thin and retry-aware. The invocation layer
passes `initial`, `quality_rework`, or `abnormal_retry` into the Executor
prompt. E must avoid repository-wide rediscovery, repeated unchanged reads, and
Harness goal/orchestration helpers used only to manage PSC; it should start from
task/review-named evidence, use targeted bounded inspection, batch independent
tool calls when supported, and defer broad verification until the implementation
stabilizes unless the Contract says otherwise. Quality rework is delta-scoped to
the Supervisor review; abnormal retry continues from durable repository state.

For DSH only, `executor.dsh_tuning.tool_result_pruner` may opt into a
per-invocation pruning override without editing the DSH home/profile. An enabled
block supplies positive `thresholdChars`, `headChars`, and `tailChars`.
Validation mirrors DSH's emitted replacement budget: retained head + the fixed
`\n\n[... tool result middle pruned ...]\n\n` marker (39 Unicode code points) +
retained tail must be at most `thresholdChars`. If absent or disabled, PSC
leaves the profile's existing pruner behavior unchanged. Tuning is
part of the Executor fingerprint, so changing it requires a fresh smoke.

Codex Executor smoke fingerprints must be semantic rather than whole-file
hashes of `$CODEX_HOME/config.toml`. Codex may persist project-trust bookkeeping
while a smoke runs. Ignore only project entries whose path is a PSC-owned
ephemeral sibling workspace named exactly `psc-executor-smoke-<32 hex>`; every
other Codex config key, including trust state for real projects, remains
security-significant and must invalidate a prior smoke when changed. Never
"align" or overwrite a stale smoke fingerprint without rerunning smoke.

DSH Executor smoke fingerprints must not require `$DSH_HOME/settings.yaml`:
current DSH releases can run without that file. Treat it as an optional
fingerprint input, hashing it when present and recording a stable missing marker
when absent. Its appearance, removal, or content changes must invalidate a prior
smoke. The selected profile's `package.json` and `cordis.patch.yml` remain
required fingerprint inputs.

Initialization is complete only after the independent MCP Python probe reports
`ready`, `runtime.json.mcp.python_interpreter` records that exact path, and
the active Supervisor harness exposes the PSC MCP tools through its required
registration path. For Codex, require
`mcp__agentic_sdlc_executor` in
`[features.code_mode].direct_only_tool_namespaces` and direct top-level
`psc_invoke_executor`. For DSH, require an active
`@deepseek-ai/dsh-mcp-client` entry with
`serverName: agentic_sdlc_executor` and visible
`mcp__agentic_sdlc_executor__psc_invoke_executor`. In both cases, a refreshed
Supervisor session must expose the tool **before** E dispatch; absence is
`supervisor_mcp_unavailable` and must not trigger a shell/Python fallback.
Then require `runtime.json` validation, Executor static probe, and a real
Executor smoke. Run
`python scripts/invoke_executor.py smoke --repository <path> --runtime-config <path>`;
it uses the same adapter as normal dispatch in a temporary workspace, requires
the exact marker file, writes a secret-free `.agentic-sdlc/executor-smoke.json`,
and fails closed. Before every dispatch, reload `runtime.json` and require a
passed smoke artifact whose Executor configuration fingerprint matches.
`python scripts/invoke_executor.py status ...` prints safe configuration and
smoke state only.
## Contract activation

Bundle import materializes and validates a new immutable Contract but does not activate it for an existing workflow. The effective `workflow_state.json.contract_version` remains unchanged until the Supervisor invokes `python scripts/psc_runtime.py activate-contract --project <project> --repository <repository>`. Activation validates the highest Approved Contract, applies exactly its one `workflow_policy` strategy, rebuilds `developing/tasks/` from its `tasks.md`, preserves historical artifacts, then updates the effective version. A bootstrap import of the first Approved Contract is the only import-time exception. A materialized draft is immutable too: later approval must create a new Approved version, never edit the draft in place.

An External Planner may hand off a completed Contract as a single portable
Markdown file, the **`PSC-CONTRACT-BUNDLE`**, emitted by the External Planner
Contract Export Prompt at
[`prompts/contract-export.md`](prompts/contract-export.md). That prompt is the
single authoritative description of the transport format and the exact
metadata/task schema; the Supervisor never parses, imports, or receives a Bundle
from any other source. The Bundle is a *transport* format only: it is validated
and materialized into the immutable `contract/vN/` execution Contract, and it is
never used as a long-lived execution Contract itself.

`import-bundle <bundle-path> --repository <path> --runtime-config <path>
[--project-id <id> | --new-project-id <id>]` performs read -> provenance copy -> parse -> strict
validation -> semantic check -> atomic materialization -> workflow-state ->
report in one pass. `--help` documents every flag. Terminal import statuses
(one per attempt, recorded in the report): `imported`, `already_imported`,
`import_failed`, `version_conflict`. Exit code 0 for `imported` and
`already_imported` (and `escalated` imports, which are workflow states, not
transport failures); exit code 2 for `import_failed`, `version_conflict`,
`project_selection_required`, `multiple_pending_bundles`, and configuration
errors.

Rules the Supervisor must follow:

- **Startup discovery.** Check `contract/imports/` only when no usable Approved
  Contract exists for the repository (usable = passes `validate-contract` with
  `status: approved`). A *pending* Bundle is a regular file directly under
  `contract/imports/` (the `reports/` subdirectory is ignored) with no
  successful import record. Exactly one pending Bundle is imported
  automatically; two or more require user selection and are never guessed; a
  pending Bundle that fails mechanically produces an `import_failed` report and
  startup continues under the existing "no valid Approved Contract" handling
  (report to the user; request an import or Planner mode -- no scheduling). When
  a usable Approved Contract already exists, pending Bundles are not
  auto-imported (explicit `import-bundle` remains available).
- **Failure and escalation.** Mechanical errors (malformed Bundle, metadata,
  stable-ID/reference/dependency violations, manifest-metadata disagreement)
  terminate as `import_failed` with **no partial `contract/vN`** and no
  `workflow_state.json` change. Semantic incompleteness or contradiction
  (uncovered Requirement, Acceptance with no implementing task, a task missing
  an implementation-critical declaration, or a blocking `UNRESOLVED` in an
  *approved* Bundle) preserves the imported artifact as immutable evidence and
  places the workflow in the existing `waiting_planner` escalation path with
  `review/escalation-NNN.md`. Draft imports materialize but never schedule:
  `workflow_state.status` is set to `waiting_planner` with hold reason
  "draft awaiting approval"; a draft containing `UNRESOLVED` additionally gets
  an escalation file recording the draft and its unresolved items. The importer
  never guesses, repairs, or promotes a draft.
- **Provenance.** Every external Bundle is *copied* (never moved, never
  deleted) into `contract/imports/` before validation; copies are byte-identical
  and retained as provenance. Exactly one import report is written under
  `contract/imports/reports/` per attempt (success, failure, conflict,
  escalation), with pinned fields `source`, `copy_path`, `sha256`, `version`,
  `import_time`, `outcome`, `materialized_path`, `warnings`, `status`. Reports
  plus `contract/` are the sole basis for restart idempotency: re-importing the
  same SHA-256 for the same declared version yields `already_imported`; the same
  version with different content yields `version_conflict` and never modifies
  the existing `vN`.
- **Workflow selection and bootstrap.** A repository may contain multiple
  independent workflows. `--project-id` selects only an existing workflow
  associated with the repository; an unknown id fails with
  `project_id_not_found` and never bootstraps. `--new-project-id` is the
  explicit opt-in for a new workflow, must not name an existing workflow, and
  is mutually exclusive with `--project-id`. New workflows use the configured
  `project_naming` rule and an undiscoverable staging directory followed by an
  atomic rename; mechanical/validation failure removes the staging tree. When
  no workflow exists, an import without a selector retains the compatibility
  bootstrap using `metadata.project_name` or the repository directory name.
- **Workflow-local versions.** Contract versions are scoped to one workflow,
  not the repository. Independent workflows may each materialize `contract/v1`;
  `version_conflict` is evaluated only against the selected workflow's
  `contract/vN`.
- **Version changes.** Importing a newer approved version into an existing
  workflow is handled exclusively by the existing workflow-policy mechanism
  (`restart: all`, `restart: pending_only`, or `invalidate_from_task: T-###`);
  the importer validates the policy shape but never interprets it, invents a
  second invalidation system, or mutates prior versions.

**Strict role boundary.** The Bundle is consumed only by the Supervisor-side
importer. The Executor never parses, imports, or receives a Bundle, and nothing
Bundle-derived is ever passed to `invoke_executor` or any adapter. The importer
never invokes an Executor, an adapter, or any coding worker.
`prompts/contract-export.md` is the **External Planner Contract Export Prompt**:
it is copied to an external Planner to emit a Bundle and is **not a Supervisor
Runtime Prompt**.

## Non-negotiable invariants

- Contract is the authority; P/S communication occurs through artifacts.
- Conversation is temporary I/O, never persistent workflow state.
- Every workflow is resumable from disk and detects meaningful workspace drift.
- Requirement, acceptance, and task references use stable IDs.
- Executor is disposable and cannot approve its own work.
- Supervisor owns scheduling, verification, retries, escalation, and state.
- Normal Executor dispatch is MCP-only; missing Supervisor MCP exposure fails
  closed and never falls back to shell execution or direct Python imports.
- In normal Executor-owned tasks, Supervisor may directly repair S0 housekeeping only; S1 (including micro product edits) goes to Executor. Explicit user-authorized task takeover remains a separate durable owner mode, never an autonomous S1 shortcut.
- Planner does not code; Supervisor does not redesign the Contract.
- Repository evidence is required for acceptance.
- The Bundle is a transport format, never a long-lived execution Contract; only
  the materialized immutable `contract/vN/` is executable, and the Executor
  never sees or parses a Bundle.

## Workflow Registry

Normal `workflow_passed` / `failed` transitions automatically move to
`recent/`; do **not** explicitly close just to mark a passing Task. Blocked
and waiting_planner workflows stay active for recovery. When the user
explicitly requests closure of a non-running workflow, use native
`psc_close_workflow(project, reason, expected_state_sha256)` or the CLI
`close-workflow`. Preserve State and all evidence.

Check the result's `registry_synced` field. If false, report that the durable
closure succeeded but index publication did not; run
`psc_reconcile_workflow_registry` to repair. Never edit index files or the
closure marker by hand. The authoritative `runtime/workflow_closure.json`
blocks discovery, Preflight/Executor admissions and Supervisor transitions;
a malformed closure also fails closed. Only successful higher-version Approved
Contract activation reopens a workflow. The registry remains observational,
never an execution authorization.

The runtime maintains a derived per-workflow registry, documented in references/workflow-registry.md. Bootstrap registers; terminal completion or explicit close unregisters. Blocked and waiting_planner stay indexed. Registry files are observational; workflow_state.json remains authoritative. GUI source and release guidance live in [psc-executor-monitor](https://github.com/XuanzheChen/psc-executor-monitor), a separate repository.
