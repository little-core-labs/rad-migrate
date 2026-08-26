"""Tests for the MCP/SSE response parser.

The headline case is ``test_data_payload_with_raw_newlines``: a payload containing
real newlines inside the JSON. Parsing each ``data:`` line independently truncates it
at the first newline and fails with "unterminated string" — the exact production bug
this parser exists to survive.
"""

from __future__ import annotations

import json

import pytest

from rad_migrate.dest.sse import (
    SSEParseError,
    ToolCallError,
    extract_tool_text,
    parse_response,
)


def _envelope(text: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}


class TestParseResponse:
    def test_plain_json_body(self) -> None:
        body = json.dumps(_envelope("hello"))
        assert parse_response(body) == _envelope("hello")

    def test_simple_sse(self) -> None:
        body = f"event: message\ndata: {json.dumps(_envelope('hi'))}\n\n"
        assert parse_response(body) == _envelope("hi")

    def test_pretty_printed_payload_without_data_prefixes(self) -> None:
        """The production regression: a pretty-printed payload whose continuation
        lines carry no ``data:`` prefix.

        Only the first physical line is prefixed, so a parser that reads each
        ``data:`` line independently gets ``{`` and dies on "unterminated string".
        Rejoining the whole block first recovers it.
        """
        envelope = _envelope(
            "Content ID: did:rad.live:content/feature/abc-123\n"
            "Upload here: https://api.rad.live/ingestion/xyz"
        )
        pretty = json.dumps(envelope, indent=2)
        body = f"event: message\ndata: {pretty}\n\n"

        naive = [ln[5:].lstrip() for ln in body.split("\n") if ln.startswith("data:")]
        assert len(naive) == 1
        with pytest.raises(json.JSONDecodeError):
            json.loads(naive[0])  # what the original parser did

        text = extract_tool_text(parse_response(body))
        assert "did:rad.live:content/feature/abc-123" in text
        assert "https://api.rad.live/ingestion/xyz" in text

    def test_multiline_data_fields_are_joined(self) -> None:
        """Spec-conformant framing: every physical line carries its own prefix."""
        pretty = json.dumps(_envelope("split"), indent=2)
        body = "".join(f"data: {line}\n" for line in pretty.split("\n")) + "\n"
        assert extract_tool_text(parse_response(body)) == "split"

    def test_payload_chunked_mid_token(self) -> None:
        """Defensive: a producer that splits the document at an arbitrary byte.

        Not spec-conformant — joining with a newline would corrupt the token — so
        the parser also tries concatenating with no separator.
        """
        payload = json.dumps(_envelope("chunked"))
        half = len(payload) // 2
        body = f"data: {payload[:half]}\ndata: {payload[half:]}\n\n"
        assert extract_tool_text(parse_response(body)) == "chunked"

    def test_last_result_event_wins(self) -> None:
        """Progress notifications precede the result; the result is what we want."""
        notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
        result = json.dumps(_envelope("final"))
        body = f"data: {notification}\n\ndata: {result}\n\n"
        assert extract_tool_text(parse_response(body)) == "final"

    def test_ignores_sse_comments_and_fields(self) -> None:
        body = (
            ": keep-alive\nevent: message\nid: 42\nretry: 3000\n"
            f"data: {json.dumps(_envelope('ok'))}\n\n"
        )
        assert extract_tool_text(parse_response(body)) == "ok"

    def test_trailing_junk_after_document(self) -> None:
        body = f"data: {json.dumps(_envelope('ok'))}\n\ndata: [DONE]\n\n"
        assert extract_tool_text(parse_response(body)) == "ok"

    @pytest.mark.parametrize("body", ["", "   \n\n", "not json at all"])
    def test_unparseable_raises(self, body: str) -> None:
        with pytest.raises(SSEParseError):
            parse_response(body)


class TestExtractToolText:
    def test_joins_multiple_text_blocks(self) -> None:
        payload = {
            "result": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        }
        assert extract_tool_text(payload) == "a\nb"

    def test_ignores_non_text_blocks(self) -> None:
        payload = {
            "result": {
                "content": [
                    {"type": "image", "data": "..."},
                    {"type": "text", "text": "only me"},
                ]
            }
        }
        assert extract_tool_text(payload) == "only me"

    def test_falls_back_to_structured_content(self) -> None:
        payload = {"result": {"content": [], "structuredContent": {"contentId": "did:x"}}}
        assert json.loads(extract_tool_text(payload)) == {"contentId": "did:x"}

    def test_jsonrpc_error_raises(self) -> None:
        payload = {"error": {"code": -32000, "message": "Key not found in database"}}
        with pytest.raises(ToolCallError) as excinfo:
            extract_tool_text(payload)
        assert "Key not found" in str(excinfo.value)
        assert excinfo.value.code == -32000

    def test_tool_level_error_raises(self) -> None:
        payload = {"result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}}
        with pytest.raises(ToolCallError, match="boom"):
            extract_tool_text(payload)

    def test_missing_result_raises(self) -> None:
        with pytest.raises(SSEParseError):
            extract_tool_text({"jsonrpc": "2.0", "id": 1})
