# Agentic SDLC Contract-Driven Runtime (PSC)

**English** | [简体中文](README.zh-CN.md)

`agentic-sdlc-contract-runtime` is a portable, artifact-first runtime for Contract-Driven Agentic SDLC workflows. It separates planning, supervision, and implementation into durable roles:

- **Planner (P)** produces an immutable, versioned Contract.
- **Supervisor (S)** owns workflow state, dispatch, verification, retries, escalation, and acceptance.
- **Executor (E)** is a disposable coding worker launched through a harness adapter.

The repository is designed so a workflow can be resumed from files on disk without relying on prior chat history. The same runtime can use either **Codex Supervisor** or **DeepSeek Harness (DSH) Supervisor**, and either Supervisor can dispatch a supported Codex or DSH Executor.

## Core properties

- Immutable `contract/vN/` execution Contracts with stable `REQ-###`, `AC-###`, and `T-###` identifiers.
- Durable workflow state under `runtime/`, including task ownership, retries, escalation, resume capsules, and Executor usage accounting.
- Blocking local MCP transport for normal Supervisor-to-Executor dispatch.
- A read-only **Preflight Checker (PC)** gate before every Executor attempt, with a strict `ALLOW`/`DENY` report, fail-closed `UNKNOWN`, independent token/elapsed accounting, and no Executor retry-budget charge.
- Independent Codex and DSH Supervisor MCP initialization paths.
- Codex and DSH Executor adapters with isolated homes and per-run provider/model/effort routing.
- Independent `quality_rework` and `abnormal_retry` budgets.
- Explicit Supervisor takeover / handback semantics.
- Real Executor smoke tests and configuration fingerprints before normal dispatch.
- Portable `PSC-CONTRACT-BUNDLE` import with provenance, validation, idempotency, and immutable materialization.
- Durable Executor token accounting; missing provider usage is reported as unavailable/inexact rather than silently treated as zero.
- Fail-closed execution boundaries: if the required Supervisor MCP tool is unavailable, PSC does not silently run E through a shell or direct Python import.

The authoritative behavioral specification is [`SKILL.md`](SKILL.md). Detailed contracts live under [`references/`](references/).

---

## Real-time Executor progress (MCP)

Normal `psc_invoke_executor` remains **one blocking call = one Executor attempt**. The MCP path runs the original invocation in a worker thread, concurrently drains the Codex/DSH `--json` stdout and stderr, and sends **best-effort request-scoped** MCP `notifications/progress` updates (step/tool/heartbeat/completion). The final parser, usage accounting, scope verification, retry budgets, and Supervisor review are unchanged. Progress messages never forward `thinking`, raw tool results, or full commands. Progress notification failures do not affect the attempt.

For every launched invocation, PSC atomically writes a latest snapshot at:

```text
<active-psc-project>/runtime/executor-progress.json
<active-psc-project>/runtime/executor-progress/<run_id>.jsonl
```

The JSON snapshot distinguishes `last_executor_event_at` from `last_heartbeat_at` and provides elapsed time, model, task, steps, tool calls, and terminal status. Human-facing MCP progress and heartbeat messages use `x h x m x s` (e.g. `1 h 2 m 3 s`). The JSON also includes `elapsed_display` and `last_event_age_display`, while retaining numeric `elapsed_seconds` and `seconds_since_executor_event` for existing consumers. The existing Executor log remains the full audit artifact; the progress JSONL contains short redacted summaries only. This observation channel is not a substitute for completion or Supervisor review.

**Probe Codex UI support before any real task:** after refreshing the Supervisor's MCP connection, invoke `psc_progress_probe` directly. It emits five progress notifications over 20 seconds and performs **no Executor launch, no retry charge, and no PSC state transition**. Seeing five updates in a raw MCP client is not evidence that Codex Desktop/TUI actually renders them. Verify progress in the client UI separately. Some Codex versions only log notifications rather than display them. If UI display fails, inspect `executor-progress.json` while a real invocation runs; do not replace blocking dispatch with polling calls. The probe does not prove end-to-end cancellation handling.

Codex Supervisors should keep the PSC MCP namespace in `direct_only_tool_namespaces` when Code Mode is available, because model-driven `exec/wait` loops can change the lifetime of an otherwise blocking MCP call.

---


## Architecture

