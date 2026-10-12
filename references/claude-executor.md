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
- `sandbox=workspace-write`: `--permission-mode acceptEdits`, with an
  **explicit prerequisite** that `<executor_home>/settings.json` contains
  `"sandbox": {"enabled": true}`. Without it the static probe fails.
  `acceptEdits` alone is not a sandbox. PSC also forces
  `sandbox.failIfUnavailable=true` and `allowUnsandboxedCommands=false`
  for this mode. On **native Windows**, Claude Code does not support its
  shell sandbox, so PSC's static probe rejects `workspace-write`.
  Use WSL2/Linux/macOS for this mode, or choose the explicitly unsafe
  `danger-full-access` setting if you knowingly accept native Windows
  execution without an OS sandbox.
- `sandbox=danger-full-access`: `--permission-mode bypassPermissions`;
  an explicitly selected unsafe/full-access mode only.

`approval_policy=never` is the normal noninteractive PSC setting, but Claude
has no direct `never + workspace-write` Codex-equivalent CLI pair. PSC uses
the mappings above. Do **not** infer that scope auditing after the process
finishes can stop edits to out-of-repository files. Run high-risk tasks in an
OS-level sandbox or container. Specifically on native Windows, select
`danger-full-access` **only with explicit user acknowledgement** if you
intend unattended code editing, or run Claude Code in WSL2 for
`workspace-write`. The default example uses `workspace-write` to document
the safer supported configuration, not a native Windows default.

Preflight always uses the **same isolated model/effort/provider route**, but
forces `--safe-mode`, `--tools ""`, `--disallowedTools "*"`,
`--strict-mcp-config`, `--disable-slash-commands` and `dontAsk`.
It starts from PSC's external, empty Checker cwd and receives bounded
evidence via stdin. This disables Claude tools and customizations independently
from Executor permissions. A CLI/version/provider that cannot support these
flags must fail closed; it must never silently run an unrestricted Checker.
Managed policy behavior is governed by the installed Claude Code version.

## Accounting and verification

The adapter parses Claude `stream-json`: the terminal `result.usage` is
authoritative when present, instead of summing it again with per-assistant
events. Anthropic-compatible usage semantics are:

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
