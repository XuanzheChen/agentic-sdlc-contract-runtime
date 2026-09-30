# Agentic SDLC Contract-Driven Runtime (PSC)

**English** | [简体中文](README.zh-CN.md)

`agentic-sdlc-contract-runtime` is a portable, artifact-first runtime for Contract-Driven Agentic SDLC workflows. It separates planning, supervision, and implementation into durable roles:

- **Planner (P)** produces an immutable, versioned Contract.
- **Supervisor (S)** owns workflow state, dispatch, verification, retries, escalation, and acceptance.
- **Executor (E)** is a disposable coding worker launched through a harness adapter.

The repository is designed so a workflow can be resumed from files on disk without relying on prior chat history. The same runtime can use either **Codex Supervisor** or **DeepSeek Harness (DSH) Supervisor**, and either Supervisor can dispatch a supported Codex or DSH Executor.

## Core properties

- Immutable `contract/vN/` execution Contracts with stable `REQ-###`, `AC-###`, and `T-###` identifiers.
- Durable workflow state under `runtime/`, including task ownership, retries, escalation, resume capsules, and Executor usage accounting.
- Blocking local MCP transport for normal Supervisor-to-Executor dispatch.
- Independent Codex and DSH Supervisor MCP initialization paths.
- Codex and DSH Executor adapters with isolated homes and per-run provider/model/effort routing.
- Independent `quality_rework` and `abnormal_retry` budgets.
- Explicit Supervisor takeover / handback semantics.
- Real Executor smoke tests and configuration fingerprints before normal dispatch.
- Portable `PSC-CONTRACT-BUNDLE` import with provenance, validation, idempotency, and immutable materialization.
- Durable Executor token accounting; missing provider usage is reported as unavailable/inexact rather than silently treated as zero.
- Fail-closed execution boundaries: if the required Supervisor MCP tool is unavailable, PSC does not silently run E through a shell or direct Python import.

The authoritative behavioral specification is [`SKILL.md`](SKILL.md). Detailed contracts live under [`references/`](references/).

---

## Architecture

```text
External Planner / Planner session
            |
            | Contract or PSC-CONTRACT-BUNDLE
            v
    immutable contract/vN/
            |
            v
       Supervisor (S)
       /            \
 Codex Supervisor   DSH Supervisor
       \            /
        \  PSC MCP /
         v        v
 scripts/psc_mcp_server.py
            |
            v
 scripts/invoke_executor.py
            |
       adapter boundary
        /          \
     Codex E       DSH E
            |
            v
      product repository

Durable control/evidence:
- runtime/workflow_state.json
- runtime/executor_attempts.json
- runtime/executor_token_usage.jsonl
- runtime/supervisor_resume.json
- developing/tasks/T-###.md
- developing/artifacts/T-###/{executor-packet.md,plan.md,coding.md,review.md,result.md}
```

Supervisor harness and Executor adapter are independent choices. For example, a DSH Supervisor may dispatch a Codex Executor, and a Codex Supervisor may dispatch a DSH Executor.

---

## Repository layout

```text
.
├─ SKILL.md
├─ prompts/
│  └─ contract-export.md
├─ references/
│  ├─ contract-schema.md
│  ├─ executor-adapters.md
│  ├─ planner-contract.md
│  ├─ runtime-config.md
│  └─ runtime-protocol.md
├─ scripts/
│  ├─ adapters/
│  │  ├─ codex.py
│  │  └─ dsh.py
│  ├─ executor_token_usage.py
│  ├─ invoke_executor.py
│  ├─ probe_mcp_runtime.py
│  ├─ psc_mcp_server.py
│  ├─ psc_runtime.py
│  └─ supervisor_runtime.py
├─ requirements-mcp.txt
└─ tests/
```

A bootstrapped workflow project contains its own immutable Contract versions, developing task artifacts, and runtime state. Conversation history is not workflow state.

