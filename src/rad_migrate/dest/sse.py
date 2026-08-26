"""Parsing MCP responses delivered as Server-Sent Events.

This is small, fiddly, and load-bearing, so it lives on its own with its own tests.

The subtlety: a strictly-conformant SSE producer escapes newlines inside a ``data:``
payload, or splits the payload across several ``data:`` lines. Rad's API has been
observed emitting **raw newlines inside the JSON**, which means continuation lines
carry no ``data:`` prefix at all. Parsing each ``data:`` line independently then fails
with "unterminated string" — the JSON was truncated at the first embedded newline.

So we try three readings, cheapest and most correct first:

1. a plain JSON body (no SSE framing at all);
2. strict SSE framing — events split on blank lines, ``data:`` lines joined;
3. permissive — everything from the first ``data:`` onward, prefixes stripped,
   rejoined and parsed as one document.

Reading 3 is what rescues the raw-newline case. Keeping 2 ahead of it means a
well-formed multi-event stream is still interpreted correctly.
"""

from __future__ import annotations

import json
from typing import Any


class SSEParseError(ValueError):
    """The response body could not be read as JSON by any strategy."""


def parse_response(text: str) -> Any:
    """Extract the JSON payload from an MCP HTTP response body."""
    if not text or not text.strip():
        raise SSEParseError("empty response body")

    for strategy in (_as_plain_json, _as_strict_sse, _as_permissive_sse):
        result = strategy(text)
        if result is not _MISSING:
            return result

    preview = text.strip()[:300]
    raise SSEParseError(f"could not parse response as JSON or SSE. First 300 chars:\n{preview}")


class _Missing:
    pass


_MISSING = _Missing()


def _as_plain_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return _MISSING
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return _MISSING


def _as_strict_sse(text: str) -> Any:
    """Standard framing: blank-line-separated events, ``data:`` lines joined."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        if raw_line.startswith("data:"):
            current.append(raw_line[5:].lstrip())
    if current:
        blocks.append(current)

    # Newline is the spec-defined join. Empty string is a fallback for producers that
    # chunk the document mid-token, where a newline would corrupt it.
    candidates = [sep.join(block) for block in blocks for sep in ("\n", "")]

    # Later events supersede earlier ones (progress notifications precede the result),
    # so prefer the last payload that both parses and looks like a JSON-RPC message.
    for payload in reversed(candidates):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            return parsed
    for payload in reversed(candidates):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    return _MISSING


def _as_permissive_sse(text: str) -> Any:
    """Rescue payloads containing raw newlines, where continuations lack a prefix.

    Everything from the first ``data:`` to the end is treated as one document, with
    any further ``data:`` prefixes stripped. This is what the original hardened
    parser did, kept as a last resort because it cannot distinguish separate events.
    """
    parts: list[str] = []
    started = False
    for raw_line in text.splitlines():
        if raw_line.startswith("data:"):
            started = True
            parts.append(raw_line[5:].lstrip())
        elif started:
            # A continuation of the payload, or an SSE field we ignore.
            if raw_line.startswith(("event:", "id:", "retry:", ":")):
                continue
            parts.append(raw_line)
    if not parts:
        return _MISSING

    joined = "\n".join(parts).strip()
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        pass

    # Trailing blank lines or a partial follow-on event can leave junk after the
    # first complete document; take just that document.
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(joined)
    except json.JSONDecodeError:
        return _MISSING
    return value


def extract_tool_text(payload: Any) -> str:
    """Pull the text content out of an MCP ``tools/call`` result.

    Raises on a JSON-RPC error so callers do not have to check twice.
    """
    if not isinstance(payload, dict):
        raise SSEParseError(f"expected a JSON object, got {type(payload).__name__}")

    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error)
            raise ToolCallError(str(message), code=error.get("code"))
        raise ToolCallError(str(error))

    result = payload.get("result")
    if result is None:
        raise SSEParseError("response contained neither 'result' nor 'error'")

    if isinstance(result, dict):
        if result.get("isError"):
            raise ToolCallError(_join_content(result.get("content")) or "tool reported an error")
        content = result.get("content")
        text = _join_content(content)
        if text:
            return text
        # Some servers answer with a structured payload and no text block.
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"])
        return json.dumps(result)
    return json.dumps(result)


def _join_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    chunks = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and "text" in block
    ]
    return "\n".join(chunks)


class ToolCallError(RuntimeError):
    """The server executed the tool and reported a failure."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
