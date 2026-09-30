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
tool `psc_invoke_executor`. The MCP server keeps `psc_invoke_executor` as the blocking transport wrapper
around the existing filesystem entrypoint and `invoke_executor()`, and also
exposes deterministic Supervisor snapshot/transition/readiness operations. State
mutation semantics live in the bundled runtime helpers; Executor configuration
remains owned by `runtime.json` and the independent Executor environment.

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
`scripts/adapters/codex.py` only constructs the Codex CLI argv. New PSC
initializations persist an explicit `executor.routing` selection containing
provider, model, and effort. When that object is present, ordinary Codex runs use
non-interactive `codex exec` with per-run `--model`, provider, and reasoning
effort overrides regardless of `config_source`; `CODEX_HOME` still supplies
provider definitions/endpoints and authentication and is never rewritten.
Legacy configurations without `executor.routing` preserve the prior behavior:
`config_source: runtime` uses top-level provider/model/effort overrides and
`config_source: executor_home` inherits the home defaults. Structured normal
dispatch also uses the current Codex
`exec --output-schema` option, while Smoke uses the same adapter without a task
completion schema. Before normal dispatch, PSC materializes a task-scoped
`executor-packet.md` so E receives referenced Requirement/Acceptance sections,
relevant implementation guidance, and global constraints rather than the entire
Contract. When `approvals_reviewer: auto_review` is configured,
the adapter verifies `--approve-for-me` support and uses that dedicated global
mode without passing `--ask-for-approval` or `--sandbox`; unsupported CLIs fail
closed. It never edits Executor-home configuration.

The child starts from ordinary OS/process environment needed for execution, but
Supervisor authentication/session variables are removed before launch. The
invocation layer then sets `CODEX_HOME=<configured executor_home>` (or DSH home);
the parent environment is never mutated. The invocation layer reloads runtime configuration, checks static
health and a matching smoke fingerprint, captures redacted stdout/stderr,
writes a raw log, applies the configured timeout, and returns a deterministic
result. It records a content/index fingerprint for every dirty tracked or untracked
path before and after the Executor. This detects files modified during the
attempt even when those paths were already dirty before dispatch. Paths outside
task Allowed Scope or in Forbidden Scope are returned as `scope_violation` for
Supervisor handling. It does not decide acceptance, edit Contract/Requirement/
review/state artifacts, or fall back to another harness.


## DSH adapter and completion framing

DSH runs use the selected profile from the independent `DSH_HOME` plus a
short-lived command-line `--patch`. When `executor.routing` is present, that
overlay replaces the `agent-default-model` row for the invocation with the
user-selected provider, model, and `reasoningEffort`; it also disables the
automatic session-title LLM call so auxiliary title generation is not left
outside Executor metering. The overlay is deleted after the process exits and
never edits `settings.yaml` or the profile.

DSH is launched with headless `--json`. The terminal `final.text` is the
completion payload used by PSC. For backward compatibility with older/custom
DSH launchers that do not emit the headless event stream, the DSH completion
parser can still accept the last JSON object that independently satisfies the
complete PSC schema when stdout contains prose or Markdown framing.

Provider accounting primarily folds changed durable DSH Session artifacts.
Current format-v2 `assistant/message` and `assistant/attempt` settlements may
carry usage only inside their embedded compact `stream`; failed or retried
`assistant/attempt` usage is billable and must be counted. Append-only growth
of an existing `.jsonl` or multi-frame `.jsonl.zstd` artifact is attributed
from the pre-invocation byte boundary instead of being ignored. If no durable
provider usage can be recovered, headless `step_end.usage` is used as a
root-Agent fallback. Missing provider usage is reported unavailable/inexact,
never as zero.
