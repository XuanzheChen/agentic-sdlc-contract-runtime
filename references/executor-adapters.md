# Executor adapter contract

The Supervisor depends on one logical operation:

```text
invoke_executor(adapter, repository, task, contract, previous_review, runtime_config)
```

An adapter translates that call to Codex CLI, Claude Code, DSH, OpenCode, or a
future harness. It returns exit status, stdout/stderr, and a durable raw log;
it must not alter PSC state semantics. The worker is launched as a fresh process
for every attempt and receives only the current task, relevant Contract
sections, constraints, implementation recommendation, and previous Supervisor
review. It must not receive Planner or prior Executor conversation history.

The worker may inspect the repository, edit only the task's allowed scope, add
tests, and write `developing/artifacts/T-###/plan.md` and `coding.md`. It must
not edit Contract versions, workflow state, reviews, results, or runtime
configuration, and it cannot approve its own work. The adapter should enforce
allowed/forbidden paths where the harness supports it and report violations.

Keep credentials in the configured Executor environment. Never copy, print,
serialize, or place secrets in `runtime.json`, task prompts, logs, or artifacts.
Harness-specific flags and authentication paths stay inside the adapter.

## Codex adapter

`scripts/invoke_executor.py` owns process invocation; `scripts/adapters/codex.py` only constructs the Codex CLI argv. It uses current non-interactive `codex exec` behavior with global `--model`, `--sandbox`, and `--ask-for-approval` flags before `exec`, plus per-run config overrides for provider and reasoning effort. It never edits Executor-home configuration. The child environment is copied from the Supervisor and receives only `CODEX_HOME=<configured executor_home>`; the parent environment is never mutated.

The invocation layer reloads runtime configuration, checks static health and a matching smoke fingerprint, captures redacted stdout/stderr, writes a raw log, applies the configured timeout, and returns a deterministic result. It records Git baseline and post-run changed paths; paths outside task Allowed Scope or in Forbidden Scope are returned as `scope_violation` for Supervisor handling. It does not decide acceptance, edit Contract/Requirement/review/state artifacts, or fall back to another harness.
