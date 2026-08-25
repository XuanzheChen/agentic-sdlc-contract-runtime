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
