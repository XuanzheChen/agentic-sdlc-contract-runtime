# Agentic SDLC Contract-Driven Runtime (PSC)

`agentic-sdlc-contract-runtime` is a portable Codex Skill for running or
authoring Contract-Driven Agentic SDLC (PSC) workflows from filesystem
artifacts. It keeps the Planner, Supervisor, and Executor independent while
preserving immutable versioned Contracts, resumable workflow state, retries,
escalation, and evidence-based verification.

## What It Provides

- An artifact-first Supervisor workflow: durable Contract, runtime, repository,
  task, review, and verification artifacts are the source of truth.
- Immutable `contract/vN/` execution Contracts with stable requirement,
  acceptance, and task IDs.
- A disposable Executor adapter boundary: Executors receive only materialized
  Contract/task information and never own workflow state or approval.
- A deterministic helper at `scripts/psc_runtime.py` for Contract validation,
  discovery, bootstrap, and Contract Bundle import.

Read [`SKILL.md`](SKILL.md) for the complete runtime instructions and the
[`references/`](references/) directory for the Contract schema, runtime
protocol, runtime configuration, Planner Contract guidance, and adapter
boundary.

## Use With Codex

Place or clone this directory where Codex discovers local Skills (for example,
the repository-local `.agents/skills/` directory), then invoke it by name:

```text
Use $agentic-sdlc-contract-runtime to resume or start a contract-driven workflow.
```

On first Supervisor use in a workspace, initialize a user-editable
`.agentic-sdlc/runtime.json` with the runtime root, project naming convention,
and Executor configuration. Runtime configuration never contains credentials;
authentication remains in the selected Executor environment.

## Blocking Executor MCP

Normal Supervisor dispatch should use the local blocking MCP tool
`psc_invoke_executor` from `scripts/psc_mcp_server.py`. This removes the
`exec_command -> background terminal -> write_stdin` polling loop: Codex waits
on one MCP `tools/call`, the existing `invoke_executor()` blocks on the
Executor process, and the same Supervisor turn resumes automatically when the
tool returns.

Install the optional MCP dependency once:

```text
python -m pip install -r requirements-mcp.txt
```

Then register the local stdio MCP server in the Supervisor Codex configuration.
Use an absolute path to this Skill checkout. On Windows, for example:

```toml
[mcp_servers.agentic_sdlc_executor]
command = "python"
args = ["E:/path/to/agentic-sdlc-contract-runtime/scripts/psc_mcp_server.py"]
tool_timeout_sec = 3600
```

`tool_timeout_sec` is the maximum duration of one Executor MCP call, not a
polling interval. Choose a value at least as large as the normal
`executor.timeout` in `.agentic-sdlc/runtime.json`. If the Executor finishes
earlier, the MCP tool returns immediately and the Supervisor continues in the
same Codex turn.

The tool returns only compact metadata such as status, changed paths, artifact
paths, and the raw log path. It intentionally excludes raw stdout/stderr and the
full structured completion body so large Executor transcripts do not inflate the
Supervisor context. Inspect `plan.md`, `coding.md`, diffs, tests, or the raw log
selectively during Supervisor verification.

The legacy command below remains available for manual debugging, CI, and
compatibility:

```text
python scripts/invoke_executor.py invoke ...
```

For normal Supervisor dispatch, do not fall back to shell execution plus
`write_stdin` polling when MCP is unavailable; fix the MCP configuration or
dependency instead.

## Initialize a Supervisor runtime

Initialization is deliberately explicit. PSC does not borrow the current
Codex session's model, provider, sandbox, authentication, or home directory.
Create `.agentic-sdlc/runtime.json` only after choosing every required value,
then run a static probe and a real smoke task:

```text
python scripts/invoke_executor.py status --repository <repository> --runtime-config <repository>/.agentic-sdlc/runtime.json
python scripts/invoke_executor.py smoke  --repository <repository> --runtime-config <repository>/.agentic-sdlc/runtime.json
```

The smoke task runs through the selected harness in an isolated workspace. It
must create an exact marker file, so a passing status proves more than a CLI
lookup: the configured executor can actually complete a constrained task.

### Required settings

