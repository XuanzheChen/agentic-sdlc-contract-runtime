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
  "executor": {
    "adapter": "codex",
    "executable": "codex",
    "executor_home": "E:\\codex-executor",
    "provider": "ccodezh",
    "model": "gpt-5.6-terra",
    "effort": "medium",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
```

Initialization is one explicit wizard. Collect Runtime Root, Project Naming
Rule, Executor Adapter, Executor Executable, Executor Home, Provider, Model,
Reasoning Effort, Approval Policy, Sandbox Mode, Timeout, and Smoke Timeout.
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