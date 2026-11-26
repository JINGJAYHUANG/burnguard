"""MCP (Model Context Protocol) security & token-tax configuration auditor with safe auto-remediation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from burnguard.pricing import MODEL_PRICING_2026, ONE_MILLION, USD_CENT, USD_MICRO, ZERO, quantize_usd

# Known official / community MCP package pin catalog & estimated eager tool footprints
_KNOWN_MCP_CATALOG: Dict[str, Tuple[str, int]] = {
    "@modelcontextprotocol/server-filesystem": ("2026.1.0", 11),
    "@modelcontextprotocol/server-github": ("2026.1.0", 34),
    "@modelcontextprotocol/server-postgres": ("2026.1.0", 6),
    "@modelcontextprotocol/server-sqlite": ("2026.1.0", 8),
    "@modelcontextprotocol/server-slack": ("2026.1.0", 14),
    "@modelcontextprotocol/server-gdrive": ("2026.1.0", 9),
    "@modelcontextprotocol/server-puppeteer": ("2026.1.0", 12),
    "@playwright/mcp": ("0.1.0", 24),
    "@notionhq/notion-mcp-server": ("1.8.0", 19),
    "@linear/mcp-server": ("1.2.0", 22),
    "@sentry/mcp-server": ("0.9.0", 18),
    "@stripe/mcp": ("0.4.0", 28),
    "@cloudflare/mcp-server-cloudflare": ("0.3.0", 31),
    "mcp-server-git": ("2026.1.0", 13),
    "mcp-server-fetch": ("2026.1.0", 2),
    "mcp-server-shell": ("0.2.0", 4),
}

_SECRET_VALUE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("Anthropic API Key", re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}")),
    ("OpenAI API Key", re.compile(r"sk-(?:proj-|live-|svcacct-)?[A-Za-z0-9_-]{16,}")),
    ("GitHub Personal Access Token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("Slack Token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("GitLab Token", re.compile(r"glpat-[A-Za-z0-9_-]{16,}")),
    ("Stripe Secret Key", re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Inline Database Credentials URI", re.compile(r"(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis)://[^/\s:@]+:[^/\s@]+@[^\s]+")),
)

_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY|AUTH|CREDENTIALS|CONNECTION_STRING|DATABASE_URL)",
    re.IGNORECASE,
)

_ENV_PLACEHOLDER_RE = re.compile(r"^(\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{[^}]+\}\}|<[^>]+>|env:[A-Za-z_][A-Za-z0-9_]*)$")

_DANGEROUS_MOUNT_PATHS = {
    "/",
    "/users",
    "/home",
    "/root",
    "/etc",
    "/var",
    "~",
    "$home",
    "${home}",
    "c:\\",
    "c:/",
    "..",
}

_SHELL_SERVER_RE = re.compile(
    r"(?:^|[-_/])(shell|bash|zsh|cmd|powershell|terminal|exec|command)(?:$|[-_/])",
    re.IGNORECASE,
)

_DANGEROUS_ARGS_SET = {
    "--dangerously-skip-permissions",
    "--allow-all",
    "--no-sandbox",
    "--unrestricted",
    "--yolo",
    "*",
}

AVG_TOKENS_PER_MCP_TOOL = 220


def redact_secret(raw: str) -> str:
    """Mask middle portion of a secret string so audit reports never leak credentials."""
    s = raw.strip()
    if "://" in s and "@" in s:
        scheme, rest = s.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***REDACTED***@{host[:32]}"
    if len(s) <= 10:
        return "***REDACTED***"
    return f"{s[:6]}...{s[-4:]}"


def _is_pinned_npm_package(pkg_token: str) -> Tuple[bool, str, str]:
    """Check whether an npm package specifier has an exact version pin (not `@latest` or unversioned).

    Returns `(is_pinned, base_package_name, version_part)`.
    """
    if pkg_token.startswith("@"):
        # Scoped package: `@scope/name` or `@scope/name@1.2.3`
        slash_idx = pkg_token.find("/")
        if slash_idx == -1:
            return False, pkg_token, ""
        at_after_slash = pkg_token.find("@", slash_idx + 1)
        if at_after_slash == -1:
            return False, pkg_token, ""
        base = pkg_token[:at_after_slash]
        ver = pkg_token[at_after_slash + 1 :]
    else:
        if "@" not in pkg_token:
            return False, pkg_token, ""
        base, ver = pkg_token.split("@", 1)

    if not ver or ver.lower() in ("latest", "next", "canary", "main", "master", "*"):
        return False, base, ver
    if ver[0] in ("^", "~", ">", "<"):
        return False, base, ver
    return True, base, ver


def _is_dangerous_filesystem_path(arg: str) -> bool:
    norm = arg.strip().rstrip("/").lower()
    if arg.strip() == "/":
        return True
    if norm in _DANGEROUS_MOUNT_PATHS:
        return True
    # Direct home directory mount such as `/Users/alice` or `/home/ubuntu` (depth <= 2)
    parts = [p for p in arg.strip().split("/") if p]
    if len(parts) == 2 and parts[0].lower() in ("users", "home"):
        return True
    if norm.endswith("/.ssh") or norm.endswith("/.aws") or norm.endswith("/.gnupg"):
        return True
    return False


@dataclass
class McpFinding:
    """Single security or token-cost vulnerability in an MCP configuration."""

    rule_id: str
    server_name: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW
    risk_points: int
    category: str  # SECURITY | SUPPLY_CHAIN | SANDBOX | COST
    title: str
    evidence: str
    remediation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "server_name": self.server_name,
            "severity": self.severity,
            "risk_points": self.risk_points,
            "category": self.category,
            "title": self.title,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class McpAuditReport:
    """Complete MCP security & token-tax audit output plus auto-remediated safe config."""

    source_path: str
    server_count: int
    active_server_count: int
    estimated_eager_tools: int
    estimated_schema_tokens_per_turn: int
    estimated_100_turn_tax_usd: Decimal
    risk_score: int  # 0..100
    security_grade: str  # A..F
    findings: List[McpFinding] = field(default_factory=list)
    remediated_config: Dict[str, Any] = field(default_factory=dict)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "CRITICAL")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")

    def to_dict(self, include_remediated: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source_path": self.source_path,
            "server_count": self.server_count,
            "active_server_count": self.active_server_count,
            "estimated_eager_tools": self.estimated_eager_tools,
            "estimated_schema_tokens_per_turn": self.estimated_schema_tokens_per_turn,
            "estimated_100_turn_tax_usd": str(quantize_usd(self.estimated_100_turn_tax_usd, USD_MICRO)),
            "estimated_100_turn_tax_display": f"${quantize_usd(self.estimated_100_turn_tax_usd, USD_CENT):,.2f}",
            "risk_score": self.risk_score,
            "security_grade": self.security_grade,
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "finding_count": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
        }
        if include_remediated:
            payload["remediated_config"] = self.remediated_config
        return payload


def _grade_from_score(score: int, critical_count: int) -> str:
    if critical_count > 0 and score >= 50:
        return "F"
    if critical_count > 0 or score >= 65:
        return "D"
    if score >= 40:
        return "C"
    if score >= 15:
        return "B"
    return "A"


def audit_mcp_config(
    config: Dict[str, Any],
    *,
    source_path: str = "<mcp_config.json>",
    reference_model: str = "claude-sonnet-4-6",
) -> McpAuditReport:
    """Audit a parsed MCP JSON config dict and produce findings + sanitized drop-in remediation."""
    safe_cfg = copy.deepcopy(config)
    root_key = "mcpServers" if "mcpServers" in safe_cfg else ("servers" if "servers" in safe_cfg else "mcpServers")
    servers_obj = safe_cfg.get(root_key)
    if not isinstance(servers_obj, dict):
        servers_obj = {}
        safe_cfg[root_key] = servers_obj

    findings: List[McpFinding] = []
    total_eager_tools = 0
    active_servers = 0

    for srv_name, srv_spec in servers_obj.items():
        if not isinstance(srv_spec, dict):
            continue

        is_disabled = bool(srv_spec.get("disabled", False))
        if not is_disabled:
            active_servers += 1

        cmd = str(srv_spec.get("command", "")).strip()
        args = [str(a) for a in srv_spec.get("args", [])] if isinstance(srv_spec.get("args"), list) else []
        env_map = srv_spec.get("env") if isinstance(srv_spec.get("env"), dict) else {}
        url = str(srv_spec.get("url") or srv_spec.get("serverUrl") or "").strip()

        # ---------------------------------------------------------------------
        # 1. Check `env` & `args` for hardcoded API keys, tokens, credentials
        # ---------------------------------------------------------------------
        for env_k, env_v in list(env_map.items()):
            val_str = str(env_v).strip()
            if not val_str or _ENV_PLACEHOLDER_RE.match(val_str):
                continue

            matched_label: Optional[str] = None
            for label, pat in _SECRET_VALUE_PATTERNS:
                if pat.search(val_str):
                    matched_label = label
                    break

            if matched_label is None and _SENSITIVE_ENV_NAME_RE.search(env_k) and len(val_str) >= 8:
                if val_str.lower() not in ("true", "false", "none", "development", "production"):
                    matched_label = f"Plaintext secret in `{env_k}`"

            if matched_label is not None:
                safe_var = re.sub(r"[^A-Za-z0-9_]", "_", env_k.upper())
                findings.append(
                    McpFinding(
                        rule_id="MCP-SEC-001",
                        server_name=srv_name,
                        severity="CRITICAL",
                        risk_points=30,
                        category="SECURITY",
                        title=f"Hardcoded {matched_label} in `{srv_name}.env.{env_k}`",
                        evidence=f"env.{env_k} = \"{redact_secret(val_str)}\" stored in plaintext JSON config.",
                        remediation=f"Move credential to OS keychain / shell environment and reference `${{{safe_var}}}`.",
                    )
                )
                env_map[env_k] = f"${{{safe_var}}}"

        for idx, arg_val in enumerate(args):
            if _ENV_PLACEHOLDER_RE.match(arg_val.strip()):
                continue
            for label, pat in _SECRET_VALUE_PATTERNS:
                if pat.search(arg_val):
                    placeholder_name = f"{ re.sub(r'[^A-Za-z0-9_]', '_', srv_name.upper()) }_SECRET_ARG"
                    findings.append(
                        McpFinding(
                            rule_id="MCP-SEC-001",
                            server_name=srv_name,
                            severity="CRITICAL",
                            risk_points=30,
                            category="SECURITY",
                            title=f"Hardcoded {label} in `{srv_name}.args[{idx}]`",
                            evidence=f"CLI argument `{redact_secret(arg_val)}` exposes credential to local process table (`ps aux`).",
                            remediation=f"Pass secret via environment variable `${{{placeholder_name}}}` instead of CLI arguments.",
                        )
                    )
                    if "://" in arg_val and "@" in arg_val:
                        args[idx] = f"${{{placeholder_name}_URI}}"
                    else:
                        args[idx] = f"${{{placeholder_name}}}"
                    break

        # ---------------------------------------------------------------------
        # 2. Check supply-chain risk: `npx -y` / unpinned npm or `uvx` package
        # ---------------------------------------------------------------------
        base_cmd = Path(cmd).name.lower() if cmd else ""
        detected_pkg_name = ""
        if base_cmd in ("npx", "bunx", "pnpm") and not is_disabled:
            has_yes = "-y" in args or "--yes" in args
            pkg_idx: Optional[int] = None
            for i, tok in enumerate(args):
                if tok.startswith("-"):
                    continue
                if tok in ("dlx", "exec"):
                    continue
                pkg_idx = i
                break

            if pkg_idx is not None:
                pkg_tok = args[pkg_idx]
                pinned, base_pkg, ver_str = _is_pinned_npm_package(pkg_tok)
                detected_pkg_name = base_pkg
                if has_yes or not pinned:
                    rec_ver = _KNOWN_MCP_CATALOG.get(base_pkg, ("1.0.0", 10))[0]
                    reason_parts = []
                    if has_yes:
                        reason_parts.append("auto-install flag `-y`")
                    if not pinned:
                        reason_parts.append(f"unpinned package `{pkg_tok}`")
                    findings.append(
                        McpFinding(
                            rule_id="MCP-SEC-002",
                            server_name=srv_name,
                            severity="HIGH",
                            risk_points=18,
                            category="SUPPLY_CHAIN",
                            title=f"Unpinned Dynamic Package Execution in `{srv_name}`",
                            evidence=f"`{cmd} {' '.join(args[:4])}` uses {' + '.join(reason_parts)}, pulling arbitrary upstream registry tarballs at launch.",
                            remediation=f"Pin exact verified version `{base_pkg}@{rec_ver}` and pre-install or verify package integrity.",
                        )
                    )
                    if not pinned:
                        args[pkg_idx] = f"{base_pkg}@{rec_ver}"
                    args = [a for a in args if a not in ("-y", "--yes")]
        elif base_cmd in ("uvx", "pipx") and not is_disabled:
            pkg_idx = next((i for i, tok in enumerate(args) if not tok.startswith("-") and tok != "run"), None)
            if pkg_idx is not None:
                pkg_tok = args[pkg_idx]
                detected_pkg_name = pkg_tok.split("==")[0].split("@")[0]
                if "==" not in pkg_tok and "@" not in pkg_tok:
                    rec_ver = _KNOWN_MCP_CATALOG.get(detected_pkg_name, ("1.0.0", 10))[0]
                    findings.append(
                        McpFinding(
                            rule_id="MCP-SEC-002",
                            server_name=srv_name,
                            severity="HIGH",
                            risk_points=15,
                            category="SUPPLY_CHAIN",
                            title=f"Unpinned Python Tool Runner (`{base_cmd} {pkg_tok}`)",
                            evidence=f"`{base_cmd}` executes latest unpinned PyPI release of `{pkg_tok}` on startup.",
                            remediation=f"Pin exact package version `{detected_pkg_name}=={rec_ver}`.",
                        )
                    )
                    args[pkg_idx] = f"{detected_pkg_name}=={rec_ver}"

        # ---------------------------------------------------------------------
        # 3. Check unrestricted filesystem root mounts & unrestricted shell execution
        # ---------------------------------------------------------------------
        for i, tok in enumerate(args):
            if not is_disabled and _is_dangerous_filesystem_path(tok):
                findings.append(
                    McpFinding(
                        rule_id="MCP-SEC-003",
                        server_name=srv_name,
                        severity="CRITICAL",
                        risk_points=28,
                        category="SANDBOX",
                        title=f"Unrestricted Filesystem Mount (`{tok}`) on `{srv_name}`",
                        evidence=f"Argument `{tok}` grants prompt-injected tool calls read/write reach across system or home directories.",
                        remediation="Scope filesystem server strictly to `${WORKSPACE_FOLDER}` and enable read-only mode where possible.",
                    )
                )
                args[i] = "${WORKSPACE_FOLDER}"

        is_shell_server = bool(
            _SHELL_SERVER_RE.search(srv_name)
            or _SHELL_SERVER_RE.search(detected_pkg_name)
            or base_cmd in ("sh", "bash", "zsh")
        )
        dangerous_flags_found = [a for a in args if a in _DANGEROUS_ARGS_SET]
        allow_all_env = str(env_map.get("ALLOWED_COMMANDS", "")).strip() == "*"

        if not is_disabled and (is_shell_server or dangerous_flags_found or allow_all_env):
            evidence_bits = []
            if is_shell_server:
                evidence_bits.append(f"shell execution server (`{cmd} {' '.join(args[:3])}`)")
            if dangerous_flags_found:
                evidence_bits.append(f"unsafe flags `{', '.join(dangerous_flags_found)}`")
            if allow_all_env:
                evidence_bits.append("`ALLOWED_COMMANDS=*`")
            findings.append(
                McpFinding(
                    rule_id="MCP-SEC-003",
                    server_name=srv_name,
                    severity="CRITICAL",
                    risk_points=28,
                    category="SANDBOX",
                    title=f"Unrestricted Shell / Command Execution Risk on `{srv_name}`",
                    evidence=f"Detected {', '.join(evidence_bits)} without container or command-allowlist sandboxing.",
                    remediation="Disable unrestricted shell MCP servers or enforce a strict read-only command allowlist inside workspace boundaries.",
                )
            )
            args = [a for a in args if a not in _DANGEROUS_ARGS_SET]
            if allow_all_env:
                env_map["ALLOWED_COMMANDS"] = "git status,git diff,pytest"
            if is_shell_server and not srv_spec.get("allowedCommands"):
                srv_spec["disabled"] = True
                srv_spec["burnguardNote"] = "Disabled by BurnGuard safe remediation due to unsandboxed shell access"

        # ---------------------------------------------------------------------
        # 4. Check insecure plaintext remote transport (`http://` non-localhost)
        # ---------------------------------------------------------------------
        if not is_disabled and url.startswith("http://"):
            lower_u = url.lower()
            is_local = lower_u.startswith("http://localhost") or lower_u.startswith("http://127.0.0.1")
            if not is_local:
                findings.append(
                    McpFinding(
                        rule_id="MCP-SEC-004",
                        server_name=srv_name,
                        severity="HIGH",
                        risk_points=20,
                        category="SECURITY",
                        title=f"Unencrypted Remote MCP Endpoint (`http://`) on `{srv_name}`",
                        evidence=f"Remote URL `{url[:64]}` transmits MCP tool payloads and headers without TLS.",
                        remediation="Upgrade remote MCP transport URL to `https://` with mTLS or OAuth 2.1 bearer scoping.",
                    )
                )
                url_key = "url" if "url" in srv_spec else "serverUrl"
                srv_spec[url_key] = "https://" + url[len("http://") :]

        # Write cleaned args back
        if "args" in srv_spec:
            srv_spec["args"] = args

        # ---------------------------------------------------------------------
        # 5. Estimate Eager Tool Count & Schema Token Tax
        # ---------------------------------------------------------------------
        if not is_disabled:
            explicit_count = srv_spec.get("toolCount") or srv_spec.get("eagerTools")
            if isinstance(srv_spec.get("tools"), list):
                explicit_count = len(srv_spec["tools"])
            if explicit_count is not None:
                est_tools = int(explicit_count)
            elif detected_pkg_name in _KNOWN_MCP_CATALOG:
                est_tools = _KNOWN_MCP_CATALOG[detected_pkg_name][1]
            else:
                est_tools = 12  # conservative default for unknown MCP server

            is_lazy = bool(srv_spec.get("defer_loading") or srv_spec.get("lazyLoad"))
            if not is_lazy:
                total_eager_tools += est_tools
                if est_tools >= 20:
                    findings.append(
                        McpFinding(
                            rule_id="MCP-COST-001",
                            server_name=srv_name,
                            severity="MEDIUM",
                            risk_points=10,
                            category="COST",
                            title=f"High Eager Tool Footprint on `{srv_name}` (~{est_tools} tools)",
                            evidence=(
                                f"`{srv_name}` injects ~{est_tools} tool schemas (~{est_tools * AVG_TOKENS_PER_MCP_TOOL:,} tokens) "
                                f"into every turn even when unused."
                            ),
                            remediation="Enable `\"defer_loading\": true` or restrict `\"allowedTools\"` to required actions.",
                        )
                    )
                    srv_spec["defer_loading"] = True

    # Global eager tool bloat check across the entire config
    ref_rate = MODEL_PRICING_2026.get(reference_model, MODEL_PRICING_2026["claude-sonnet-4-6"])
    eager_schema_tokens = total_eager_tools * AVG_TOKENS_PER_MCP_TOOL
    tax_100_turns = (Decimal(eager_schema_tokens) * Decimal("100") * ref_rate.input_per_m) / ONE_MILLION

    if total_eager_tools > 30:
        findings.append(
            McpFinding(
                rule_id="MCP-COST-002",
                server_name="<global>",
                severity="HIGH" if total_eager_tools >= 60 else "MEDIUM",
                risk_points=14 if total_eager_tools >= 60 else 8,
                category="COST",
                title=f"Excessive Global Eager MCP Tool Count ({total_eager_tools} tools across {active_servers} servers)",
                evidence=(
                    f"{total_eager_tools} eager tools add ~{eager_schema_tokens:,} input tokens per turn "
                    f"(${quantize_usd(tax_100_turns, USD_CENT):,.2f} uncached token tax per 100 turns on {ref_rate.model_id})."
                ),
                remediation="Set `\"defer_loading\": true` across secondary MCP servers and split configs by project workspace.",
            )
        )
        for srv_spec in servers_obj.values():
            if isinstance(srv_spec, dict) and not srv_spec.get("disabled"):
                srv_spec.setdefault("defer_loading", True)

    # Sort findings by severity & risk points
    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: (sev_order.get(f.severity, 9), -f.risk_points, f.server_name))

    raw_points = sum(f.risk_points for f in findings)
    risk_score = min(100, raw_points)
    crit_count = sum(1 for f in findings if f.severity == "CRITICAL")
    grade = _grade_from_score(risk_score, crit_count)

    return McpAuditReport(
        source_path=source_path,
        server_count=len(servers_obj),
        active_server_count=active_servers,
        estimated_eager_tools=total_eager_tools,
        estimated_schema_tokens_per_turn=eager_schema_tokens,
        estimated_100_turn_tax_usd=tax_100_turns,
        risk_score=risk_score,
        security_grade=grade,
        findings=findings,
        remediated_config=safe_cfg,
    )


def audit_mcp_file(path: Path, *, reference_model: str = "claude-sonnet-4-6") -> McpAuditReport:
    """Load and audit an MCP JSON configuration file from disk."""
    if not path.exists():
        raise FileNotFoundError(f"MCP configuration file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"MCP config must be a JSON object: {path}")
    return audit_mcp_config(data, source_path=str(path), reference_model=reference_model)
