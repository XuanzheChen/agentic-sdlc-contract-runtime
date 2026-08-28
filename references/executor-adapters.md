# Executor adapter contract

The Supervisor depends on one logical operation:

```text
invoke_executor(adapter, repository, task, contract, previous_review, runtime_config, *, project)
```

`project` is Supervisor-runtime context used by the invocation layer to resolve
`developing/artifacts/T-###/`; it is not a harness-specific adapter parameter.
An adapter translates that call to Codex CLI, Claude Code, DSH, OpenCode, or a
future harness. It returns exit status, stdout/stderr, and a durable raw log;
it must not alter PSC state semantics. The worker is launched as a fresh process
for every attempt and receives only the current task, relevant Contract
sections, constraints, implementation recommendation, and previous Supervisor
review. It must not receive Planner or prior Executor conversation history.

The worker may inspect the repository, edit only the task's allowed scope, and
add tests. For a normal task it returns a strict structured completion object
with `plan`, `coding_summary`, `modified_files`, `tests`, `known_risks`, and
`unresolved_issues`. `scripts/invoke_executor.py` faithfully materializes that
Executor-owned content as `developing/artifacts/T-###/plan.md` and `coding.md`.
The Executor never writes those runtime-root paths directly, and invalid output
or a failed process creates no successful task artifacts. It must not edit
Contract versions, workflow state, reviews, results, or runtime configuration,
and it cannot approve its own work. The invocation layer should enforce
allowed/forbidden paths where the harness supports it and report violations.

Keep credentials in the configured Executor environment. Never copy, print,
serialize, or place secrets in `runtime.json`, task prompts, logs, or artifacts.
Harness-specific flags and authentication paths stay inside the adapter.

## Supervisor transport

Normal Supervisor dispatch reaches this adapter through the local blocking MCP
tool `psc_invoke_executor`. The MCP server is only a transport wrapper around
the existing filesystem entrypoint and `invoke_executor()`; it does not own PSC
state semantics or Executor configuration.

A normal Supervisor must expose the namespace
`mcp__agentic_sdlc_executor` as a direct model tool by adding it to
`[features.code_mode].direct_only_tool_namespaces`. This prevents a
long-running MCP request from being wrapped in a Code Mode background cell.

Normal dispatch must not call the MCP tool through `functions.exec`, a
JavaScript cell, `exec_command`, or any other polling host, and must not use
`wait` or `write_stdin` for Executor lifecycle management. Long Executor
waiting belongs inside one direct MCP `tools/call` request. If direct exposure
is unavailable in the current session, normal dispatch fails closed until the
Codex configuration/tool inventory is refreshed. The CLI invoke command remains
supported for humans, debugging, CI, and recovery.

The MCP response deliberately omits raw stdout/stderr and the full completion
payload. Raw process output stays in the executor log and semantic completion
content is persisted as task artifacts, so the Supervisor can retrieve only the
evidence required for review.

## Codex adapter

`scripts/invoke_executor.py` owns process invocation;
`scripts/adapters/codex.py` only constructs the Codex CLI argv. With
`config_source: runtime`, ordinary Codex runs use non-interactive `codex exec`
with global `--model`, `--sandbox`, and `--ask-for-approval` flags before
`exec`, plus per-run config overrides for provider and reasoning effort. With
`config_source: executor_home`, the adapter omits `--model` and all provider/
reasoning-effort `--config` overrides, leaving `<executor_home>/config.toml` as
the source of truth. Structured normal dispatch also uses the current Codex
`exec --output-schema` option, while Smoke uses the same adapter without a task
completion schema. When `approvals_reviewer: auto_review` is configured,
the adapter verifies `--approve-for-me` support and uses that dedicated global
mode without passing `--ask-for-approval` or `--sandbox`; unsupported CLIs fail
closed. It never edits Executor-home configuration.

The child environment is copied from the Supervisor and receives only
`CODEX_HOME=<configured executor_home>`; the parent environment is never
mutated. The invocation layer reloads runtime configuration, checks static
health and a matching smoke fingerprint, captures redacted stdout/stderr,
writes a raw log, applies the configured timeout, and returns a deterministic
result. It records a content/index fingerprint for every dirty tracked or untracked
path before and after the Executor. This detects files modified during the
attempt even when those paths were already dirty before dispatch. Paths outside
task Allowed Scope or in Forbidden Scope are returned as `scope_violation` for
Supervisor handling. It does not decide acceptance, edit Contract/Requirement/
review/state artifacts, or fall back to another harness.


## DSH completion framing

DSH-backed models are still required to produce the exact PSC completion schema,
but the transport tolerates framing noise that DSH cannot reliably suppress.
The parser first tries strict whole-stdout JSON. For DSH only, if that fails, it
scans stdout and accepts the **last** JSON object that independently satisfies
the complete PSC schema. Prose and Markdown fences around that object are
ignored; partial, malformed, or schema-incompatible JSON remains a failure.
Codex output-schema dispatch remains strict whole-response JSON.
