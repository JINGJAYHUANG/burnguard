"""Unit tests for exact Decimal 2026 pricing and multi-schema transcript parsing."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from burnguard.parser import parse_transcript_file, parse_transcript_path
from burnguard.pricing import (
    MODEL_PRICING_2026,
    compute_turn_cost,
    load_custom_pricing,
    quantize_usd,
    resolve_model_rate,
)


def test_decimal_turn_cost_exact_precision() -> None:
    rate = MODEL_PRICING_2026["claude-opus-4-6"]
    cost = compute_turn_cost(
        rate,
        uncached_input_tokens=12_345,
        cached_input_tokens=50_000,
        cache_write_tokens=4_000,
        output_tokens=1_200,
    )
    # 12345 * 15 / 1e6 = 0.185175
    # 50000 * 1.5 / 1e6 = 0.075000
    # 4000 * 18.75 / 1e6 = 0.075000
    # 1200 * 75 / 1e6 = 0.090000
    assert isinstance(cost.total_usd, Decimal)
    assert cost.uncached_input_usd == Decimal("0.185175")
    assert cost.cached_input_usd == Decimal("0.075")
    assert cost.cache_write_usd == Decimal("0.075")
    assert cost.output_usd == Decimal("0.09")
    assert cost.total_usd == Decimal("0.425175")
    assert quantize_usd(cost.total_usd) == Decimal("0.43")


def test_model_alias_and_provider_prefix_resolution() -> None:
    r1, matched1 = resolve_model_rate("anthropic/claude-sonnet-4-6-20260514")
    assert matched1 is True
    assert r1.model_id == "claude-sonnet-4-6"

    r2, matched2 = resolve_model_rate("models/gemini-3.1-pro-preview")
    assert matched2 is True
    assert r2.model_id == "gemini-3.1-pro"

    r3, matched3 = resolve_model_rate("openai/gpt-5-codex-latest")
    assert matched3 is True
    assert r3.model_id == "gpt-5-codex"

    r_unknown, matched_u = resolve_model_rate("totally-custom-local-llm")
    assert matched_u is False
    assert r_unknown.model_id == "generic-frontier-2026"


def test_custom_pricing_json_override(tmp_path: Path) -> None:
    cfg = {
        "enterprise-sonnet": {
            "provider": "enterprise",
            "input_per_m": "2.40",
            "cached_input_per_m": "0.24",
            "cache_write_per_m": "3.00",
            "output_per_m": "12.00",
        }
    }
    p = tmp_path / "custom_pricing.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = load_custom_pricing(p)
    rate, matched = resolve_model_rate("enterprise-sonnet-v2", custom_rates=loaded)
    assert matched is True
    assert rate.input_per_m == Decimal("2.40")
    assert rate.cached_input_per_m == Decimal("0.24")


def test_parse_anthropic_claude_code_paired_jsonl(tmp_path: Path) -> None:
    lines = [
        {
            "type": "assistant",
            "system": "Static system prompt",
            "tools": [{"name": "Bash", "description": "Run shell"}],
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "pytest"}}
                ],
                "usage": {
                    "input_tokens": 4000,
                    "cache_read_input_tokens": 10000,
                    "cache_creation_input_tokens": 500,
                    "output_tokens": 250,
                },
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "is_error": True,
                        "content": "Exit code 1: 2 failed",
                    }
                ],
            },
        },
    ]
    f = tmp_path / "claude_session.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    sessions = parse_transcript_file(f)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.schema_flavor == "anthropic"
    assert len(s.turns) == 1
    t0 = s.turns[0]
    assert t0.uncached_input_tokens == 4000
    assert t0.cached_input_tokens == 10000
    assert t0.cache_write_tokens == 500
    assert t0.total_input_tokens == 14500
    assert t0.output_tokens == 250
    assert len(t0.tool_calls) == 1
    assert t0.tool_calls[0].is_error is True
    assert "2 failed" in t0.tool_calls[0].output_preview


def test_parse_openai_codex_prompt_and_reasoning_tokens(tmp_path: Path) -> None:
    doc = {
        "session_id": "codex-run-1",
        "turns": [
            {
                "model": "gpt-5-codex",
                "usage": {
                    "prompt_tokens": 20000,
                    "completion_tokens": 1500,
                    "prompt_tokens_details": {"cached_tokens": 15000},
                    "completion_tokens_details": {"reasoning_tokens": 900},
                },
            }
        ],
    }
    f = tmp_path / "openai.json"
    f.write_text(json.dumps(doc), encoding="utf-8")

    sessions = parse_transcript_file(f)
    assert len(sessions) == 1
    t0 = sessions[0].turns[0]
    assert t0.schema_flavor == "openai"
    # OpenAI prompt_tokens includes cached_tokens -> uncached must be 5000!
    assert t0.cached_input_tokens == 15000
    assert t0.uncached_input_tokens == 5000
    assert t0.reasoning_tokens == 900
    assert t0.output_tokens == 600


def test_parse_gemini_usage_metadata_and_declarations(tmp_path: Path) -> None:
    record = {
        "modelVersion": "gemini-3.1-pro",
        "systemInstruction": {"parts": [{"text": "Review TS types."}]},
        "tools": [
            {
                "functionDeclarations": [
                    {"name": "read_file", "description": "Read a file", "parameters": {"type": "object"}}
                ]
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 12000,
            "cachedContentTokenCount": 9000,
            "candidatesTokenCount": 400,
            "thoughtsTokenCount": 200,
        },
        "candidates": [
            {
                "content": {
                    "parts": [{"functionCall": {"id": "c1", "name": "read_file", "args": {"path": "a.ts"}}}]
                }
            }
        ],
    }
    f = tmp_path / "gemini.json"
    f.write_text(json.dumps(record), encoding="utf-8")

    sessions = parse_transcript_file(f)
    assert len(sessions) == 1
    t0 = sessions[0].turns[0]
    assert t0.schema_flavor == "gemini"
    assert t0.uncached_input_tokens == 3000
    assert t0.cached_input_tokens == 9000
    assert t0.output_tokens == 400
    assert t0.reasoning_tokens == 200
    assert len(t0.tool_definitions) == 1
    assert t0.tool_calls[0].name == "read_file"


def test_parse_directory_skips_mcp_and_package_configs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    (tmp_path / "mcp_config.json").write_text('{"mcpServers": {"fs": {"command": "npx"}}}', encoding="utf-8")
    (tmp_path / "run.jsonl").write_text(
        json.dumps({"model": "gpt-5", "input_tokens": 2000, "output_tokens": 100}) + "\n",
        encoding="utf-8",
    )
    sessions = parse_transcript_path(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].session_id == "run"


def test_parse_multi_session_json_bundle(tmp_path: Path) -> None:
    bundle = {
        "sessions": [
            {
                "session_id": "s1",
                "model": "claude-haiku-4-5",
                "turns": [{"uncached_input_tokens": 1000, "output_tokens": 100}],
            },
            {
                "session_id": "s2",
                "model": "gemini-3-flash",
                "turns": [{"uncached_input_tokens": 2000, "output_tokens": 200}],
            },
        ]
    }
    p = tmp_path / "bundle.json"
    p.write_text(json.dumps(bundle), encoding="utf-8")
    sessions = parse_transcript_file(p)
    assert [s.session_id for s in sessions] == ["s1", "s2"]


def test_malformed_jsonl_raises_clean_value_error(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.jsonl"
    bad.write_text('{"model": "gpt-5", "input_tokens": 100}\n{not_valid_json}\n', encoding="utf-8")
    try:
        parse_transcript_file(bad)
        assert False, "Expected ValueError on corrupt JSONL"
    except ValueError as exc:
        assert "corrupt.jsonl:2" in str(exc)
