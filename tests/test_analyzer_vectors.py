"""Unit tests for all 4 token-waste detectors and exact Decimal Waterfall invariants."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from burnguard.analyzer import (
    CATEGORY_CACHE_BUST,
    CATEGORY_CONTEXT_DUMP,
    CATEGORY_RETRY_LOOP,
    CATEGORY_TOOL_BLOAT,
    analyze_path,
)


def test_detects_runaway_retry_error_loop(tmp_path: Path) -> None:
    turns = []
    for i in range(5):
        turns.append(
            {
                "model": "claude-sonnet-4-6",
                "uncached_input_tokens": 10_000,
                "cached_input_tokens": 20_000,
                "output_tokens": 500,
                "tool_calls": [
                    {
                        "id": f"c_{i}",
                        "name": "Bash",
                        "input": {"command": "npm test"},
                        "is_error": True,
                        "output": "Exit code 1: ReferenceError: window is not defined",
                    }
                ],
            }
        )
    f = tmp_path / "loop.jsonl"
    f.write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")

    rep = analyze_path(f)
    loop_findings = [x for x in rep.findings if x.category == CATEGORY_RETRY_LOOP]
    assert len(loop_findings) == 1
    # Each turn costs: 10k*3/1e6 ($0.03) + 20k*0.3/1e6 ($0.006) + 500*15/1e6 ($0.0075) = $0.0435
    # 4 redundant retries (turns 1..4) = 4 * 0.0435 = $0.1740 exact!
    assert rep.total_spend_usd == Decimal("0.2175")
    assert loop_findings[0].recoverable_usd == Decimal("0.174")


def test_detects_prompt_cache_busting_dynamic_timestamp(tmp_path: Path) -> None:
    turns = []
    for i in range(4):
        turns.append(
            {
                "model": "claude-opus-4-6",
                "system": f"Current time: 2026-11-23T12:00:0{i}Z\nYou are a senior Rust architect.",
                "uncached_input_tokens": 20_000,
                "cached_input_tokens": 0,
                "output_tokens": 300,
            }
        )
    f = tmp_path / "cache_bust.jsonl"
    f.write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")

    rep = analyze_path(f)
    cache_findings = [x for x in rep.findings if x.category == CATEGORY_CACHE_BUST]
    assert len(cache_findings) == 1
    assert "ISO-8601 timestamp" in cache_findings[0].title
    assert cache_findings[0].recoverable_usd > Decimal("0.50")


def test_detects_bloated_unused_mcp_tool_schemas(tmp_path: Path) -> None:
    tools = [
        {
            "name": f"mcp_unused_{idx}",
            "description": "Verbose enterprise tool definition " * 25,
            "estimated_tokens": 300,
        }
        for idx in range(20)
    ]
    tools.append({"name": "ReadFile", "description": "Read a file", "estimated_tokens": 80})

    turns = [
        {
            "model": "gpt-5",
            "tools": tools,
            "uncached_input_tokens": 12_000,
            "cached_input_tokens": 12_000,
            "output_tokens": 200,
            "tool_calls": [{"id": f"r{i}", "name": "ReadFile", "input": {"path": "main.py"}, "output": "ok"}],
        }
        for i in range(4)
    ]
    f = tmp_path / "bloat.jsonl"
    f.write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")

    rep = analyze_path(f)
    bloat = [x for x in rep.findings if x.category == CATEGORY_TOOL_BLOAT]
    assert len(bloat) == 1
    assert "20/21 tools unused" in bloat[0].title
    assert bloat[0].recoverable_usd > Decimal("0")


def test_detects_oversized_context_dump_snowball(tmp_path: Path) -> None:
    giant_log = "WARN unfiltered build log line 0123456789abcdef\n" * 600  # ~28,800 chars (~7,200 tok)
    turns = [
        {
            "model": "claude-sonnet-4-6",
            "uncached_input_tokens": 8_000,
            "cached_input_tokens": 4_000,
            "output_tokens": 200,
            "tool_calls": [{"id": "d1", "name": "Bash", "input": {"command": "cat build.log"}, "output": giant_log}],
        },
        {
            "model": "claude-sonnet-4-6",
            "uncached_input_tokens": 16_000,
            "cached_input_tokens": 4_000,
            "output_tokens": 200,
        },
        {
            "model": "claude-sonnet-4-6",
            "uncached_input_tokens": 18_000,
            "cached_input_tokens": 4_000,
            "output_tokens": 200,
        },
    ]
    f = tmp_path / "dump.jsonl"
    f.write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")

    rep = analyze_path(f)
    dumps = [x for x in rep.findings if x.category == CATEGORY_CONTEXT_DUMP]
    assert len(dumps) == 1
    assert dumps[0].recoverable_usd > Decimal("0.03")


def test_waterfall_savings_never_double_count_or_exceed_spend() -> None:
    bench_dir = Path(__file__).resolve().parent.parent / "examples" / "benchmarks"
    rep = analyze_path(bench_dir)
    assert rep.session_count == 3
    # All 4 categories must have positive recoverable savings on the benchmark suite
    assert rep.category_savings_usd[CATEGORY_RETRY_LOOP] > Decimal("0")
    assert rep.category_savings_usd[CATEGORY_CACHE_BUST] > Decimal("0")
    assert rep.category_savings_usd[CATEGORY_TOOL_BLOAT] > Decimal("0")
    assert rep.category_savings_usd[CATEGORY_CONTEXT_DUMP] > Decimal("0")

    # Exact Decimal identity invariants
    summed_findings = sum((f.recoverable_usd for f in rep.findings), Decimal("0"))
    summed_categories = sum(rep.category_savings_usd.values(), Decimal("0"))
    assert summed_findings == rep.recoverable_usd
    assert summed_categories == rep.recoverable_usd
    assert Decimal("0") < rep.recoverable_usd < rep.total_spend_usd
    assert rep.total_spend_usd - rep.recoverable_usd == rep.optimized_spend_usd


def test_clean_well_cached_session_produces_zero_findings(tmp_path: Path) -> None:
    tools = [{"name": "ReadFile", "description": "Read", "estimated_tokens": 60}]
    turns = [
        {
            "model": "claude-sonnet-4-6",
            "system": "Frozen static prompt",
            "tools": tools,
            "uncached_input_tokens": 2_000,
            "cached_input_tokens": 0,
            "cache_write_tokens": 6_000,
            "output_tokens": 200,
            "tool_calls": [{"id": "c1", "name": "ReadFile", "input": {"path": "a.py"}, "output": "x = 1"}],
        },
        {
            "model": "claude-sonnet-4-6",
            "system": "Frozen static prompt",
            "tools": tools,
            "uncached_input_tokens": 500,
            "cached_input_tokens": 7_500,
            "cache_write_tokens": 0,
            "output_tokens": 180,
            "tool_calls": [{"id": "c2", "name": "ReadFile", "input": {"path": "b.py"}, "output": "y = 2"}],
        },
    ]
    f = tmp_path / "clean.jsonl"
    f.write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")

    rep = analyze_path(f)
    assert rep.findings == []
    assert rep.recoverable_usd == Decimal("0")
    assert rep.optimized_spend_usd == rep.total_spend_usd


def test_detects_heterogeneous_consecutive_error_streak(tmp_path: Path) -> None:
    turns = [
        {
            "model": "gpt-5",
            "uncached_input_tokens": 8000,
            "output_tokens": 200,
            "tool_calls": [{"id": "e1", "name": "Bash", "input": {"cmd": "make build"}, "is_error": True, "output": "Exit code 2"}],
        },
        {
            "model": "gpt-5",
            "uncached_input_tokens": 9000,
            "output_tokens": 200,
            "tool_calls": [{"id": "e2", "name": "PythonExec", "input": {"cmd": "python setup.py"}, "is_error": True, "output": "SyntaxError"}],
        },
        {
            "model": "gpt-5",
            "uncached_input_tokens": 10000,
            "output_tokens": 200,
            "tool_calls": [{"id": "e3", "name": "CargoRun", "input": {"cmd": "cargo check"}, "is_error": True, "output": "Exit code 101"}],
        },
    ]
    f = tmp_path / "streak.jsonl"
    f.write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")
    rep = analyze_path(f)
    loops = [x for x in rep.findings if x.category == CATEGORY_RETRY_LOOP]
    assert len(loops) == 1
    assert "Consecutive error streak" in loops[0].title
    assert loops[0].recoverable_usd > Decimal("0.09")
