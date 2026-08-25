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
attempts and state after every meaningful transition. On an implementation
failure retry the same task with the review as input. On missing, contradictory,
unsafe, or impossible Contract information, stop and write
`review/escalation-NNN.md`, set `workflow_state.status` to `waiting_planner`,
and wait for a new Contract version or an explicit resolution artifact.

Read [`references/runtime-protocol.md`](references/runtime-protocol.md) for the
state machine, discovery, bootstrap, resume, drift, retry, escalation, and
artifact ownership rules. Read [`references/executor-adapters.md`](references/executor-adapters.md)
when invoking or changing a harness.

## Executor boundary

Call only the logical `invoke_executor(adapter, repository, task, contract,
previous_review, runtime_config)` interface. Keep Codex, Claude, DSH, OpenCode,
or any future harness behind an adapter; changing the adapter must not change
PSC semantics. Each invocation is a fresh, stateless worker. Give it only the
current task, relevant REQ/AC sections, constraints, implementation guidance,
and previous Supervisor review. It may inspect and modify the repository, add
tests, and write `developing/artifacts/T-###/plan.md` and `coding.md`, but it
must not alter Contract files, requirements, workflow state, reviews, or results.

Never copy or expose credentials. `runtime.json` contains configuration only;
authentication remains in the selected Executor environment.

## Explicit Planner mode

Planner mode is an optional Contract authoring convenience, not a runtime
dependency. Planner may inspect the repository, clarify the request, and write a
new immutable `contract/vN/` containing `requirements.md`, `acceptance.md`,
`implementation.md`, `constraints.md`, `tasks.md`, and `metadata.json`. Include
stable IDs (`REQ-###`, `AC-###`, `T-###`), complete task scope/dependencies, and
all decisions needed by an independent Supervisor. Use `status: draft` until
the user explicitly approves; then write `status: approved`. Planner must not
code, invoke Executors, or mutate Supervisor state. Avoid conversation-only
phrases such as "as discussed earlier" in an approved Contract.

For the exact portable Contract and runtime layouts, use the references above
and [`references/planner-contract.md`](references/planner-contract.md).

## Local initialization and deterministic helper

First Supervisor use in a workspace must ensure `.agentic-sdlc/` and a
user-editable `.agentic-sdlc/runtime.json` exist. Ask once for runtime root,
project naming, adapter/harness, Executor home, provider, model, effort,
approval, sandbox, and timeout; persist the answers and reload the file on every
run. Never put keys, passwords, tokens, cookies, or other credentials there.
Use [`references/runtime-config.md`](references/runtime-config.md) for the
config shape and initialization questions.

The bundled `scripts/psc_runtime.py` performs read-only discovery and Contract
validation, and safe project bootstrap/state writes when explicitly requested:

```text
python scripts/psc_runtime.py validate-contract <contract-dir> --repository <path>
python scripts/psc_runtime.py discover --repository <path> --runtime-config <path>
python scripts/psc_runtime.py bootstrap <contract-dir> --repository <path> --runtime-config <path>
python scripts/psc_runtime.py import-bundle <bundle-path> --repository <path> --runtime-config <path> [--project-id <id>]
python scripts/psc_runtime.py auto-import --repository <path> --runtime-config <path> [--project-id <id>]
```

Use the script rather than reimplementing JSON/ID/dependency checks. It never
invokes an Executor and never edits product source. Inspect its `--help` output
for optional naming and baseline flags.

## Contract Bundle import

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
[--project-id <id>]` performs read -> provenance copy -> parse -> strict
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
- **Bootstrap.** Explicit `import-bundle` works when the repository and
  `.agentic-sdlc/runtime.json` exist but no workflow exists; it creates the
  normal workflow layout, `runtime/project.json`, and
  `runtime/workflow_state.json` (`initialized` for approved, `waiting_planner`
  for draft). Naming reuses the configured `project_naming` rule with precedence
  `--project-id` > `metadata.project_name` > repository directory name.
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
- Supervisor coordinates and verifies; it must not implement product code itself.
- Planner does not code; Supervisor does not redesign the Contract.
- Repository evidence is required for acceptance.
- The Bundle is a transport format, never a long-lived execution Contract; only
  the materialized immutable `contract/vN/` is executable, and the Executor
  never sees or parses a Bundle.
