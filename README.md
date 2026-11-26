# BurnGuard (`burnguard`) — v1.0.0

[![License: MIT](https://img.shields.io/badge/License-MIT-emerald.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](./pyproject.toml)
[![Live Demo](https://img.shields.io/badge/Live_Demo-Interactive_HTML_Report-6366f1.svg)](https://jingjayhuang.github.io/unit-500-lifeline/burnguard-demo.html)

**AI Agent Token Cost Optimizer, Deterministic CI Budget Circuit-Breaker & MCP Security Auditor**
**AI 智能体 Token 成本优化器、CI 预算熔断器与 MCP 安全审计工具（零依赖 · 纯本地离线 · MIT 开源）**

> Engineered by **Unit-500** — cut **Claude Code, OpenAI Codex, Google Gemini, Cursor & MCP** token spend by **35–65%** while sealing local MCP credential-leak and supply-chain risks. **100% offline, zero runtime dependencies, exact `decimal.Decimal` accounting.**
> 由 **Unit-500** 打造——将 **Claude Code、Codex、Gemini、Cursor 与 MCP** 的 Token 开销降低 **35%–65%**，同时阻断本地 MCP 明文密钥泄露与供应链风险。**100% 离线运行、零第三方运行时依赖、`decimal.Decimal` 精确计费。**

---

## English Overview

### Four Token-Burn Vectors Detected & Quantified
1. **Runaway Retry / Error Loops (`RUNAWAY_RETRY_LOOP`)** — Consecutive failing tool/shell invocations re-billing massive context windows on every retry.
2. **Prompt-Cache Busting (`PROMPT_CACHE_BUSTING`)** — Dynamic timestamps or UUIDs mutating system prompt prefixes and invalidating KV-cache reads.
3. **Bloated Eager MCP Schemas (`BLOATED_TOOL_SCHEMAS`)** — Unused MCP tool definitions injected into every turn instead of lazy-loading on demand.
4. **Oversized File & Command Dumps (`OVERSIZED_CONTEXT_DUMP`)** — Unfiltered logs or lockfiles inflating every subsequent turn's input token count.

BurnGuard calculates both standalone waste and **de-duplicated Waterfall recoverable USD** (`Retry Loops -> Context Dumps -> MCP Schema Bloat -> Cache-Busting Recovery`) so savings are never double-counted.

### Key Capabilities
- **Multi-Schema Transcript Parser**: Anthropic / Claude Code (`cache_read_input_tokens`, paired `tool_use`/`tool_result` JSONL), OpenAI / Codex (`cached_tokens`, `reasoning_tokens`), Google Gemini (`usageMetadata`, `functionDeclarations`), and generic JSON/JSONL harnesses.
- **MCP Security & Token-Tax Auditor (`audit-mcp`)**: Flags hardcoded API keys/tokens (auto-redacted in output), unpinned `npx -y` / `uvx` supply-chain execution, unrestricted `/` or `$HOME` mounts, and unencrypted `http://` servers — with `--fix-preview` and `--write-safe` remediation.
- **Deterministic CI Circuit Breaker (`budget-guard`)**: Strict exit codes (`0` PASS, `2` BREACH_BUDGET, `3` BREACH_WASTE, `4` BREACH_SECURITY, `10` WARN) + GitHub Actions annotations (`--format github`).
- **100% Offline Dark-Mode HTML Dashboard & Local Web Workbench (`report` & `serve`)**: Zero external CDNs; see [`examples/sample-flagship-report.html`](./examples/sample-flagship-report.html).

---

## 简体中文说明

### 核心功能一览
- **多框架会话日志解析 (`burnguard analyze`)**：原生支持 **Claude Code / Anthropic**、**OpenAI / Codex**、**Google Gemini** 及通用 JSON/JSONL 智能体调用轨迹，基于内置 2026 模型价格表与 `decimal.Decimal` 精确核算每一轮输入、缓存命中、推理及输出成本。
- **四维浪费诊断与瀑布流去重归因**：自动定位**死循环重试 (`RUNAWAY_RETRY_LOOP`)**、**提示词缓存击穿 (`PROMPT_CACHE_BUSTING`)**、**冗余 MCP 工具注入 (`BLOATED_TOOL_SCHEMAS`)** 与**超大文件/日志倾倒 (`OVERSIZED_CONTEXT_DUMP`)**，输出精确可挽回美元金额。
- **MCP 配置安全与成本审计 (`burnguard audit-mcp`)**：检测 `env` 明文密钥（自动脱敏）、未锁定版本的 `npx -y` 供应链风险、挂载 `/` 或 `$HOME` 的越权目录暴露，并支持 `--fix-preview` / `--write-safe` 一键生成加固配置。
- **CI / 定时任务预算熔断器 (`burnguard budget-guard`)**：确定性退出码（`0` 通过、`2` 超预算、`3` 浪费超标、`4` 严重安全风险、`10` 预警），可直接接入 GitHub Actions。
- **零外部依赖离线 HTML 诊断看板 (`burnguard report` / `burnguard serve`)**：无需联网或外部 CDN，开箱查看 [`examples/sample-flagship-report.html`](./examples/sample-flagship-report.html)。

---

## Quickstart / 快速上手 (Python 3.11+, Zero Dependencies)

```bash
# Install directly from GitHub / 直接从 GitHub 安装（零第三方运行时依赖）
python3 -m venv .venv
.venv/bin/pip install git+https://github.com/JINGJAYHUANG/burnguard.git

# Or install from a local clone / 或从本地克隆安装
.venv/bin/pip install .

# 1. Analyze agent session transcripts / 分析智能体会话日志
.venv/bin/burnguard analyze examples/benchmarks

# 2. Audit & remediate MCP configs / 审计并修复 MCP 配置
.venv/bin/burnguard audit-mcp examples/mcp/vulnerable_mcp_config.json --fix-preview

# 3. CI budget & security gate / CI 预算与安全门禁
.venv/bin/burnguard budget-guard --input examples/benchmarks --daily-limit 10.00 --max-waste-pct 45 --max-retry-loops 0 --format github

# 4. Generate offline HTML report / 生成纯离线交互式 HTML 报告
.venv/bin/burnguard report examples/benchmarks --mcp examples/mcp/vulnerable_mcp_config.json --budget-limit 15.00 --html examples/sample-flagship-report.html

# 5. Run test suite / 运行单元测试套件
.venv/bin/pytest -v
```

---

## License / 开源协议

Released as **$0 Free & Open-Source Software** under the [MIT License](./LICENSE).
本项目采用 [MIT License](./LICENSE) 免费开源发布（零遥测、零云端依赖）。
