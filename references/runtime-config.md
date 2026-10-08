# PSC runtime.json

Create `.agentic-sdlc/runtime.json` only in Supervisor/local-runtime mode. It
is the one user-editable runtime configuration and is reloaded from disk on
every execution. Do not write it in Planner mode unless the user also asks to
initialize a local Supervisor workspace.

Use this shape, replacing example values only after explicitly asking the user:

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

Set `config_source` to `executor_home` when provider definitions,
endpoints, credentials, and other harness defaults live in the independent
Executor home. New initializations must still collect an explicit
`executor.routing` selection with `provider`, `model`, and `effort`.
That routing is a per-PSC-run override and does not edit the Executor home.
Legacy schema-version-1 configurations without `executor.routing` remain
supported: Codex continues to inherit provider/model/effort when
`config_source=executor_home`, while old runtime-sourced top-level
`provider`/`model`/`effort` remain valid. The Runtime still owns approval
and sandbox flags. A readable Executor-home configuration remains part of the
non-sensitive fingerprint when `config_source=executor_home`; authentication
files are never read for identity.

Initialization uses two deliberately separate configuration layers.

First select the PSC **MCP Python Runtime**. It is infrastructure for the local
MCP transport and must be recorded in `runtime.json` as the exact
`mcp.python_interpreter` path. It remains separate from the Executor and
product Python runtimes. Do not infer it from the
currently activated project environment, IDE interpreter, repository virtualenv,
or conda environment. Probe a candidate with
`scripts/probe_mcp_runtime.py --python <candidate> --repository <repository>`
(and `--project-python <path>` when the project interpreter is known). Reject
the candidate if it is the project interpreter, lives inside the product
repository, is older than Python 3.10, cannot import SSL, or lacks pip. If the
candidate is otherwise valid but lacks `mcp>=2,<3`, install MCP only into that
explicitly selected independent runtime. Never repair or mutate the project
Python to satisfy PSC infrastructure dependencies.

Use the selected interpreter's exact path both as the active Supervisor MCP
server `command` and as `runtime.json.mcp.python_interpreter`. Supervisor
registration is harness-specific: Codex uses
`mcp_servers.agentic_sdlc_executor`; DSH uses an
`@deepseek-ai/dsh-mcp-client` Cordis row with
`serverName: agentic_sdlc_executor`. These registrations and
`runtime.json.mcp.python_interpreter` must point at the same selected PSC MCP
Python. The Supervisor harness is independent of `executor.adapter`.

For backward compatibility, an existing schema-version-1 `runtime.json`
without `mcp` remains valid. Do not silently invent a path for it. Once the
user confirms/selects the MCP Python for that workspace, add the `mcp` block
and persist the exact interpreter path.

Before the Executor/runtime layer, initialize the current **Supervisor MCP
client**. For Codex, preserve/add the PSC MCP server and the direct-only
namespace entry. For DSH, run
`scripts/configure_supervisor_mcp.py configure --mcp-python <path> --scope home`.
The DSH bootstrapper writes an `- insert:` patch to the home-level
`$DSH_HOME/cordis.patch.yml`, preserves unrelated rows, and migrates the
legacy top-level `- id: mcp-agentic-sdlc-executor` shape that only targets an
existing row and therefore does not insert the PSC MCP client. DSH Desktop's
reserved `profiles/desktop` is never mutated directly. After either harness
configuration changes, restart/refresh the Supervisor and verify the expected
PSC tool is actually visible. Missing exposure is a configuration failure;
normal execution must not invoke `invoke_executor.py` or import
`psc_mcp_server.py` as a fallback.

Second, initialize the Executor/runtime layer. Collect Runtime Root, Project
Naming Rule, Executor Adapter, Executor Executable, Executor Home, and Config
Source (`runtime` or `executor_home`) first. For every new initialization,
explicitly ask the user to select the Executor Provider, Model, and Reasoning
Effort; Model and Reasoning Effort are mandatory user-confirmed initialization
parameters and must never be inferred from CODEX_HOME, DSH_HOME, the Supervisor,
or a previous project. Persist the three values under `executor.routing`.
When `config_source=executor_home`, the home still supplies provider
definitions/endpoints/credentials and must be readable, but the per-run routing
selection overrides its default model route without rewriting the home.
Record the selected MCP Python Runtime path in
`mcp.python_interpreter`. Finally collect Approval Policy, Sandbox Mode,
Timeout, Max Timeout (`maxTimeout`), and Smoke Timeout. `maxTimeout` must be a positive integer
greater than or equal to `timeout`. Existing runtime files without
`maxTimeout` remain valid and keep fixed-timeout behavior until the user adds
the field.
Missing or invalid values produce `configuration_required`; they are never
inferred from the Supervisor model, provider, `CODEX_HOME`, project `.codex/`,
global Codex configuration, IDE, or conversation history. Existing valid values
are not re-asked.