| Setting | Meaning |
| --- | --- |
| `runtime_root` | Directory that holds durable PSC workflow projects. It contains Contracts, task state, reviews, results, and imported Bundle provenance. It may be relative to the repository. |
| `project_naming` | Folder-name template for newly bootstrapped workflows. `YYYYMMDD-{requirement}` yields a date plus the concise requirement slug. |
| `executor.adapter` | Harness to invoke: `codex` or `dsh`. The adapter determines CLI construction, child environment isolation, smoke fingerprinting, and structured-output handling. |
| `executor.executable` | Explicit path or PATH command for the selected harness. PSC verifies that it is runnable before dispatch. |
| `executor.executor_home` | Independently managed harness home. PSC never creates, copies, or edits credentials in this directory. |
| `executor.config_source` | `runtime` makes PSC pass the declared provider, model, and reasoning effort to Codex. `executor_home` keeps those values in the independent harness home instead. |
| `executor.provider`, `model`, `effort` | Required only for `codex` with `config_source: runtime`. They make the execution configuration explicit and are not collected for home-owned configuration. |
| `executor.profile` | Required for `dsh`; names an existing DSH profile, such as `headless`. |
| `executor.approval_policy` | Codex approval mode: `untrusted`, `on-request`, or `never`. For non-interactive work, `never` is normally appropriate because the sandbox remains the access boundary. |
| `executor.sandbox` | Allowed values: `read-only`, `workspace-write`, or `danger-full-access`. Use `workspace-write` unless a stricter or explicitly approved mode is required. |
| `executor.timeout` | Maximum seconds for a normal task invocation. |
| `executor.smoke_timeout` | Maximum seconds for the smoke invocation. |

`on-request` can block a non-interactive executor. `danger-full-access` is an
advanced selection and must be made deliberately. Runtime configuration must
not contain API keys, tokens, passwords, or copied authentication files.

### Supported harnesses

#### Codex (`codex`)

The Codex adapter launches a fresh `codex exec` process for every attempt. Its
child process receives `CODEX_HOME=<executor_home>`; the Supervisor's process
environment is unchanged.

With `config_source: runtime`, provide `provider`, `model`, and `effort` in
`runtime.json`; PSC supplies the corresponding Codex CLI overrides. With
`config_source: executor_home`, omit those fields and provide a readable
`<executor_home>/config.toml`. PSC does not read `auth.json`; it fingerprints
the non-secret configuration so a changed home configuration requires a new
smoke result.

```json
{
  "schema_version": 1,
  "runtime_root": ".agentic-sdlc/developing",
  "project_naming": "YYYYMMDD-{requirement}",
  "executor": {
    "adapter": "codex",
    "executable": "codex",
    "executor_home": "E:\\codex-executor",
    "config_source": "executor_home",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
```

#### DeepSeek Harness (`dsh`)

The DSH adapter starts the selected profile as a fresh process and supplies
`DSH_HOME=<executor_home>` only to that child. Use an already configured DSH
home and an existing profile; PSC does not initialize profiles or touch DSH
credentials. DSH owns its provider, model, and reasoning configuration, so use
`config_source: executor_home` and omit Codex-specific model fields.

DSH does not expose a Codex-compatible output-schema flag. PSC therefore
requires the same strict JSON task-completion object in the prompt and rejects
any other normal-task final response. During smoke, the profile is also asked to
report its active model as `PSC_MODEL: <model-id>`; this identity is recorded in
the secret-free smoke artifact when available.

```json
{
  "schema_version": 1,
  "runtime_root": ".agentic-sdlc/developing",
  "project_naming": "YYYYMMDD-{requirement}",
  "executor": {
    "adapter": "dsh",
    "executable": "dsh",
    "executor_home": "C:\\Users\\you\\.dsh",
    "config_source": "executor_home",
    "profile": "headless",
    "approval_policy": "never",
    "sandbox": "workspace-write",
    "timeout": 1800,
    "smoke_timeout": 120
  }
}
```

After smoke passes, re-run it whenever the selected adapter, executable, home,
profile, approval policy, sandbox, model settings, or relevant non-secret home
configuration changes. PSC refuses normal task dispatch when the smoke
fingerprint is missing or stale.

## Executor Isolation and Health

Initialization is an explicit wizard: it never borrows the Supervisor model, provider, `CODEX_HOME`, or project/global Codex configuration. Choose an independently managed Executor home; PSC never copies `config.toml`, `auth.json`, or credentials into it. Codex dispatch uses a child-only `CODEX_HOME`, so the Supervisor environment stays unchanged.

