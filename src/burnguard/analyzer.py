"""Deterministic Decimal token spend analyzer and 4-vector waste detector."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from burnguard.parser import (
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    parse_transcript_path,
)
from burnguard.pricing import (
    ONE_MILLION,
    USD_CENT,
    USD_MICRO,
    ZERO,
    ModelRate,
    TurnCostBreakdown,
    compute_turn_cost,
    quantize_usd,
    resolve_model_rate,
)

CATEGORY_RETRY_LOOP = "RUNAWAY_RETRY_LOOP"
CATEGORY_CACHE_BUST = "PROMPT_CACHE_BUSTING"
CATEGORY_TOOL_BLOAT = "BLOATED_TOOL_SCHEMAS"
CATEGORY_CONTEXT_DUMP = "OVERSIZED_CONTEXT_DUMP"

_DYNAMIC_PREFIX_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "ISO-8601 timestamp",
        re.compile(r"\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"),
    ),
    (
        "Unix epoch timestamp",
        re.compile(r"\b1[7-9]\d{8}(?:\d{3})?\b"),
    ),
    (
        "Per-turn UUID / request nonce",
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    ),
)

DUMP_THRESHOLD_TOKENS = 1500
DUMP_TARGET_SUMMARY_TOKENS = 300
LAZY_STUB_TOKENS_PER_TOOL = 12


@dataclass
class TurnTelemetry:
    """Computed exact Decimal spend and waste annotations for a single turn."""

    turn_index: int
    model: str
    resolved_model: str
    uncached_input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost: TurnCostBreakdown
    cache_hit_ratio: Decimal
    has_error: bool
    tool_names: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "model": self.model,
            "resolved_model": self.resolved_model,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_input_tokens": self.uncached_input_tokens + self.cached_input_tokens + self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "uncached_input_usd": str(quantize_usd(self.cost.uncached_input_usd, USD_MICRO)),
            "cached_input_usd": str(quantize_usd(self.cost.cached_input_usd, USD_MICRO)),
            "cache_write_usd": str(quantize_usd(self.cost.cache_write_usd, USD_MICRO)),
            "output_usd": str(quantize_usd(self.cost.output_usd + self.cost.reasoning_usd, USD_MICRO)),
            "total_usd": str(quantize_usd(self.cost.total_usd, USD_MICRO)),
            "cache_hit_ratio": str(quantize_usd(self.cache_hit_ratio, Decimal("0.0001"))),
            "has_error": self.has_error,
            "tool_names": self.tool_names,
        }


@dataclass
class CostFinding:
    """Actionable cost optimization finding with exact Decimal recoverable USD."""

    finding_id: str
    session_id: str
    category: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW
    title: str
    affected_turns: List[int]
    wasted_tokens: int
    standalone_savings_usd: Decimal
    recoverable_usd: Decimal
    evidence: str
    recommendation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "session_id": self.session_id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "affected_turns": self.affected_turns,
            "wasted_tokens": self.wasted_tokens,
            "standalone_savings_usd": str(quantize_usd(self.standalone_savings_usd, USD_MICRO)),
            "recoverable_usd": str(quantize_usd(self.recoverable_usd, USD_MICRO)),
            "recoverable_usd_display": f"${quantize_usd(self.recoverable_usd, USD_CENT):,.2f}",
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass
class SessionSummary:
    """Complete spend & optimization telemetry for a single session."""

    session_id: str
    source_path: str
    schema_flavor: str
    primary_model: str
    turn_count: int
    uncached_input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_spend_usd: Decimal
    recoverable_usd: Decimal
    optimized_spend_usd: Decimal
    savings_pct: Decimal
    cache_hit_pct: Decimal
    turns: List[TurnTelemetry] = field(default_factory=list)
    findings: List[CostFinding] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return self.uncached_input_tokens + self.cached_input_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.output_tokens + self.reasoning_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_path": self.source_path,
            "schema_flavor": self.schema_flavor,
            "primary_model": self.primary_model,
            "turn_count": self.turn_count,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_input_tokens": self.total_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "total_spend_usd": str(quantize_usd(self.total_spend_usd, USD_MICRO)),
            "recoverable_usd": str(quantize_usd(self.recoverable_usd, USD_MICRO)),
            "optimized_spend_usd": str(quantize_usd(self.optimized_spend_usd, USD_MICRO)),
            "savings_pct": str(quantize_usd(self.savings_pct, Decimal("0.01"))),
            "cache_hit_pct": str(quantize_usd(self.cache_hit_pct, Decimal("0.01"))),
            "turns": [t.to_dict() for t in self.turns],
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class AnalysisReport:
    """Aggregated multi-session cost & waste report."""

    target_path: str
    session_count: int
    turn_count: int
    total_tokens: int
    total_input_tokens: int
    total_cached_tokens: int
    total_output_tokens: int
    total_spend_usd: Decimal
    recoverable_usd: Decimal
    optimized_spend_usd: Decimal
    savings_pct: Decimal
    overall_cache_hit_pct: Decimal
    category_savings_usd: Dict[str, Decimal]
    model_spend_usd: Dict[str, Decimal]
    sessions: List[SessionSummary]
    findings: List[CostFinding]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_path": self.target_path,
            "session_count": self.session_count,
            "turn_count": self.turn_count,
            "total_tokens": self.total_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_spend_usd": str(quantize_usd(self.total_spend_usd, USD_MICRO)),
            "total_spend_display": f"${quantize_usd(self.total_spend_usd, USD_CENT):,.2f}",
            "recoverable_usd": str(quantize_usd(self.recoverable_usd, USD_MICRO)),
            "recoverable_display": f"${quantize_usd(self.recoverable_usd, USD_CENT):,.2f}",
            "optimized_spend_usd": str(quantize_usd(self.optimized_spend_usd, USD_MICRO)),
            "optimized_spend_display": f"${quantize_usd(self.optimized_spend_usd, USD_CENT):,.2f}",
            "savings_pct": str(quantize_usd(self.savings_pct, Decimal("0.01"))),
            "overall_cache_hit_pct": str(quantize_usd(self.overall_cache_hit_pct, Decimal("0.01"))),
            "category_savings_usd": {
                k: str(quantize_usd(v, USD_MICRO)) for k, v in self.category_savings_usd.items()
            },
            "model_spend_usd": {
                k: str(quantize_usd(v, USD_MICRO)) for k, v in self.model_spend_usd.items()
            },
            "sessions": [s.to_dict() for s in self.sessions],
            "findings": [f.to_dict() for f in self.findings],
        }


def _price_input_token_removal(
    tokens_to_remove: int,
    avail_uncached: int,
    avail_cached: int,
    rate: ModelRate,
) -> Tuple[Decimal, int, int]:
    """Remove `tokens_to_remove` from a turn's remaining input pool, returning `(saved_usd, new_uncached, new_cached)`.

    Removes uncached tokens proportionally to the turn's uncached/cached mix so savings never exceed real spend.
    """
    total_avail = avail_uncached + avail_cached
    if tokens_to_remove <= 0 or total_avail <= 0:
        return ZERO, avail_uncached, avail_cached

    clamped = min(tokens_to_remove, total_avail)
    # Split removal proportionally across uncached and cached pools
    remove_uncached = int((Decimal(clamped) * Decimal(avail_uncached)) / Decimal(total_avail))
    remove_cached = clamped - remove_uncached
    if remove_cached > avail_cached:
        overflow = remove_cached - avail_cached
        remove_cached = avail_cached
        remove_uncached = min(avail_uncached, remove_uncached + overflow)
    if remove_uncached > avail_uncached:
        remove_uncached = avail_uncached

    saved = (
        (Decimal(remove_uncached) * rate.input_per_m)
        + (Decimal(remove_cached) * rate.cached_input_per_m)
    ) / ONE_MILLION
    return saved, avail_uncached - remove_uncached, avail_cached - remove_cached


def _severity_from_usd(amount: Decimal, share_of_session: Decimal) -> str:
    if amount >= Decimal("1.00") or share_of_session >= Decimal("0.25"):
        return "CRITICAL"
    if amount >= Decimal("0.20") or share_of_session >= Decimal("0.12"):
        return "HIGH"
    if amount >= Decimal("0.03") or share_of_session >= Decimal("0.05"):
        return "MEDIUM"
    return "LOW"


def _detect_retry_loops(
    session: NormalizedSession,
    telemetries: Sequence[TurnTelemetry],
) -> Tuple[List[Tuple[List[int], List[int], str, str]], Set[int]]:
    """Identify runaway retry/error loops.

    Returns `(clusters, eliminated_turns)` where each cluster is
    `(all_loop_turns, redundant_turns, loop_label, preview)`.
    """
    clusters: List[Tuple[List[int], List[int], str, str]] = []
    eliminated: Set[int] = set()

    # 1. Exact/normalized signature repetition (identical tool+target invoked >=3 times, or >=2 with error)
    sig_turns: Dict[str, List[Tuple[int, ToolCallRecord]]] = {}
    for t in session.turns:
        for tc in t.tool_calls:
            sig_turns.setdefault(tc.normalized_signature, []).append((t.turn_index, tc))

    for sig, occurrences in sig_turns.items():
        distinct_turns = sorted({t_idx for t_idx, _ in occurrences})
        error_count = sum(1 for _, tc in occurrences if tc.is_error)
        if len(distinct_turns) >= 3 and (error_count >= 2 or len(distinct_turns) >= 4):
            first_tc = occurrences[0][1]
            redundant = [ti for ti in distinct_turns[1:] if ti not in eliminated]
            if redundant:
                eliminated.update(redundant)
                label = f"{first_tc.name} ({first_tc.arguments_preview or 'repeated arguments'})"
                err_snippet = next((tc.output_preview for _, tc in occurrences if tc.output_preview), "repeated invocation")
                clusters.append((distinct_turns, redundant, label, err_snippet))

    # 2. Consecutive failing-turn streaks (>=3 consecutive turns with failing tool calls not already captured)
    streak: List[int] = []
    for t in session.turns:
        has_err = telemetries[t.turn_index].has_error
        if has_err:
            streak.append(t.turn_index)
        else:
            if len(streak) >= 3:
                redundant = [ti for ti in streak[1:] if ti not in eliminated]
                if redundant:
                    eliminated.update(redundant)
                    tool_names = sorted(
                        {tc.name for ti in streak for tc in session.turns[ti].tool_calls}
                    )
                    err_prev = next(
                        (
                            tc.output_preview
                            for ti in streak
                            for tc in session.turns[ti].tool_calls
                            if tc.is_error and tc.output_preview
                        ),
                        "consecutive failing tool executions",
                    )
                    clusters.append(
                        (
                            list(streak),
                            redundant,
                            f"Consecutive error streak across {', '.join(tool_names) or 'tools'}",
                            err_prev,
                        )
                    )
            streak = []

    if len(streak) >= 3:
        redundant = [ti for ti in streak[1:] if ti not in eliminated]
        if redundant:
            eliminated.update(redundant)
            tool_names = sorted(
                {tc.name for ti in streak for tc in session.turns[ti].tool_calls}
            )
            err_prev = next(
                (
                    tc.output_preview
                    for ti in streak
                    for tc in session.turns[ti].tool_calls
                    if tc.is_error and tc.output_preview
                ),
                "consecutive failing tool executions",
            )
            clusters.append(
                (
                    list(streak),
                    redundant,
                    f"Consecutive error streak across {', '.join(tool_names) or 'tools'}",
                    err_prev,
                )
            )

    return clusters, eliminated


def analyze_single_session(
    session: NormalizedSession,
    *,
    model_override: Optional[str] = None,
    custom_rates: Optional[Mapping[str, ModelRate]] = None,
) -> SessionSummary:
    """Compute exact Decimal spend and run waterfall 4-vector waste analysis on one session."""
    telemetries: List[TurnTelemetry] = []
    rates_by_turn: List[ModelRate] = []

    for turn in session.turns:
        effective_model = model_override or turn.model
        rate, _ = resolve_model_rate(effective_model, custom_rates=custom_rates)
        rates_by_turn.append(rate)
        cost = compute_turn_cost(
            rate,
            uncached_input_tokens=turn.uncached_input_tokens,
            cached_input_tokens=turn.cached_input_tokens,
            cache_write_tokens=turn.cache_write_tokens,
            output_tokens=turn.output_tokens,
            reasoning_tokens=turn.reasoning_tokens,
        )
        tot_in = turn.total_input_tokens
        hit_ratio = (
            Decimal(turn.cached_input_tokens) / Decimal(tot_in)
            if tot_in > 0
            else ZERO
        )
        has_err = any(tc.is_error for tc in turn.tool_calls)
        telemetries.append(
            TurnTelemetry(
                turn_index=turn.turn_index,
                model=effective_model,
                resolved_model=rate.model_id,
                uncached_input_tokens=turn.uncached_input_tokens,
                cached_input_tokens=turn.cached_input_tokens,
                cache_write_tokens=turn.cache_write_tokens,
                output_tokens=turn.output_tokens,
                reasoning_tokens=turn.reasoning_tokens,
                cost=cost,
                cache_hit_ratio=hit_ratio,
                has_error=has_err,
                tool_names=[tc.name for tc in turn.tool_calls],
            )
        )

    total_spend = sum((t.cost.total_usd for t in telemetries), ZERO)
    findings: List[CostFinding] = []
    finding_seq = 0

    def next_id(prefix: str) -> str:
        nonlocal finding_seq
        finding_seq += 1
        return f"BG-{prefix}-{finding_seq:03d}"

    # Track remaining mutable input pools per turn for mathematically exact waterfall de-duplication
    rem_uncached = [t.uncached_input_tokens for t in telemetries]
    rem_cached = [t.cached_input_tokens for t in telemetries]

    # -------------------------------------------------------------------------
    # DETECTOR 1: Runaway Retry / Error Loops (eliminates redundant turns first)
    # -------------------------------------------------------------------------
    retry_clusters, eliminated_turns = _detect_retry_loops(session, telemetries)
    for all_turns, redundant_turns, label, err_preview in retry_clusters:
        standalone_usd = sum((telemetries[ti].cost.total_usd for ti in redundant_turns), ZERO)
        waterfall_usd = standalone_usd  # Stage 1 in waterfall
        wasted_tok = sum(session.turns[ti].total_tokens for ti in redundant_turns)
        for ti in redundant_turns:
            rem_uncached[ti] = 0
            rem_cached[ti] = 0

        if waterfall_usd > ZERO:
            share = (waterfall_usd / total_spend) if total_spend > ZERO else ZERO
            findings.append(
                CostFinding(
                    finding_id=next_id("LOOP"),
                    session_id=session.session_id,
                    category=CATEGORY_RETRY_LOOP,
                    severity=_severity_from_usd(waterfall_usd, share),
                    title=f"Runaway Retry Loop: {label[:72]}",
                    affected_turns=all_turns,
                    wasted_tokens=wasted_tok,
                    standalone_savings_usd=standalone_usd,
                    recoverable_usd=waterfall_usd,
                    evidence=(
                        f"Executed across {len(all_turns)} turns (turns {all_turns[0]}..{all_turns[-1]}) "
                        f"with {len(redundant_turns)} redundant retry turns re-billing full context. "
                        f"Sample error: '{err_preview[:130]}'"
                    ),
                    recommendation=(
                        "Add a max-2-retry circuit breaker per normalized `(tool_name, args_hash)` in your "
                        "agent harness and force root-cause file inspection before re-running failing commands."
                    ),
                )
            )

    # -------------------------------------------------------------------------
    # DETECTOR 2: Oversized File / Command Dumps (snowballing across later turns)
    # -------------------------------------------------------------------------
    n_turns = len(session.turns)
    for t_idx, turn in enumerate(session.turns):
        if t_idx >= n_turns - 1:
            continue  # A dump on the very last turn isn't re-sent on subsequent turns
        for tc in turn.tool_calls:
            if tc.output_estimated_tokens < DUMP_THRESHOLD_TOKENS:
                continue
            excess = tc.output_estimated_tokens - DUMP_TARGET_SUMMARY_TOKENS
            standalone_saved = ZERO
            waterfall_saved = ZERO
            impacted_turns: List[int] = []
            wasted_tokens_total = 0

            for future_idx in range(t_idx + 1, n_turns):
                rate_f = rates_by_turn[future_idx]
                orig_t = telemetries[future_idx]
                s_delta, _, _ = _price_input_token_removal(
                    excess,
                    orig_t.uncached_input_tokens,
                    orig_t.cached_input_tokens,
                    rate_f,
                )
                standalone_saved += s_delta

                if future_idx in eliminated_turns:
                    continue

                w_delta, new_u, new_c = _price_input_token_removal(
                    excess,
                    rem_uncached[future_idx],
                    rem_cached[future_idx],
                    rate_f,
                )
                if w_delta > ZERO:
                    removed_tok = (rem_uncached[future_idx] - new_u) + (rem_cached[future_idx] - new_c)
                    rem_uncached[future_idx] = new_u
                    rem_cached[future_idx] = new_c
                    waterfall_saved += w_delta
                    wasted_tokens_total += removed_tok
                    impacted_turns.append(future_idx)

            if standalone_saved > ZERO and (waterfall_saved > ZERO or standalone_saved >= Decimal("0.001")):
                share = (waterfall_saved / total_spend) if total_spend > ZERO else ZERO
                findings.append(
                    CostFinding(
                        finding_id=next_id("DUMP"),
                        session_id=session.session_id,
                        category=CATEGORY_CONTEXT_DUMP,
                        severity=_severity_from_usd(max(waterfall_saved, standalone_saved), share),
                        title=f"Oversized Output Dump from `{tc.name}` (~{tc.output_estimated_tokens:,} tok)",
                        affected_turns=[t_idx] + impacted_turns,
                        wasted_tokens=wasted_tokens_total or (excess * (n_turns - t_idx - 1)),
                        standalone_savings_usd=standalone_saved,
                        recoverable_usd=waterfall_saved,
                        evidence=(
                            f"Turn {t_idx} `{tc.name}` ({tc.arguments_preview[:70]}) injected "
                            f"~{tc.output_estimated_tokens:,} tokens ({tc.output_chars:,} chars) into history, "
                            f"compounding across {n_turns - t_idx - 1} subsequent turns."
                        ),
                        recommendation=(
                            f"Filter or paginate `{tc.name}` output (e.g., line-range reads, `grep -n`, "
                            f"`pytest -q --tb=short`, or head/tail truncation at {DUMP_TARGET_SUMMARY_TOKENS} tokens)."
                        ),
                    )
                )

    # -------------------------------------------------------------------------
    # DETECTOR 3: Bloated Tool / MCP Schemas (unused eager tools injected every turn)
    # -------------------------------------------------------------------------
    used_tools: Set[str] = {
        tc.name for t in session.turns for tc in t.tool_calls
    }
    declared_by_name: Dict[str, int] = {}
    for t in session.turns:
        for td in t.tool_definitions:
            declared_by_name[td.name] = max(declared_by_name.get(td.name, 0), td.estimated_tokens)

    unused_tools = {
        name: tok for name, tok in declared_by_name.items() if name not in used_tools
    }
    unused_count = len(unused_tools)
    unused_tok_per_turn = sum(unused_tools.values())
    total_declared_count = len(declared_by_name)

    if unused_count >= 5 and unused_tok_per_turn >= 500:
        reclaim_per_turn = max(0, unused_tok_per_turn - (unused_count * LAZY_STUB_TOKENS_PER_TOOL))
        if reclaim_per_turn > 0:
            standalone_tool_saved = ZERO
            waterfall_tool_saved = ZERO
            affected_tool_turns: List[int] = []
            wasted_tool_tokens = 0

            for ti, turn in enumerate(session.turns):
                if not turn.tool_definitions and turn.tool_schema_tokens == 0:
                    continue
                rate_t = rates_by_turn[ti]
                orig_t = telemetries[ti]
                s_delta, _, _ = _price_input_token_removal(
                    reclaim_per_turn,
                    orig_t.uncached_input_tokens,
                    orig_t.cached_input_tokens,
                    rate_t,
                )
                standalone_tool_saved += s_delta

                if ti in eliminated_turns:
                    continue

                w_delta, new_u, new_c = _price_input_token_removal(
                    reclaim_per_turn,
                    rem_uncached[ti],
                    rem_cached[ti],
                    rate_t,
                )
                if w_delta > ZERO:
                    removed_tok = (rem_uncached[ti] - new_u) + (rem_cached[ti] - new_c)
                    rem_uncached[ti] = new_u
                    rem_cached[ti] = new_c
                    waterfall_tool_saved += w_delta
                    wasted_tool_tokens += removed_tok
                    affected_tool_turns.append(ti)

            if standalone_tool_saved > ZERO:
                sample_unused = ", ".join(sorted(unused_tools.keys())[:6])
                share = (waterfall_tool_saved / total_spend) if total_spend > ZERO else ZERO
                findings.append(
                    CostFinding(
                        finding_id=next_id("MCP"),
                        session_id=session.session_id,
                        category=CATEGORY_TOOL_BLOAT,
                        severity=_severity_from_usd(max(waterfall_tool_saved, standalone_tool_saved), share),
                        title=(
                            f"Bloated Tool/MCP Schemas: {unused_count}/{total_declared_count} "
                            f"tools unused (~{unused_tok_per_turn:,} tok/turn)"
                        ),
                        affected_turns=affected_tool_turns or list(range(n_turns)),
                        wasted_tokens=wasted_tool_tokens or (reclaim_per_turn * n_turns),
                        standalone_savings_usd=standalone_tool_saved,
                        recoverable_usd=waterfall_tool_saved,
                        evidence=(
                            f"{unused_count} of {total_declared_count} declared tool schemas "
                            f"(~{unused_tok_per_turn:,} tokens/turn; e.g. {sample_unused}) were injected across "
                            f"{n_turns} turns without ever being invoked."
                        ),
                        recommendation=(
                            "Enable MCP lazy tool loading (`defer_loading: true` / Tool Search) or scope "
                            "`allowedTools` per workspace so unused MCP schemas are loaded on-demand."
                        ),
                    )
                )

    # -------------------------------------------------------------------------
    # DETECTOR 4: Prompt-Cache Busting Patterns (dynamic prefixes / uncached context)
    # -------------------------------------------------------------------------
    if n_turns >= 2:
        sys_prompts = [t.system_prompt for t in session.turns if t.system_prompt]
        mutating_prefix = len(set(sys_prompts)) > 1 if len(sys_prompts) >= 2 else False
        matched_dynamic_reasons: List[str] = []
        for reason_label, regex in _DYNAMIC_PREFIX_PATTERNS:
            if any(regex.search(sp[:1200]) for sp in sys_prompts):
                matched_dynamic_reasons.append(reason_label)

        tot_session_in = sum(t.total_input_tokens for t in session.turns)
        tot_session_cached = sum(t.cached_input_tokens for t in session.turns)
        overall_hit_ratio = (
            Decimal(tot_session_cached) / Decimal(tot_session_in)
            if tot_session_in > 0
            else ZERO
        )

        # Evaluate cacheable prefix savings across turns 1..N-1 on models supporting prompt caching
        standalone_cache_saved = ZERO
        waterfall_cache_saved = ZERO
        cache_bust_turns: List[int] = []
        cacheable_tokens_missed = 0

        # Account for initial Turn 0 cache-write surcharge if Anthropic-style cache creation wasn't paid yet
        rate0 = rates_by_turn[0]
        write_surcharge_per_m = max(ZERO, rate0.cache_write_per_m - rate0.input_per_m)
        base_prefix_tok_orig = int(Decimal(telemetries[0].uncached_input_tokens) * Decimal("0.85"))
        base_prefix_tok_rem = int(Decimal(rem_uncached[0]) * Decimal("0.85"))

        standalone_write_cost = (Decimal(base_prefix_tok_orig) * write_surcharge_per_m) / ONE_MILLION
        waterfall_write_cost = (Decimal(base_prefix_tok_rem) * write_surcharge_per_m) / ONE_MILLION

        prev_orig_total = session.turns[0].total_input_tokens
        prev_rem_total = rem_uncached[0] + rem_cached[0] + session.turns[0].cache_write_tokens

        for ti in range(1, n_turns):
            rate_t = rates_by_turn[ti]
            if not rate_t.supports_prompt_cache:
                prev_orig_total = session.turns[ti].total_input_tokens
                prev_rem_total = rem_uncached[ti] + rem_cached[ti]
                continue

            discount_per_m = max(ZERO, rate_t.input_per_m - rate_t.cached_input_per_m)
            orig_t = telemetries[ti]

            # Standalone: up to 85% of prior turn's context should hit cache on turn `ti`
            expected_cached_orig = int(Decimal(prev_orig_total) * Decimal("0.85"))
            missed_orig = min(
                orig_t.uncached_input_tokens,
                max(0, expected_cached_orig - orig_t.cached_input_tokens),
            )
            if missed_orig >= 1024:
                standalone_cache_saved += (Decimal(missed_orig) * discount_per_m) / ONE_MILLION

            prev_orig_total = session.turns[ti].total_input_tokens

            if ti in eliminated_turns:
                continue

            expected_cached_rem = int(Decimal(prev_rem_total) * Decimal("0.85"))
            missed_rem = min(
                rem_uncached[ti],
                max(0, expected_cached_rem - rem_cached[ti]),
            )
            if missed_rem >= 1024:
                w_gain = (Decimal(missed_rem) * discount_per_m) / ONE_MILLION
                waterfall_cache_saved += w_gain
                cacheable_tokens_missed += missed_rem
                cache_bust_turns.append(ti)
                rem_uncached[ti] -= missed_rem
                rem_cached[ti] += missed_rem

            prev_rem_total = rem_uncached[ti] + rem_cached[ti] + session.turns[ti].cache_write_tokens

        standalone_net_cache = max(ZERO, standalone_cache_saved - standalone_write_cost)
        waterfall_net_cache = max(ZERO, waterfall_cache_saved - waterfall_write_cost)

        if (mutating_prefix or overall_hit_ratio < Decimal("0.25")) and standalone_net_cache >= Decimal("0.001"):
            if mutating_prefix and matched_dynamic_reasons:
                cause_desc = (
                    f"System prompt mutates across turns due to injected {', '.join(matched_dynamic_reasons)}, "
                    f"invalidating KV prefix cache (current cache hit rate: {overall_hit_ratio * Decimal('100'):.1f}%)."
                )
                title = f"Prompt-Cache Busting: Dynamic {matched_dynamic_reasons[0]} in System Prefix"
            elif mutating_prefix:
                cause_desc = (
                    f"System prompt content changes between turns ({len(set(sys_prompts))} distinct variants), "
                    f"breaking prefix cache matching (hit rate: {overall_hit_ratio * Decimal('100'):.1f}%)."
                )
                title = "Prompt-Cache Busting: Mutating System Prompt Prefix"
            else:
                cause_desc = (
                    f"Multi-turn mega-context re-sent uncached ({overall_hit_ratio * Decimal('100'):.1f}% cache hit rate "
                    f"across {tot_session_in:,} input tokens) without ephemeral cache breakpoints or stable ordering."
                )
                title = f"Uncached Mega-Context ({overall_hit_ratio * Decimal('100'):.1f}% Cache Hit Rate)"

            share = (waterfall_net_cache / total_spend) if total_spend > ZERO else ZERO
            findings.append(
                CostFinding(
                    finding_id=next_id("CACHE"),
                    session_id=session.session_id,
                    category=CATEGORY_CACHE_BUST,
                    severity=_severity_from_usd(max(waterfall_net_cache, standalone_net_cache), share),
                    title=title,
                    affected_turns=cache_bust_turns or list(range(1, n_turns)),
                    wasted_tokens=cacheable_tokens_missed,
                    standalone_savings_usd=standalone_net_cache,
                    recoverable_usd=waterfall_net_cache,
                    evidence=cause_desc,
                    recommendation=(
                        "Freeze static system instructions & deterministically sorted tool schemas at the top of "
                        "the prompt, move dynamic timestamps/status into the latest user message, and attach "
                        "`cache_control: {\"type\": \"ephemeral\"}` breakpoints."
                    ),
                )
            )

    # Sort findings highest recoverable savings first
    findings.sort(key=lambda f: (f.recoverable_usd, f.standalone_savings_usd), reverse=True)

    recoverable_total = sum((f.recoverable_usd for f in findings), ZERO)
    if recoverable_total > total_spend:
        recoverable_total = total_spend
    optimized_spend = max(ZERO, total_spend - recoverable_total)
    savings_pct = (
        (recoverable_total * Decimal("100")) / total_spend
        if total_spend > ZERO
        else ZERO
    )

    u_sum = sum(t.uncached_input_tokens for t in telemetries)
    c_sum = sum(t.cached_input_tokens for t in telemetries)
    w_sum = sum(t.cache_write_tokens for t in telemetries)
    o_sum = sum(t.output_tokens for t in telemetries)
    r_sum = sum(t.reasoning_tokens for t in telemetries)
    tot_in_sum = u_sum + c_sum + w_sum
    cache_hit_pct = (
        (Decimal(c_sum) * Decimal("100")) / Decimal(tot_in_sum)
        if tot_in_sum > 0
        else ZERO
    )

    return SessionSummary(
        session_id=session.session_id,
        source_path=session.source_path,
        schema_flavor=session.schema_flavor,
        primary_model=model_override or session.primary_model,
        turn_count=len(telemetries),
        uncached_input_tokens=u_sum,
        cached_input_tokens=c_sum,
        cache_write_tokens=w_sum,
        output_tokens=o_sum,
        reasoning_tokens=r_sum,
        total_spend_usd=total_spend,
        recoverable_usd=recoverable_total,
        optimized_spend_usd=optimized_spend,
        savings_pct=savings_pct,
        cache_hit_pct=cache_hit_pct,
        turns=telemetries,
        findings=findings,
    )


def analyze_sessions(
    sessions: Sequence[NormalizedSession],
    *,
    target_path: str = "<memory>",
    model_override: Optional[str] = None,
    custom_rates: Optional[Mapping[str, ModelRate]] = None,
) -> AnalysisReport:
    """Analyze a collection of NormalizedSessions and aggregate exact Decimal metrics."""
    summaries = [
        analyze_single_session(
            sess,
            model_override=model_override,
            custom_rates=custom_rates,
        )
        for sess in sessions
    ]

    total_spend = sum((s.total_spend_usd for s in summaries), ZERO)
    total_recoverable = sum((s.recoverable_usd for s in summaries), ZERO)
    optimized = max(ZERO, total_spend - total_recoverable)
    savings_pct = (
        (total_recoverable * Decimal("100")) / total_spend
        if total_spend > ZERO
        else ZERO
    )

    total_in = sum(s.total_input_tokens for s in summaries)
    total_cached = sum(s.cached_input_tokens for s in summaries)
    total_out = sum(s.output_tokens + s.reasoning_tokens for s in summaries)
    total_tok = total_in + total_out
    overall_cache_pct = (
        (Decimal(total_cached) * Decimal("100")) / Decimal(total_in)
        if total_in > 0
        else ZERO
    )

    cat_savings: Dict[str, Decimal] = {
        CATEGORY_RETRY_LOOP: ZERO,
        CATEGORY_CACHE_BUST: ZERO,
        CATEGORY_TOOL_BLOAT: ZERO,
        CATEGORY_CONTEXT_DUMP: ZERO,
    }
    all_findings: List[CostFinding] = []
    for s in summaries:
        for f in s.findings:
            cat_savings[f.category] = cat_savings.get(f.category, ZERO) + f.recoverable_usd
            all_findings.append(f)

    all_findings.sort(key=lambda f: (f.recoverable_usd, f.standalone_savings_usd), reverse=True)

    model_spend: Dict[str, Decimal] = {}
    for s in summaries:
        for t in s.turns:
            model_spend[t.resolved_model] = model_spend.get(t.resolved_model, ZERO) + t.cost.total_usd

    return AnalysisReport(
        target_path=target_path,
        session_count=len(summaries),
        turn_count=sum(s.turn_count for s in summaries),
        total_tokens=total_tok,
        total_input_tokens=total_in,
        total_cached_tokens=total_cached,
        total_output_tokens=total_out,
        total_spend_usd=total_spend,
        recoverable_usd=total_recoverable,
        optimized_spend_usd=optimized,
        savings_pct=savings_pct,
        overall_cache_hit_pct=overall_cache_pct,
        category_savings_usd=cat_savings,
        model_spend_usd=model_spend,
        sessions=summaries,
        findings=all_findings,
    )


def analyze_path(
    path: Path,
    *,
    model_override: Optional[str] = None,
    custom_rates: Optional[Mapping[str, ModelRate]] = None,
) -> AnalysisReport:
    """Parse and analyze a transcript file or directory."""
    sessions = parse_transcript_path(path)
    return analyze_sessions(
        sessions,
        target_path=str(path),
        model_override=model_override,
        custom_rates=custom_rates,
    )
