"""BurnGuard Pro — AI Agent Token Cost Optimization & MCP Security Auditing."""

from decimal import Decimal

from burnguard.pricing import (
     MODEL_PRICING_2026,
    ModelRate,
    compute_turn_cost,
    quantize_usd,
    resolve_model_rate,
)
from burnguard.parser import (
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    ToolDefinitionRecord,
    parse_transcript_path,
)
from burnguard.analyzer import (
    AnalysisReport,
    CostFinding,
    SessionSummary,
    analyze_path,
    analyze_sessions,
)
from burnguard.mcp_audit import (
    McpAuditReport,
    McpFinding,
    audit_mcp_config,
    audit_mcp_file,
)
from burnguard.budget_guard import (
    BudgetDecision,
    ExitCode,
    evaluate_budget,
)
from burnguard.report_html import generate_html_report

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "Decimal",
    "MODEL_PRICING_2026",
    "ModelRate",
    "compute_turn_cost",
    "quantize_usd",
    "resolve_model_rate",
    "NormalizedSession",
    "NormalizedTurn",
    "ToolCallRecord",
    "ToolDefinitionRecord",
    "parse_transcript_path",
    "AnalysisReport",
    "CostFinding",
    "SessionSummary",
    "analyze_path",
    "analyze_sessions",
    "McpAuditReport",
    "McpFinding",
    "audit_mcp_config",
    "audit_mcp_file",
    "BudgetDecision",
    "ExitCode",
    "evaluate_budget",
    "generate_html_report",
]
