# Portable Contract authoring checklist

When the user explicitly requests Planner mode, inspect the repository and
prepare a new `contract/vN/` without changing product code. Include:

1. `requirements.md`: complete behavior, boundaries, and stable `REQ-###` IDs.
2. `acceptance.md`: observable, testable `AC-###` criteria and failure behavior.
3. `implementation.md`: architecture guidance and non-binding implementation
   recommendations that an independent Executor can follow.
4. `constraints.md`: repository, security, compatibility, forbidden-scope, and
   operational constraints.
5. `tasks.md`: `T-###` tasks, REQ/AC coverage, dependencies, allowed/forbidden
   scope, and required tests.
6. `metadata.json`: schema/version/status/author/time and explicit
   `workflow_policy` where version changes can invalidate work.

Keep the draft self-contained and portable. Before asking for approval, check
that a fresh Supervisor with no conversation transcript could schedule and
verify every task. Write `status: draft`; after explicit approval, create a new
immutable version with `status: approved` and `supersedes` rather than editing
the draft. A semantic change creates a new version and leaves older versions
untouched.
