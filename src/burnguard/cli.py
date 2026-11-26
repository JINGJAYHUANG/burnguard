"""Command-line interface for BurnGuard Pro (`burnguard`)."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import List, Optional, Sequence

from burnguard import __version__
from burnguard.analyzer import AnalysisReport, analyze_path
from burnguard.budget_guard import BudgetDecision, ExitCode, evaluate_budget
from burnguard.mcp_audit import McpAuditReport, audit_mcp_file
from burnguard.pricing import (
    MODEL_PRICING_2026,
    USD_CENT,
    USD_MICRO,
    ZERO,
    load_custom_pricing,
    quantize_usd,
)
from burnguard.report_html import generate_html_report
from burnguard.web_server import create_web_server


def _parse_decimal(val: Optional[str], flag_name: str) -> Optional[Decimal]:
    if val is None:
        return None
    cleaned = val.strip().lstrip("$").replace(",", "")
    try:
        d = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value for {flag_name}: {val!r}") from exc
    if d < ZERO:
        raise ValueError(f"{flag_name} must be non-negative, got {val!r}")
    return d


def _format_analysis_text(report: AnalysisReport) -> str:
    lines: List[str] = [
        "=" * 78,
        f"BurnGuard Pro v{__version__} — Agent Token Cost & Waste Diagnostic",
        "=" * 78,
        f"Target Path       : {report.target_path}",
        f"Sessions Analyzed : {report.session_count} ({report.turn_count} billed turns)",
        f"Total Tokens      : {report.total_tokens:,} (Input: {report.total_input_tokens:,} | Cached: {report.total_cached_tokens:,} | Output: {report.total_output_tokens:,})",
        f"Prompt Cache Hit  : {quantize_usd(report.overall_cache_hit_pct, Decimal('0.1'))}%",
        f"Actual USD Spend  : ${quantize_usd(report.total_spend_usd, USD_CENT):,.2f} (${quantize_usd(report.total_spend_usd, USD_MICRO)} exact)",
        f"Recoverable Waste : ${quantize_usd(report.recoverable_usd, USD_CENT):,.2f} ({quantize_usd(report.savings_pct, Decimal('0.1'))}% reduction)",
        f"Optimized Target  : ${quantize_usd(report.optimized_spend_usd, USD_CENT):,.2f}",
        "-" * 78,
        "4-Vector Recoverable Savings Breakdown (De-Duplicated Waterfall):",
    ]
    for cat, amt in report.category_savings_usd.items():
        lines.append(f"  - {cat:<26} : ${quantize_usd(amt, USD_CENT):>8,.2f}  (${quantize_usd(amt, USD_MICRO)} exact)")

    lines.append("-" * 78)
    if not report.findings:
        lines.append("Findings: Zero waste anti-patterns detected. Telemetry is clean.")
    else:
        lines.append(f"Detected Findings ({len(report.findings)}):")
        for idx, f in enumerate(report.findings, start=1):
            lines.append(
                f"\n  [{idx}] [{f.severity}] {f.title}\n"
                f"      ID / Session : {f.finding_id} ({f.session_id}) | Category: {f.category}\n"
                f"      Recoverable  : ${quantize_usd(f.recoverable_usd, USD_CENT):,.2f} "
                f"(${quantize_usd(f.recoverable_usd, USD_MICRO)} waterfall | ${quantize_usd(f.standalone_savings_usd, USD_MICRO)} standalone)\n"
                f"      Evidence     : {f.evidence}\n"
                f"      Fix          : {f.recommendation}"
            )
    lines.append("=" * 78)
    return "\n".join(lines)


def _format_mcp_text(report: McpAuditReport, show_fix_preview: bool) -> str:
    lines: List[str] = [
        "=" * 78,
        f"BurnGuard Pro v{__version__} — MCP Security & Token-Tax Audit",
        "=" * 78,
        f"Config File       : {report.source_path}",
        f"MCP Servers       : {report.active_server_count} active / {report.server_count} total",
        f"Eager Tool Count  : ~{report.estimated_eager_tools} tools (~{report.estimated_schema_tokens_per_turn:,} schema tokens/turn)",
        f"100-Turn Tax Est. : ${quantize_usd(report.estimated_100_turn_tax_usd, USD_CENT):,.2f}",
        f"Security Grade    : {report.security_grade} (Risk Score: {report.risk_score}/100 | Critical: {report.critical_count} | High: {report.high_count})",
        "-" * 78,
    ]
    if not report.findings:
        lines.append("Findings: Zero MCP security or eager-bloat risks found.")
    else:
        lines.append(f"Vulnerabilities & Cost Risks ({len(report.findings)}):")
        for idx, f in enumerate(report.findings, start=1):
            lines.append(
                f"\n  [{idx}] [{f.severity}] ({f.rule_id}) {f.title}\n"
                f"      Server      : {f.server_name} (+{f.risk_points} risk pts)\n"
                f"      Evidence    : {f.evidence}\n"
                f"      Remediation : {f.remediation}"
            )

    if show_fix_preview:
        lines.extend(
            [
                "",
                "-" * 78,
                "Auto-Remediated Safe MCP Config Preview (--fix-preview):",
                "-" * 78,
                json.dumps(report.remediated_config, indent=2, ensure_ascii=False),
            ]
        )
    lines.append("=" * 78)
    return "\n".join(lines)


def _format_budget_text(decision: BudgetDecision, fmt: str) -> str:
    if fmt == "github":
        out_lines: List[str] = []
        for v in decision.violations:
            gh_level = "error" if v.level == "ERROR" else "warning"
            out_lines.append(f"::{gh_level} title=BurnGuard {v.code}::{v.message}")
        if not out_lines:
            out_lines.append(
                f"::notice title=BurnGuard PASS::Evaluated spend ${quantize_usd(decision.projected_total_usd, USD_CENT):,.2f} within budget."
            )
        return "\n".join(out_lines)

    lines = [
        f"BurnGuard Budget Circuit-Breaker : {decision.status} (exit_code={decision.exit_code})",
        f"Evaluated Spend                  : ${quantize_usd(decision.evaluated_spend_usd, USD_CENT):,.2f} (${quantize_usd(decision.evaluated_spend_usd, USD_MICRO)} exact)",
        f"Projected Total                  : ${quantize_usd(decision.projected_total_usd, USD_CENT):,.2f}",
        f"Recoverable Waste                : ${quantize_usd(decision.recoverable_waste_usd, USD_CENT):,.2f} ({quantize_usd(decision.waste_pct, Decimal('0.1'))}%)",
        f"Runaway Retry Loops              : {decision.retry_loop_count}",
    ]
    if decision.headroom_usd is not None:
        lines.append(f"Remaining Budget Headroom        : ${quantize_usd(decision.headroom_usd, USD_CENT):,.2f}")
    for v in decision.violations:
        lines.append(f"  [{v.level}] {v.code}: {v.message}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="burnguard",
        description="BurnGuard Pro — AI Agent Token Cost Optimizer, CI Budget Circuit-Breaker & MCP Security Auditor",
    )
    parser.add_argument("--version", action="version", version=f"burnguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. `burnguard analyze <session_log_or_dir>`
    p_analyze = sub.add_parser(
        "analyze",
        help="Analyze JSONL/JSON agent transcripts for exact Decimal USD spend and 4 waste anti-patterns",
    )
    p_analyze.add_argument("target", type=Path, help="Path to session .jsonl/.json file or directory of logs")
    p_analyze.add_argument("--format", choices=("text", "json"), default="text", help="Output format")
    p_analyze.add_argument("--model-override", default=None, help="Override model pricing profile")
    p_analyze.add_argument("--pricing-file", type=Path, default=None, help="Optional custom JSON pricing table")
    p_analyze.add_argument("--out", type=Path, default=None, help="Write output to file instead of stdout")

    # 2. `burnguard audit-mcp <mcp_config.json>`
    p_mcp = sub.add_parser(
        "audit-mcp",
        help="Audit MCP server configurations for leaked keys, unpinned npx -y, root mounts, and eager tool bloat",
    )
    p_mcp.add_argument("config_path", type=Path, help="Path to mcp_config.json / claude_desktop_config.json")
    p_mcp.add_argument(
        "--fix-preview",
        action="store_true",
        help="Print auto-remediated safe MCP JSON configuration",
    )
    p_mcp.add_argument(
        "--write-safe",
        type=Path,
        default=None,
        help="Write sanitized MCP JSON config to the specified file path",
    )
    p_mcp.add_argument("--model", default="claude-sonnet-4-6", help="Reference model for eager tool tax estimate")
    p_mcp.add_argument("--format", choices=("text", "json"), default="text", help="Output format")
    p_mcp.add_argument("--fail-on-critical", action="store_true", help="Exit with code 4 if CRITICAL findings exist")

    # 3. `burnguard budget-guard`
    p_budget = sub.add_parser(
        "budget-guard",
        help="Deterministic pre-flight / cron CI budget circuit-breaker evaluator",
    )
    p_budget.add_argument("--input", type=Path, default=None, help="Optional session log or directory to include")
    p_budget.add_argument("--current-spend", default="0", help="Accumulated ledger spend in USD (Decimal)")
    p_budget.add_argument("--projected-next", default="0", help="Estimated USD of next scheduled run")
    p_budget.add_argument("--session-limit", default=None, help="Hard session USD cap")
    p_budget.add_argument("--daily-limit", default=None, help="Hard daily USD cap")
    p_budget.add_argument("--monthly-limit", default=None, help="Hard monthly USD cap")
    p_budget.add_argument("--warn-pct", default="80", help="Warning threshold percentage (default: 80)")
    p_budget.add_argument("--strict-warn", action="store_true", help="Return exit code 10 on budget warnings")
    p_budget.add_argument("--max-waste-pct", default=None, help="Trip circuit breaker if waste %% exceeds threshold")
    p_budget.add_argument("--max-retry-loops", type=int, default=None, help="Max allowed runaway retry loops")
    p_budget.add_argument("--mcp-config", type=Path, default=None, help="Gate execution on MCP security config")
    p_budget.add_argument("--pricing-file", type=Path, default=None, help="Optional custom JSON pricing table")
    p_budget.add_argument("--format", choices=("text", "json", "github"), default="text", help="Output format")

    # 4. `burnguard report <input> --html <out.html>`
    p_report = sub.add_parser(
        "report",
        help="Generate self-contained 100%% offline dark-mode interactive HTML diagnostic report",
    )
    p_report.add_argument("input", type=Path, help="Transcript file, directory, or MCP config JSON")
    p_report.add_argument("--html", type=Path, required=True, help="Output path for self-contained HTML dashboard")
    p_report.add_argument("--mcp", type=Path, default=None, help="Optional companion MCP config JSON to include")
    p_report.add_argument("--json", type=Path, default=None, help="Optional companion JSON telemetry output path")
    p_report.add_argument("--budget-limit", default=None, help="Optional USD budget cap bar to display")
    p_report.add_argument("--title", default=None, help="Custom report title")

    # 5. `burnguard serve`
    p_serve = sub.add_parser(
        "serve",
        help="Launch zero-dependency local interactive web analyzer on 127.0.0.1",
    )
    p_serve.add_argument("input", type=Path, nargs="?", default=None, help="Optional initial session log or dir")
    p_serve.add_argument("--mcp", type=Path, default=None, help="Optional initial MCP config JSON")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")

    # 6. `burnguard models`
    p_models = sub.add_parser("models", help="List built-in 2026 exact Decimal model pricing profiles")
    p_models.add_argument("--format", choices=("text", "json"), default="text")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "analyze":
            custom_rates = load_custom_pricing(args.pricing_file) if args.pricing_file else None
            report = analyze_path(
                args.target,
                model_override=args.model_override,
                custom_rates=custom_rates,
            )
            rendered = (
                json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
                if args.format == "json"
                else _format_analysis_text(report)
            )
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(rendered + "\n", encoding="utf-8")
            else:
                print(rendered)
            return int(ExitCode.PASS)

        if args.command == "audit-mcp":
            mcp_rep = audit_mcp_file(args.config_path, reference_model=args.model)
            if args.write_safe:
                args.write_safe.parent.mkdir(parents=True, exist_ok=True)
                args.write_safe.write_text(
                    json.dumps(mcp_rep.remediated_config, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            if args.format == "json":
                print(
                    json.dumps(
                        mcp_rep.to_dict(include_remediated=bool(args.fix_preview or args.write_safe)),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(_format_mcp_text(mcp_rep, show_fix_preview=bool(args.fix_preview)))
            if args.fail_on_critical and mcp_rep.critical_count > 0:
                return int(ExitCode.BREACH_SECURITY)
            return int(ExitCode.PASS)

        if args.command == "budget-guard":
            custom_rates = load_custom_pricing(args.pricing_file) if args.pricing_file else None
            decision = evaluate_budget(
                transcript_path=args.input,
                current_spend_usd=_parse_decimal(args.current_spend, "--current-spend") or ZERO,
                projected_next_usd=_parse_decimal(args.projected_next, "--projected-next") or ZERO,
                session_limit_usd=_parse_decimal(args.session_limit, "--session-limit"),
                daily_limit_usd=_parse_decimal(args.daily_limit, "--daily-limit"),
                monthly_limit_usd=_parse_decimal(args.monthly_limit, "--monthly-limit"),
                warn_pct=_parse_decimal(args.warn_pct, "--warn-pct") or Decimal("80"),
                strict_warn=bool(args.strict_warn),
                max_waste_pct=_parse_decimal(args.max_waste_pct, "--max-waste-pct"),
                max_retry_loops=args.max_retry_loops,
                mcp_config_path=args.mcp_config,
                custom_rates=custom_rates,
            )
            if args.format == "json":
                print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(_format_budget_text(decision, args.format))
            return int(decision.exit_code)

        if args.command == "report":
            b_limit = _parse_decimal(args.budget_limit, "--budget-limit")
            bundle = generate_html_report(
                args.input,
                args.html,
                mcp_path=args.mcp,
                budget_limit_usd=b_limit,
                title=args.title,
            )
            if args.json:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                json_payload = {
                    "analysis": bundle["analysis"].to_dict() if bundle["analysis"] else None,
                    "mcp_audit": bundle["mcp_audit"].to_dict() if bundle["mcp_audit"] else None,
                }
                args.json.write_text(
                    json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            analysis_obj = bundle["analysis"]
            mcp_obj = bundle["mcp_audit"]
            msg_parts = [f"BurnGuard HTML report written to: {args.html}"]
            if analysis_obj is not None:
                msg_parts.append(
                    f"Spend=${quantize_usd(analysis_obj.total_spend_usd, USD_CENT):,.2f} | "
                    f"Recoverable=${quantize_usd(analysis_obj.recoverable_usd, USD_CENT):,.2f} "
                    f"({quantize_usd(analysis_obj.savings_pct, Decimal('0.1'))}%)"
                )
            if mcp_obj is not None:
                msg_parts.append(f"MCP Grade={mcp_obj.security_grade} (Risk {mcp_obj.risk_score}/100)")
            print(" | ".join(msg_parts))
            return int(ExitCode.PASS)

        if args.command == "serve":
            srv = create_web_server(
                host=args.host,
                port=args.port,
                initial_input=args.input,
                initial_mcp=args.mcp,
            )
            actual_host, actual_port = srv.server_address[:2]
            print(f"BurnGuard Pro local web analyzer listening on http://{actual_host}:{actual_port}")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                srv.server_close()
            return int(ExitCode.PASS)

        if args.command == "models":
            if args.format == "json":
                print(
                    json.dumps(
                        {k: v.to_dict() for k, v in MODEL_PRICING_2026.items()},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"{'Model ID':<22} {'Provider':<11} {'Input/1M':>10} {'Cached/1M':>10} {'Write/1M':>10} {'Output/1M':>10}")
                print("-" * 78)
                for k, r in MODEL_PRICING_2026.items():
                    print(
                        f"{k:<22} {r.provider:<11} ${r.input_per_m:>9.2f} ${r.cached_input_per_m:>9.3f} "
                        f"${r.cache_write_per_m:>9.2f} ${r.output_per_m:>9.2f}"
                    )
            return int(ExitCode.PASS)

    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"burnguard error: {exc}", file=sys.stderr)
        return int(ExitCode.CLI_USAGE_ERROR)

    return int(ExitCode.PASS)


if __name__ == "__main__":
    raise SystemExit(main())
