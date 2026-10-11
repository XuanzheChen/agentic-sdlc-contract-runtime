# Harness-neutral Preflight Checker performance optimization

This document covers **ASCR Preflight Checker** code only, not the Executor implementation, and never changes the Executor's model, provider, effort, approval, sandbox, or retry budget.

## Changes

1. **Stable empty cwd**: PC operates from a deterministic directory under the system temporary directory, keyed by the canonical repository path. This prevents a random per-invocation cwd from appearing near the beginning of the harness system message. The cwd contains no input files; independently generated output-schema and tool-restriction overlays reside in a unique, temporary invocation directory and are cleaned up. Before and after a model call, unexpected cwd content is a fail-closed runtime error. If the provider call already happened, actual provider usage is recorded, not zeroed. The two supported adapters retain their existing read-only/scope tool restrictions.
2. **Static-first prompt**: The schema and universal decision rules come first, followed by frozen Contract global constraints. Task-specific text, scoped Contract packet and repository excerpts follow, then binding SHA values and current runtime ledger facts. This moves rapidly changing information away from the prefix without suppressing or weakening evidence binding. The full Contract packet SHA remains unchanged and is revalidated before E.
3. **Deterministic evidence selection**: Native code ranks explicitly configured and task-scoped candidate files by exact Task/Review references and configured category. The text excerpt default decreases from 48,000 to 36,000 total characters and from 8,000 to 6,500 per file. Complete-line truncation and conspicuous partial-content warnings remain mandatory. Paths not selected because of the file-count limit are listed as **metadata only**, with native SHA hashes bound separately; changes invalidate admission. The model must not infer the contents or absence of metadata-only files, and must DENY if inspection would be necessary for safe launch. There is no new open-ended repository traversal or background caching of ALLOW decisions.
4. **Compact JSON without harness coupling**: The generic checker instructions discourage duplicating source text and redoing Executor acceptance. They do not set model-specific max-output parameters, provider knobs or thinking-effort overrides. The exact strict JSON schema and all blocking-evidence requirements remain.
5. **Observability for later real trials**: The per-run report now carries `prompt_metrics` (rendered text characters, stable-prefix length/SHA, selected file excerpt size and count). These are structural proxies, **not a token cache estimate**. Actual cached/uncached input, output, and latency remain the independent provider usage source.

## Security and science invariants

- Every E attempt still receives a freshly performed PC model check, never an old ALLOW replay.
- Runtime SHA revalidation remains mandatory, including omitted-file metadata SHA and the latest ledger facts.
- An unreadable/changed file, contaminated cwd, invalid JSON response, failure to persist mandatory evidence, or stale evidence remains UNKNOWN / blocked.
- Checker continues to run from outside the research repository without shell/write/MCP capabilities. In the DSH adapter, read-only tool restriction patch verification is unchanged. Runtime never grants research physical execution permission merely because PC was optimized.
- No operation edits a project's workflow status, scientific artifacts, approved Contract, frozen historical evidence, or Executor budget.
- Strict harness neutrality: no adapter-specific generation/output-token cap is introduced.

## Validation and deployment

Run the full offline test suite under the PSC MCP Python environment:

```
python -m pytest tests -q -p no:cacheprovider --disable-warnings
```

Tests assert stable/empty cwd for both adapters, no repository path exposure, corruption fail-closed, provider billing retained if the cwd is contaminated *after* launch, static prompt prefix stability, Contract-scope evidence and omitted-file SHA revalidation, compact schema, and absence of harness-specific output cap fields.

Real optimization effect is deliberately **not claimed from CI**. After deploying the merged Skill and restarting the Supervisor MCP service, perform controlled A/B measurements using the same provider/model/effort and equivalent Task/Contract inputs in an isolated DEV workspace: per-run input, cached input, output, wall time, decision validity and actual RMB cost. Compare first-call cold cache separately from repeated warm calls; provider cache TTL/behavior may dominate results. Never run paid scientific branches or reset T-005 to benchmark PC.

Rollback via Git revert plus Skill redeployment/MCP restart. The stable temporary cwd may be left empty on disk, by design; it is never a runtime authority or a cross-invocation data cache.
