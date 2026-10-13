# Claude Code Executor (independent Home)

Claude Code is supported as an **Executor** adapter (`executor.adapter=claude`).
This does not add a Claude Code **Supervisor** MCP bootstrap. Existing Codex
and DSH Supervisors dispatch through the same PSC MCP methods and retry,
acceptance, smoke, scope and Preflight boundaries.

## Initialize an isolated configuration

Create an independent directory **outside the repository** and outside the
interactive/CCSwitch-managed `~/.claude` or `CLAUDE_CONFIG_DIR`:

```powershell
New-Item -ItemType Directory -Force 'E:\claude-executor' | Out-Null
@'
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://your-claude-compatible-provider.example/v1",
    "ANTHROPIC_AUTH_TOKEN": "YOUR_TOKEN"
  },
  "sandbox": {
    "enabled": true
  }
}
'@ | Set-Content 'E:\claude-executor\settings.json' -Encoding utf8
```

The example is a **template**, not a working provider endpoint. Configure your
own CC-compatible provider and credential; prefer an OS-secured local settings
file or provider-supported credential mechanism. The URL and token must **not**
be placed in `.agentic-sdlc/runtime.json`, the PSC source tree, or Git. If a
provider uses other environment names, put them in this isolated home's
`settings.json` `env` map. Do not copy the global CCSwitch profile or secrets
automatically. This home remains your responsibility to provision.

For a manual probe without changing global configuration:

```powershell
$env:CLAUDE_CONFIG_DIR = 'E:\claude-executor'
claude auth status
claude -p --model YOUR_MODEL_ID --effort high 'Reply OK'
Remove-Item Env:CLAUDE_CONFIG_DIR
```

## PSC runtime.json Executor block

```json
{
  "adapter": "claude",
  "executable": "claude",
  "executor_home": "E:\\claude-executor",
  "config_source": "executor_home",
  "routing": {
    "provider": "independent-home",
    "model": "YOUR_MODEL_ID",
    "effort": "high"
  },
  "approval_policy": "never",
  "sandbox": "workspace-write",
  "timeout": 1800,
  "maxTimeout": 7200,
  "smoke_timeout": 120
}
```

`routing.model` and `routing.effort` become the Claude Code CLI
`--model` / `--effort` arguments. `routing.provider` is an **audit label only**
for Claude: the provider endpoint and credential **always** come from the
independent home's settings. Claude requires `config_source=executor_home`
and an explicit route. It never reads the CCSwitch profile to infer routing.

ASCR sets `CLAUDE_CONFIG_DIR` **only in the spawned child**. The child scrubs
inherited `ANTHROPIC_*`, `CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR` and related
model selection variables before setting its home. The Supervisor's
environment remains untouched. User settings are loaded from that home;
project/local Claude settings are excluded via `--setting-sources user`.
Project instructions/CLAUDE.md, managed organization policy and OS-level
environment or permissions may still apply: this is configuration separation,
**not a security container**.

Every Executor starts an independent `claude -p` call with the full PSC task
on **stdin**, streaming JSON on stdout and `--no-session-persistence`.
The PSC Completion schema uses `--json-schema`. No sessions or auth are copied
between Supervisor and Executor. Smoke must be fresh after home settings
changes.

## Permissions and Preflight

Claude permissions are different from Codex permissions:

- `sandbox=read-only`: `--permission-mode dontAsk` and read-oriented tools
  (`Read,Glob,Grep`), without automatically permitted edits.
- `sandbox=workspace-write` on macOS/Linux/WSL2: `acceptEdits` plus
  an explicitly enabled Claude shell sandbox. PSC forces strict failure
  when sandboxing is unavailable.
- `sandbox=workspace-write` on **native Windows**: native Claude shell
  sandbox is unavailable. PSC requires `claude_native_windows_mode`:
  - `edit-only`: file tools (`Read,Glob,Grep,Edit,Write`), **no shell
    commands/tests** in the Executor.
  - `trusted-shell`: normal editing plus automatically approved Bash for
    build/test work, with the ordinary Windows account's permissions.
    This is **not an OS sandbox**, and the agent can run commands that
    affect files or networks outside the project.
  Both options preserve the independent Claude Home, provider mapping,
  Smoke, restricted Preflight, timeout and PSC scope audit. Claude Code's
  unsupported native Windows shell sandbox is disabled for those runs.
- `sandbox=danger-full-access`: `bypassPermissions` (more permissive);
  **not required** for native Windows support.

For native Windows full coding without WSL2 or any project move, explicitly
add to the `executor` block:

```json
"claude_native_windows_mode": "trusted-shell"
```

This is an opt-in. The `edit-only` alternative avoids autonomous shell
execution. The independent Home can use `"sandbox":{"enabled":false}`
on native Windows. The runtime disables inherited MCP tools for Executor
runs via `--strict-mcp-config` and `--disallowedTools mcp__*`.
`acceptEdits` is a Claude permission mode, not a filesystem sandbox.
PSC scope auditing after the run cannot stop out-of-project shell activity.
For stronger protection without WSL2, a separate low-privilege Windows user
with NTFS ACL restrictions is a possible **optional** future hardening layer.


Preflight always uses the **same isolated model/effort/provider route**, but
forces `--safe-mode`, `--tools ""`, `--disallowedTools "*"`,
`--strict-mcp-config`, `--disable-slash-commands` and `dontAsk`.
It starts from PSC's external, empty Checker cwd and receives bounded
evidence via stdin. This disables Claude tools and customizations independently
from Executor permissions. A CLI/version/provider that cannot support these
flags must fail closed; it must never silently run an unrestricted Checker.
Managed policy behavior is governed by the installed Claude Code version.

## Accounting and verification

The adapter parses Claude `stream-json`. It prefers terminal `result.modelUsage`
(camelCase, grouped by model) because that covers **main Executor plus Subagents**.
If absent, it uses terminal `result.usage` (snake_case, **main Agent only**) and
marks `usage_scope=main_agent_only`, `subagents_included=false` and
whole-run `exact=false`. Never sum `modelUsage`, terminal `usage`, and assistant
events together. Anthropic-compatible usage semantics are:

```text
PSC input_tokens = input_tokens (uncached)
                 + cache_read_input_tokens
                 + cache_creation_input_tokens
PSC cached_input_tokens = cache_read_input_tokens
PSC cache_write_input_tokens = cache_creation_input_tokens
PSC output_tokens = output_tokens
```

If the terminal aggregate is missing, completed assistant events may provide
a lower bound; otherwise usage remains unavailable. Claude does not expose a
separate reasoning-output count, so reporting marks usage `exact=false`,
`reasoning_tokens_available=false` rather than pretending that a zero count
was measured. Run `psc_executor_smoke` (or the existing readiness API)
before initial live dispatch; the CI tests use simulated responses only.

Official docs: https://code.claude.com/docs/en/cli-reference ,
https://code.claude.com/docs/en/env-vars and
https://code.claude.com/docs/en/sandboxing .
