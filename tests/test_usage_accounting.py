from __future__ import annotations

import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'usage_accounting.py'
_SPEC = importlib.util.spec_from_file_location('usage_accounting_test_module', MODULE)
assert _SPEC is not None and _SPEC.loader is not None
usage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(usage)


def test_parse_codex_usage_with_explicit_uncached_and_cached():
    parsed = usage.parse_process_usage(
        (
            'Token usage: input 2,006,766 '
            '(uncached 190,062 / cached 1,816,704), '
            'output 30,642, reasoning 4,199, total 2,037,408'
        ),
        '',
        adapter='codex',
    )
    assert parsed is not None
    assert parsed['source'] == 'codex_process_output'
    assert parsed['uncached_input_tokens'] == 190_062
    assert parsed['cache_read_tokens'] == 1_816_704
    assert parsed['cache_write_tokens'] == 0
    assert parsed['input_tokens'] == 2_006_766
    assert parsed['output_tokens'] == 30_642
    assert parsed['reasoning_tokens'] == 4_199
    assert parsed['total_tokens'] == 2_037_408
    assert parsed['reported_total_tokens'] == 2_037_408


def test_parse_codex_usage_keeps_reported_total_separate_from_cache_traffic():
    parsed = usage.parse_process_usage(
        (
            'Token usage: total=850,468 input=693,376 '
            '(+ 19,951,616 cached) output=157,092 (reasoning 33,289)'
        ),
        '',
        adapter='codex',
    )
    assert parsed is not None
    assert parsed['uncached_input_tokens'] == 693_376
    assert parsed['cache_read_tokens'] == 19_951_616
    assert parsed['input_tokens'] == 20_644_992
    assert parsed['output_tokens'] == 157_092
    assert parsed['total_tokens'] == 20_802_084
    assert parsed['reported_total_tokens'] == 850_468


def test_snapshot_and_delta_dsh_projection(tmp_path: Path):
    sessions = tmp_path / 'storages' / 'session_projcache' / 'sessions'
    sessions.mkdir(parents=True)
    record = sessions / 'session-a.json'
    record.write_text(
        json.dumps(
            {
                'record': {
                    'rows': {
                        'tokenUsage': {
                            'totals': {
                                'uncachedInputTokens': 100,
                                'cacheReadTokens': 900,
                                'cacheWriteTokens': 10,
                                'outputTokens': 50,
                            }
                        }
                    }
                }
            }
        ),
        encoding='utf-8',
    )
    before = usage.snapshot_dsh_usage(tmp_path)

    record.write_text(
        json.dumps(
            {
                'record': {
                    'rows': {
                        'tokenUsage': {
                            'totals': {
                                'uncachedInputTokens': 140,
                                'cacheReadTokens': 1_500,
                                'cacheWriteTokens': 15,
                                'outputTokens': 80,
                            }
                        }
                    }
                }
            }
        ),
        encoding='utf-8',
    )
    after = usage.snapshot_dsh_usage(tmp_path)
    parsed = usage.dsh_usage_delta(before, after)

    assert parsed is not None
    assert parsed['source'] == 'dsh_projection_delta'
    assert parsed['uncached_input_tokens'] == 40
    assert parsed['cache_read_tokens'] == 600
    assert parsed['cache_write_tokens'] == 5
    assert parsed['input_tokens'] == 645
    assert parsed['output_tokens'] == 30
    assert parsed['reasoning_tokens'] is None
    assert parsed['total_tokens'] == 675
    assert parsed['sessions_changed'] == 1


def test_dsh_delta_counts_new_session_and_ignores_missing_cache(tmp_path: Path):
    assert usage.snapshot_dsh_usage(tmp_path) == {}
    after = {
        'new-session': {
            'uncachedInputTokens': 7,
            'cacheReadTokens': 11,
            'cacheWriteTokens': 2,
            'outputTokens': 3,
        }
    }
    parsed = usage.dsh_usage_delta({}, after)
    assert parsed is not None
    assert parsed['input_tokens'] == 20
    assert parsed['total_tokens'] == 23
