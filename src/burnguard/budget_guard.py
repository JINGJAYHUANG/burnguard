"""Deterministic pre-flight / cron CI budget circuit-breaker evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from burnguard.analyzer import AnalysisReport, CATEGORY_RETRY_LOOP, analyze_path
from burnguard.mcp_audit import McpAuditReport, audit_mcp_file
from burnguard.pricing import ModelRate, USD_CENT, USD_MICRO, ZERO, quantize_usd


class ExitCode(IntEnum):
    """Deterministic exit codes for CI pipelines, cron monitors, and agent harnesses."""

    PASS = 0
    CLI_USAGE_ERROR = 1
    BREACH_BUDGET = 2
    BREACH_WASTE = 3
    BREACH_SECURITY = 4
    WARN = 10


@dataclass
class BudgetViolation:
    """Single threshold breach or warning emitted by budget-guard."""

    level: str  # ERROR | WARN
    code: str
    exit_code: int
    message: str
    actual: str
    threshold: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "exit_code": self.exit_code,
            "message": self.message,
            "actual": self.actual,
            "threshold": self.threshold,
        }


@dataclass
class BudgetDecision:
    """Result of deterministic budget circuit-breaker evaluation."""

    status: str  # PASS | WARN | BLOCK
    exit_code: int
    evaluated_spend_usd: Decimal
    projected_total_usd: Decimal
    recoverable_waste_usd: Decimal
    waste_pct: Decimal
    retry_loop_count: int
    mcp_critical_count: int
    session_limit_usd: Optional[Decimal] = None
    daily_limit_usd: Optional[Decimal] = None
    monthly_limit_usd: Optional[Decimal] = None
    headroom_usd: Optional[Decimal] = None
    violations: List[BudgetViolation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": int(self.exit_code),
            "circuit_breaker_tripped": self.status == "BLOCK",
            "evaluated_spend_usd": str(quantize_usd(self.evaluated_spend_usd, USD_MICRO)),
            "projected_total_usd": str(quantize_usd(self.projected_total_usd, USD_MICRO)),
            "recoverable_waste_usd": str(quantize_usd(self.recoverable_waste_usd, USD_MICRO)),
            "waste_pct": str(quantize_usd(self.waste_pct, Decimal("0.01"))),
            "retry_loop_count": self.retry_loop_count,
            "mcp_critical_count": self.mcp_critical_count,
            "session_limit_usd": str(quantize_usd(self.session_limit_usd, USD_CENT))
            if self.session_limit_usd is not None
            else None,
            "daily_limit_usd": str(quantize_usd(self.daily_limit_usd, USD_CENT))
            if self.daily_limit_usd is not None
            else None,
            "monthly_limit_usd": str(quantize_usd(self.monthly_limit_usd, USD_CENT))
            if self.monthly_limit_usd is not None
            else None,
            "headroom_usd": str(quantize_usd(self.headroom_usd, USD_MICRO))
            if self.headroom_usd is not None
            else None,
            "violations": [v.to_dict() for v in self.violations],
        }


def evaluate_budget(
    *,
    transcript_path: Optional[Path] = None,
    analysis_report: Optional[AnalysisReport] = None,
    current_spend_usd: Decimal = ZERO,
    projected_next_usd: Decimal = ZERO,
    session_limit_usd: Optional[Decimal] = None,
    daily_limit_usd: Optional[Decimal] = None,
    monthly_limit_usd: Optional[Decimal] = None,
    warn_pct: Decimal = Decimal("80"),
    strict_warn: bool = False,
    max_waste_pct: Optional[Decimal] = None,
    max_retry_loops: Optional[int] = None,
    mcp_config_path: Optional[Path] = None,
    mcp_report: Optional[McpAuditReport] = None,
    fail_on_critical_mcp: bool = True,
    custom_rates: Optional[Mapping[str, ModelRate]] = None,
) -> BudgetDecision:
    """Run deterministic circuit-breaker checks across USD caps, waste ratios, and MCP security."""
    report = analysis_report
    if report is None and transcript_path is not None:
        report = analyze_path(transcript_path, custom_rates=custom_rates)

    transcript_spend = report.total_spend_usd if report is not None else ZERO
    recoverable = report.recoverable_usd if report is not None else ZERO
    waste_pct = report.savings_pct if report is not None else ZERO
    retry_loops = (
        sum(1 for f in report.findings if f.category == CATEGORY_RETRY_LOOP)
        if report is not None
        else 0
    )

    evaluated_spend = current_spend_usd + transcript_spend
    projected_total = evaluated_spend + projected_next_usd

    violations: List[BudgetViolation] = []
    headrooms: List[Decimal] = []

    def _check_cap(label: str, code_prefix: str, actual_val: Decimal, limit_val: Optional[Decimal]) -> None:
        if limit_val is None:
            return
        headrooms.append(limit_val - actual_val)
        if actual_val > limit_val:
            violations.append(
                BudgetViolation(
                    level="ERROR",
                    code=f"{code_prefix}_LIMIT_EXCEEDED",
                    exit_code=int(ExitCode.BREACH_BUDGET),
                    message=f"{label} spend ${quantize_usd(actual_val, USD_CENT):,.2f} exceeds hard cap ${quantize_usd(limit_val, USD_CENT):,.2f}.",
                    actual=str(quantize_usd(actual_val, USD_MICRO)),
                    threshold=str(quantize_usd(limit_val, USD_MICRO)),
                )
            )
        elif limit_val > ZERO:
            pct_used = (actual_val * Decimal("100")) / limit_val
            if pct_used >= warn_pct:
                violations.append(
                    BudgetViolation(
                        level="WARN",
                        code=f"{code_prefix}_WARN_THRESHOLD",
                        exit_code=int(ExitCode.WARN),
                        message=(
                            f"{label} spend at {quantize_usd(pct_used, Decimal('0.1'))}% of budget "
                            f"(${quantize_usd(actual_val, USD_CENT):,.2f} / ${quantize_usd(limit_val, USD_CENT):,.2f})."
                        ),
                        actual=str(quantize_usd(actual_val, USD_MICRO)),
                        threshold=f"{warn_pct}% of {quantize_usd(limit_val, USD_CENT)}",
                    )
                )

    session_actual = transcript_spend + projected_next_usd if report is not None else projected_total
    _check_cap("Session", "SESSION", session_actual, session_limit_usd)
    _check_cap("Daily", "DAILY", projected_total, daily_limit_usd)
    _check_cap("Monthly", "MONTHLY", projected_total, monthly_limit_usd)

    if max_waste_pct is not None and waste_pct > max_waste_pct:
        violations.append(
            BudgetViolation(
                level="ERROR",
                code="WASTE_RATIO_EXCEEDED",
                exit_code=int(ExitCode.BREACH_WASTE),
                message=(
                    f"Recoverable token waste {quantize_usd(waste_pct, Decimal('0.1'))}% "
                    f"(${quantize_usd(recoverable, USD_CENT):,.2f}) exceeds max allowed {max_waste_pct}%."
                ),
                actual=f"{quantize_usd(waste_pct, Decimal('0.01'))}%",
                threshold=f"{max_waste_pct}%",
            )
        )

    if max_retry_loops is not None and retry_loops > max_retry_loops:
        violations.append(
            BudgetViolation(
                level="ERROR",
                code="RUNAWAY_LOOP_LIMIT_EXCEEDED",
                exit_code=int(ExitCode.BREACH_WASTE),
                message=f"Detected {retry_loops} runaway retry loop(s), exceeding limit {max_retry_loops}.",
                actual=str(retry_loops),
                threshold=str(max_retry_loops),
            )
        )

    mcp_res = mcp_report
    if mcp_res is None and mcp_config_path is not None:
        mcp_res = audit_mcp_file(mcp_config_path)

    mcp_crit = mcp_res.critical_count if mcp_res is not None else 0
    if mcp_res is not None and fail_on_critical_mcp and mcp_crit > 0:
        violations.append(
            BudgetViolation(
                level="ERROR",
                code="CRITICAL_MCP_SECURITY_RISK",
                exit_code=int(ExitCode.BREACH_SECURITY),
                message=(
                    f"MCP config {mcp_res.source_path} has {mcp_crit} CRITICAL security vulnerability(ies) "
                    f"(grade {mcp_res.security_grade}, risk {mcp_res.risk_score}/100)."
                ),
                actual=f"{mcp_crit} critical",
                threshold="0 critical",
            )
        )

    errors = [v for v in violations if v.level == "ERROR"]
    warns = [v for v in violations if v.level == "WARN"]

    if errors:
        # Priority order: BUDGET (2) -> WASTE (3) -> SECURITY (4)
        priority = {
            int(ExitCode.BREACH_BUDGET): 0,
            int(ExitCode.BREACH_WASTE): 1,
            int(ExitCode.BREACH_SECURITY): 2,
        }
        errors.sort(key=lambda e: priority.get(e.exit_code, 99))
        status = "BLOCK"
        chosen_exit = errors[0].exit_code
    elif warns:
        status = "WARN"
        chosen_exit = int(ExitCode.WARN) if strict_warn else int(ExitCode.PASS)
    else:
        status = "PASS"
        chosen_exit = int(ExitCode.PASS)

    min_headroom = min(headrooms) if headrooms else None

    return BudgetDecision(
        status=status,
        exit_code=chosen_exit,
        evaluated_spend_usd=evaluated_spend,
        projected_total_usd=projected_total,
        recoverable_waste_usd=recoverable,
        waste_pct=waste_pct,
        retry_loop_count=retry_loops,
        mcp_critical_count=mcp_crit,
        session_limit_usd=session_limit_usd,
        daily_limit_usd=daily_limit_usd,
        monthly_limit_usd=monthly_limit_usd,
        headroom_usd=min_headroom,
        violations=violations,
    )
