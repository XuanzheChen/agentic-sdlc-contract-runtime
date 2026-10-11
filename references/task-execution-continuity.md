# P0: Pending-only Contract task execution continuity

## Problem and authoritative paths

Before this fix, \`activate_contract\` changed \`workflow_state.contract_version\` and rebuilt task files but did not migrate \`runtime/executor_attempts.json\`. Both the Supervisor's \`_retry_state\` and the Executor's \`_attempt_key\` look up \`vN:T-###\`. Consequently, \`restart: pending_only\` created a silent, empty retry round for any unfinished task, including previously attempted tasks.

This change separates **Executor attempt continuity** from **Preflight ALLOW/DENY validity**. It never transfers historical Preflight authorization or bypasses a fresh current-version PC check.

## Authority: one atomic ledger transaction

For an Approved target version \`vN\` declaring \`supersedes: M\` and \`workflow_policy: {"restart":"pending_only"}\`, the native Runtime validates:

- Exact source/target Contract version and lineage, materialized Approved status and target Contract validation.
- Stable semantic task identity across all shared Task IDs: Title, Goal, Requirements, Acceptance, Dependencies, Forbidden Scope. Changes to Allowed Scope, Implementation Notes and Required Verification do not by themselves create a different Task, but both complete Task sources and both full Contract byte hashes remain audit-bound.
- The workflow's completed prefix and order. Previously PASS-completed tasks are **not reexecuted**. Only pending tasks get vN retry records. New truly virgin pending tasks receive legal initial eligibility; any existing source retry record is inherited exactly. Unclassified legacy attempts, or source Executor usage evidence with a missing retry entry, fail closed.
- Existing target-version retry records without coherent provenance are a conflict, **never overwritten**.

The Runtime atomically writes both the derived vN task retry records and their \`continuity\` provenance in **the same \`runtime/executor_attempts.json\` schema-v2 file**, using unique sibling staging, flush/fsync and atomic \`os.replace\`. Old version task entries remain intact. Provenance includes source/target versions, per-task identities, original source and inherited retry state, source/target full Contract hashes, Task hashes, source retry state SHA, target initial and latest retry state SHA, and migration time. No second counter ledger exists.

Activation publishes the complete ledger transition **before** switching workflow_state to the new Contract version. If state publication subsequently fails, the old version remains active and retrying activation verifies/idempotently reuses the already-written target ledger transition. A ledger rename failure leaves the previous ledger untouched. All migration, activation, native E dispatch and explicit exhaustion reset routes use the same process-safe workflow lock (\`runtime/executor-dispatch.lock\`) to prevent concurrent double initial executions.

## Already-active upgrades and recovery

For workflows where an older runtime *already activated* v3 without migrating attempts, the new native tool is:

\`psc_recover_task_continuity(project, repository, source_version, target_version, expected_state_sha256, expected_ledger_sha256?)\`

It executes the **same validation and atomic ledger migration**, restricted to the workflow's active source or target version, and requires a compare-and-swap expected workflow-state SHA. Supplying the current full \`executor_attempts.json\` SHA further protects against concurrent edits. The tool does **not** change \`workflow_state.json\`, inspect an external scientific engine, launch E, or issue Preflight authorization. \`migrated\` and \`already_migrated\` results are safe to recheck; incomplete/mismatched migrations fail closed.

The read-only native \`psc_supervisor_snapshot\` now exposes \`task_continuity\`, and its \`retry\` field reports the inherited authoritative counters only after verification. Without valid provenance, it shows a blocked continuity state rather than zero attempts.

The MCP \`psc_preflight_check\` and \`psc_invoke_executor\` both check continuity **before** paid PC/E calls; \`psc_invoke_executor\` also verifies task order, inherited attempt eligibility and the old independent retry budget caps, while normal charging atomically updates the vN task state and its last-state SHA. No \`retry_kind=initial\`, \`quality_rework\` or \`abnormal_retry\` parameter can bypass unresolved continuity. T-008 cannot dispatch while T-007 remains the earliest unfinished task. The established explicit user-authorized budget-exhaustion reset path updates the continuity SHA when starting a *new* execution round.

For \`restart: all\`, \`invalidate_from_task\`, and legacy non-pending-only workflows, existing retry semantics are preserved.

## Safe recovery of the measurement-instrumentation workflow (instructions only)

**No consumer project mutation has been performed by this PR.** After syncing the Skill from merged main and restarting the Supervisor MCP service:

1. Read \`psc_supervisor_snapshot(project=<absolute workflow directory>, repository=<absolute repository directory>, contract=<project>/contract/v3)\`. Confirm approved v3, \`last_completed_task=T-006\`, \`current_task=T-007\`, native T-001..T-006 PASS, workflow SHA and source v2 contract provenance. The expected pre-recovery \`task_continuity.status\` is \`blocked\`.
2. Read the SHA256 of \`runtime/executor_attempts.json\` **without changing it**. Call \`psc_recover_task_continuity(project=..., repository=..., source_version=2, target_version=3, expected_state_sha256=<the snapshot SHA>, expected_ledger_sha256=<read-only ledger SHA>)\` once. Do not patch the JSON manually; if the tool blocks, stop and investigate.
3. Read a fresh native Snapshot. It must show the verified continuity state for T-007: \`execution_round=1\`, \`initial_attempted=true\`, \`quality_retries_used=1\`, \`abnormal_retries_used=0\`. At the current 3+3 limits, remaining quality=2, abnormal=3. Verify the old \`v2:T-007\` entry, earlier native PASS artifacts and the T-008 virgin state are unchanged. Observe that no Preflight ALLOW is inherited.
4. Invoke a *fresh* \`psc_preflight_check\` for approved Contract v3 with the current task, source repository and runtime configuration. Only a current finalized ALLOW with valid task/config/evidence binding permits \`psc_invoke_executor\` to be called. Do not reuse prior v2 DENY/ALLOW. For T-007, \`retry_kind=quality_rework\` reflects the inherited prior initial and rejection; confirm that the latest native Supervisor Review supports that classification before dispatch.
5. Do not dispatch T-008 until T-007 has its own native PASS. No physical simulation is authorized by these recovery steps.

## Tests, fail-closed and limits

Run \`python -m pytest tests -q -p no:cacheprovider --disable-warnings\`. Regression tests exercise pending-only activation, explicitly recovered legacy upgrades, full retry inheritance/charging, identical source storage and PASS history, missing/altered provenance, source/target task and Contract drift, unclassified source usage, budget exhaustion, repeated/concurrent recovery, interrupted activation/rename failure, v2->v3->v4, proper task order, and non-pending-only compatibility.

The ledger migration transaction is atomic **for the ledger file**, not a cross-file atomic commit of the entire Contract activation (tasks, state, workflow registry). The ledger is committed *first* and state is switched only afterward; rerunning activation verifies and reuses a committed ledger migration. Fatal process/OS failure can still leave the source version active with prepared target ledger entries; this is recoverable through idempotent activation, and never grants a free new initial.

Provenance assumes the approved Contract source bytes and historic native PASS artifacts are trustworthy. If they are modified, the validator blocks rather than guessing a new budget. This release does not automatically repair broken source histories or assign unclassified v1 retry costs. Scientific approvals, physical branch budgets and the research workflow remain outside the scope of this runtime migration.

Rollback requires reverting code and restarting MCP; **never** delete inherited attempt records or rewrite historical source entries as a shortcut. A migration recorded by the new runtime may not be safely interpreted by old runtime dispatch logic, so after reverting you must stop dispatch and audit the pending-only lineage before resuming.