```text
External Planner / Planner session
            |
            | Contract or PSC-CONTRACT-BUNDLE
            v
    immutable contract/vN/
            |
            v
       Supervisor (S)
       /            \
 Codex Supervisor   DSH Supervisor
       \            /
        \  PSC MCP /
         v        v
 scripts/psc_mcp_server.py
            |
            v
 scripts/invoke_executor.py
            |
       adapter boundary
        /          \
     Codex E       DSH E
            |
            v
      product repository

Durable control/evidence:
- runtime/workflow_state.json
- runtime/executor_attempts.json
- runtime/executor_token_usage.jsonl
- runtime/preflight/latest.json
- runtime/preflight_token_usage.jsonl
- runtime/supervisor_resume.json
- developing/tasks/T-###.md
- developing/artifacts/T-###/{executor-packet.md,plan.md,coding.md,review.md,result.md}
```

Supervisor harness and Executor adapter are independent choices. For example, a DSH Supervisor may dispatch a Codex Executor, and a Codex Supervisor may dispatch a DSH Executor.

Between the MCP runtime and the Executor invocation layer, a read-only **Preflight Checker (PC)** gates every attempt. See [Preflight Checker gate](#15-preflight-checker-gate).

---

## Repository layout

```text
.
├─ SKILL.md
├─ prompts/
│  └─ contract-export.md
├─ references/
│  ├─ contract-schema.md
│  ├─ executor-adapters.md
│  ├─ planner-contract.md
│  ├─ runtime-config.md
│  └─ runtime-protocol.md
├─ scripts/
│  ├─ adapters/
│  │  ├─ codex.py
│  │  └─ dsh.py
│  ├─ executor_token_usage.py
│  ├─ invoke_executor.py
│  ├─ preflight_checker.py
│  ├─ probe_mcp_runtime.py
│  ├─ psc_mcp_server.py
│  ├─ psc_runtime.py
│  └─ supervisor_runtime.py
├─ requirements-mcp.txt
└─ tests/
```

A bootstrapped workflow project contains its own immutable Contract versions, developing task artifacts, and runtime state. Conversation history is not workflow state.

---

## Installing the Skill

Place this repository where the Supervisor can discover local Skills, for example as a repository-local skill:

```text
<product-repository>/.agents/skills/agentic-sdlc-contract-runtime/
```

Then invoke it explicitly, for example:

```text
Use $agentic-sdlc-contract-runtime to resume or start the PSC workflow.
```

The Skill is intentionally not a replacement for ordinary small coding tasks. It is meant for work that benefits from explicit contracts, independent verification, durable retries, and resumable multi-step execution.

---

# 1. Supervisor MCP setup

Normal Executor dispatch is MCP-only. Both supported Supervisor harnesses run the same local stdio MCP server:

```text
scripts/psc_mcp_server.py
```

That server exposes:

- `psc_supervisor_snapshot`
- `psc_ensure_executor_ready`
- `psc_invoke_executor`
- `psc_preflight_check`
- `psc_commit_supervisor_transition`

The visible tool name differs by Supervisor harness.

## 1.1 Select an independent MCP Python

PSC infrastructure should not be installed into the product project's Python environment merely to make the workflow run.

Probe an explicitly selected interpreter:

```text
python scripts/probe_mcp_runtime.py \
  --python <candidate-python> \
  --repository <product-repository>
```

When the project interpreter is known:

```text
python scripts/probe_mcp_runtime.py \
  --python <candidate-python> \
  --repository <product-repository> \
  --project-python <project-python>
```

A usable MCP Python must be:

- Python 3.10 or newer.
- Outside the product repository.
- Different from the known product interpreter.
- Able to import SSL/OpenSSL.
- Able to run pip.
- Able to import `mcp.server.MCPServer`.

If the interpreter is otherwise healthy but the MCP SDK is missing:

```text
<candidate-python> -m pip install -r requirements-mcp.txt
```

Persist the exact selected path as:

```json
{
  "mcp": {
    "python_interpreter": "<absolute-python-path>"
  }
}
```

in `.agentic-sdlc/runtime.json`.

## 1.2 Codex Supervisor

Register the PSC MCP server in the effective Codex configuration:

```toml
[mcp_servers.agentic_sdlc_executor]
command = "F:/Miniconda3/envs/psc-mcp/python.exe"
args = ["E:/path/to/agentic-sdlc-contract-runtime/scripts/psc_mcp_server.py"]
tool_timeout_sec = 3600

[features.code_mode]
direct_only_tool_namespaces = ["mcp__agentic_sdlc_executor"]
```

Preserve existing MCP server entries and existing `direct_only_tool_namespaces`; append the PSC namespace instead of replacing unrelated configuration.

`tool_timeout_sec` is a call timeout, not a polling interval. It should cover the longest intended Executor MCP call.

After changing Codex configuration, start a refreshed Supervisor session. Normal dispatch is ready only when `psc_invoke_executor` is exposed as a direct top-level MCP tool.

## 1.3 DSH Supervisor

DSH Supervisor initialization is performed by the bundled bootstrapper:

```text
python scripts/configure_supervisor_mcp.py configure \
  --mcp-python <absolute-mcp-python> \
  --scope home
```

For DSH Desktop, use the home-level `$DSH_HOME/cordis.patch.yml`. Electron
owns `$DSH_HOME/profiles/desktop`, so the CLI/bootstrapper intentionally does
not mutate that reserved profile directly. The home-level patch is applied
after the profile layer and therefore affects Desktop as well.

The required patch shape is an **insert**:

```yaml
- insert:
    - id: mcp-agentic-sdlc-executor
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: agentic_sdlc_executor
        transport: stdio
        command: 'F:\Miniconda3\envs\psc-mcp\python.exe'
        args:
          - 'E:\path\to\agentic-sdlc-contract-runtime\scripts\psc_mcp_server.py'
        toolCallTimeoutMs: 7200000
        failOnStartupError: true
```

A legacy top-level block that starts directly with
`- id: mcp-agentic-sdlc-executor` only targets an existing Cordis row and
does not insert the PSC MCP client when that row is absent. The bootstrapper
automatically migrates that known legacy shape to `- insert:` and preserves
unrelated patch entries.

After any create/migrate/update, fully restart or refresh DSH. Tool registration
happens during Harness startup. A fresh session should contain:

```text
mcp__agentic_sdlc_executor__psc_supervisor_snapshot
mcp__agentic_sdlc_executor__psc_ensure_executor_ready
mcp__agentic_sdlc_executor__psc_invoke_executor
mcp__agentic_sdlc_executor__psc_preflight_check
mcp__agentic_sdlc_executor__psc_commit_supervisor_transition
```

Persistent config can be checked with:

```text
python scripts/configure_supervisor_mcp.py check --scope home
```


## 1.4 No silent fallback

Missing Supervisor MCP exposure is a configuration error, not permission to emulate MCP through another route.

Normal Supervisor execution must not:

```text
python scripts/invoke_executor.py invoke ...
```

as a fallback, must not import `scripts/psc_mcp_server.py` from a shell/Python subprocess, and must not call the internal Executor implementation directly.

The compatibility function `invoke_executor_tool()` deliberately returns:

```text
status = mcp_transport_required
reason = direct_python_dispatch_forbidden
```

and does not launch E.

The CLI `invoke` and `smoke` commands remain available for humans, tests, debugging, and recovery, but they are not normal replacements for a missing Supervisor MCP tool.

## 1.5 Preflight Checker gate

A read-only **Preflight Checker (PC)** runs before **every** Executor attempt. The gate lives inside `psc_invoke_executor`, so a Supervisor cannot reach E by skipping the optional `psc_preflight_check` companion tool.

PC reuses the **same** adapter, executable, executor home, provider, model, and reasoning effort as E, but is a separate read-only invocation with its own run id, timeout, log, report, and token/elapsed ledger. It never edits the Executor home or `runtime.json`.

- **Codex**: forced `--sandbox read-only --ask-for-approval never` (the Executor's configured sandbox/approval policy is ignored for PC), an isolated empty temporary working directory outside the repository, the strict report schema through `--output-schema`, and the prompt on stdin.
- **DSH**: a short-lived command-line `--patch` overlay that disables shell, filesystem writes, code editing, MCP client tools, external tools, and dangerous tools, placed before the first app-owned flag. PSC composes the profile `cordis.patch.yml` first and the runtime patch last and verifies every restriction; if that cannot be proven, the check fails closed with `dsh_tool_restrictions_unverifiable`.
- **No free filesystem access**: the runtime gathers a bounded, task-specific evidence bundle (Task file, task-scoped Contract packet, previous Supervisor review, Contract binding, bounded Allowed-Scope/configured files) with deterministic SHA-256 hashes and transports it through a runtime-owned prompt.

PC returns a strict report:

```json
{
  "schema_version": 1,
  "decision": "ALLOW",
  "summary": "Task and Contract are consistent; scope is sufficient.",
  "findings": []
}
```

A `DENY` must contain at least one `blocking` finding; every finding carries `evidence` strings and a `resolution_owner` of `runtime`, `supervisor`, or `planner`. Invalid JSON, schema violations, non-zero exits, timeouts, spawn failures, unverifiable DSH restrictions, and stale evidence all resolve to `UNKNOWN`.

Runtime enforcement:

- `ALLOW` proceeds, and compact verified facts are injected into the E prompt as `## Verified Preflight Facts`, authoritative for that attempt unless a listed hash no longer matches.
- `DENY` returns `preflight_denied`; `UNKNOWN` returns `preflight_unknown`. Neither launches E, both are `retryable=false`, and **neither charges an Executor retry budget**.
- Immediately before E launches, PC evidence is re-gathered and re-hashed. Any change invalidates the decision with `preflight_evidence_stale`.
- Supervisor repairs the finding named by `resolution_owner` and dispatches again; the next dispatch re-runs PC with fresh hashes. `runtime` means a runtime/adapter configuration fault, `supervisor` an in-session repair, `planner` a Contract revision.
- The only bypass is an explicit `preflight.enabled=false` in the user-owned `runtime.json`. It is a user decision, never a Supervisor shortcut.

Configuration:

```json
{
  "preflight": {
    "enabled": true,
    "timeout": 900,
    "include_scope_files": true,
    "include_files": ["src/example.py"],
    "max_files": 12
  }
}
```

Checker accounting is independent and stays under the PSC project runtime:

```text
<project>/runtime/preflight/latest.json
<project>/runtime/preflight/T-###-<timestamp>-<run>.json
<project>/runtime/preflight_token_usage.jsonl
<project>/runtime/preflight_token_usage_summary.json
```

Do not merge PC usage into `runtime/executor_token_usage.jsonl` or report it as Executor usage. See [`references/runtime-config.md`](references/runtime-config.md), [`references/executor-adapters.md`](references/executor-adapters.md), and [`references/runtime-protocol.md`](references/runtime-protocol.md).

---

# 2. Runtime configuration

`.agentic-sdlc/runtime.json` is the user-editable runtime configuration for the local PSC workflow. It contains configuration only; never place API keys, passwords, tokens, copied auth files, or other credentials in it.

A representative Codex Executor configuration:

```json
{
  "schema_version": 1,
  "runtime_root": "E:\\AI_Runtime",
  "project_naming": "YYYYMMDD-{requirement}",
  "mcp": {
    "python_interpreter": "F:\\Miniconda3\\envs\\psc-mcp\\python.exe"
  },
  "executor": {
    "adapter": "codex",
    "executable": "codex",
    "executor_home": "E:\\codex-executor",
    "config_source": "executor_home",
    "routing": {
      "provider": "codexzh",
      "model": "gpt-6-luna",
      "effort": "medium"
    },
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "maxTimeout": 7200,
    "smoke_timeout": 120
  }
}
```

A representative DSH Executor configuration:

```json
{
  "schema_version": 1,
  "runtime_root": "E:\\AI_Runtime",
  "project_naming": "YYYYMMDD-{requirement}",
  "mcp": {
    "python_interpreter": "F:\\Miniconda3\\envs\\psc-mcp\\python.exe"
  },
  "executor": {
    "adapter": "dsh",
    "executable": "dsh",
    "executor_home": "E:\\dsh-executor\\.dsh",
    "config_source": "executor_home",
    "routing": {
      "provider": "opencode-go",
      "model": "deepseek-v4.1-flash",
      "effort": "high"
    },
    "profile": "headless",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "maxTimeout": 7200,
    "smoke_timeout": 120
  }
}
```

For every new initialization, provider, model, and reasoning effort are explicit user-confirmed Executor settings. They are persisted under `executor.routing` and are not inferred from the Supervisor model or from `CODEX_HOME` / `DSH_HOME`.

`executor_home` remains independently managed and supplies provider definitions, endpoints, and authentication. PSC does not copy or rewrite authentication material.

See [`references/runtime-config.md`](references/runtime-config.md) for validation and compatibility rules.

---

# 3. Executor adapters

## Codex Executor

The Codex adapter launches a fresh `codex exec` process for every attempt.

Important behavior:

- Child-only `CODEX_HOME=<executor_home>`.
- Per-run provider/model/reasoning-effort routing when `executor.routing` is present.
- Structured completion output for normal tasks.
- Prompt transport through stdin to avoid oversized Windows command lines.
- Supervisor credentials/session overrides are stripped before child launch.
- Executor home configuration is fingerprinted for smoke invalidation without reading auth material.

## DSH Executor

The DSH adapter launches the selected profile in a fresh process.

Important behavior:

- Child-only `DSH_HOME=<executor_home>`.
- Required `executor.profile`, typically a non-interactive profile such as `headless`.
- Short-lived command-line `--patch` for per-run provider/model/reasoning-effort routing.
- DSH home/profile configuration is not rewritten to switch the PSC route.
- Headless `--json` completion parsing.
- Durable Session usage folding is preferred for accounting; `step_end.usage` is a fallback.
- `settings.yaml` is optional in current DSH releases. If present it participates in fingerprinting; absence is a valid, fingerprint-significant state.
- The selected profile's `package.json` and `cordis.patch.yml` remain required fingerprint inputs.

Executor adapter details are documented in [`references/executor-adapters.md`](references/executor-adapters.md).

---



# 4. Executor health and smoke

Before normal dispatch, PSC validates the configured Executor and requires a current smoke fingerprint.

Useful commands:

```text
python scripts/invoke_executor.py status \
  --repository <repository> \
  --runtime-config <repository>/.agentic-sdlc/runtime.json
```

```text
python scripts/invoke_executor.py smoke \
  --repository <repository> \
  --runtime-config <repository>/.agentic-sdlc/runtime.json
```

Smoke runs the selected harness in an isolated temporary workspace and requires an exact marker-file result.

Normal MCP dispatch also performs readiness checking. If the stored smoke is absent or stale, `psc_invoke_executor` runs the real smoke inside the MCP runtime before launching the task.

Relevant configuration changes invalidate smoke. PSC does not "align" stale fingerprints without rerunning smoke.

---

# 5. Contract model

An executable Contract is an immutable directory:

```text
contract/vN/
├─ requirements.md
├─ acceptance.md
├─ implementation.md
├─ constraints.md
├─ tasks.md
└─ metadata.json
```

Key properties:

- Stable Requirement IDs: `REQ-###`
- Stable Acceptance IDs: `AC-###`
- Stable Task IDs: `T-###`
- Explicit task dependencies.
- Explicit Allowed Scope and Forbidden Scope.
- Approved Contracts are immutable.
- A newer version is created rather than editing an old version in place.
- Supervisor executes the highest applicable Approved Contract according to workflow activation policy.

The full schema is in [`references/contract-schema.md`](references/contract-schema.md).

---

# 6. Workflow execution

A normal task cycle is:

```text
1. Supervisor reloads runtime + workflow artifacts.
2. Supervisor snapshots current PSC state.
3. Supervisor validates Contract/task ownership and retry state.
4. Supervisor calls the native PSC MCP dispatch tool.
5. MCP verifies Executor readiness/smoke.
6. MCP runs the read-only Preflight Check; a `DENY`/`UNKNOWN` blocks E without charging a retry budget.
7. Executor receives a task-scoped executor-packet.md plus the verified Preflight facts.
8. Executor edits product code and returns structured completion.
9. PSC materializes plan.md / coding.md.
10. Supervisor independently inspects diffs and required verification.
11. Supervisor commits pass / quality_rework / blocked / waiting_planner.
12. On pass, PSC advances the task and writes a resume capsule.
```

The Executor is evidence-producing construction, not an approver. Supervisor verification is independent.

At each successful task boundary PSC writes:

```text
runtime/supervisor_resume.json
runtime/resume/T-###.json
```

so a fresh Supervisor session can resume from durable artifacts without previous conversation context.

---

# 7. Retry model

Retry accounting is task-local and split into two independent budgets.

## `quality_rework`

Used when E completed an implementation but Supervisor verification rejects it for acceptance, correctness, completeness, or another implementation-quality reason.

Limit: 3 retries per task execution round.

## `abnormal_retry`

Used when an Executor attempt fails abnormally, for example:

- timeout/no return
- process failure
- spawn failure after launch
- invalid structured completion
- artifact persistence failure

Limit: 3 retries per task execution round.

The first dispatch in a round uses `retry_kind="initial"` and consumes neither retry budget.

A deterministic pre-launch transport failure such as Windows `WinError 206` / `ENAMETOOLONG` is handled separately as a non-retryable runtime failure. PSC blocks without burning normal retry budgets until the runtime/adapter is repaired.

When a budget is exhausted, the workflow stops for a user decision. Supported durable resolutions include:

- reset both task-local budgets and continue with E in a new execution round
- Supervisor takeover for only the current task, then automatic handback to E
- sticky Supervisor takeover for the current and subsequent tasks

See [`references/runtime-protocol.md`](references/runtime-protocol.md).

---

# 8. Supervisor execution ownership

`workflow_state.execution_owner` is either:

```text
executor
supervisor
```

The default is `executor`.

When ownership is `supervisor`, S may implement product code directly but must still obey:

- immutable Contract semantics
- Allowed/Forbidden Scope
- independent verification
- normal review/result artifacts

The scoped takeover mode records an automatic `return_owner=executor` instruction. After the scoped task passes and reaches the next task boundary, ownership returns to E without another user decision.

---


# 9. Executor artifacts

For task `T-001`, PSC may materialize:

```text
developing/artifacts/T-001/
├─ executor-packet.md
├─ plan.md
├─ coding.md
├─ review.md
└─ result.md
```

Ownership:

- `executor-packet.md`: invocation/runtime materialization
- `plan.md`: Executor semantic output
- `coding.md`: Executor semantic output
- `review.md`: Supervisor
- `result.md`: Supervisor, only at terminal pass boundary

The normal MCP result is intentionally compact. Large stdout/stderr stay in the Executor log, and semantic output stays in artifacts so Supervisor context does not need the full raw transcript.

---

# 10. Executor token accounting

Every real E invocation can be persisted to:

```text
runtime/executor_token_usage.jsonl
runtime/executor_token_usage_summary.json
```

The normalized usage fields are:

```text
input_tokens
uncached_input_tokens
cached_input_tokens
cache_write_input_tokens
output_tokens
reasoning_output_tokens
total_tokens
```

Reasoning output is already part of output accounting and is not added again to `total_tokens`.

Usage reporting distinguishes exact totals from lower bounds. Missing provider usage is not converted to zero.

For DSH, accounting primarily folds durable Session artifacts, including provider usage embedded in assistant message/attempt streams and retry/child attempts. Headless `step_end.usage` is used only as a fallback when durable usage is unavailable.

Report current Contract usage with:

```text
python scripts/psc_runtime.py executor-usage \
  --project <workflow-project>
```

or select a version explicitly:

```text
python scripts/psc_runtime.py executor-usage \
  --project <workflow-project> \
  --contract-version <N>
```

---

# 11. PSC-CONTRACT-BUNDLE import

`prompts/contract-export.md` is the External Planner Contract Export Prompt. It instructs an external planning session to emit a single portable `PSC-CONTRACT-BUNDLE` Markdown artifact.

Typical handoff:

```text
External Planner
      |
      v
PSC-CONTRACT-BUNDLE.md
      |
      v
Supervisor importer
      |
      v
immutable contract/vN/
      |
      v
normal PSC execution
```

Import:

```text
python scripts/psc_runtime.py import-bundle <bundle-path> \
  --repository <repository> \
  --runtime-config <repository>/.agentic-sdlc/runtime.json
```

The importer:

- copies the original Bundle for provenance
- validates metadata and stable references
- checks semantic completeness
- materializes immutable Contract files atomically
- records an import report
- never lets E parse the Bundle
- never overwrites an existing Contract version

A repository may have multiple independent PSC workflows. Use `--project-id` only for an existing workflow, or `--new-project-id` to explicitly create a new workflow.

Startup auto-import can consume exactly one pending Bundle when no usable Approved Contract exists. Multiple pending candidates require an explicit choice.

Importing a newer Approved Contract into an existing workflow does not silently activate it. Use `activate-contract` so the declared workflow policy controls restart/invalidation behavior.

---

# 12. Runtime helper commands

Contract validation:

```text
python scripts/psc_runtime.py validate-contract <contract-dir> \
  --repository <repository>
```

Discover associated workflows:

```text
python scripts/psc_runtime.py discover \
  --repository <repository> \
  --runtime-config <runtime.json>
```

Bootstrap:

```text
python scripts/psc_runtime.py bootstrap <contract-dir> \
  --repository <repository> \
  --runtime-config <runtime.json>
```

Bundle operations:

```text
python scripts/psc_runtime.py import-bundle <bundle-path> \
  --repository <repository> \
  --runtime-config <runtime.json>

python scripts/psc_runtime.py auto-import \
  --repository <repository> \
  --runtime-config <runtime.json>
```

Activate the highest valid Approved Contract:

```text
python scripts/psc_runtime.py activate-contract \
  --project <workflow-project> \
  --repository <repository>
```

Execution ownership:

```text
python scripts/psc_runtime.py set-execution-owner \
  --project <workflow-project> \
  --owner <executor|supervisor> \
  --reason "<reason>"
```

Retry exhaustion:

```text
python scripts/psc_runtime.py resolve-retry-exhaustion \
  --project <workflow-project> \
  --decision <reset-and-continue-executor|switch-to-supervisor-for-current-task|switch-to-supervisor>
```

Finish scoped Supervisor takeover:

```text
python scripts/psc_runtime.py finish-scoped-supervisor-takeover \
  --project <workflow-project> \
  --task <T-###>
```

Resume after a repaired non-retryable runtime failure:

```text
python scripts/psc_runtime.py resolve-runtime-failure \
  --project <workflow-project> \
  --reason "<repair evidence>"
```

Executor usage:

```text
python scripts/psc_runtime.py executor-usage \
  --project <workflow-project>
```

Use `--help` for optional selectors and command-specific arguments.

---

# 13. Security and isolation

PSC keeps the Supervisor, MCP runtime, and Executor environment separate.

Important invariants:

- `runtime.json` contains no credentials.
- Executor authentication remains in the selected independent Executor home.
- Supervisor authentication/session environment variables are stripped before Executor launch where applicable.
- The parent Supervisor process environment is not rewritten to become the Executor environment.
- Executor may edit product code within task scope but cannot approve itself or mutate Contract/runtime/review/result state.
- Planner does not code.
- Supervisor does not silently redesign an Approved Contract.
- A missing MCP tool is an explicit configuration failure.
- The Preflight Checker runs read-only, with no shell, filesystem writes, code editing, MCP/external tools, or dangerous tools, and from an isolated workspace outside the repository; it receives only runtime-gathered bounded evidence.
- Direct Python import is not a supported dispatch transport.
- Repository evidence, not Executor self-report, decides acceptance.

---

# 14. Testing

Install the MCP test dependency:

```text
python -m pip install pytest -r requirements-mcp.txt
```

Run the full offline suite:

```text
python -m pytest tests -q
```

GitHub Actions runs the same test suite on Python 3.11 for pushes and pull requests.

The tests cover Contract validation/import, workflow hardening, Executor adapters, MCP dispatch, retry semantics, DSH/Codex configuration behavior, token accounting, fingerprinting, the Supervisor MCP initialization/fail-closed contract, and the Preflight Checker gate (ALLOW/DENY/UNKNOWN, configuration mismatch, stale SHA evidence, E-prompt injection, scope fail-closed behavior, no retry-budget charge, and Codex/DSH read-only command and DSH patch construction). Every Preflight test is offline: checker subprocesses are mocked and no real model call is made.

---

## Further reading

- [`SKILL.md`](SKILL.md) — complete runtime behavior and operating rules.
- [`references/runtime-protocol.md`](references/runtime-protocol.md) — workflow state machine, retry/exhaustion, ownership, escalation, and resume behavior.
- [`references/runtime-config.md`](references/runtime-config.md) — `runtime.json`, MCP Python, Executor routing, smoke, and fingerprint rules.
- [`references/executor-adapters.md`](references/executor-adapters.md) — Supervisor MCP transport and Codex/DSH Executor adapter contracts.
- [`references/contract-schema.md`](references/contract-schema.md) — immutable Contract structure and validation.
- [`prompts/contract-export.md`](prompts/contract-export.md) — External Planner Bundle export format.

## Workflow Registry / GUI companion

PSC maintains a lightweight active workflow index; see references/workflow-registry.md. Windows GUI companion is developed in the separate psc-executor-monitor project (repository publication pending).
