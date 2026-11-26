"""Exact Decimal 2026 model token pricing tables and cost math."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

ONE_MILLION = Decimal("1000000")
USD_CENT = Decimal("0.01")
USD_MICRO = Decimal("0.000001")
ZERO = Decimal("0")


@dataclass(frozen=True)
class ModelRate:
    """Per-1M-token rates in exact USD Decimal."""

    model_id: str
    provider: str
    input_per_m: Decimal
    cached_input_per_m: Decimal
    cache_write_per_m: Decimal
    output_per_m: Decimal
    reasoning_per_m: Decimal
    supports_prompt_cache: bool = True

    @property
    def cache_discount_ratio(self) -> Decimal:
        """Fraction of input cost saved when tokens hit prompt cache."""
        if self.input_per_m <= ZERO:
            return ZERO
        saved = self.input_per_m - self.cached_input_per_m
        return max(ZERO, saved / self.input_per_m)

    def to_dict(self) -> Dict[str, str | bool]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "input_per_m": str(self.input_per_m),
            "cached_input_per_m": str(self.cached_input_per_m),
            "cache_write_per_m": str(self.cache_write_per_m),
            "output_per_m": str(self.output_per_m),
            "reasoning_per_m": str(self.reasoning_per_m),
            "supports_prompt_cache": self.supports_prompt_cache,
        }


# Canonical 2026 frontier & coding-agent model pricing table (USD per 1M tokens)
MODEL_PRICING_2026: Dict[str, ModelRate] = {
    # Anthropic Claude 4.x / 3.7 family (Claude Code primary models)
    "claude-opus-4-6": ModelRate(
        model_id="claude-opus-4-6",
        provider="anthropic",
        input_per_m=Decimal("15.00"),
        cached_input_per_m=Decimal("1.50"),
        cache_write_per_m=Decimal("18.75"),
        output_per_m=Decimal("75.00"),
        reasoning_per_m=Decimal("75.00"),
    ),
    "claude-opus-4-5": ModelRate(
        model_id="claude-opus-4-5",
        provider="anthropic",
        input_per_m=Decimal("15.00"),
        cached_input_per_m=Decimal("1.50"),
        cache_write_per_m=Decimal("18.75"),
        output_per_m=Decimal("75.00"),
        reasoning_per_m=Decimal("75.00"),
    ),
    "claude-sonnet-4-6": ModelRate(
        model_id="claude-sonnet-4-6",
        provider="anthropic",
        input_per_m=Decimal("3.00"),
        cached_input_per_m=Decimal("0.30"),
        cache_write_per_m=Decimal("3.75"),
        output_per_m=Decimal("15.00"),
        reasoning_per_m=Decimal("15.00"),
    ),
    "claude-sonnet-4-5": ModelRate(
        model_id="claude-sonnet-4-5",
        provider="anthropic",
        input_per_m=Decimal("3.00"),
        cached_input_per_m=Decimal("0.30"),
        cache_write_per_m=Decimal("3.75"),
        output_per_m=Decimal("15.00"),
        reasoning_per_m=Decimal("15.00"),
    ),
    "claude-haiku-4-5": ModelRate(
        model_id="claude-haiku-4-5",
        provider="anthropic",
        input_per_m=Decimal("0.80"),
        cached_input_per_m=Decimal("0.08"),
        cache_write_per_m=Decimal("1.00"),
        output_per_m=Decimal("4.00"),
        reasoning_per_m=Decimal("4.00"),
    ),
    "claude-3-7-sonnet": ModelRate(
        model_id="claude-3-7-sonnet",
        provider="anthropic",
        input_per_m=Decimal("3.00"),
        cached_input_per_m=Decimal("0.30"),
        cache_write_per_m=Decimal("3.75"),
        output_per_m=Decimal("15.00"),
        reasoning_per_m=Decimal("15.00"),
    ),
    # OpenAI / Codex 2026 models (automatic zero-surcharge cache writes)
    "gpt-5": ModelRate(
        model_id="gpt-5",
        provider="openai",
        input_per_m=Decimal("5.00"),
        cached_input_per_m=Decimal("0.50"),
        cache_write_per_m=Decimal("5.00"),
        output_per_m=Decimal("20.00"),
        reasoning_per_m=Decimal("20.00"),
    ),
    "gpt-5-codex": ModelRate(
        model_id="gpt-5-codex",
        provider="openai",
        input_per_m=Decimal("5.00"),
        cached_input_per_m=Decimal("0.50"),
        cache_write_per_m=Decimal("5.00"),
        output_per_m=Decimal("20.00"),
        reasoning_per_m=Decimal("20.00"),
    ),
    "gpt-5-mini": ModelRate(
        model_id="gpt-5-mini",
        provider="openai",
        input_per_m=Decimal("0.60"),
        cached_input_per_m=Decimal("0.06"),
        cache_write_per_m=Decimal("0.60"),
        output_per_m=Decimal("2.40"),
        reasoning_per_m=Decimal("2.40"),
    ),
    "codex-mini": ModelRate(
        model_id="codex-mini",
        provider="openai",
        input_per_m=Decimal("1.50"),
        cached_input_per_m=Decimal("0.375"),
        cache_write_per_m=Decimal("1.50"),
        output_per_m=Decimal("6.00"),
        reasoning_per_m=Decimal("6.00"),
    ),
    "o3": ModelRate(
        model_id="o3",
        provider="openai",
        input_per_m=Decimal("2.00"),
        cached_input_per_m=Decimal("0.50"),
        cache_write_per_m=Decimal("2.00"),
        output_per_m=Decimal("8.00"),
        reasoning_per_m=Decimal("8.00"),
    ),
    "o4-mini": ModelRate(
        model_id="o4-mini",
        provider="openai",
        input_per_m=Decimal("1.10"),
        cached_input_per_m=Decimal("0.275"),
        cache_write_per_m=Decimal("1.10"),
        output_per_m=Decimal("4.40"),
        reasoning_per_m=Decimal("4.40"),
    ),
    "gpt-4.1": ModelRate(
        model_id="gpt-4.1",
        provider="openai",
        input_per_m=Decimal("2.00"),
        cached_input_per_m=Decimal("0.50"),
        cache_write_per_m=Decimal("2.00"),
        output_per_m=Decimal("8.00"),
        reasoning_per_m=Decimal("8.00"),
    ),
    "gpt-4o": ModelRate(
        model_id="gpt-4o",
        provider="openai",
        input_per_m=Decimal("2.50"),
        cached_input_per_m=Decimal("1.25"),
        cache_write_per_m=Decimal("2.50"),
        output_per_m=Decimal("10.00"),
        reasoning_per_m=Decimal("10.00"),
    ),
    # Google Gemini 2.5 & 3.x family
    "gemini-3.1-pro": ModelRate(
        model_id="gemini-3.1-pro",
        provider="google",
        input_per_m=Decimal("2.00"),
        cached_input_per_m=Decimal("0.20"),
        cache_write_per_m=Decimal("2.00"),
        output_per_m=Decimal("12.00"),
        reasoning_per_m=Decimal("12.00"),
    ),
    "gemini-3-pro": ModelRate(
        model_id="gemini-3-pro",
        provider="google",
        input_per_m=Decimal("2.00"),
        cached_input_per_m=Decimal("0.20"),
        cache_write_per_m=Decimal("2.00"),
        output_per_m=Decimal("12.00"),
        reasoning_per_m=Decimal("12.00"),
    ),
    "gemini-3-flash": ModelRate(
        model_id="gemini-3-flash",
        provider="google",
        input_per_m=Decimal("0.30"),
        cached_input_per_m=Decimal("0.03"),
        cache_write_per_m=Decimal("0.30"),
        output_per_m=Decimal("2.50"),
        reasoning_per_m=Decimal("2.50"),
    ),
    "gemini-2.5-pro": ModelRate(
        model_id="gemini-2.5-pro",
        provider="google",
        input_per_m=Decimal("1.25"),
        cached_input_per_m=Decimal("0.125"),
        cache_write_per_m=Decimal("1.25"),
        output_per_m=Decimal("10.00"),
        reasoning_per_m=Decimal("10.00"),
    ),
    "gemini-2.5-flash": ModelRate(
        model_id="gemini-2.5-flash",
        provider="google",
        input_per_m=Decimal("0.30"),
        cached_input_per_m=Decimal("0.03"),
        cache_write_per_m=Decimal("0.30"),
        output_per_m=Decimal("2.50"),
        reasoning_per_m=Decimal("2.50"),
    ),
    # DeepSeek V3.2 / R1 API
    "deepseek-v3.2": ModelRate(
        model_id="deepseek-v3.2",
        provider="deepseek",
        input_per_m=Decimal("0.55"),
        cached_input_per_m=Decimal("0.14"),
        cache_write_per_m=Decimal("0.55"),
        output_per_m=Decimal("2.19"),
        reasoning_per_m=Decimal("2.19"),
    ),
    "deepseek-r1": ModelRate(
        model_id="deepseek-r1",
        provider="deepseek",
        input_per_m=Decimal("0.55"),
        cached_input_per_m=Decimal("0.14"),
        cache_write_per_m=Decimal("0.55"),
        output_per_m=Decimal("2.19"),
        reasoning_per_m=Decimal("2.19"),
    ),
}

DEFAULT_FALLBACK_RATE = ModelRate(
    model_id="generic-frontier-2026",
    provider="generic",
    input_per_m=Decimal("3.00"),
    cached_input_per_m=Decimal("0.30"),
    cache_write_per_m=Decimal("3.75"),
    output_per_m=Decimal("15.00"),
    reasoning_per_m=Decimal("15.00"),
)

_MODEL_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("claude-opus-4-6", "claude-opus-4-6"),
    ("claude-4-6-opus", "claude-opus-4-6"),
    ("claude-opus-4-5", "claude-opus-4-5"),
    ("claude-opus-4", "claude-opus-4-6"),
    ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("claude-4-6-sonnet", "claude-sonnet-4-6"),
    ("claude-sonnet-4-5", "claude-sonnet-4-5"),
    ("claude-sonnet-4", "claude-sonnet-4-6"),
    ("claude-haiku-4-5", "claude-haiku-4-5"),
    ("claude-haiku-4", "claude-haiku-4-5"),
    ("claude-3-7-sonnet", "claude-3-7-sonnet"),
    ("claude-3-5-sonnet", "claude-3-7-sonnet"),
    ("gpt-5-mini", "gpt-5-mini"),
    ("gpt-5-codex", "gpt-5-codex"),
    ("gpt-5", "gpt-5"),
    ("codex-mini", "codex-mini"),
    ("codex", "gpt-5-codex"),
    ("o4-mini", "o4-mini"),
    ("o3", "o3"),
    ("gpt-4.1", "gpt-4.1"),
    ("gpt-4o", "gpt-4o"),
    ("gemini-3.1-pro", "gemini-3.1-pro"),
    ("gemini-3-1-pro", "gemini-3.1-pro"),
    ("gemini-3-pro", "gemini-3-pro"),
    ("gemini-3-flash", "gemini-3-flash"),
    ("gemini-2.5-pro", "gemini-2.5-pro"),
    ("gemini-2-5-pro", "gemini-2.5-pro"),
    ("gemini-2.5-flash", "gemini-2.5-flash"),
    ("gemini-2-5-flash", "gemini-2.5-flash"),
    ("deepseek-r1", "deepseek-r1"),
    ("deepseek-reasoner", "deepseek-r1"),
    ("deepseek-chat", "deepseek-v3.2"),
    ("deepseek-v3", "deepseek-v3.2"),
)


def load_custom_pricing(path: Path) -> Dict[str, ModelRate]:
    """Load custom Decimal model rates from a JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    custom: Dict[str, ModelRate] = {}
    for model_key, spec in raw.items():
        inp = Decimal(str(spec["input_per_m"]))
        cached = Decimal(str(spec.get("cached_input_per_m", inp)))
        write = Decimal(str(spec.get("cache_write_per_m", inp)))
        out = Decimal(str(spec["output_per_m"]))
        reas = Decimal(str(spec.get("reasoning_per_m", out)))
        custom[model_key.lower()] = ModelRate(
            model_id=model_key,
            provider=str(spec.get("provider", "custom")),
            input_per_m=inp,
            cached_input_per_m=cached,
            cache_write_per_m=write,
            output_per_m=out,
            reasoning_per_m=reas,
            supports_prompt_cache=bool(spec.get("supports_prompt_cache", cached < inp)),
        )
    return custom


