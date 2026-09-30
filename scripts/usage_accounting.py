from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DSH_BUCKET_KEYS = (
    'uncachedInputTokens',
    'cacheReadTokens',
    'cacheWriteTokens',
    'outputTokens',
)
_TOKEN = r'([0-9][0-9,_]*)'


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value.replace(',', '').replace('_', '').strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _normalized_usage(
    *,
    source: str,
    uncached_input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    output_tokens: int | None,
    reasoning_tokens: int | None,
    reported_input_tokens: int | None = None,
    reported_total_tokens: int | None = None,
    sessions_changed: int | None = None,
) -> dict[str, Any]:
    input_parts = (uncached_input_tokens, cache_read_tokens, cache_write_tokens)
    input_tokens = (
        sum(value or 0 for value in input_parts)
        if any(value is not None for value in input_parts)
        else reported_input_tokens
    )
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else reported_total_tokens
    )
    usage: dict[str, Any] = {
        'schema_version': 1,
        'source': source,
        'input_tokens': input_tokens,
        'uncached_input_tokens': uncached_input_tokens,
        'cache_read_tokens': cache_read_tokens,
        'cache_write_tokens': cache_write_tokens,
        'output_tokens': output_tokens,
        'reasoning_tokens': reasoning_tokens,
        'total_tokens': total_tokens,
        'reported_input_tokens': reported_input_tokens,
        'reported_total_tokens': reported_total_tokens,
    }
    if sessions_changed is not None:
        usage['sessions_changed'] = sessions_changed
    return usage


def _find_dsh_totals(node: Any) -> dict[str, int] | None:
    if isinstance(node, dict):
        token_usage = node.get('tokenUsage')
        if isinstance(token_usage, dict):
            totals = token_usage.get('totals')
            if isinstance(totals, dict):
                parsed = {key: _nonnegative_int(totals.get(key)) for key in DSH_BUCKET_KEYS}
                if any(value is not None for value in parsed.values()):
                    return {key: value or 0 for key, value in parsed.items()}
        for value in node.values():
            found = _find_dsh_totals(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_dsh_totals(value)
            if found is not None:
                return found
    return None


def snapshot_dsh_usage(executor_home: Path) -> dict[str, dict[str, int]]:
    """Read DSH's durable per-session token-usage projection cache.

    The cache is derived state rather than the authoritative session log, so a
    missing, malformed, or not-yet-flushed record is ignored instead of making
    Executor invocation fail.
    """
    root = Path(executor_home) / 'storages' / 'session_projcache' / 'sessions'
    if not root.is_dir():
        return {}
    snapshot: dict[str, dict[str, int]] = {}
    try:
        paths = list(root.glob('*.json'))
    except OSError:
        return {}
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        totals = _find_dsh_totals(value)
        if totals is not None:
            snapshot[path.stem] = totals
    return snapshot


def dsh_usage_delta(
    before: dict[str, dict[str, int]],
    after: dict[str, dict[str, int]],
) -> dict[str, Any] | None:
    """Sum positive per-session projection deltas for one DSH invocation."""
    aggregate = {key: 0 for key in DSH_BUCKET_KEYS}
    sessions_changed = 0
    for session_id, current in after.items():
        previous = before.get(session_id, {})
        deltas = {
            key: max(0, int(current.get(key, 0)) - int(previous.get(key, 0)))
            for key in DSH_BUCKET_KEYS
        }
        if any(deltas.values()):
            sessions_changed += 1
            for key, value in deltas.items():
                aggregate[key] += value
    if not any(aggregate.values()):
        return None
    return _normalized_usage(
        source='dsh_projection_delta',
        uncached_input_tokens=aggregate['uncachedInputTokens'],
        cache_read_tokens=aggregate['cacheReadTokens'],
        cache_write_tokens=aggregate['cacheWriteTokens'],
        output_tokens=aggregate['outputTokens'],
        reasoning_tokens=None,
        sessions_changed=sessions_changed,
    )


def _usage_window(text: str) -> str:
    lower = text.lower()
    markers = (
        'token usage',
        'uncached_input_tokens',
        'uncached input',
        'cache_read_tokens',
        'cache read tokens',
        'cached input',
    )
    starts = [lower.rfind(marker) for marker in markers]
    start = max(starts)
    if start >= 0:
        return text[start:start + 2500]
    for line in reversed(text.splitlines()):
        lowered = line.lower()
        keyword_count = sum(
            key in lowered for key in ('input', 'output', 'cached', 'reasoning', 'total')
        )
        if keyword_count >= 3 and re.search(r'\d', line):
            return line
    return ''


def _first_number(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return _nonnegative_int(match.group(1)) if match else None


def _last_number(pattern: str, text: str) -> int | None:
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
    return _nonnegative_int(matches[-1].group(1)) if matches else None


def parse_process_usage(stdout: str, stderr: str, *, adapter: str) -> dict[str, Any] | None:
    """Best-effort parser for harness token summaries written to stdout/stderr.

    This is the primary Codex source and a DSH fallback when its durable
    projection is unavailable. Reported totals are retained because some
    harnesses exclude cache traffic from their own displayed total; the
    normalized total always sums the normalized input buckets plus output.
    """
    text = '\n'.join(part for part in (stdout, stderr) if part)
    window = _usage_window(text)
    if not window:
        return None

    explicit_uncached = _last_number(
        rf'\buncached(?:\s+input)?(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )
    cache_read = _last_number(
        rf'\bcache[\s_-]*read(?:\s+input)?(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )
    if cache_read is None:
        cache_read = _last_number(
            rf'\bcached(?:\s+input)?(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
            window,
        )
    if cache_read is None:
        cache_read = _last_number(rf'{_TOKEN}\s+cached\b', window)

    cache_write = _last_number(
        rf'\bcache[\s_-]*write(?:\s+input)?(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )
    reported_input = _first_number(
        rf'\binput(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )
    output = _first_number(
        rf'\boutput(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )
    reasoning = _first_number(
        rf'\breasoning(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )
    reported_total = _first_number(
        rf'\btotal(?:\s+tokens?)?\s*[:=]?\s*{_TOKEN}',
        window,
    )

    uncached = explicit_uncached if explicit_uncached is not None else reported_input
    if uncached is None and output is None and reported_total is None:
        return None
    if cache_read is None and reported_input is not None:
        cache_read = 0
    if cache_write is None and reported_input is not None:
        cache_write = 0

    return _normalized_usage(
        source=f'{adapter}_process_output',
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=reasoning,
        reported_input_tokens=reported_input,
        reported_total_tokens=reported_total,
    )
