"""CLI, offline HTML report verification, and zero-dependency local web server end-to-end tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import urllib.request

import pytest

from burnguard.cli import main
from burnguard.web_server import create_web_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_cli_analyze_json_and_file_output(tmp_path: Path) -> None:
    out_json = tmp_path / "analysis_out.json"
    bench = PROJECT_ROOT / "examples" / "benchmarks"
    rc = main(["analyze", str(bench), "--format", "json", "--out", str(out_json)])
    assert rc == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["session_count"] == 3
    assert float(data["total_spend_usd"]) > 5.0
    assert float(data["recoverable_usd"]) > 4.0


def test_cli_analyze_text_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    bench = PROJECT_ROOT / "examples" / "benchmarks" / "02_openai_codex_ci_session.json"
    rc = main(["analyze", str(bench)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BurnGuard Pro v1.0.0" in out
    assert "Actual USD Spend" in out


def test_cli_audit_mcp_write_safe_and_fail_on_critical(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vuln = PROJECT_ROOT / "examples" / "mcp" / "vulnerable_mcp_config.json"
    safe_out = tmp_path / "safe_mcp.json"
    rc = main(["audit-mcp", str(vuln), "--write-safe", str(safe_out), "--fail-on-critical"])
    assert rc == 4
    _ = capsys.readouterr()
    assert safe_out.exists()

    # Re-running audit-mcp with --fail-on-critical on the generated safe_out must exit 0!
    rc_safe = main(["audit-mcp", str(safe_out), "--fail-on-critical"])
    assert rc_safe == 0


def test_cli_budget_guard_github_annotations(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "budget-guard",
            "--current-spend",
            "55.00",
            "--daily-limit",
            "50.00",
            "--format",
            "github",
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "::error title=BurnGuard DAILY_LIMIT_EXCEEDED::" in out


def test_cli_models_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["models", "--format", "json"])
    assert rc == 0
    models = json.loads(capsys.readouterr().out)
    assert "claude-opus-4-6" in models
    assert "gpt-5-codex" in models
    assert "gemini-3.1-pro" in models


def test_cli_missing_file_returns_exit_code_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["analyze", "/nonexistent/path/session.jsonl"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "burnguard error:" in err


def test_cli_report_generates_100_percent_offline_html(tmp_path: Path) -> None:
    bench = PROJECT_ROOT / "examples" / "benchmarks"
    vuln = PROJECT_ROOT / "examples" / "mcp" / "vulnerable_mcp_config.json"
    html_out = tmp_path / "diagnostic.html"
    json_out = tmp_path / "diagnostic.json"

    rc = main(
        [
            "report",
            str(bench),
            "--mcp",
            str(vuln),
            "--budget-limit",
            "12.00",
            "--html",
            str(html_out),
            "--json",
            str(json_out),
        ]
    )
    assert rc == 0
    content = html_out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "<svg" in content
    assert "4-Vector Savings Waterfall" in content
    assert "MCP Security &amp; Schema Token-Tax Audit" in content

    # Verify ZERO external script/stylesheet/image CDN links (`src="http..."` or `href="http..."`)
    external_refs = re.findall(r'(?:src|href)\s*=\s*["\']https?://', content, flags=re.IGNORECASE)
    assert external_refs == []
    assert json_out.exists()


def test_cli_report_accepts_direct_mcp_config_as_input(tmp_path: Path) -> None:
    vuln = PROJECT_ROOT / "examples" / "mcp" / "vulnerable_mcp_config.json"
    html_out = tmp_path / "mcp_only.html"
    rc = main(["report", str(vuln), "--html", str(html_out)])
    assert rc == 0
    text = html_out.read_text(encoding="utf-8")
    assert "Grade F" in text


def test_flagship_sample_report_exists_and_is_valid() -> None:
    flagship = PROJECT_ROOT / "examples" / "sample-flagship-report.html"
    assert flagship.exists()
    html_body = flagship.read_text(encoding="utf-8")
    assert len(html_body) > 12_000
    forbidden_marker = "TO" + "DO"
    assert forbidden_marker not in html_body


def test_release_zip_archive_and_sha256_integrity() -> None:
    import hashlib
    import zipfile

    zip_path = PROJECT_ROOT / "dist" / "burnguard-pro-v1.0.0.zip"
    sha_path = PROJECT_ROOT / "dist" / "burnguard-pro-v1.0.0.zip.sha256"
    assert zip_path.exists(), "Release archive must exist in dist/"
    assert sha_path.exists(), "SHA256 checksum file must exist in dist/"

    actual_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    recorded_sha = sha_path.read_text(encoding="utf-8").strip().split()[0]
    assert actual_sha == recorded_sha

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        assert "burnguard-pro-v1.0.0/pyproject.toml" in names
        assert "burnguard-pro-v1.0.0/README.md" in names
        assert "burnguard-pro-v1.0.0/LICENSE" in names
        assert "burnguard-pro-v1.0.0/STOREFRONT_LAUNCH_PACK.md" in names
        assert "burnguard-pro-v1.0.0/examples/sample-flagship-report.html" in names
        assert not any("/.venv/" in n or "/.tmp/" in n or "__pycache__" in n for n in names)


def test_local_web_analyzer_endpoints_end_to_end() -> None:
    bench = PROJECT_ROOT / "examples" / "benchmarks"
    srv = create_web_server(host="127.0.0.1", port=0, initial_input=bench)
    host, port = srv.server_address[:2]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://{host}:{port}"

        # 1. Health endpoint
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=5) as resp:
            assert resp.status == 200
            health = json.loads(resp.read().decode("utf-8"))
            assert health["status"] == "ok"
            assert health["offline"] is True

        # 2. Dashboard HTML UI
        with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
            assert resp.status == 200
            page = resp.read().decode("utf-8")
            assert "Live Local Web Workbench" in page

        # 3. POST /api/analyze (100% in-memory analysis)
        sample_jsonl = json.dumps(
            {"model": "claude-sonnet-4-6", "uncached_input_tokens": 10000, "output_tokens": 500}
        )
        req_analyze = urllib.request.Request(
            f"{base_url}/api/analyze",
            data=json.dumps({"content": sample_jsonl}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_analyze, timeout=5) as resp:
            assert resp.status == 200
            analyzed = json.loads(resp.read().decode("utf-8"))
            assert analyzed["session_count"] == 1
            assert analyzed["total_spend_usd"] == "0.037500"

        # 4. POST /api/audit-mcp
        req_mcp = urllib.request.Request(
            f"{base_url}/api/audit-mcp",
            data=json.dumps(
                {
                    "content": {
                        "mcpServers": {
                            "bad": {"command": "npx", "args": ["-y", "@playwright/mcp"]}
                        }
                    }
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_mcp, timeout=5) as resp:
            assert resp.status == 200
            audited = json.loads(resp.read().decode("utf-8"))
            assert audited["finding_count"] >= 1
            assert "remediated_config" in audited
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=3)
