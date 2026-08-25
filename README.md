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

Alternatively, place a Bundle directly in an existing workflow's
`contract/imports/` directory. At startup, when no usable Approved Contract
exists, the Supervisor auto-imports exactly one pending Bundle; it asks the
user to choose when multiple Bundles are present.

The importer copies the original Bundle as provenance, parses and validates it,
then atomically materializes the exact six Contract files in `contract/vN/`.
It never lets an Executor parse a Bundle, never overwrites an existing Contract
version, and never silently changes a declared version. Draft or semantically
incomplete Contracts wait for Planner resolution rather than starting coding.

## Helper Commands

Run commands from this Skill directory, supplying the target repository and
its runtime configuration as appropriate:

```text
python scripts/psc_runtime.py validate-contract <contract-dir> --repository <path>
python scripts/psc_runtime.py discover --repository <path> --runtime-config <path>
python scripts/psc_runtime.py bootstrap <contract-dir> --repository <path> --runtime-config <path>
python scripts/psc_runtime.py import-bundle <bundle-path> --repository <path> --runtime-config <path>
python scripts/psc_runtime.py auto-import --repository <path> --runtime-config <path>
```

Use `--help` on any subcommand for optional flags such as `--project-id`.

## Validation

The focused offline test suite exercises Bundle parsing, validation,
materialization, provenance, idempotency, discovery, bootstrap, documentation,
and the Executor boundary:

```text
python -m pytest tests -q
```
