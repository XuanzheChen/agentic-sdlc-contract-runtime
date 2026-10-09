"""Deterministic, user-facing E/PC usage report for Supervisor MCP results.

The seven token fields are never conflated across E and PC. Ratios belong
exclusively to the current E invocation; aggregates have no ratio display.
"""
from __future__ import annotations

import math
from typing import Any

from executor_progress import format_elapsed_time

TOKEN_ROWS = (
    ("输入 Token", "input_tokens"),
    ("未缓存输入", "uncached_input_tokens"),
    ("缓存命中输入", "cached_input_tokens"),
    ("缓存写入输入", "cache_write_input_tokens"),
    ("输出 Token", "output_tokens"),
    ("其中 Reasoning", "reasoning_output_tokens"),
    ("总 Token", "total_tokens"),
)


def _duration(usage: dict[str, Any] | None) -> str:
    if not isinstance(usage, dict):
        return "不可用"
    value = usage.get("elapsed_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "不可用"
    if not math.isfinite(value) or value < 0:
        return "不可用"
    return format_elapsed_time(value)


def _percentage(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "不可用"
    if not math.isfinite(value) or not 0 <= value <= 1:
        return "不可用"
    return f"{value * 100:.1f}%"


def _count(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "次数未知"


def _cell(usage: dict[str, Any] | None, field: str) -> str:
    if not isinstance(usage, dict) or usage.get("available") is False:
        return "不可用"
    value = usage.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "不可用"
    # Provider-inexact and missing records are a lower bound, never exact zero.
    return f"{'≥' if usage.get('exact') is False else ''}{value:,}"


def _combined_cell(
    executor_total: dict[str, Any] | None,
    checker_total: dict[str, Any] | None,
    field: str,
) -> str:
    """E Contract cumulative plus workflow PC cumulative, never an invented zero."""
    for usage in (executor_total, checker_total):
        if not isinstance(usage, dict) or usage.get("available") is False:
            return "不可用"
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "不可用"
    assert executor_total is not None and checker_total is not None
    combined = executor_total[field] + checker_total[field]
    exact = executor_total.get("exact") is True and checker_total.get("exact") is True
    return f"{'' if exact else '≥'}{combined:,}"


def _accuracy(label: str, usage: dict[str, Any] | None, *, checker: bool = False) -> str | None:
    if not isinstance(usage, dict):
        return f"{label}：累计记录不可用，无法确认精确性。"
    if usage.get("exact", False):
        return None
    prefix = "checker_" if checker else ""
    missing = usage.get(f"unavailable_{prefix}invocations")
    inexact = usage.get(f"inexact_{prefix}invocations")
    message = f"{label}：Token 数据不完整（表中 ≥ 表示已知下界）"
    if isinstance(missing, int) and isinstance(inexact, int):
        message += f"；不可用 {missing} 次，非精确 {inexact} 次"
    return message + "。"


def format_usage_report(
    executor_usage: dict[str, Any],
    checker_workflow_total: dict[str, Any],
) -> dict[str, Any]:
    """Build one ready-to-display table after an actual E invocation returns."""
    invocation = executor_usage.get("invocation")
    if not isinstance(invocation, dict):
        invocation = {}
    contract = executor_usage.get("contract_total")
    if not isinstance(contract, dict):
        contract = None
    pc = checker_workflow_total if isinstance(checker_workflow_total, dict) else None
    label = contract.get("contract", "当前版本") if contract else "当前版本"
    e_count = _count(contract.get("invocations")) if contract else "次数未知"
    pc_count = _count(pc.get("checker_invocations")) if pc else "次数未知"

    lines = [
        f"本次 E 耗时 {_duration(invocation)}；E 累计耗时 {_duration(contract)}。",
        "",
        f"| 指标 | 本次 E | E 累计（{label}，{e_count} 次） | PC 累计（{pc_count} 次） | 累计总计（E+PC） |",
        "|---|---:|---:|---:|---:|",
    ]
    for title, field in TOKEN_ROWS:
        lines.append(
            f"| {title} | {_cell(invocation, field)} | {_cell(contract, field)} | {_cell(pc, field)} | {_combined_cell(contract, pc, field)} |"
        )
    lines.extend([
        f"| 缓存命中率 | {_percentage(invocation.get('cache_hit_rate'))} | — | — | — |",
        f"| 输出/输入比 | {_percentage(invocation.get('output_input_ratio'))} | — | — | — |",
        "",
        "E 累计限当前 Contract；PC 累计覆盖本工作流全部 Contract 版本；累计总计为这两个范围的 E+PC 之和，不含本次 E 的重复加计。",
    ])
    for accuracy in (
        _accuracy("本次 E", invocation),
        _accuracy("E 累计", contract),
        _accuracy("PC 累计", pc, checker=True),
    ):
        if accuracy:
            lines.append(accuracy)
    if executor_usage.get("persistence_error"):
        lines.append("E 用量持久化失败；累计值不可用，不得当作零。")
    return {
        "markdown": "\n".join(lines),
        "checker_workflow_total": pc,
    }
