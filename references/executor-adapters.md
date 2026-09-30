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
result. It records Git baseline and post-run changed paths; paths outside task
Allowed Scope or in Forbidden Scope are returned as `scope_violation` for
Supervisor handling. It does not decide acceptance, edit Contract/Requirement/
review/state artifacts, or fall back to another harness.

## Usage accounting

Every invocation returns a normalized `usage` object when metering data is
available and writes the same object into the raw Executor log. The normalized
schema separates `uncached_input_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `output_tokens`, and `reasoning_tokens`, and derives
`input_tokens` and `total_tokens`. Harness-reported input/total values are
also retained when present because some CLIs display totals that exclude cache
traffic.

For Codex, PSC parses the captured process output on a best-effort basis. For
DSH, PSC uses the harness's durable session projection under
`<executor_home>/storages/session_projcache/sessions/*.json`: it snapshots
`rows.tokenUsage.totals` before and after the child process, then sums positive
per-session deltas for uncached input, cache read, cache write, and output.
DSH's aggregate token projection does not expose reasoning as a separate total,
so that field is `null`. If the projection is unavailable, PSC falls back to
the same process-output parser.

The DSH projection is derived cache state, not the authoritative session log;
a missing or stale record therefore makes usage unavailable rather than making
the Executor attempt fail. The delta also assumes the configured DSH home is
dedicated to this Executor. Concurrent DSH activity sharing that home can be
included in the measured delta.
