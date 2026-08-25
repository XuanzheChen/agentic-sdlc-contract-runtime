# PSC runtime.json

Create `.agentic-sdlc/runtime.json` only in Supervisor/local-runtime mode. It
is the one user-editable runtime configuration and is reloaded from disk on
every execution. Do not write it in Planner mode unless the user also asks to
initialize a local Supervisor workspace.

Use this shape, replacing example values after asking the user once:

```json
{
  "schema_version": 1,
  "runtime_root": "E:\\AI_Runtime",
  "project_naming": "YYYYMMDD-{requirement}",
  "executor": {
    "adapter": "codex",
    "executor_home": "E:\\codex-executor",
    "provider": "ccodezh",
    "model": "gpt-5.6-terra",
    "effort": "medium",
    "approval": "auto",
    "sandbox": "workspace-write",
    "timeout": 1800
  }
}
```

The ten values to collect are runtime root, naming rule, adapter/harness,
Executor home (when applicable), provider, model, reasoning effort, approval
mode, sandbox mode, and timeout. Missing values may be requested again; do not
re-ask values that are present. Never place API keys, passwords, tokens,
cookies, private keys, or other credentials in this file. Authentication is
owned by the selected Executor environment and must not be copied or exposed.
