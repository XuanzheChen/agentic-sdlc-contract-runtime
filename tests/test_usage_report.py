"""User-facing reporting contract: only current-E ratios and readable durations."""
from __future__ import annotations

import sys

from conftest import SKILL_ROOT

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import preflight_checker as PC
from executor_token_usage import _usage
from usage_report import format_usage_report


def example_usage(*, elapsed: float | None = None):
    fields = dict(
        source="fixture", input_tokens=100_000,
        uncached_input_tokens=2_200, cached_input_tokens=97_800,
        cache_write_input_tokens=0, output_tokens=500,
        reasoning_output_tokens=200,
    )
    if elapsed is not None:
        fields["elapsed_seconds"] = elapsed
    return _usage(**fields)


def test_usage_report_five_columns_with_weighted_ratios_for_all_categories():
    invocation = {**example_usage(elapsed=1037.280),
                  "cache_hit_rate": 0.978, "output_input_ratio": 0.005}
    contract = {
        "available": True, "exact": True, "contract": "v3", "invocations": 3,
        "elapsed_seconds": 2419.159,
        "input_tokens": 360_000, "uncached_input_tokens": 12_000,
        "cached_input_tokens": 348_000, "cache_write_input_tokens": 0,
        "output_tokens": 2_100, "reasoning_output_tokens": 800,
        "total_tokens": 362_100,
    }
    pc = {
        "exact": True, "checker_invocations": 5,
        "input_tokens": 45_000, "uncached_input_tokens": 5_000,
        "cached_input_tokens": 40_000, "cache_write_input_tokens": 0,
        "output_tokens": 900, "reasoning_output_tokens": 350,
        "total_tokens": 45_900,
    }
    message = format_usage_report({
        "invocation": invocation, "contract_total": contract,
    }, pc)["markdown"]
    assert "本次 E 耗时 0 h 17 m 17 s" in message
    assert "E 累计耗时 0 h 40 m 19 s" in message
    assert "| 指标 | 本次 E | E 累计（v3，3 次） | PC 累计（5 次） | 累计总计（E+PC） |" in message
    assert "| 总 Token | 100,500 | 362,100 | 45,900 | 408,000 |" in message
    assert "| 缓存命中率 | 97.8% | 96.7% | 88.9% | 95.8% |" in message
    assert "| 输出/输入比 | 0.5% | 0.6% | 2.0% | 0.7% |" in message
    assert len([line for line in message.splitlines() if line.startswith("| ")]) == 10
    assert "本次缓存命中率" not in message
    assert "累计比率未提供" not in message
    assert "1,037.280 秒" not in message


def test_usage_report_inexact_or_missing_never_forges_zeros_or_ratios():
    invocation = {**example_usage(), "available": False, "exact": False}
    contract = {
        "contract": "v1", "invocations": 4, "exact": False,
        "unavailable_invocations": 2, "inexact_invocations": 1,
        **{key: 0 for _label, key in __import__("usage_report").TOKEN_ROWS},
    }
    pc = {
        "checker_invocations": 2, "exact": False,
        "unavailable_checker_invocations": 1,
        "inexact_checker_invocations": 0,
        **{key: 0 for _label, key in __import__("usage_report").TOKEN_ROWS},
    }
    message = format_usage_report({"invocation": invocation, "contract_total": contract}, pc)["markdown"]
    assert "本次 E 耗时 不可用；E 累计耗时 不可用" in message
    assert "| 总 Token | 不可用 | ≥0 | ≥0 | ≥0 |" in message
    assert "| 缓存命中率 | 不可用 | 不可用 | 不可用 | 不可用 |" in message
    assert "| 输出/输入比 | 不可用 | 不可用 | 不可用 | 不可用 |" in message
    assert "E 累计：Token 数据不完整" in message
    assert "PC 累计：Token 数据不完整" in message
    assert "不可用 2 次" in message


def test_ratios_never_divide_by_zero_or_average_percentages():
    contract = {**example_usage(), "exact": True, "invocations": 1, "contract": "v1"}
    invocation = {**example_usage()}
    pc = {"exact": True, "checker_invocations": 0, **{
        key: 0 for _label, key in __import__("usage_report").TOKEN_ROWS
    }}
    message = format_usage_report({
        "invocation": invocation, "contract_total": contract
    }, pc)["markdown"]
    assert "| 缓存命中率 | 97.8% | 97.8% | 不可用 | 97.8% |" in message
    assert "| 输出/输入比 | 0.5% | 0.5% | 不可用 | 0.5% |" in message

    # Output/input can exceed 100%; cache hit cannot.
    invocation["input_tokens"] = 2
    invocation["cached_input_tokens"] = 1
    invocation["output_tokens"] = 6
    contract["input_tokens"] = 2
    contract["cached_input_tokens"] = 1
    contract["output_tokens"] = 6
    message = format_usage_report({
        "invocation": invocation, "contract_total": contract
    }, pc)["markdown"]
    assert "| 输出/输入比 | 300.0% | 300.0% | 不可用 | 300.0% |" in message


def test_pc_workflow_total_counts_all_contract_versions_and_missing_usage(tmp_path):
    project = tmp_path / "developing" / "workflow"
    v1, v2 = project / "contract" / "v1", project / "contract" / "v2"
    v1.mkdir(parents=True)
    v2.mkdir(parents=True)
    PC.record_preflight_usage(
        project, v1, task="T-001", retry_kind="initial",
        status="allow", decision="ALLOW", reason=None,
        run_id="pc-1", usage=example_usage(elapsed=15),
    )
    second = PC.record_preflight_usage(
        project, v2, task="T-002", retry_kind="initial",
        status="unknown", decision="UNKNOWN", reason="usage_missing",
        run_id="pc-2",
        usage={"available": False, "exact": False, "source": "test"},
    )
    assert second["checker_contract_total"]["checker_invocations"] == 1
    total = PC.workflow_checker_usage(project)
    assert total["checker_invocations"] == 2
    assert total["unavailable_checker_invocations"] == 1
    assert total["input_tokens"] == 100_000
    assert total["total_tokens"] == 100_500
    assert total["exact"] is False
    assert second["checker_workflow_total"] == total
    import json
    summary = json.loads(PC.preflight_usage_summary_path(project).read_text(encoding="utf-8"))
    assert summary["workflow_total"] == total
    assert summary["contracts"]["v1"]["checker_invocations"] == 1
    assert summary["contracts"]["v2"]["checker_invocations"] == 1
