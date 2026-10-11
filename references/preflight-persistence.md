# Preflight persistence and admission audit

This Skill-level hardening addresses the 2026-10-11 developer fault report. It changes no research contracts, scientific guardrails, study data, workflow status, E retry budgets, or local ACLs.

## Confirmed causes and limits

The old producer saved a unique Checker report, then its provider usage/summary, then replaced `runtime/preflight/latest.json`. On a persistent Windows WinError 5 it correctly returned UNKNOWN (after a previous hotfix), but the unique model receipt and usage ledger retained the earlier Checker decision. Those records are **not** final admission. Old Supervisor reviews also were passed to the model without bound, current native accounting recovery facts.

The OS-level cause of the WinError 5 in the research workspace remains **unverified**. In an isolated developer-owned Windows directory, we tested both regular ACL and an inherited Everyone DeleteChild DENY: `os.replace` and `ReplaceFileW` both succeeded in that environment. Thus inherited DeleteChild DENY alone does not prove the research failure. A second isolated test opens the target without FILE_SHARE_DELETE and verifies that atomic publishing fails while the old target remains intact. Do not change ACLs, elevate permissions, delete the target, or claim that a generic retry cures a persistent denial.

## Atomic publication and diagnosis

`_write_json` stages a unique sibling and fsyncs it. It tries `os.replace` with bounded Windows sharing/access retries. When *all three* fail on an existing Windows file with WinError 5/32/33, it attempts Win32 `ReplaceFileW` once as an additional atomic technique. A persistent failure preserves the prior destination, fails closed and returns a diagnostic listing the failed operation, attempt, WinError/errno, service PID, target existence and limited file attributes. No non-atomic overwrite, unlink-before-replace, or ACL repair is permitted. A live-service token/handle investigation is still necessary if both OS APIs fail.

## Two-layer status

Each new result includes `checker_decision` and `checker_status` for the model's decision, separate from `decision`, `status`, `admission_status` and `admission_finalized` for final runtime admission. The unique report and immutable token-ledger row are recorded **before** latest publication and explicitly say `admission_status=unfinalized`. Only a successful latest publication can finalize the current admission. If any mandatory write fails, the native result becomes UNKNOWN and blocks E even when the model said ALLOW. If an earlier publication exists, it remains historical; its ALLOW can never authorize this attempt.

Use `psc_supervisor_snapshot(...).preflight_publication` or the read-only `preflight_publication_status(project)` to correlate the latest ledger run_id with the latest mirror. A mismatched run ID means `stale_latest=true` and **unfinalized**, not ALLOW. No historic ledger entry, receipt or mirror is rewritten, and the existing recovery/deduplication path remains independent of Executor accounting. Never infer authorization from the aggregated ALLOW counter.

## Current native evidence

After idempotent usage recovery and before the model request, PSC assembles runtime facts with the current ledger bytes SHA, exact record count, recovered run IDs, last run ID, runtime protocol revision and process PID. Facts are bound into the Checker evidence SHA and checked again before E launch. Old Supervisor review text is explicitly historical; the model may use *these specific verified runtime facts* over superseded claims about ledger counts or the PC code path. The facts **do not** assert external service health, general readiness, permission to perform physical experiments, or scientific approval. Malformed rows and changed ledgers cause fail-closed checking.

## Verification, deployment, rollback

Tests cover ALLOW/DENY with report, usage-summary and latest failures; stale latest; prompt recovery facts; ledger changes mid-call; native Windows ReplaceFileW fallback and delete-sharing denial; no E dispatch and no E budget charging. Run `python -m pytest tests -q -p no:cacheprovider --disable-warnings`, and test Windows behavior in a developer-owned temp directory. Updating the GitHub repository does **not** hot reload an already running Supervisor MCP process: deploy the Skill copy and restart the service when the project is ready.

Rollback is a normal Git revert of this ASCR commit followed by redeployment and MCP restart. It does not rewrite historical reports/ledger entries. Repaired code does not retroactively authorize the blocked T-005. Before any resumed scheduling, acquire the newest native Snapshot, Contract, task and review, then perform one **fresh** native PC. E may start only with an explicit finalized ALLOW and matching evidence revalidation; on any permission or persistence failure, keep UNKNOWN and stop.