`executor_home` is independently managed. The Runtime never creates, copies, or
overwrites its `config.toml`, `auth.json`, or authentication material. It sets
`CODEX_HOME` only in the Executor child process. The effective Supervisor Codex
home is `$CODEX_HOME` when set, otherwise `~/.codex` (resolved from the current
user home, including on Windows). If `executor_home` equals that home or a
repository Codex home, explicit user confirmation must be recorded as
`allow_shared_executor_home: true`.

For a disposable non-interactive Codex Executor, use `approval_policy: never`
with `sandbox: workspace-write`. `never` does not grant full access; sandbox
remains the boundary. `danger-full-access` is advanced and must be explicitly
selected. `on-request` is supported for supervised work but can block
`codex exec`. Optional `approvals_reviewer: auto_review` is distinct from
approval policy and requires `approval_policy: on-request`,
`sandbox: workspace-write`, and a Codex CLI that supports `--approve-for-me`.
That mode uses its dedicated CLI behavior rather than combining conflicting
approval/sandbox flags. Legacy ambiguous `approval: auto` requires an explicit
choice; it is never silently converted. Never place credentials in this file.

## DSH adapter

For `adapter: dsh`, set `executor_home` to the independently managed DSH
home and set `profile` to an existing profile name such as `headless`. New
initializations also write `executor.routing.provider`,
`executor.routing.model`, and `executor.routing.effort`. PSC generates a
short-lived command-line `--patch` that overrides the DSH
`agent-default-model` row for that invocation, so changing the PSC Executor
model or effort does not modify `DSH_HOME/settings.yaml` or the profile.
Command-line overlays outrank the profile/home settings layers. The same
short-lived patch disables automatic session-title LLM calls so they cannot
escape Executor metering. DSH is invoked with `--json`; the final event is used
for structured completion and `step_end.usage` is an accounting fallback.

DSH Executor tuning can optionally be scoped to PSC without editing the
persistent profile. The currently supported tuning block is:

```json
"dsh_tuning": {
  "tool_result_pruner": {
    "enabled": true,
    "thresholdChars": 4096,
    "headChars": 2048,
    "tailChars": 512
  }
}
```

When `enabled` is true, PSC adds a `tool-result-pruner` override to the same
short-lived `--patch`. When the block is absent or `enabled` is false, PSC
leaves the profile's existing pruner behavior untouched; false does not disable
DSH's built-in pruner. The three character limits must be positive integers.
PSC mirrors DSH's emitted replacement budget: `headChars` + the fixed
`\n\n[... tool result middle pruned ...]\n\n` marker (39 Unicode code points) +
`tailChars` must be at most `thresholdChars`. DSH tuning is part of the
Executor configuration fingerprint, so changing it invalidates a prior smoke
and requires a fresh readiness smoke before dispatch.
The primary invocation ledger still folds changed durable Session artifacts so
child/retry attempts are included. Credentials are never copied into
`runtime.json`.

Current DSH releases do not require `DSH_HOME/settings.yaml` to exist. PSC
therefore treats that file as an optional fingerprint input: if it exists, its
contents are hashed; if it is absent, fingerprint creation and static probing
remain valid. The missing/present state itself is fingerprint-significant, so
creating or removing `settings.yaml` invalidates an old smoke result. The
selected profile's `package.json` and `cordis.patch.yml` remain required
fingerprint inputs.


## Adaptive normal-task timeout

For a normal Executor task, any `subprocess.TimeoutExpired` result is treated
as evidence that the configured time budget was insufficient. The Executor may
produce no stdout/stderr, semantic artifacts, or repository changes before the
deadline and still simply be running slowly. The invocation layer therefore
atomically writes:

```text
executor.timeout = min(executor.timeout * 2, executor.maxTimeout)
```

This growth applies only after a normal Executor process was launched and hit
its runtime deadline. Pre-launch/input/configuration/spawn failures do not
change `timeout`. Smoke uses `smoke_timeout` and never changes the normal
timeout.

## Executor retry budgets

Retry accounting is durable per Contract version and Task ID in
`runtime/executor_attempts.json` and uses two independent budgets:

- `quality_rework`: up to three retries after Supervisor rejects a completed
  implementation for quality/acceptance reasons.
- `abnormal_retry`: up to three retries after Executor/runtime abnormal
  failure, including timeout/no return, process failure, invalid completion, or
  artifact persistence failure.

The first dispatch uses `retry_kind="initial"` and does not consume either
budget. Every later dispatch must explicitly use `quality_rework` or
`abnormal_retry`; MCP refuses a second `initial` dispatch rather than
guessing. If a quality-rework dispatch itself ends abnormally, only the abnormal
budget is charged, preserving the quality opportunity.

The durable file uses schema version 2 and stores
`initial_attempted`, `quality_retries_used`, and
`abnormal_retries_used` for each `vN:T-###` key. Legacy schema-v1 aggregate
attempt counts are retained as unclassified audit data and are not charged to
either new budget because their historical cause cannot be reconstructed
safely.

Exhausting either budget causes MCP to return `status: retry_limit_reached`
with reason `quality_rework_limit_reached` or
`executor_abnormal_retry_limit_reached`. Supervisor then blocks the workflow
and informs the user which independent budget was exhausted.