---

## Installing the Skill

Place this repository where the Supervisor can discover local Skills, for example as a repository-local skill:

```text
<product-repository>/.agents/skills/agentic-sdlc-contract-runtime/
```

Then invoke it explicitly, for example:

```text
Use $agentic-sdlc-contract-runtime to resume or start the PSC workflow.
```

The Skill is intentionally not a replacement for ordinary small coding tasks. It is meant for work that benefits from explicit contracts, independent verification, durable retries, and resumable multi-step execution.

---

# 1. Supervisor MCP setup

Normal Executor dispatch is MCP-only. Both supported Supervisor harnesses run the same local stdio MCP server:

```text
scripts/psc_mcp_server.py
```

That server exposes:

- `psc_supervisor_snapshot`
- `psc_ensure_executor_ready`
- `psc_invoke_executor`
- `psc_commit_supervisor_transition`

The visible tool name differs by Supervisor harness.

## 1.1 Select an independent MCP Python

PSC infrastructure should not be installed into the product project's Python environment merely to make the workflow run.

Probe an explicitly selected interpreter:

```text
python scripts/probe_mcp_runtime.py \
  --python <candidate-python> \
  --repository <product-repository>
```

When the project interpreter is known:

```text
python scripts/probe_mcp_runtime.py \
  --python <candidate-python> \
  --repository <product-repository> \
  --project-python <project-python>
```

A usable MCP Python must be:

- Python 3.10 or newer.
- Outside the product repository.
- Different from the known product interpreter.
- Able to import SSL/OpenSSL.
- Able to run pip.
- Able to import `mcp.server.MCPServer`.

If the interpreter is otherwise healthy but the MCP SDK is missing:

```text
<candidate-python> -m pip install -r requirements-mcp.txt
```

Persist the exact selected path as:

```json
{
  "mcp": {
    "python_interpreter": "<absolute-python-path>"
  }
}
```

in `.agentic-sdlc/runtime.json`.

## 1.2 Codex Supervisor

Register the PSC MCP server in the effective Codex configuration:

```toml
[mcp_servers.agentic_sdlc_executor]
command = "F:/Miniconda3/envs/psc-mcp/python.exe"
args = ["E:/path/to/agentic-sdlc-contract-runtime/scripts/psc_mcp_server.py"]
tool_timeout_sec = 3600

[features.code_mode]
direct_only_tool_namespaces = ["mcp__agentic_sdlc_executor"]
```

Preserve existing MCP server entries and existing `direct_only_tool_namespaces`; append the PSC namespace instead of replacing unrelated configuration.

`tool_timeout_sec` is a call timeout, not a polling interval. It should cover the longest intended Executor MCP call.

After changing Codex configuration, start a refreshed Supervisor session. Normal dispatch is ready only when `psc_invoke_executor` is exposed as a direct top-level MCP tool.

## 1.3 DSH Supervisor

DSH uses its official MCP client plugin. Merge a PSC MCP row into the active Supervisor profile's `cordis.patch.yml`, for example:

```text
$DSH_HOME/profiles/desktop/cordis.patch.yml
```

or an intentionally selected home-level:

```text
$DSH_HOME/cordis.patch.yml
```

Example:

```yaml
- insert:
    - id: mcp-agentic-sdlc-executor
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: agentic_sdlc_executor
        transport: stdio
        command: 'F:\Miniconda3\envs\psc-mcp\python.exe'
        args:
          - 'E:\path\to\agentic-sdlc-contract-runtime\scripts\psc_mcp_server.py'
        toolCallTimeoutMs: 3600000
        failOnStartupError: true
```

Do not overwrite unrelated Cordis rows. If the patch file is the literal empty list `[]`, replace that list with the entry rather than appending YAML underneath it.

DSH exposes MCP tools as:

```text
mcp__<serverName>__<tool>
```

so the expected PSC names include:

```text
mcp__agentic_sdlc_executor__psc_supervisor_snapshot
mcp__agentic_sdlc_executor__psc_ensure_executor_ready
mcp__agentic_sdlc_executor__psc_invoke_executor
mcp__agentic_sdlc_executor__psc_commit_supervisor_transition
```

Restart or refresh the DSH Supervisor after changing its Cordis configuration. Normal dispatch must not begin until the current DSH tool inventory contains the PSC tools.

## 1.4 No silent fallback

Missing Supervisor MCP exposure is a configuration error, not permission to emulate MCP through another route.

Normal Supervisor execution must not:

```text
python scripts/invoke_executor.py invoke ...
```

as a fallback, must not import `scripts/psc_mcp_server.py` from a shell/Python subprocess, and must not call the internal Executor implementation directly.

The compatibility function `invoke_executor_tool()` deliberately returns:

```text
status = mcp_transport_required
reason = direct_python_dispatch_forbidden
```

and does not launch E.

The CLI `invoke` and `smoke` commands remain available for humans, tests, debugging, and recovery, but they are not normal replacements for a missing Supervisor MCP tool.

---

# 2. Runtime configuration

`.agentic-sdlc/runtime.json` is the user-editable runtime configuration for the local PSC workflow. It contains configuration only; never place API keys, passwords, tokens, copied auth files, or other credentials in it.

A representative Codex Executor configuration:

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

A representative DSH Executor configuration:

```json
{
  "schema_version": 1,
  "runtime_root": "E:\\AI_Runtime",
  "project_naming": "YYYYMMDD-{requirement}",
  "mcp": {
    "python_interpreter": "F:\\Miniconda3\\envs\\psc-mcp\\python.exe"
  },
  "executor": {
    "adapter": "dsh",
    "executable": "dsh",
    "executor_home": "E:\\dsh-executor\\.dsh",
    "config_source": "executor_home",
    "routing": {
      "provider": "opencode-go",
      "model": "deepseek-v4.1-flash",
      "effort": "high"
    },
    "profile": "headless",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "maxTimeout": 7200,
    "smoke_timeout": 120
  }
}
```

For every new initialization, provider, model, and reasoning effort are explicit user-confirmed Executor settings. They are persisted under `executor.routing` and are not inferred from the Supervisor model or from `CODEX_HOME` / `DSH_HOME`.

`executor_home` remains independently managed and supplies provider definitions, endpoints, and authentication. PSC does not copy or rewrite authentication material.

See [`references/runtime-config.md`](references/runtime-config.md) for validation and compatibility rules.

---

# 3. Executor adapters

## Codex Executor

The Codex adapter launches a fresh `codex exec` process for every attempt.

Important behavior:

- Child-only `CODEX_HOME=<executor_home>`.
- Per-run provider/model/reasoning-effort routing when `executor.routing` is present.
- Structured completion output for normal tasks.
- Prompt transport through stdin to avoid oversized Windows command lines.
- Supervisor credentials/session overrides are stripped before child launch.
- Executor home configuration is fingerprinted for smoke invalidation without reading auth material.

## DSH Executor

The DSH adapter launches the selected profile in a fresh process.

Important behavior:

- Child-only `DSH_HOME=<executor_home>`.
- Required `executor.profile`, typically a non-interactive profile such as `headless`.
- Short-lived command-line `--patch` for per-run provider/model/reasoning-effort routing.
- DSH home/profile configuration is not rewritten to switch the PSC route.
- Headless `--json` completion parsing.
- Durable Session usage folding is preferred for accounting; `step_end.usage` is a fallback.
- `settings.yaml` is optional in current DSH releases. If present it participates in fingerprinting; absence is a valid, fingerprint-significant state.
- The selected profile's `package.json` and `cordis.patch.yml` remain required fingerprint inputs.

Executor adapter details are documented in [`references/executor-adapters.md`](references/executor-adapters.md).

---

