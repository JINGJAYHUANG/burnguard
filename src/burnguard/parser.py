"""Multi-schema agent session transcript parser (Claude Code, Anthropic, OpenAI/Codex, Gemini, Generic)."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def estimate_tokens_from_text(text: str) -> int:
    """Deterministic UTF-8 byte/4 token estimator for sub-component attribution."""
    if not text:
        return 0
    byte_len = len(text.encode("utf-8", errors="replace"))
    return max(1, math.ceil(byte_len / 4))


def estimate_tokens_from_obj(obj: Any) -> int:
    """Estimate token footprint of an arbitrary JSON-serializable object."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return estimate_tokens_from_text(obj)
    serialized = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return estimate_tokens_from_text(serialized)


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


_ERROR_PATTERNS = re.compile(
    r"(Traceback \(most recent call last\)|"
    r"Command exited with code [1-9]\d*|"
    r"Exit code [1-9]\d*|"
    r"\b(?:SyntaxError|TypeError|ValueError|KeyError|AssertionError|ModuleNotFoundError|FileNotFoundError|PermissionError)\b|"
    r"\b(?:ENOENT|EACCES|ECONNREFUSED|ETIMEDOUT)\b|"
    r"HTTP/\d(?:\.\d)?\s+(?:429|500|502|503|504)\b|"
    r"\"status\"\s*:\s*\"error\")",
    re.IGNORECASE,
)


def looks_like_error_output(text: str) -> bool:
    """Heuristic check for shell/tool error payloads when explicit `is_error` is absent."""
    if not text:
        return False
    sample = text[:4000]
    return bool(_ERROR_PATTERNS.search(sample))


@dataclass
class ToolDefinitionRecord:
    """Normalized tool/MCP schema definition present in a turn or session."""

    name: str
    estimated_tokens: int
    server_name: Optional[str] = None
    description_chars: int = 0


@dataclass
class ToolCallRecord:
    """Normalized tool invocation and its result payload."""

    call_id: str
    name: str
    arguments_preview: str
    normalized_signature: str
    is_error: bool = False
    output_chars: int = 0
    output_estimated_tokens: int = 0
    output_preview: str = ""


@dataclass
class NormalizedTurn:
    """Provider-agnostic representation of one billed LLM generation turn."""

    turn_index: int
    model: str
    schema_flavor: str
    timestamp: Optional[str] = None
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    system_prompt: str = ""
    system_prompt_tokens: int = 0
    tool_definitions: List[ToolDefinitionRecord] = field(default_factory=list)
    tool_schema_tokens: int = 0
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    raw_user_chars: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.uncached_input_tokens + self.cached_input_tokens + self.cache_write_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.output_tokens + self.reasoning_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


@dataclass
class NormalizedSession:
    """Normalized multi-turn agent transcript loaded from a file."""

    session_id: str
    source_path: str
    schema_flavor: str
    turns: List[NormalizedTurn] = field(default_factory=list)

    @property
    def primary_model(self) -> str:
        if not self.turns:
            return "unknown"
        counts: Dict[str, int] = {}
        for t in self.turns:
            counts[t.model] = counts.get(t.model, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0]


