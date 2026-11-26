"""Self-contained, offline dark-mode interactive HTML diagnostic dashboard generator."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from burnguard.analyzer import (
    CATEGORY_CACHE_BUST,
    CATEGORY_CONTEXT_DUMP,
    CATEGORY_RETRY_LOOP,
    CATEGORY_TOOL_BLOAT,
    AnalysisReport,
    TurnTelemetry,
)
from burnguard.mcp_audit import McpAuditReport
from burnguard.pricing import USD_CENT, USD_MICRO, ZERO, quantize_usd


def _esc(val: Any) -> str:
    return html.escape(str(val), quote=True)


def _render_waterfall_svg(report: AnalysisReport) -> str:
    """Render an inline crisp SVG waterfall chart showing exact Decimal spend -> savings -> optimized spend."""
    total = float(report.total_spend_usd)
    if total <= 0:
        return '<div class="empty-chart">No transcript spend recorded in target input.</div>'

    steps = [
        ("Current Spend", float(report.total_spend_usd), "total"),
        ("Retry Loops", -float(report.category_savings_usd.get(CATEGORY_RETRY_LOOP, ZERO)), "save"),
        ("Context Dumps", -float(report.category_savings_usd.get(CATEGORY_CONTEXT_DUMP, ZERO)), "save"),
        ("MCP Bloat", -float(report.category_savings_usd.get(CATEGORY_TOOL_BLOAT, ZERO)), "save"),
        ("Cache Busting", -float(report.category_savings_usd.get(CATEGORY_CACHE_BUST, ZERO)), "save"),
        ("Optimized Net", float(report.optimized_spend_usd), "net"),
    ]

    width = 680
    height = 250
    pad_top = 28
    pad_bottom = 44
    pad_left = 56
    pad_right = 20
    plot_h = height - pad_top - pad_bottom
    plot_w = width - pad_left - pad_right
    col_w = plot_w / len(steps)
    bar_w = min(68.0, col_w * 0.62)

    def y_of(val: float) -> float:
        clamped = max(0.0, min(total, val))
        return pad_top + plot_h - (clamped / total) * plot_h

    svg_parts = [
        f'<svg viewBox="0 0 {width} {height}" class="telemetry-svg" role="img" aria-label="Token Spend Optimization Waterfall">',
    ]

    # Horizontal grid lines
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        val = total * frac
        gy = y_of(val)
        svg_parts.append(
            f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{width - pad_right}" y2="{gy:.1f}" stroke="#1E293B" stroke-dasharray="3 3" />'
        )
        svg_parts.append(
            f'<text x="{pad_left - 8}" y="{gy + 4:.1f}" text-anchor="end" fill="#64748B" font-size="10" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">${val:.2f}</text>'
        )

    running = total
    for idx, (label, delta, kind) in enumerate(steps):
        cx = pad_left + idx * col_w + col_w / 2.0
        bx = cx - bar_w / 2.0
        if kind == "total":
            top_v, bot_v = total, 0.0
            fill = "#F59E0B"
            val_label = f"${total:.2f}"
        elif kind == "net":
            top_v, bot_v = max(0.0, delta), 0.0
            fill = "#38BDF8"
            val_label = f"${delta:.2f}"
        else:
            next_running = max(0.0, running + delta)
            top_v, bot_v = running, next_running
            running = next_running
            fill = "#10B981"
            val_label = f"-${abs(delta):.2f}" if abs(delta) >= 0.005 else "$0.00"

        y_top = y_of(top_v)
        y_bot = y_of(bot_v)
        bh = max(3.0, y_bot - y_top)

        svg_parts.append(
            f'<rect x="{bx:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="4" fill="{fill}" fill-opacity="0.88" />'
        )
        svg_parts.append(
            f'<text x="{cx:.1f}" y="{max(14.0, y_top - 6.0):.1f}" text-anchor="middle" fill="#E2E8F0" font-size="11" font-weight="600" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">{val_label}</text>'
        )
        svg_parts.append(
            f'<text x="{cx:.1f}" y="{height - 16}" text-anchor="middle" fill="#94A3B8" font-size="11">{_esc(label)}</text>'
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def _render_turn_timeline_svg(report: AnalysisReport) -> str:
    """Render turn-by-turn stacked token burn & error loop pulse SVG across the highest-spend session."""
    if not report.sessions:
        return '<div class="empty-chart">No session turns available.</div>'

    primary_session = max(report.sessions, key=lambda s: s.total_spend_usd)
    turns: Sequence[TurnTelemetry] = primary_session.turns[:40]
    if not turns:
        return '<div class="empty-chart">No turns recorded.</div>'

    max_tok = max(
        (t.uncached_input_tokens + t.cached_input_tokens + t.cache_write_tokens + t.output_tokens + t.reasoning_tokens)
        for t in turns
    )
    if max_tok <= 0:
        max_tok = 1

    width = 680
    height = 250
    pad_top = 28
    pad_bottom = 44
    pad_left = 56
    pad_right = 20
    plot_h = height - pad_top - pad_bottom
    plot_w = width - pad_left - pad_right
    col_w = plot_w / len(turns)
    bar_w = max(6.0, min(32.0, col_w * 0.68))

    svg = [
        f'<svg viewBox="0 0 {width} {height}" class="telemetry-svg" role="img" aria-label="Turn-by-Turn Token Burn Timeline">',
    ]

    for frac in (0.0, 0.5, 1.0):
        tok_v = int(max_tok * frac)
        gy = pad_top + plot_h - frac * plot_h
        svg.append(
            f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{width - pad_right}" y2="{gy:.1f}" stroke="#1E293B" stroke-dasharray="3 3" />'
        )
        svg.append(
            f'<text x="{pad_left - 8}" y="{gy + 4:.1f}" text-anchor="end" fill="#64748B" font-size="10" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">{tok_v // 1000}k</text>'
        )

    for i, t in enumerate(turns):
        cx = pad_left + i * col_w + col_w / 2.0
        bx = cx - bar_w / 2.0
        c_tok = t.cached_input_tokens
        u_tok = t.uncached_input_tokens + t.cache_write_tokens
        o_tok = t.output_tokens + t.reasoning_tokens

        h_c = (c_tok / max_tok) * plot_h
        h_u = (u_tok / max_tok) * plot_h
        h_o = (o_tok / max_tok) * plot_h

        y_base = pad_top + plot_h
        # Cached input (bottom - slate/cyan)
        if h_c > 0.5:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - h_c:.1f}" width="{bar_w:.1f}" height="{h_c:.1f}" fill="#0EA5E9" fill-opacity="0.55" />'
            )
        # Uncached input (middle - amber or crimson on error)
        u_color = "#EF4444" if t.has_error else "#F59E0B"
        if h_u > 0.5:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - h_c - h_u:.1f}" width="{bar_w:.1f}" height="{h_u:.1f}" fill="{u_color}" fill-opacity="0.88" />'
            )
        # Output tokens (top - emerald)
        if h_o > 0.5:
            svg.append(
                f'<rect x="{bx:.1f}" y="{y_base - h_c - h_u - h_o:.1f}" width="{bar_w:.1f}" height="{h_o:.1f}" fill="#10B981" />'
            )

        if t.has_error:
            top_y = max(12.0, y_base - h_c - h_u - h_o - 7.0)
            svg.append(f'<circle cx="{cx:.1f}" cy="{top_y:.1f}" r="3.5" fill="#EF4444" />')

        if len(turns) <= 16 or i % max(1, len(turns) // 10) == 0:
            svg.append(
                f'<text x="{cx:.1f}" y="{height - 16}" text-anchor="middle" fill="#94A3B8" font-size="10" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">T{t.turn_index}</text>'
            )

    svg.append("</svg>")
    return "\n".join(svg)


def render_html_dashboard(
    *,
    analysis: Optional[AnalysisReport] = None,
    mcp_audit: Optional[McpAuditReport] = None,
    title: str = "BurnGuard Pro — Token FinOps & MCP Security Diagnostic",
    budget_limit_usd: Optional[Decimal] = None,
) -> str:
    """Build a 100% offline, zero-CDN interactive dark-mode HTML diagnostic report."""
    generated_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    total_spend = analysis.total_spend_usd if analysis else ZERO
    recoverable = analysis.recoverable_usd if analysis else ZERO
    optimized = analysis.optimized_spend_usd if analysis else ZERO
    savings_pct = analysis.savings_pct if analysis else ZERO
    cache_hit_pct = analysis.overall_cache_hit_pct if analysis else ZERO
    total_tokens = analysis.total_tokens if analysis else 0
    session_count = analysis.session_count if analysis else 0
    turn_count = analysis.turn_count if analysis else 0

    avg_recoverable_per_session = (
        float(recoverable / Decimal(session_count))
        if session_count > 0
        else float(mcp_audit.estimated_100_turn_tax_usd if mcp_audit else ZERO)
    )

    mcp_grade = mcp_audit.security_grade if mcp_audit else "N/A"
    mcp_score = mcp_audit.risk_score if mcp_audit else 0
    mcp_tax_display = (
        f"${quantize_usd(mcp_audit.estimated_100_turn_tax_usd, USD_CENT):,.2f}"
        if mcp_audit
        else "$0.00"
    )

    status_label = "OPTIMIZATION READY"
    status_class = "badge-amber"
    if mcp_audit and mcp_audit.critical_count > 0:
        status_label = f"CRITICAL MCP RISK ({mcp_audit.critical_count})"
        status_class = "badge-red"
    elif budget_limit_usd is not None and total_spend > budget_limit_usd:
        status_label = "BUDGET CAP BREACHED"
        status_class = "badge-red"
    elif recoverable == ZERO and (not mcp_audit or len(mcp_audit.findings) == 0):
        status_label = "CLEAN TELEMETRY"
        status_class = "badge-green"

    findings_rows_html: List[str] = []
    if analysis and analysis.findings:
        for f in analysis.findings:
            sev_cls = f"sev-{f.severity.lower()}"
            turns_str = (
                f"T{f.affected_turns[0]}..T{f.affected_turns[-1]} ({len(f.affected_turns)} turns)"
                if len(f.affected_turns) > 2
                else ", ".join(f"T{x}" for x in f.affected_turns)
            )
            findings_rows_html.append(
                f"""
                <div class="finding-card" data-category="{_esc(f.category)}" data-severity="{_esc(f.severity)}">
                  <div class="finding-header">
                    <div class="finding-badges">
                      <span class="sev-pill {sev_cls}">{_esc(f.severity)}</span>
                      <span class="cat-pill">{_esc(f.category)}</span>
                      <span class="mono-dim">{_esc(f.finding_id)} · Session {_esc(f.session_id)} · {_esc(turns_str)}</span>
                    </div>
                    <div class="finding-savings">
                      <span class="save-amount">+${quantize_usd(f.recoverable_usd, USD_CENT):,.2f}</span>
                      <span class="mono-dim">(${quantize_usd(f.recoverable_usd, USD_MICRO)} exact)</span>
                    </div>
                  </div>
                  <h4 class="finding-title">{_esc(f.title)}</h4>
                  <p class="finding-evidence"><strong>Evidence:</strong> {_esc(f.evidence)}</p>
                  <p class="finding-fix"><strong>Remediation:</strong> {_esc(f.recommendation)}</p>
                </div>
                """
            )
    else:
        findings_rows_html.append(
            '<div class="empty-box">No transcript waste findings detected (or only MCP configuration audited).</div>'
        )

    session_rows_html: List[str] = []
    if analysis and analysis.sessions:
        for s in analysis.sessions:
            session_rows_html.append(
                f"""
                <tr>
                  <td class="mono">{_esc(s.session_id)}</td>
                  <td><span class="cat-pill">{_esc(s.schema_flavor)}</span></td>
                  <td class="mono">{_esc(s.primary_model)}</td>
                  <td class="mono num">{s.turn_count}</td>
                  <td class="mono num">{s.total_tokens:,}</td>
                  <td class="mono num">{quantize_usd(s.cache_hit_pct, Decimal('0.1'))}%</td>
                  <td class="mono num">${quantize_usd(s.total_spend_usd, Decimal('0.0001'))}</td>
                  <td class="mono num text-green">${quantize_usd(s.recoverable_usd, Decimal('0.0001'))} ({quantize_usd(s.savings_pct, Decimal('0.1'))}%)</td>
                  <td class="mono num text-cyan">${quantize_usd(s.optimized_spend_usd, Decimal('0.0001'))}</td>
                </tr>
                """
            )

    mcp_section_html = ""
    if mcp_audit is not None:
        mcp_cards: List[str] = []
        for mf in mcp_audit.findings:
            sev_cls = f"sev-{mf.severity.lower()}"
            mcp_cards.append(
                f"""
                <div class="finding-card">
                  <div class="finding-header">
                    <div class="finding-badges">
                      <span class="sev-pill {sev_cls}">{_esc(mf.severity)}</span>
                      <span class="cat-pill">{_esc(mf.rule_id)} · {_esc(mf.category)}</span>
                      <span class="mono-dim">Server: {_esc(mf.server_name)}</span>
                    </div>
                    <div class="mono-dim">+{mf.risk_points} risk pts</div>
                  </div>
                  <h4 class="finding-title">{_esc(mf.title)}</h4>
                  <p class="finding-evidence"><strong>Evidence:</strong> {_esc(mf.evidence)}</p>
                  <p class="finding-fix"><strong>Auto-Remediation:</strong> {_esc(mf.remediation)}</p>
                </div>
                """
            )
        safe_json_str = json.dumps(mcp_audit.remediated_config, indent=2, ensure_ascii=False)
        mcp_section_html = f"""
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>MCP Security &amp; Schema Token-Tax Audit</h2>
              <p class="subtext">Source: <span class="mono">{_esc(mcp_audit.source_path)}</span> · {mcp_audit.active_server_count}/{mcp_audit.server_count} active servers · ~{mcp_audit.estimated_eager_tools} eager tools (~{mcp_audit.estimated_schema_tokens_per_turn:,} tok/turn)</p>
            </div>
            <div class="grade-chip grade-{mcp_grade.lower()}">Grade {_esc(mcp_grade)} · Risk {mcp_score}/100</div>
          </div>
          <div class="mcp-grid">
            <div>
              {''.join(mcp_cards) if mcp_cards else '<div class="empty-box">Zero MCP vulnerabilities found.</div>'}
            </div>
            <div class="code-card">
              <div class="code-card-head">
                <span>Sanitized Drop-In Config (<code>--fix-preview</code>)</span>
                <button type="button" class="btn-action" id="copySafeMcpBtn">Copy Safe JSON</button>
              </div>
              <pre id="safeMcpCode">{_esc(safe_json_str)}</pre>
            </div>
          </div>
        </section>
        """

    waterfall_svg = _render_waterfall_svg(analysis) if analysis else ""
    timeline_svg = _render_turn_timeline_svg(analysis) if analysis else ""
    primary_sess_name = (
        max(analysis.sessions, key=lambda s: s.total_spend_usd).session_id
        if (analysis and analysis.sessions)
        else "n/a"
    )

    budget_banner_html = ""
    if budget_limit_usd is not None:
        pct = (
            quantize_usd((total_spend * Decimal("100")) / budget_limit_usd, Decimal("0.1"))
            if budget_limit_usd > ZERO
            else ZERO
        )
        budget_banner_html = f"""
        <div class="budget-bar-wrap">
          <div class="budget-bar-meta">
            <span>Budget Circuit-Breaker Cap: <strong>${quantize_usd(budget_limit_usd, USD_CENT):,.2f}</strong></span>
            <span class="mono">Used: ${quantize_usd(total_spend, USD_CENT):,.2f} ({pct}%)</span>
          </div>
          <div class="budget-track">
            <div class="budget-fill" style="width: {min(100.0, float(pct)):.1f}%;"></div>
          </div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --bg: #090C10;
      --panel: #11161F;
      --panel-alt: #161D29;
      --border: #232E3F;
      --text: #E6EDF3;
      --muted: #8B9BB0;
      --amber: #F59E0B;
      --green: #10B981;
      --red: #EF4444;
      --cyan: #38BDF8;
      --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      line-height: 1.5;
      padding: 0 0 64px 0;
    }}
    .topbar {{
      background: #0D1118;
      border-bottom: 1px solid var(--border);
      padding: 18px 28px;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-mark {{
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: #1A2230;
      border: 1px solid var(--amber);
      display: grid;
      place-items: center;
      color: var(--amber);
      font-family: var(--mono);
      font-weight: 800;
      font-size: 15px;
    }}
    .brand h1 {{
      margin: 0;
      font-size: 18px;
      letter-spacing: -0.01em;
    }}
    .brand p {{
      margin: 2px 0 0 0;
      font-size: 12px;
      color: var(--muted);
    }}
    .top-meta {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 12px;
      font-family: var(--mono);
      color: var(--muted);
    }}
    .status-pill {{
      padding: 5px 11px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 11px;
      letter-spacing: 0.04em;
    }}
    .badge-amber {{ background: rgba(245, 158, 11, 0.15); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.4); }}
    .badge-red {{ background: rgba(239, 68, 68, 0.16); color: #FCA5A5; border: 1px solid rgba(239, 68, 68, 0.5); }}
    .badge-green {{ background: rgba(16, 185, 129, 0.16); color: #6EE7B7; border: 1px solid rgba(16, 185, 129, 0.45); }}
    .container {{
      max-width: 1360px;
      margin: 24px auto;
      padding: 0 24px;
      display: flex;
      flex-direction: column;
      gap: 22px;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
    }}
    .kpi-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 18px;
    }}
    .kpi-label {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .kpi-value {{
      font-family: var(--mono);
      font-size: 26px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .kpi-sub {{
      font-family: var(--mono);
      font-size: 11px;
      color: var(--muted);
    }}
    .text-amber {{ color: var(--amber); }}
    .text-green {{ color: var(--green); }}
    .text-cyan {{ color: var(--cyan); }}
    .text-red {{ color: var(--red); }}
    .mono {{ font-family: var(--mono); }}
    .mono-dim {{ font-family: var(--mono); font-size: 11px; color: var(--muted); }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 22px;
    }}
    .panel-head {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .panel-head h2 {{
      margin: 0;
      font-size: 16px;
    }}
    .subtext {{
      margin: 4px 0 0 0;
      font-size: 12px;
      color: var(--muted);
    }}
    .charts-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(460px, 1fr));
      gap: 18px;
    }}
    .chart-box {{
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
    }}
    .chart-box h3 {{
      margin: 0 0 4px 0;
      font-size: 14px;
    }}
    .telemetry-svg {{
      width: 100%;
      height: auto;
      display: block;
      margin-top: 8px;
    }}
    .legend-row {{
      display: flex;
      gap: 14px;
      font-size: 11px;
      color: var(--muted);
      margin-top: 6px;
    }}
    .dot {{
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 2px;
      margin-right: 5px;
    }}
    .sim-grid {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 20px;
      align-items: center;
    }}
    @media (max-width: 860px) {{
      .sim-grid, .charts-grid, .mcp-grid {{ grid-template-columns: 1fr !important; }}
    }}
    .slider-group {{
      margin-bottom: 14px;
    }}
    .slider-group label {{
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      margin-bottom: 6px;
    }}
    input[type="range"] {{
      width: 100%;
      accent-color: var(--green);
    }}
    .sim-output {{
      background: var(--panel-alt);
      border: 1px solid rgba(16, 185, 129, 0.35);
      border-radius: 10px;
      padding: 18px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }}
    .filter-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }}
    .filter-btn, .btn-action {{
      background: var(--panel-alt);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      cursor: pointer;
      font-family: var(--mono);
    }}
    .filter-btn.active, .btn-action:hover {{
      border-color: var(--amber);
      color: var(--amber);
    }}
    .finding-card {{
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 15px 18px;
      margin-bottom: 12px;
    }}
    .finding-header {{
      display: flex;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 8px;
    }}
    .finding-badges {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .sev-pill {{
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .sev-critical {{ background: rgba(239, 68, 68, 0.2); color: #FCA5A5; border: 1px solid rgba(239, 68, 68, 0.45); }}
    .sev-high {{ background: rgba(245, 158, 11, 0.2); color: #FCD34D; border: 1px solid rgba(245, 158, 11, 0.45); }}
    .sev-medium {{ background: rgba(56, 189, 248, 0.16); color: #7DD3FC; border: 1px solid rgba(56, 189, 248, 0.4); }}
    .sev-low {{ background: rgba(148, 163, 184, 0.16); color: #CBD5E1; border: 1px solid #475569; }}
    .cat-pill {{
      font-family: var(--mono);
      font-size: 11px;
      padding: 2px 7px;
      border-radius: 4px;
      background: #0D121A;
      border: 1px solid var(--border);
      color: var(--muted);
    }}
    .save-amount {{
      font-family: var(--mono);
      font-weight: 700;
      color: var(--green);
      font-size: 15px;
      margin-right: 6px;
    }}
    .finding-title {{
      margin: 4px 0 8px 0;
      font-size: 14px;
    }}
    .finding-evidence, .finding-fix {{
      margin: 4px 0;
      font-size: 13px;
      color: #C9D4E0;
    }}
    .finding-fix strong {{ color: var(--green); }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
    }}
    th {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
    }}
    td.num, th.num {{ text-align: right; }}
    .mcp-grid {{
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 18px;
    }}
    .code-card {{
      background: #0A0E14;
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .code-card-head {{
      padding: 10px 14px;
      background: #121824;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
    }}
    pre {{
      margin: 0;
      padding: 14px;
      overflow: auto;
      max-height: 460px;
      font-family: var(--mono);
      font-size: 12px;
      color: #A7F3D0;
    }}
    .grade-chip {{
      font-family: var(--mono);
      font-weight: 700;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
    }}
    .grade-a, .grade-b {{ background: rgba(16,185,129,0.16); color: #6EE7B7; border: 1px solid rgba(16,185,129,0.4); }}
    .grade-c {{ background: rgba(245,158,11,0.16); color: #FCD34D; border: 1px solid rgba(245,158,11,0.4); }}
    .grade-d, .grade-f {{ background: rgba(239,68,68,0.18); color: #FCA5A5; border: 1px solid rgba(239,68,68,0.45); }}
    .budget-bar-wrap {{
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
    }}
    .budget-bar-meta {{
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .budget-track {{
      height: 8px;
      background: #0B0F17;
      border-radius: 99px;
      overflow: hidden;
    }}
    .budget-fill {{
      height: 100%;
      background: var(--amber);
    }}
    .empty-box, .empty-chart {{
      padding: 20px;
      text-align: center;
      color: var(--muted);
      font-size: 13px;
    }}
    footer {{
      max-width: 1360px;
      margin: 12px auto 0;
      padding: 0 24px;
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 12px;
      font-family: var(--mono);
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark">BG</div>
      <div>
        <h1>{_esc(title)}</h1>
        <p>BurnGuard Pro v1.0.0 · 100% Offline Decimal FinOps &amp; MCP Supply-Chain Auditor</p>
      </div>
    </div>
    <div class="top-meta">
      <span>Generated {_esc(generated_utc)}</span>
      <span class="status-pill {status_class}">{_esc(status_label)}</span>
    </div>
  </header>

  <main class="container">
    {budget_banner_html}

    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Verified Transcript Spend</div>
        <div class="kpi-value text-amber">${quantize_usd(total_spend, USD_CENT):,.2f}</div>
        <div class="kpi-sub">${quantize_usd(total_spend, USD_MICRO)} exact · {total_tokens:,} tok</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Recoverable USD Savings</div>
        <div class="kpi-value text-green">${quantize_usd(recoverable, USD_CENT):,.2f}</div>
        <div class="kpi-sub">{quantize_usd(savings_pct, Decimal('0.1'))}% net Waterfall reduction</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Optimized Target Spend</div>
        <div class="kpi-value text-cyan">${quantize_usd(optimized, USD_CENT):,.2f}</div>
        <div class="kpi-sub">{session_count} session(s) · {turn_count} turns</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Prompt Cache Hit Rate</div>
        <div class="kpi-value">{quantize_usd(cache_hit_pct, Decimal('0.1'))}%</div>
        <div class="kpi-sub">Target &ge; 75.0% on multi-turn runs</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Waste Anti-Patterns</div>
        <div class="kpi-value">{len(analysis.findings) if analysis else 0}</div>
        <div class="kpi-sub">Loops · Cache · Schemas · Dumps</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">MCP Security Posture</div>
        <div class="kpi-value">{_esc(mcp_grade)}</div>
        <div class="kpi-sub">Risk {mcp_score}/100 · {mcp_tax_display}/100 turns</div>
      </div>
    </section>

    {f'''
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Visual Cost Telemetry &amp; Context Burn Profile</h2>
          <p class="subtext">Exact Decimal Waterfall Attribution &amp; Turn-by-Turn Token Stack (Primary Session: <span class="mono">{_esc(primary_sess_name)}</span>)</p>
        </div>
      </div>
      <div class="charts-grid">
        <div class="chart-box">
          <h3>4-Vector Savings Waterfall (USD)</h3>
          {waterfall_svg}
        </div>
        <div class="chart-box">
          <h3>Turn-by-Turn Context Growth &amp; Retry Failures</h3>
          {timeline_svg}
          <div class="legend-row">
            <span><i class="dot" style="background:#0EA5E9;"></i>Cached Input</span>
            <span><i class="dot" style="background:#F59E0B;"></i>Uncached Input</span>
            <span><i class="dot" style="background:#EF4444;"></i>Error / Retry Turn</span>
            <span><i class="dot" style="background:#10B981;"></i>Output</span>
          </div>
        </div>
      </div>
    </section>
    ''' if analysis else ''}

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Interactive Engineering Team FinOps ROI Forecast</h2>
          <p class="subtext">Project monthly &amp; annual cash preserved if the audited waste rate (${avg_recoverable_per_session:.4f}/session) is eliminated across your workflow.</p>
        </div>
      </div>
      <div class="sim-grid">
        <div>
          <div class="slider-group">
            <label><span>Agent Sessions per Developer / Day</span><strong class="mono" id="sessPerDayLabel">12</strong></label>
            <input type="range" id="sessPerDaySlider" min="1" max="100" value="12" />
          </div>
          <div class="slider-group">
            <label><span>Active Engineering Seats</span><strong class="mono" id="seatsLabel">5</strong></label>
            <input type="range" id="seatsSlider" min="1" max="50" value="5" />
          </div>
        </div>
        <div class="sim-output">
          <div>
            <div class="kpi-label">Projected Monthly Savings</div>
            <div class="kpi-value text-green" id="monthlySavedVal">$0.00</div>
            <div class="kpi-sub">22 active engineering days/mo</div>
          </div>
          <div>
            <div class="kpi-label">Projected Annual Savings</div>
            <div class="kpi-value text-cyan" id="annualSavedVal">$0.00</div>
            <div class="kpi-sub" id="paybackLabel">Payback vs $59 Pro: &lt; 1 day</div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Actionable Token Waste Diagnostics</h2>
          <p class="subtext">De-duplicated Waterfall savings guarantee zero token double-counting across overlapping findings.</p>
        </div>
      </div>
      <div class="filter-bar" id="findingFilters">
        <button type="button" class="filter-btn active" data-filter="ALL">All ({len(analysis.findings) if analysis else 0})</button>
        <button type="button" class="filter-btn" data-filter="CRITICAL">Critical</button>
        <button type="button" class="filter-btn" data-filter="RUNAWAY_RETRY_LOOP">Retry Loops</button>
        <button type="button" class="filter-btn" data-filter="PROMPT_CACHE_BUSTING">Cache Busting</button>
        <button type="button" class="filter-btn" data-filter="BLOATED_TOOL_SCHEMAS">MCP Schema Bloat</button>
        <button type="button" class="filter-btn" data-filter="OVERSIZED_CONTEXT_DUMP">Oversized Dumps</button>
      </div>
      <div id="findingsList">
        {''.join(findings_rows_html)}
      </div>
    </section>

    {mcp_section_html}

    {f'''
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Session-Level Ledger Breakdown</h2>
          <p class="subtext">Target: <span class="mono">{_esc(analysis.target_path)}</span></p>
        </div>
      </div>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Session ID</th>
              <th>Schema</th>
              <th>Primary Model</th>
              <th class="num">Turns</th>
              <th class="num">Total Tokens</th>
              <th class="num">Cache Hit</th>
              <th class="num">Actual Spend</th>
              <th class="num">Recoverable</th>
              <th class="num">Optimized Net</th>
            </tr>
          </thead>
          <tbody>
            {''.join(session_rows_html)}
          </tbody>
        </table>
      </div>
    </section>
    ''' if (analysis and analysis.sessions) else ''}
  </main>

  <footer>
    <span>BurnGuard Pro v1.0.0 · Engineered by Unit-500</span>
    <span>Zero External Requests · 100% Local Python Decimal Engine</span>
  </footer>

  <script>
    (function() {{
      const avgSavePerSession = {avg_recoverable_per_session:.6f};
      const sessSlider = document.getElementById('sessPerDaySlider');
      const seatsSlider = document.getElementById('seatsSlider');
      const sessLbl = document.getElementById('sessPerDayLabel');
      const seatsLbl = document.getElementById('seatsLabel');
      const monthlyEl = document.getElementById('monthlySavedVal');
      const annualEl = document.getElementById('annualSavedVal');
      const paybackEl = document.getElementById('paybackLabel');

      function updateForecast() {{
        if (!sessSlider || !seatsSlider) return;
        const s = parseInt(sessSlider.value, 10) || 1;
        const seats = parseInt(seatsSlider.value, 10) || 1;
        sessLbl.textContent = String(s);
        seatsLbl.textContent = String(seats);
        const dailySave = avgSavePerSession * s * seats;
        const monthlySave = dailySave * 22;
        const annualSave = monthlySave * 12;
        monthlyEl.textContent = '$' + monthlySave.toLocaleString('en-US', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
        annualEl.textContent = '$' + annualSave.toLocaleString('en-US', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
        const licensePrice = seats === 1 ? 29 : (seats <= 2 ? 59 : 149);
        if (dailySave > 0.01) {{
          const days = licensePrice / dailySave;
          paybackEl.textContent = days < 1
            ? ('Payback vs $' + licensePrice + ' tier: ' + Math.max(1, Math.round(days * 24)) + ' hrs')
            : ('Payback vs $' + licensePrice + ' tier: ' + days.toFixed(1) + ' days');
        }} else {{
          paybackEl.textContent = 'Baseline session already optimized';
        }}
      }}
      if (sessSlider && seatsSlider) {{
        sessSlider.addEventListener('input', updateForecast);
        seatsSlider.addEventListener('input', updateForecast);
        updateForecast();
      }}

      const filterContainer = document.getElementById('findingFilters');
      if (filterContainer) {{
        filterContainer.addEventListener('click', function(ev) {{
          const btn = ev.target.closest('.filter-btn');
          if (!btn) return;
          const filter = btn.getAttribute('data-filter');
          filterContainer.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          document.querySelectorAll('#findingsList .finding-card').forEach(card => {{
            const cat = card.getAttribute('data-category');
            const sev = card.getAttribute('data-severity');
            const show = (filter === 'ALL') || (cat === filter) || (sev === filter);
            card.style.display = show ? '' : 'none';
          }});
        }});
      }}

      const copyBtn = document.getElementById('copySafeMcpBtn');
      const codePre = document.getElementById('safeMcpCode');
      if (copyBtn && codePre) {{
        copyBtn.addEventListener('click', function() {{
          const raw = codePre.textContent || '';
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(raw).then(function() {{
              copyBtn.textContent = 'Copied!';
              setTimeout(() => {{ copyBtn.textContent = 'Copy Safe JSON'; }}, 1600);
            }});
          }}
        }});
      }}
    }})();
  </script>
</body>
</html>
"""


def generate_html_report(
    input_path: Path,
    output_html_path: Path,
    *,
    mcp_path: Optional[Path] = None,
    budget_limit_usd: Optional[Decimal] = None,
    title: Optional[str] = None,
) -> Dict[str, Optional[Any]]:
    """Analyze `input_path` (transcript file, directory, or MCP JSON) and write self-contained HTML report."""
    from burnguard.analyzer import analyze_path
    from burnguard.mcp_audit import audit_mcp_file

    analysis_res: Optional[AnalysisReport] = None
    mcp_res: Optional[McpAuditReport] = None

    # Check if input_path itself is an MCP JSON config
    is_direct_mcp = False
    if input_path.is_file() and input_path.suffix.lower() == ".json":
        try:
            peek = json.loads(input_path.read_text(encoding="utf-8"))
            if isinstance(peek, dict) and ("mcpServers" in peek or ("servers" in peek and "turns" not in peek)):
                is_direct_mcp = True
        except json.JSONDecodeError:
            is_direct_mcp = False

    if is_direct_mcp:
        mcp_res = audit_mcp_file(input_path)
    else:
        analysis_res = analyze_path(input_path)
        # Auto-detect companion MCP config if input_path is a directory and mcp_path wasn't explicitly set
        if mcp_path is None and input_path.is_dir():
            for candidate_name in ("mcp_config.json", "mcp.json", ".mcp.json", "claude_desktop_config.json", "vulnerable_mcp_config.json"):
                cand = input_path / candidate_name
                if cand.is_file():
                    mcp_path = cand
                    break

    if mcp_path is not None and mcp_res is None:
        mcp_res = audit_mcp_file(mcp_path)

    doc_title = title or f"BurnGuard Pro Diagnostic — {input_path.name}"
    html_str = render_html_dashboard(
        analysis=analysis_res,
        mcp_audit=mcp_res,
        title=doc_title,
        budget_limit_usd=budget_limit_usd,
    )
    output_html_path.parent.mkdir(parents=True, exist_ok=True)
    output_html_path.write_text(html_str, encoding="utf-8")
    return {"analysis": analysis_res, "mcp_audit": mcp_res, "html_path": output_html_path}