The wizard asks for Config Source before model settings. With `runtime`, it
collects Provider, Model, and Reasoning Effort and passes the corresponding
Codex CLI overrides. With `executor_home`, it does not collect those fields;
the readable `<executor_home>/config.toml` supplies them and its SHA-256 is
used for non-sensitive smoke invalidation. Runtime never edits that file or
`auth.json`.

For a disposable, non-interactive Codex Executor, the recommended configuration is:

```json
{
  "approval_policy": "never",
  "sandbox": "workspace-write"
}
```

`never` does not mean full access: `sandbox` still limits the child process. `danger-full-access` is an explicit advanced choice. `on-request` is supported for supervised execution but can cause `codex exec` to wait for unavailable approval. Initialization collects the executable and smoke timeout as explicit fields, then requires valid `runtime.json`, static probe PASS, and a real smoke task PASS before Ready. Normal Executors return a structured completion; the invocation layer materializes its `plan.md` and `coding.md` under the workflow project without requiring cross-sandbox Runtime Root writes. A changed Executor configuration must pass smoke again before dispatch.

## External Planner Contract Bundle

The [`prompts/contract-export.md`](prompts/contract-export.md) file is the
**External Planner Contract Export Prompt**. Copy that prompt into an external
Planner such as ChatGPT Web, Claude, another Codex session, or a human-assisted
planning conversation after the project has been planned. It instructs that
Planner to produce one self-contained `PSC-CONTRACT-BUNDLE` Markdown file.

The external Planner does not need access to this Skill, the local repository,
or the Supervisor's conversation. The expected handoff is:

```text
External Planner
  -> PSC-CONTRACT-BUNDLE.md
  -> Supervisor importer
  -> immutable contract/vN/
  -> normal PSC Supervisor and Executor flow
```

Save the emitted Bundle and either give its path to the Supervisor or import it
directly:

```text
python scripts/psc_runtime.py import-bundle <bundle-path> \
  --repository <target-repository> \
  --runtime-config <target-repository>/.agentic-sdlc/runtime.json
```

A repository can contain multiple independent workflows (one per requirement
or development request). Use `--project-id` only to select an existing
associated workflow. To start a new request explicitly, use
`--new-project-id <id>`; it is mutually exclusive with `--project-id`, rejects
an existing id, and creates a fresh workflow whose Contract namespace starts
at `contract/v1` even when another workflow already has `contract/v1`.
An unknown `--project-id` never implicitly creates a workflow. New workflow
bootstrap uses `project_naming` and commits the standard layout atomically;
failed validation leaves no discoverable half-initialized workflow.

Alternatively, place a Bundle directly in an existing workflow's
`contract/imports/` directory. At startup, when no usable Approved Contract
exists, the Supervisor auto-imports exactly one pending Bundle; it asks the
user to choose when multiple Bundles are present.

The importer copies the original Bundle as provenance, parses and validates it,
then atomically materializes the exact six Contract files in `contract/vN/`.
It never lets an Executor parse a Bundle, never overwrites an existing Contract
version, and never silently changes a declared version. Draft or semantically
incomplete Contracts wait for Planner resolution rather than starting coding.

Importing a newer Approved Contract into an existing workflow does not change
the effective execution version. After reviewing the materialized Contract,
run `activate-contract` to apply its declared workflow policy, rebuild the
pending task queue, preserve historical artifacts, and update
`runtime/workflow_state.json`.

## Helper Commands

Run commands from this Skill directory, supplying the target repository and
its runtime configuration as appropriate:

```text
python scripts/psc_runtime.py validate-contract <contract-dir> --repository <path>
python scripts/psc_runtime.py discover --repository <path> --runtime-config <path>
python scripts/psc_runtime.py bootstrap <contract-dir> --repository <path> --runtime-config <path>
python scripts/psc_runtime.py import-bundle <bundle-path> --repository <path> --runtime-config <path>
python scripts/psc_runtime.py auto-import --repository <path> --runtime-config <path>
python scripts/psc_runtime.py activate-contract --project <workflow-project> --repository <path>
python scripts/invoke_executor.py smoke --repository <path> --runtime-config <path>
python scripts/invoke_executor.py status --repository <path> --runtime-config <path>
```

Use `--help` on any subcommand for optional flags such as `--project-id` and
`--new-project-id`.

## Validation

The focused offline test suite exercises Bundle parsing, validation,
materialization, provenance, idempotency, discovery, bootstrap, documentation,
and the Executor boundary:

```text
python -m pytest tests -q
```
