"""Unit tests for MCP security & token-tax auditing and CI budget-guard circuit breakers."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from burnguard.budget_guard import ExitCode, evaluate_budget
from burnguard.mcp_audit import audit_mcp_config, audit_mcp_file


def test_mcp_detects_secrets_and_redacts_evidence() -> None:
    fake_secret = "sk-ant-SYNTHETIC0SUPER0SECRET0VALUE9876"
    cfg = {
        "mcpServers": {
            "custom": {
                "command": "node",
                "args": ["server.js", "postgres://admin:PlainPass123@db.internal:5432/prod"],
                "env": {"ANTHROPIC_API_KEY": fake_secret},
                "defer_loading": True,
            }
        }
    }
    rep = audit_mcp_config(cfg)
    sec_001 = [f for f in rep.findings if f.rule_id == "MCP-SEC-001"]
    assert len(sec_001) == 2
    serialized = json.dumps(rep.to_dict(include_remediated=True))
    # Raw secrets MUST NEVER leak in evidence or remediated config!
    assert fake_secret not in serialized
    assert "PlainPass123" not in serialized
    assert rep.remediated_config["mcpServers"]["custom"]["env"]["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY}"


def test_mcp_detects_npx_unpinned_root_mount_shell_and_http() -> None:
    vuln_path = Path(__file__).resolve().parent.parent / "examples" / "mcp" / "vulnerable_mcp_config.json"
    rep = audit_mcp_file(vuln_path)
    assert rep.security_grade == "F"
    assert rep.risk_score == 100
    rule_ids = {f.rule_id for f in rep.findings}
    assert "MCP-SEC-001" in rule_ids  # Hardcoded keys
    assert "MCP-SEC-002" in rule_ids  # Unpinned npx -y / uvx
    assert "MCP-SEC-003" in rule_ids  # `/` mount + unsandboxed shell
    assert "MCP-SEC-004" in rule_ids  # Plaintext remote http://
    assert "MCP-COST-001" in rule_ids  # Per-server eager tools >= 20
    assert "MCP-COST-002" in rule_ids  # Global eager tools > 30
    assert rep.estimated_100_turn_tax_usd > Decimal("5.00")


def test_mcp_auto_remediation_is_idempotent_and_achieves_grade_a() -> None:
    vuln_path = Path(__file__).resolve().parent.parent / "examples" / "mcp" / "vulnerable_mcp_config.json"
    first_pass = audit_mcp_file(vuln_path)
    assert first_pass.critical_count >= 3

    second_pass = audit_mcp_config(first_pass.remediated_config)
    assert second_pass.findings == []
    assert second_pass.risk_score == 0
    assert second_pass.security_grade == "A"


def test_mcp_localhost_http_and_pinned_scoped_package_pass_cleanly() -> None:
    clean_cfg = {
        "mcpServers": {
            "local-dev": {
                "url": "http://127.0.0.1:8090/mcp",
                "eagerTools": 4,
                "defer_loading": True,
            },
            "pinned-fs": {
                "command": "npx",
                "args": ["@modelcontextprotocol/server-filesystem@2026.1.0", "${WORKSPACE_FOLDER}"],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                "defer_loading": True,
            },
        }
    }
    rep = audit_mcp_config(clean_cfg)
    assert rep.findings == []
    assert rep.security_grade == "A"


def test_budget_guard_pass_and_headroom() -> None:
    dec = evaluate_budget(
        current_spend_usd=Decimal("12.50"),
        projected_next_usd=Decimal("2.00"),
        daily_limit_usd=Decimal("25.00"),
        monthly_limit_usd=Decimal("500.00"),
    )
    assert dec.status == "PASS"
    assert dec.exit_code == int(ExitCode.PASS)
    assert dec.projected_total_usd == Decimal("14.50")
    assert dec.headroom_usd == Decimal("10.50")


def test_budget_guard_warn_strict_vs_non_strict() -> None:
    non_strict = evaluate_budget(
        current_spend_usd=Decimal("85.00"),
        monthly_limit_usd=Decimal("100.00"),
        warn_pct=Decimal("80"),
        strict_warn=False,
    )
    assert non_strict.status == "WARN"
    assert non_strict.exit_code == int(ExitCode.PASS)

    strict = evaluate_budget(
        current_spend_usd=Decimal("85.00"),
        monthly_limit_usd=Decimal("100.00"),
        warn_pct=Decimal("80"),
        strict_warn=True,
    )
    assert strict.status == "WARN"
    assert strict.exit_code == int(ExitCode.WARN)


def test_budget_guard_breach_budget_exit_code_2() -> None:
    dec = evaluate_budget(
        current_spend_usd=Decimal("24.00"),
        projected_next_usd=Decimal("3.50"),
        daily_limit_usd=Decimal("25.00"),
    )
    assert dec.status == "BLOCK"
    assert dec.exit_code == int(ExitCode.BREACH_BUDGET)


def test_budget_guard_breach_waste_and_retry_loops_exit_code_3() -> None:
    bench = Path(__file__).resolve().parent.parent / "examples" / "benchmarks" / "01_claude_code_runaway_session.jsonl"
    dec = evaluate_budget(
        transcript_path=bench,
        daily_limit_usd=Decimal("100.00"),
        max_retry_loops=0,
    )
    assert dec.status == "BLOCK"
    assert dec.exit_code == int(ExitCode.BREACH_WASTE)
    assert dec.retry_loop_count >= 1


def test_budget_guard_breach_mcp_security_exit_code_4() -> None:
    vuln_mcp = Path(__file__).resolve().parent.parent / "examples" / "mcp" / "vulnerable_mcp_config.json"
    dec = evaluate_budget(
        current_spend_usd=Decimal("1.00"),
        daily_limit_usd=Decimal("50.00"),
        mcp_config_path=vuln_mcp,
    )
    assert dec.status == "BLOCK"
    assert dec.exit_code == int(ExitCode.BREACH_SECURITY)
    assert dec.mcp_critical_count >= 3


def test_mcp_supports_vscode_servers_root_key() -> None:
    vscode_cfg = {
        "servers": {
            "home-fs": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/developer"],
            }
        }
    }
    rep = audit_mcp_config(vscode_cfg)
    assert rep.critical_count == 1
    assert rep.remediated_config["servers"]["home-fs"]["args"][-1] == "${WORKSPACE_FOLDER}"
    re_audit = audit_mcp_config(rep.remediated_config)
    assert re_audit.security_grade == "A"
