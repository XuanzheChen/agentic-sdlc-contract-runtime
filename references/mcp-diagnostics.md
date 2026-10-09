# PSC MCP diagnostics and transition recovery

All native tools registered in scripts/psc_mcp_server.py use a common
structured Python-exception boundary. If an invoked Python tool raises,
the SDK receives a normal structured response with status=tool_error,
error_id, error_code, exception_type, failure_phase, mutation_status,
state_sha_before, state_sha_after, retryable, next_action and
diagnostic_log_path. No Executor retry budget is consumed by Supervisor
review/commit failures.

Full tracebacks are written to project/runtime/mcp-diagnostics/
mcp-errors-YYYYMMDD.jsonl. The fallback directory comes from
PSC_MCP_DIAGNOSTIC_DIR or ~/.psc-mcp/diagnostics. MCP stdout is reserved
for the protocol; diagnostic records do not contain raw tool arguments,
inline Review Markdown, or secrets from known credential-shaped fields.

## Durable Supervisor commit journal

The native Supervisor commit has a per-project process-safe file lock.
Review, Result, Resume and State files are staged first. A write-ahead
transaction JSON is persisted under
project/runtime/supervisor-transactions/st-<id>.json. The journal records
source/target state SHA, file hashes, applied paths, a failure path and
phase (prepared, applying, partial, failed, committed). The authoritative
workflow_state.json replacement occurs last.

Multiple files cannot be replaced in one filesystem atomic operation.
Review may already be modified even if workflow state remains unchanged.
The error boundary reports mutation_status: no_monitored_mutation,
partial_mutation, committed_state or unknown. "No monitored mutation"
covers only the known Supervisor files, not arbitrary external effects.
Never treat generic MCP failures as confirmed rollbacks.

## Safe recovery

1. After a failed native commit, inspect the returned error_id and full
   traceback at diagnostic_log_path.
2. Call psc_transition_diagnostics with the exact project directory to
   retrieve recent journals, error IDs, and authoritative State SHA.
3. Call psc_supervisor_snapshot again to verify the current Contract,
   Task, State SHA, and retry budgets.
4. Re-issue only after independently reconciling the evidence. For
   partial_mutation, committed_state or unknown, stop automatic scheduling.
   Never edit workflow_state.json manually or blindly replay a commit.

This diagnostic tool is read-only and does not call Executor or charge
its retry budgets.

## Limitations

This interception starts only after a registered Python tool body is
invoked. MCP client connection failures, process death, schema
validation before dispatch, or host transport failures can occur outside
the guard. Use the MCP process stderr and client transport logs for
those cases. A generic "Error executing tool" provides no proof of
rollback. Restart the MCP service after updating the Skill.