def resolve_model_rate(
    model_name: Optional[str],
    custom_rates: Optional[Mapping[str, ModelRate]] = None,
) -> Tuple[ModelRate, bool]:
    """Resolve a raw model identifier (including date suffixes or provider prefixes) to a ModelRate.

    Returns `(rate, matched_exact_or_family)`.
    """
    if not model_name:
        return DEFAULT_FALLBACK_RATE, False

    normalized = model_name.strip().lower()
    if "/" in normalized:
        normalized = normalized.split("/")[-1]
    if normalized.startswith("models/"):
        normalized = normalized[len("models/") :]

    if custom_rates:
        if normalized in custom_rates:
            return custom_rates[normalized], True
        for key, rate in custom_rates.items():
            if normalized.startswith(key):
                return rate, True

    if normalized in MODEL_PRICING_2026:
        return MODEL_PRICING_2026[normalized], True

    for prefix, canonical in _MODEL_ALIASES:
        if normalized == prefix or normalized.startswith(prefix) or prefix in normalized:
            return MODEL_PRICING_2026[canonical], True

    return DEFAULT_FALLBACK_RATE, False


@dataclass(frozen=True)
class TurnCostBreakdown:
    """Exact Decimal cost components for a single normalized turn."""

    uncached_input_usd: Decimal
    cached_input_usd: Decimal
    cache_write_usd: Decimal
    output_usd: Decimal
    reasoning_usd: Decimal
    total_usd: Decimal

    @property
    def total_input_usd(self) -> Decimal:
        return self.uncached_input_usd + self.cached_input_usd + self.cache_write_usd