def _normalize_command_or_args(tool_name: str, args_obj: Any) -> Tuple[str, str]:
    """Return `(arguments_preview, normalized_signature)` for retry-loop grouping."""
    if isinstance(args_obj, str):
        try:
            parsed = json.loads(args_obj)
            args_obj = parsed
        except (ValueError, TypeError):
            collapsed = " ".join(args_obj.strip().split())
            preview = collapsed[:160]
            return preview, f"{tool_name}:{_stable_hash(collapsed)}"

    if isinstance(args_obj, dict):
        # Extract primary target key if present (command, cmd, path, file_path, url, query)
        for key in ("command", "cmd", "CommandLine", "file_path", "path", "Path", "url", "query"):
            if key in args_obj and isinstance(args_obj[key], str):
                val = " ".join(args_obj[key].strip().split())
                preview = f"{key}={val[:140]}"
                return preview, f"{tool_name}:{key}:{_stable_hash(val)}"
        canonical = json.dumps(args_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return canonical[:160], f"{tool_name}:{_stable_hash(canonical)}"

    fallback = str(args_obj)
    return fallback[:160], f"{tool_name}:{_stable_hash(fallback)}"


def _extract_system_text(system_val: Any) -> str:
    if not system_val:
        return ""
    if isinstance(system_val, str):
        return system_val
    if isinstance(system_val, list):
        parts: List[str] = []
        for item in system_val:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
                elif "content" in item and isinstance(item["content"], str):
                    parts.append(item["content"])
        return "\n".join(parts)
    if isinstance(system_val, dict):
        if "parts" in system_val and isinstance(system_val["parts"], list):
            return "\n".join(
                str(p.get("text", "")) for p in system_val["parts"] if isinstance(p, dict)
            )
        if "text" in system_val:
            return str(system_val["text"])
    return json.dumps(system_val, sort_keys=True, ensure_ascii=False)


def _extract_tool_definitions(tools_val: Any) -> List[ToolDefinitionRecord]:
    if not tools_val or not isinstance(tools_val, list):
        return []
    defs: List[ToolDefinitionRecord] = []
    for entry in tools_val:
        if not isinstance(entry, dict):
            continue
        # Gemini style: {"functionDeclarations": [...]}
        if "functionDeclarations" in entry and isinstance(entry["functionDeclarations"], list):
            for decl in entry["functionDeclarations"]:
                if isinstance(decl, dict):
                    name = str(decl.get("name", "unnamed_tool"))
                    desc = str(decl.get("description", ""))
                    server = name.split("__")[0] if "__" in name else None
                    est = int(decl.get("estimated_tokens") or estimate_tokens_from_obj(decl))
                    defs.append(
                        ToolDefinitionRecord(
                            name=name,
                            estimated_tokens=est,
                            server_name=server,
                            description_chars=len(desc),
                        )
                    )
            continue

        # OpenAI style: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        if "function" in entry and isinstance(entry["function"], dict):
            fn = entry["function"]
            name = str(fn.get("name", "unnamed_tool"))
            desc = str(fn.get("description", ""))
        else:
            name = str(entry.get("name", "unnamed_tool"))
            desc = str(entry.get("description", ""))

        server = entry.get("server") or (
            name.split("__")[0]
            if "__" in name
            else (name.split(".")[0] if name.startswith("mcp.") else None)
        )
        est = int(entry.get("estimated_tokens") or entry.get("tokens") or estimate_tokens_from_obj(entry))
        defs.append(
            ToolDefinitionRecord(
                name=name,
                estimated_tokens=est,
                server_name=str(server) if server else None,
                description_chars=len(desc),
            )
        )
    return defs


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif "content" in item:
                    chunks.append(_stringify_content(item["content"]))
                else:
                    chunks.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(chunks)
    if isinstance(content, dict):
        if isinstance(content.get("output"), str):
            return content["output"]
        if isinstance(content.get("text"), str):
            return content["text"]
        if "result" in content:
            return _stringify_content(content["result"])
    return json.dumps(content, ensure_ascii=False)


def _attach_result_to_call(
    call: ToolCallRecord,
    output_text: str,
    explicit_error: Optional[bool] = None,
    explicit_tokens: Optional[int] = None,
) -> None:
    chars = len(output_text)
    est_tok = explicit_tokens if explicit_tokens is not None else estimate_tokens_from_text(output_text)
    is_err = bool(explicit_error) if explicit_error is not None else looks_like_error_output(output_text)
    call.is_error = call.is_error or is_err
    call.output_chars += chars
    call.output_estimated_tokens += est_tok
    if not call.output_preview and output_text:
        call.output_preview = " ".join(output_text.strip().split())[:200]


def _parse_records_into_session(
    records: Sequence[Dict[str, Any]],
    session_id: str,
    source_path: str,
    session_level_tools: Optional[List[ToolDefinitionRecord]] = None,
    session_level_system: str = "",
    session_level_model: Optional[str] = None,
) -> NormalizedSession:
    turns: List[NormalizedTurn] = []
    call_lookup: Dict[str, ToolCallRecord] = {}
    pending_system: str = session_level_system
    sticky_tools: List[ToolDefinitionRecord] = list(session_level_tools or [])
    detected_flavor = "generic"

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue

        # Capture standalone system or tool configuration events if emitted before turns
        if "tools" in rec and isinstance(rec["tools"], list):
            extracted_tools = _extract_tool_definitions(rec["tools"])
            if extracted_tools:
                sticky_tools = extracted_tools

        if rec.get("role") == "system" and "usage" not in rec:
            pending_system = _stringify_content(rec.get("content", ""))
            continue

        # Check if this record is a non-billed user/tool_result event (e.g. Claude Code user line or OpenAI tool role)
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
        usage = (
            rec.get("usage")
            or (msg.get("usage") if msg else None)
            or rec.get("usageMetadata")
            or rec.get("token_usage")
        )

        has_direct_token_fields = any(
            k in rec
            for k in (
                "input_tokens",
                "prompt_tokens",
                "uncached_input_tokens",
                "promptTokenCount",
            )
        )

        if not usage and not has_direct_token_fields:
            # Inspect for tool_result blocks or role=="tool" payloads attaching to prior turn
            role = rec.get("role") or (msg.get("role") if msg else None) or rec.get("type")
            content_blocks = (msg.get("content") if msg else None) or rec.get("content")

            if role == "tool" and ("tool_call_id" in rec or "name" in rec):
                out_text = _stringify_content(content_blocks)
                cid = str(rec.get("tool_call_id", ""))
                target_call = call_lookup.get(cid)
                if target_call is None and turns and turns[-1].tool_calls:
                    target_call = turns[-1].tool_calls[-1]
                if target_call is not None:
                    _attach_result_to_call(
                        target_call,
                        out_text,
                        explicit_error=rec.get("is_error"),
                    )
                continue

            if isinstance(content_blocks, list):
                matched_result = False
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        matched_result = True
                        cid = str(block.get("tool_use_id") or block.get("id") or "")
                        out_text = _stringify_content(block.get("content"))
                        target_call = call_lookup.get(cid)
                        if target_call is None and turns and turns[-1].tool_calls:
                            target_call = turns[-1].tool_calls[-1]
                        if target_call is not None:
                            _attach_result_to_call(
                                target_call,
                                out_text,
                                explicit_error=block.get("is_error"),
                            )
                    elif isinstance(block, dict) and "functionResponse" in block:
                        matched_result = True
                        fresp = block["functionResponse"]
                        fname = str(fresp.get("name", ""))
                        out_text = _stringify_content(fresp.get("response"))
                        target_call = call_lookup.get(fname)
                        if target_call is None and turns and turns[-1].tool_calls:
                            target_call = turns[-1].tool_calls[-1]
                        if target_call is not None:
                            _attach_result_to_call(
                                target_call,
                                out_text,
                                explicit_error=fresp.get("is_error"),
                            )
                if matched_result:
                    continue

            # Check if user dumped a huge raw message directly
            if role == "user" and content_blocks and turns:
                raw_text = _stringify_content(content_blocks)
                turns[-1].raw_user_chars += len(raw_text)
            continue

        # We have a billed LLM generation turn! Determine flavor and token breakdown.
        model = str(
            rec.get("model")
            or (msg.get("model") if msg else None)
            or rec.get("modelVersion")
            or session_level_model
            or "generic-frontier-2026"
        )
        timestamp = rec.get("timestamp") or rec.get("created_at") or rec.get("time")
        if timestamp is not None:
            timestamp = str(timestamp)

        uncached_in = 0
        cached_in = 0
        cache_write = 0
        out_tok = 0
        reas_tok = 0
        turn_flavor = "generic"

        if isinstance(usage, dict):
            if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
                # Google Gemini usageMetadata
                turn_flavor = "gemini"
                prompt_total = int(usage.get("promptTokenCount", 0) or 0)
                cached_in = int(usage.get("cachedContentTokenCount", 0) or 0)
                uncached_in = max(0, prompt_total - cached_in)
                out_tok = int(usage.get("candidatesTokenCount", 0) or 0)
                reas_tok = int(usage.get("thoughtsTokenCount", 0) or 0)
            elif (
                "cache_read_input_tokens" in usage
                or "cache_creation_input_tokens" in usage
                or ("input_tokens" in usage and msg is not None)
                or model.lower().startswith("claude")
                and "input_tokens" in usage
            ):
                # Anthropic / Claude Code usage: `input_tokens` is uncached tokens after cache read/write
                turn_flavor = "anthropic"
                uncached_in = int(usage.get("input_tokens", 0) or 0)
                cached_in = int(usage.get("cache_read_input_tokens", 0) or 0)
                cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
                out_tok = int(usage.get("output_tokens", 0) or 0)
            elif "prompt_tokens" in usage or "completion_tokens" in usage:
                # OpenAI / Codex usage: `prompt_tokens` includes `cached_tokens`
                turn_flavor = "openai"
                prompt_total = int(usage.get("prompt_tokens", 0) or 0)
                p_details = usage.get("prompt_tokens_details") or {}
                cached_in = int(
                    p_details.get("cached_tokens", usage.get("cached_tokens", 0)) or 0
                )
                uncached_in = max(0, prompt_total - cached_in)
                comp_total = int(usage.get("completion_tokens", 0) or 0)
                c_details = usage.get("completion_tokens_details") or {}
                reas_tok = int(
                    c_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0)) or 0
                )
                out_tok = max(0, comp_total - reas_tok)
            else:
                turn_flavor = "generic"
                uncached_in = int(
                    usage.get("uncached_input_tokens", usage.get("input_tokens", 0)) or 0
                )
                cached_in = int(
                    usage.get("cached_input_tokens", usage.get("cached_tokens", 0)) or 0
                )
                cache_write = int(usage.get("cache_write_tokens", 0) or 0)
                out_tok = int(usage.get("output_tokens", 0) or 0)
                reas_tok = int(usage.get("reasoning_tokens", 0) or 0)
        else:
            # Flat generic turn record
            turn_flavor = str(rec.get("provider") or rec.get("schema") or "generic")
            if "uncached_input_tokens" in rec:
                uncached_in = int(rec.get("uncached_input_tokens", 0) or 0)
                cached_in = int(
                    rec.get("cached_input_tokens", rec.get("cached_tokens", 0)) or 0
                )
            elif "prompt_tokens" in rec:
                prompt_total = int(rec.get("prompt_tokens", 0) or 0)
                cached_in = int(
                    rec.get("cached_input_tokens", rec.get("cached_tokens", 0)) or 0
                )
                uncached_in = max(0, prompt_total - cached_in)
            else:
                uncached_in = int(rec.get("input_tokens", 0) or 0)
                cached_in = int(
                    rec.get("cached_input_tokens", rec.get("cached_tokens", 0)) or 0
                )
            cache_write = int(
                rec.get("cache_write_tokens", rec.get("cache_creation_input_tokens", 0)) or 0
            )
            out_tok = int(
                rec.get("output_tokens", rec.get("completion_tokens", 0)) or 0
            )
            reas_tok = int(rec.get("reasoning_tokens", 0) or 0)

        if detected_flavor == "generic" and turn_flavor != "generic":
            detected_flavor = turn_flavor

        # System prompt extraction (from explicit field, Gemini systemInstruction, or embedded messages list)
        sys_text = ""
        if "system" in rec:
            sys_text = _extract_system_text(rec["system"])
        elif msg and "system" in msg:
            sys_text = _extract_system_text(msg["system"])
        elif "system_prompt" in rec:
            sys_text = _extract_system_text(rec["system_prompt"])
        elif "systemInstruction" in rec:
            sys_text = _extract_system_text(rec["systemInstruction"])
        elif isinstance(rec.get("messages"), list):
            sys_msgs = [
                _stringify_content(m.get("content"))
                for m in rec["messages"]
                if isinstance(m, dict) and m.get("role") in ("system", "developer")
            ]
            if sys_msgs:
                sys_text = "\n".join(sys_msgs)

        if not sys_text and pending_system:
            sys_text = pending_system

        # Tool definitions
        turn_tools = sticky_tools
        if "tool_schemas" in rec and isinstance(rec["tool_schemas"], list):
            turn_tools = _extract_tool_definitions(rec["tool_schemas"])
            sticky_tools = turn_tools
        elif "available_tools" in rec and isinstance(rec["available_tools"], list):
            turn_tools = _extract_tool_definitions(rec["available_tools"])
            sticky_tools = turn_tools

        tool_schema_tok = int(
            rec.get("tool_schema_tokens")
            or sum(td.estimated_tokens for td in turn_tools)
        )

        # Extract tool calls made in this turn (and any inline results or embedded prior messages)
        turn_calls: List[ToolCallRecord] = []

        # 1. Anthropic content blocks (`tool_use`)
        content_list = (msg.get("content") if msg else None) or rec.get("content")
        if isinstance(content_list, list):
            for block in content_list:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    cid = str(block.get("id") or f"call_{len(turns)}_{len(turn_calls)}")
                    tname = str(block.get("name", "unknown_tool"))
                    preview, sig = _normalize_command_or_args(tname, block.get("input", {}))
                    tc = ToolCallRecord(
                        call_id=cid,
                        name=tname,
                        arguments_preview=preview,
                        normalized_signature=sig,
                    )
                    turn_calls.append(tc)
                    call_lookup[cid] = tc

        # 2. OpenAI / Generic `tool_calls` array (on record, `message`, or `choices[0].message`)
        raw_tcalls = rec.get("tool_calls") or (msg.get("tool_calls") if msg else None)
        if not raw_tcalls and isinstance(rec.get("choices"), list) and rec["choices"]:
            first_choice = rec["choices"][0]
            if isinstance(first_choice, dict) and isinstance(first_choice.get("message"), dict):
                raw_tcalls = first_choice["message"].get("tool_calls")

        if isinstance(raw_tcalls, list):
            for tc_raw in raw_tcalls:
                if not isinstance(tc_raw, dict):
                    continue
                cid = str(tc_raw.get("id") or tc_raw.get("call_id") or f"call_{len(turns)}_{len(turn_calls)}")
                if "function" in tc_raw and isinstance(tc_raw["function"], dict):
                    fn = tc_raw["function"]
                    tname = str(fn.get("name", "unknown_tool"))
                    raw_args = fn.get("arguments", {})
                else:
                    tname = str(tc_raw.get("name") or tc_raw.get("tool") or "unknown_tool")
                    raw_args = tc_raw.get("arguments", tc_raw.get("args", tc_raw.get("input", {})))

                preview, sig = _normalize_command_or_args(tname, raw_args)
                tc = ToolCallRecord(
                    call_id=cid,
                    name=tname,
                    arguments_preview=preview,
                    normalized_signature=sig,
                    is_error=bool(tc_raw.get("is_error") or tc_raw.get("error")),
                )
                if "output" in tc_raw or "result" in tc_raw or "response" in tc_raw:
                    out_payload = tc_raw.get("output", tc_raw.get("result", tc_raw.get("response")))
                    out_str = _stringify_content(out_payload)
                    explicit_tok = tc_raw.get("output_tokens_est") or tc_raw.get("result_tokens")
                    _attach_result_to_call(
                        tc,
                        out_str,
                        explicit_error=tc_raw.get("is_error")
                        if "is_error" in tc_raw
                        else (True if tc_raw.get("exit_code") not in (None, 0) else None),
                        explicit_tokens=int(explicit_tok) if explicit_tok is not None else None,
                    )
                turn_calls.append(tc)
                call_lookup[cid] = tc

        # 3. Gemini `candidates[0].content.parts[*].functionCall`
        candidates = rec.get("candidates")
        if isinstance(candidates, list) and candidates:
            cand0 = candidates[0]
            if isinstance(cand0, dict) and isinstance(cand0.get("content"), dict):
                parts = cand0["content"].get("parts") or []
                for p in parts:
                    if isinstance(p, dict) and isinstance(p.get("functionCall"), dict):
                        fcall = p["functionCall"]
                        tname = str(fcall.get("name", "unknown_tool"))
                        cid = str(fcall.get("id") or tname)
                        preview, sig = _normalize_command_or_args(tname, fcall.get("args", {}))
                        tc = ToolCallRecord(
                            call_id=cid,
                            name=tname,
                            arguments_preview=preview,
                            normalized_signature=sig,
                        )
                        turn_calls.append(tc)
                        call_lookup[cid] = tc

        # 4. Also check OpenAI request-style `messages` array if tool outputs from immediately preceding step are embedded
        if isinstance(rec.get("messages"), list):
            for m in rec["messages"]:
                if isinstance(m, dict) and m.get("role") == "tool":
                    cid = str(m.get("tool_call_id", ""))
                    if cid in call_lookup and call_lookup[cid].output_chars == 0:
                        _attach_result_to_call(
                            call_lookup[cid],
                            _stringify_content(m.get("content")),
                            explicit_error=m.get("is_error"),
                        )

        # 5. Inline `tool_results` list on generic turn record
        if isinstance(rec.get("tool_results"), list):
            for tr in rec["tool_results"]:
                if not isinstance(tr, dict):
                    continue
                cid = str(tr.get("call_id") or tr.get("tool_use_id") or "")
                target = call_lookup.get(cid)
                if target is None and turn_calls:
                    target = turn_calls[-1]
                if target is not None:
                    _attach_result_to_call(
                        target,
                        _stringify_content(tr.get("output", tr.get("content", ""))),
                        explicit_error=tr.get("is_error"),
                    )

        sys_tok = int(rec.get("system_prompt_tokens") or estimate_tokens_from_text(sys_text))
        turns.append(
            NormalizedTurn(
                turn_index=len(turns),
                model=model,
                schema_flavor=turn_flavor,
                timestamp=timestamp,
                uncached_input_tokens=uncached_in,
                cached_input_tokens=cached_in,
                cache_write_tokens=cache_write,
                output_tokens=out_tok,
                reasoning_tokens=reas_tok,
                system_prompt=sys_text,
                system_prompt_tokens=sys_tok,
                tool_definitions=list(turn_tools),
                tool_schema_tokens=tool_schema_tok,
                tool_calls=turn_calls,
            )
        )

    return NormalizedSession(
        session_id=session_id,
        source_path=source_path,
        schema_flavor=detected_flavor,
        turns=turns,
    )


