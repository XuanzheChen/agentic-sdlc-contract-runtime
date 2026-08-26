# PSC Contract schema and validation

An Approved Contract is an immutable directory `contract/vN/` containing exactly
these required artifacts (additional evidence files are allowed):

```text
requirements.md   # REQ-### headings
acceptance.md     # AC-### headings
implementation.md
constraints.md
tasks.md          # T-### headings and references
metadata.json
```

`metadata.json` must be an object with `schema_version` (currently `1`), a
positive integer `version`, `status`, `created_by`, `created_at`, and optional
`supersedes` and `workflow_policy`. Only `approved` is executable. A project
uses the highest numeric version with that status; drafts and rejected or
superseded versions are never selected.

Stable IDs are mandatory and must be unique within their namespace:

- requirements: `REQ-001`, `REQ-002`, ...
- acceptance criteria: `AC-001`, `AC-002`, ...
- tasks: `T-001`, `T-002`, ...

Each task must identify its requirement and acceptance references. The following
declarations are all required: `Dependencies`, `Allowed Scope`, `Forbidden
Scope`, `Implementation Notes`, and `Required Verification`. Every
reference must resolve. Dependencies must name existing tasks, must not self
reference, and must form an acyclic graph. A task's allowed and forbidden paths
are repository-relative; the intersection must be empty.

Validation may reject or escalate only for structural/status/repository
mismatch, unresolved IDs, cyclic dependencies, contradictory or infeasible
instructions, or serious security risk. It must not silently change contractual
meaning or redesign the architecture. A failed implementation is a task retry,
not a new Contract, unless the intended behavior itself changes.

`workflow_policy` is required and contains exactly one strategy: `restart: all`,
`restart: pending_only`, or `invalidate_from_task: T-###`. It may not combine
`restart` and `invalidate_from_task`.

The Contract must be self-contained. Approved text must not depend on
conversation-only references (`as discussed`, `the user's earlier message`,
or equivalent). Planner decisions, priorities, edge cases, and the explicit
workflow invalidation policy belong in these files.

Contract version numbers are workflow-local. A repository with multiple
independent workflows may validly contain `contract/v1` in each workflow;
same-version conflicts are detected only when two different Bundles target the
same workflow's `contract/vN`.

## Contract Bundle transport

An External Planner hands off a Contract as a single `PSC-CONTRACT-BUNDLE`
Markdown file, documented only by `prompts/contract-export.md` (the External
Planner Contract Export Prompt). The Bundle is a transport format, not an
execution Contract: the importer validates it and materializes the immutable
`contract/vN/` directory before scheduling, and the Executor never sees or
parses a Bundle.

- **Six artifacts.** A Bundle contains exactly six `FILE:` sections, one per
  canonical artifact (`metadata.json`, `requirements.md`, `acceptance.md`,
  `implementation.md`, `constraints.md`, `tasks.md`). The `## CONTRACT-MANIFEST`
  `Files:` list must equal exactly that set, and `Version:`/`Status:` must agree
  with `metadata.json` (numeric equality for version; status equality after
  trimming/lowercasing); disagreement is a mechanical `import_failed`.
- **Import-only statuses.** Bundle import accepts only `draft` and `approved`.
  An approved Bundle is executable; a draft Bundle materializes but never
  schedules (`waiting_planner` until Planner/user approval). `superseded` and
  `rejected` are valid only for already-materialized versions, never inside a
  Bundle.
- **AC -> REQ resolution.** Every `REQ-###` referenced by `acceptance.md` must
  resolve to a `REQ-###` heading in `requirements.md` (import check).
- **C-### uniqueness.** Only formal Constraint definitions count for duplication:
  a `## C-###` heading or a line beginning `C-###:`. Incidental references such
  as `See C-001` are not definitions.
- **UNRESOLVED rule.** An *approved* Bundle must not contain the literal marker
  `UNRESOLVED` in any canonical artifact (it is contradictory with its status
  and escalates). A *draft* may legitimately contain `UNRESOLVED`, but the
  importer records those items in an escalation file and never promotes the
  draft.
- **Materialization.** Import is atomic (staging + single rename): a failed
  import leaves no partial `vN`. A pre-existing `contract/vN/` is never
  modified, replaced, merged, or renumbered; re-importing the same SHA-256 at
  the same version is `already_imported`, and a different Bundle declaring an
  existing version is `version_conflict`.

The same mechanical and semantic validation engine applies to a materialized
direct Contract and a Bundle. Bundle semantic failures are retained and escalated
as immutable evidence; direct validation reports the same semantic errors. Once
materialized, every `contract/vN/` is immutable regardless of status. Approving
a draft creates a new approved version with `supersedes`, never edits the draft.
