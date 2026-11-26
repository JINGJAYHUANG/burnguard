"""Zero-dependency local interactive web analyzer (`burnguard serve`)."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from burnguard import __version__
from burnguard.analyzer import AnalysisReport, analyze_path, analyze_sessions
from burnguard.mcp_audit import McpAuditReport, audit_mcp_config, audit_mcp_file
from burnguard.parser import (
    NormalizedSession,
    _extract_system_text,
    _extract_tool_definitions,
    _parse_records_into_session,
)
from burnguard.report_html import render_html_dashboard


def parse_raw_transcript_payload(raw_text: str, session_name: str = "live-paste") -> List[NormalizedSession]:
    """Parse pasted JSON or JSONL session text strictly in memory (zero temp files)."""
    stripped = raw_text.strip()
    if not stripped:
        return []

    # Try standard JSON first if it begins with `[` or `{` and parses cleanly as a single document
    if stripped[0] in ("[", "{"):
        try:
            doc = json.loads(stripped)
            if isinstance(doc, list):
                sess = _parse_records_into_session(doc, session_id=session_name, source_path="<browser>")
                return [sess] if sess.turns else []
            if isinstance(doc, dict):
                s_tools = _extract_tool_definitions(doc.get("tools", []))
                s_sys = _extract_system_text(doc.get("system") or doc.get("system_prompt"))
                s_model = doc.get("model")
                for key in ("turns", "events", "steps", "records"):
                    if isinstance(doc.get(key), list):
                        sess = _parse_records_into_session(
                            doc[key],
                            session_id=str(doc.get("session_id") or session_name),
                            source_path="<browser>",
                            session_level_tools=s_tools,
                            session_level_system=s_sys,
                            session_level_model=str(s_model) if s_model else None,
                        )
                        return [sess] if sess.turns else []
                sess = _parse_records_into_session([doc], session_id=session_name, source_path="<browser>")
                return [sess] if sess.turns else []
        except json.JSONDecodeError:
            pass

    # Parse as JSONL lines
    records: List[Dict[str, Any]] = []
    for idx, line in enumerate(stripped.splitlines(), start=1):
        ln = line.strip()
        if not ln:
            continue
        obj = json.loads(ln)
        if isinstance(obj, dict):
            records.append(obj)
    sess = _parse_records_into_session(records, session_id=session_name, source_path="<browser>")
    return [sess] if sess.turns else []


def _inject_interactive_workbench(base_html: str) -> str:
    """Inject an interactive Paste-and-Analyze local workbench panel into the served HTML UI."""
    workbench = """
    <section class="panel" style="max-width:1360px;margin:20px auto;padding:22px;">
      <div class="panel-head">
        <div>
          <h2>Live Local Web Workbench (Zero Cloud · 127.0.0.1 Only)</h2>
          <p class="subtext">Paste any JSONL/JSON session transcript or <code>mcp_config.json</code> below to run instant Decimal analysis in memory.</p>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
        <div>
          <textarea id="liveInputArea" rows="10" style="width:100%;background:#0A0E14;color:#E6EDF3;border:1px solid #232E3F;border-radius:8px;padding:12px;font-family:ui-monospace,monospace;font-size:12px;" placeholder='Paste JSONL session turns or {"mcpServers": {...}} here...'></textarea>
          <div style="display:flex;gap:10px;margin-top:10px;">
            <button type="button" class="btn-action" id="runAnalyzeBtn">Analyze Transcript</button>
            <button type="button" class="btn-action" id="runMcpAuditBtn">Audit MCP Config</button>
          </div>
        </div>
        <div class="code-card">
          <div class="code-card-head"><span>Live Engine Output (Exact Decimal JSON)</span></div>
          <pre id="liveResultPre">Ready. Waiting for local payload...</pre>
        </div>
      </div>
    </section>
    <script>
      (function() {
        const inputEl = document.getElementById('liveInputArea');
        const outEl = document.getElementById('liveResultPre');
        async function postEndpoint(url, bodyObj) {
          outEl.textContent = 'Running local Decimal engine...';
          try {
            const resp = await fetch(url, {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(bodyObj)
            });
            const data = await resp.json();
            outEl.textContent = JSON.stringify(data, null, 2);
          } catch (err) {
            outEl.textContent = 'Error: ' + String(err);
          }
        }
        const aBtn = document.getElementById('runAnalyzeBtn');
        const mBtn = document.getElementById('runMcpAuditBtn');
        if (aBtn) aBtn.addEventListener('click', () => postEndpoint('/api/analyze', { content: inputEl.value }));
        if (mBtn) mBtn.addEventListener('click', () => postEndpoint('/api/audit-mcp', { content: inputEl.value }));
      })();
    </script>
    """
    return base_html.replace("</main>", "</main>\n" + workbench)


def create_web_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    initial_input: Optional[Path] = None,
    initial_mcp: Optional[Path] = None,
) -> ThreadingHTTPServer:
    """Create a configured `ThreadingHTTPServer` instance (usable directly in tests or CLI)."""
    preload_analysis: Optional[AnalysisReport] = (
        analyze_path(initial_input) if initial_input is not None else None
    )
    preload_mcp: Optional[McpAuditReport] = (
        audit_mcp_file(initial_mcp) if initial_mcp is not None else None
    )

    class BurnGuardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # Keep console clean and avoid leaking payloads into logs

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/health"):
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "product": "BurnGuard Pro",
                        "version": __version__,
                        "offline": True,
                    },
                )
                return

            if self.path.startswith("/api/summary"):
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "analysis": preload_analysis.to_dict() if preload_analysis else None,
                        "mcp_audit": preload_mcp.to_dict() if preload_mcp else None,
                    },
                )
                return

            dashboard = render_html_dashboard(
                analysis=preload_analysis,
                mcp_audit=preload_mcp,
                title="BurnGuard Pro — Local Interactive Web Analyzer",
            )
            page = _inject_interactive_workbench(dashboard).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 20 * 1024 * 1024:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request payload size."})
                return

            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                req = json.loads(body)
            except json.JSONDecodeError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON request: {exc}"})
                return

            if self.path.startswith("/api/analyze"):
                content = str(req.get("content", ""))
                model_override = req.get("model_override")
                try:
                    sessions = parse_raw_transcript_payload(content)
                    rep = analyze_sessions(
                        sessions,
                        target_path="<web-workbench>",
                        model_override=str(model_override) if model_override else None,
                    )
                    self._send_json(HTTPStatus.OK, rep.to_dict())
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            if self.path.startswith("/api/audit-mcp"):
                content = req.get("content", req.get("config"))
                try:
                    cfg_obj = json.loads(content) if isinstance(content, str) else content
                    if not isinstance(cfg_obj, dict):
                        raise ValueError("MCP configuration must be a JSON object.")
                    audit_rep = audit_mcp_config(cfg_obj, source_path="<web-workbench>")
                    self._send_json(HTTPStatus.OK, audit_rep.to_dict(include_remediated=True))
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {self.path}"})

    return ThreadingHTTPServer((host, port), BurnGuardHandler)