def _is_mcp_config_object(data: Any) -> bool:
    return isinstance(data, dict) and (
        "mcpServers" in data
        or ("servers" in data and isinstance(data["servers"], dict) and "turns" not in data)
    )


def parse_transcript_file(file_path: Path) -> List[NormalizedSession]:
    """Parse a single `.jsonl` or `.json` file into one or more NormalizedSessions."""
    raw_text = file_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []

    if file_path.suffix.lower() == ".jsonl":
        records: List[Dict[str, Any]] = []
        for line_num, line in enumerate(raw_text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL in {file_path}:{line_num}: {exc}") from exc
            if isinstance(obj, dict):
                records.append(obj)
        sess = _parse_records_into_session(
            records,
            session_id=file_path.stem,
            source_path=str(file_path),
        )
        return [sess] if sess.turns else []

    # Standard `.json` file
    data = json.loads(raw_text)
    if _is_mcp_config_object(data):
        return []

    if isinstance(data, list):
        sess = _parse_records_into_session(
            data,
            session_id=file_path.stem,
            source_path=str(file_path),
        )
        return [sess] if sess.turns else []

    if isinstance(data, dict):
        # Multi-session bundle support: `{"sessions": [...]}`
        if isinstance(data.get("sessions"), list):
            sessions: List[NormalizedSession] = []
            for idx, s_obj in enumerate(data["sessions"]):
                if isinstance(s_obj, dict):
                    sid = str(s_obj.get("session_id") or f"{file_path.stem}-{idx+1}")
                    s_tools = _extract_tool_definitions(s_obj.get("tools", []))
                    s_sys = _extract_system_text(
                        s_obj.get("system") or s_obj.get("system_prompt") or s_obj.get("systemInstruction")
                    )
                    s_model = s_obj.get("model")
                    items = (
                        s_obj.get("turns")
                        or s_obj.get("events")
                        or s_obj.get("messages")
                        or s_obj.get("steps")
                        or []
                    )
                    sess = _parse_records_into_session(
                        items,
                        session_id=sid,
                        source_path=str(file_path),
                        session_level_tools=s_tools,
                        session_level_system=s_sys,
                        session_level_model=str(s_model) if s_model else None,
                    )
                    if sess.turns:
                        sessions.append(sess)
            return sessions

        s_tools = _extract_tool_definitions(data.get("tools", []))
        s_sys = _extract_system_text(
            data.get("system") or data.get("system_prompt") or data.get("systemInstruction")
        )
        s_model = data.get("model")
        sid = str(data.get("session_id") or data.get("id") or file_path.stem)
        for list_key in ("turns", "events", "steps", "records", "requests"):
            if isinstance(data.get(list_key), list):
                sess = _parse_records_into_session(
                    data[list_key],
                    session_id=sid,
                    source_path=str(file_path),
                    session_level_tools=s_tools,
                    session_level_system=s_sys,
                    session_level_model=str(s_model) if s_model else None,
                )
                return [sess] if sess.turns else []

        # Single response object (e.g., single OpenAI/Anthropic/Gemini API response JSON)
        sess = _parse_records_into_session(
            [data],
            session_id=sid,
            source_path=str(file_path),
            session_level_tools=s_tools,
            session_level_system=s_sys,
            session_level_model=str(s_model) if s_model else None,
        )
        return [sess] if sess.turns else []

    return []


def parse_transcript_path(target: Path) -> List[NormalizedSession]:
    """Parse either a single session log file or recursively scan a directory of logs."""
    if not target.exists():
        raise FileNotFoundError(f"Transcript path does not exist: {target}")

    if target.is_file():
        return parse_transcript_file(target)

    sessions: List[NormalizedSession] = []
    candidates = sorted(
        p
        for p in target.rglob("*")
        if p.is_file()
        and p.suffix.lower() in (".jsonl", ".json")
        and not any(
            part.startswith(".") or part in ("node_modules", "__pycache__", "dist", "build")
            for part in p.relative_to(target).parts
        )
    )
    for file_path in candidates:
        lower_name = file_path.name.lower()
        if lower_name in ("package.json", "package-lock.json", "tsconfig.json", "mcp_config.json", "mcp.json", "claude_desktop_config.json"):
            continue
        try:
            sessions.extend(parse_transcript_file(file_path))
        except (ValueError, json.JSONDecodeError):
            continue
    return sessions