def compute_turn_cost(
    rate: ModelRate,
    *,
    uncached_input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> TurnCostBreakdown:
    """Compute exact unrounded Decimal USD spend for a single turn."""
    u_in = (Decimal(max(0, uncached_input_tokens)) * rate.input_per_m) / ONE_MILLION
    c_in = (Decimal(max(0, cached_input_tokens)) * rate.cached_input_per_m) / ONE_MILLION
    w_in = (Decimal(max(0, cache_write_tokens)) * rate.cache_write_per_m) / ONE_MILLION
    out = (Decimal(max(0, output_tokens)) * rate.output_per_m) / ONE_MILLION
    reas = (Decimal(max(0, reasoning_tokens)) * rate.reasoning_per_m) / ONE_MILLION
    total = u_in + c_in + w_in + out + reas
    return TurnCostBreakdown(
        uncached_input_usd=u_in,
        cached_input_usd=c_in,
        cache_write_usd=w_in,
        output_usd=out,
        reasoning_usd=reas,
        total_usd=total,
    )


def quantize_usd(amount: Decimal, precision: Decimal = USD_CENT) -> Decimal:
    """Quantize a Decimal USD value with standard financial ROUND_HALF_UP."""
    return amount.quantize(precision, rounding=ROUND_HALF_UP)


def format_usd(amount: Decimal, places: int = 4) -> str:
    """Format Decimal USD cleanly with configurable decimal places."""
    prec = Decimal("1") if places <= 0 else Decimal("0." + ("0" * (places - 1)) + "1")
    q = quantize_usd(amount, prec)
    return f"${q:,.{places}f}"
