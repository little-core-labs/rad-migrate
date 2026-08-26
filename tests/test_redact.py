"""Tests for secret redaction.

A leaked token in a ``--json`` payload or an error message is the realistic way this
tool would burn someone, so these are treated as security tests, not formatting tests.
"""

from __future__ import annotations

import pytest

from radmigrate.redact import (
    MASK,
    clear_registered_secrets,
    redact,
    redact_obj,
    register_secret,
)

# Fake credentials are assembled at runtime rather than written as literals.
# A convincing-looking token in a source file trips GitHub push protection and every
# secret scanner in CI — a permanent false positive in exchange for no extra safety,
# since these values are worthless either way.
SECRET = "rad" + "_live_" + "sk_9f8e7d6c5b4a3210deadbeef"


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear_registered_secrets()


class TestRegisteredSecrets:
    def test_registered_value_is_masked_everywhere(self) -> None:
        register_secret(SECRET)
        text = f"POST failed with Authorization: Bearer {SECRET} to /mcp"
        result = redact(text)
        assert SECRET not in result
        assert MASK in result

    def test_masked_even_without_surrounding_context(self) -> None:
        """A bare token has no pattern to match on; registration is what catches it."""
        register_secret(SECRET)
        assert SECRET not in redact(f"...{SECRET}...")

    def test_short_values_are_not_registered(self) -> None:
        """Masking every occurrence of a short string would shred ordinary output."""
        register_secret("abc")
        assert redact("abc def") == "abc def"

    def test_longest_match_wins(self) -> None:
        register_secret(SECRET)
        register_secret(SECRET[:12])
        assert SECRET not in redact(f"key={SECRET}")


#: Token shapes real scanners recognise, built at runtime so the literals never
#: appear in this file. See the note beside SECRET.
FAKE_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".abc.def"
FAKE_GITHUB = "gh" + "p_" + "abcdefghijklmnopqrst"
FAKE_OPENAI = "sk" + "-" + "abcdefghijklmnop"


class TestPatterns:
    def test_bearer_token_without_registration(self) -> None:
        """The safety net: a token we were never told about, in upstream error text."""
        result = redact(f"upstream said: Authorization: Bearer {FAKE_JWT}")
        assert FAKE_JWT not in result
        assert MASK in result

    @pytest.mark.parametrize(
        "text",
        [
            f'api_key="{FAKE_OPENAI}"',
            f"access_token: {FAKE_GITHUB}",
            '{"token": "abcdefghijklmnop"}',
            "password=hunter2hunter2hunter2",
            "secret = s3cr3tv4lu3zzzz",
        ],
    )
    def test_credential_assignments(self, text: str) -> None:
        assert MASK in redact(text)

    def test_netscape_cookie_line(self) -> None:
        line = ".youtube.com\tTRUE\t/\tTRUE\t1799999999\t__Secure-3PSID\tg.a000xyzSECRETVALUE"
        result = redact(line)
        assert "g.a000xyzSECRETVALUE" not in result
        assert "__Secure-3PSID" in result  # the name is fine; the value is not

    def test_session_cookie_assignment(self) -> None:
        assert "AbCdEfGh12345678" not in redact("Cookie: SAPISID=AbCdEfGh12345678; other=1")

    def test_signed_url_query_params(self) -> None:
        url = "https://api.rad.live/ingestion/x?X-Amz-Signature=deadbeefcafebabe1234"
        assert "deadbeefcafebabe1234" not in redact(url)

    def test_ordinary_text_untouched(self) -> None:
        text = "Downloaded 47 videos from @channel at 1080p in 12m 30s"
        assert redact(text) == text


class TestRedactObj:
    def test_secret_keys_are_masked_by_name(self) -> None:
        payload = {"api_key": "anything-at-all", "title": "My Video"}
        result = redact_obj(payload)
        assert result == {"api_key": MASK, "title": "My Video"}

    def test_recurses_into_nested_structures(self) -> None:
        register_secret(SECRET)
        payload = {"items": [{"error": f"failed with {SECRET}"}], "count": 1}
        result = redact_obj(payload)
        assert SECRET not in str(result)
        assert result["count"] == 1

    def test_none_secret_stays_none(self) -> None:
        """Distinguishes 'not set' from 'set but hidden'."""
        assert redact_obj({"token": None}) == {"token": None}

    def test_non_string_scalars_pass_through(self) -> None:
        payload = {"width": 1920, "landscape": True, "ratio": 1.78}
        assert redact_obj(payload) == payload

    def test_keys_are_not_rewritten(self) -> None:
        result = redact_obj({"bearer_note": "fine"})
        assert "bearer_note" in result


def test_full_output_path_is_redacted() -> None:
    """End-to-end: a registered key must not survive into --json output."""
    import io
    import json

    from radmigrate.reporting import Mode, Reporter

    register_secret(SECRET)
    out, err = io.StringIO(), io.StringIO()
    reporter = Reporter(Mode.JSON, stdout=out, stderr=err)
    reporter.error(f"publish failed: Bearer {SECRET}")
    reporter.result({"ok": False, "api_key": SECRET, "detail": f"used {SECRET}"})

    combined = out.getvalue() + err.getvalue()
    assert SECRET not in combined
    assert json.loads(out.getvalue())["api_key"] == MASK
