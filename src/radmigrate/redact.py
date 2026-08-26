"""Secret redaction.

Everything this tool prints — console output, ``--json`` payloads, NDJSON progress
events, and exception text — passes through :func:`redact` first.

Two layers, deliberately belt-and-braces:

1. **Registered secrets.** Values we *know* are secret (the API key, cookie file
   contents) are registered at startup and matched literally. This is the reliable
   layer.
2. **Patterns.** A safety net for secrets we were never told about — a Bearer token
   in an upstream error message, a signed URL in a stack trace. Catches the case
   where a dependency leaks something into text we then re-print.

The realistic way a tool like this burns someone is a token echoed inside an error
string, so the pattern layer runs even when nothing is registered.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[redacted]"

# Values registered at runtime. Longest-first so that a key that contains another
# registered value as a substring still masks completely.
_registered: set[str] = set()

# Don't register short values — masking every occurrence of a 6-character string
# would shred ordinary output for no security benefit.
_MIN_SECRET_LEN = 8

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>  /  "Bearer <token>"
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), f"Bearer {MASK}"),
    # Authorization header in a dict repr or header dump
    (
        re.compile(r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*['\"]?)[^'\",}\s]{8,}"),
        rf"\1{MASK}",
    ),
    # api_key=... / api-key: ... / apiKey": "..." / token=... / secret=...
    (
        re.compile(
            r"(?i)(['\"]?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
            r"['\"]?\s*[:=]\s*['\"]?)[^'\",}&\s]{8,}"
        ),
        rf"\1{MASK}",
    ),
    # Netscape cookie file lines: TAB-separated, secret is the final field.
    (
        re.compile(r"(?m)^(\.?[\w.-]+\t(?:TRUE|FALSE)\t[^\t]*\t(?:TRUE|FALSE)\t\d+\t[^\t]+\t).+$"),
        rf"\1{MASK}",
    ),
    # Known-sensitive YouTube/Google session cookie names wherever they appear.
    (
        re.compile(
            r"(?i)\b((?:__Secure-)?(?:1P|3P)?[_-]?(?:SID|SIDCC|SAPISID|APISID|HSID|SSID|"
            r"LOGIN_INFO|PSID|PAPISID)[A-Z0-9_-]*\s*[=:]\s*)[^\s;,'\"]{8,}"
        ),
        rf"\1{MASK}",
    ),
    # Signed-URL credentials in query strings.
    (
        re.compile(r"(?i)([?&](?:x-amz-signature|signature|sig|key|token)=)[^&\s'\"]{8,}"),
        rf"\1{MASK}",
    ),
)


def register_secret(value: str | None) -> None:
    """Mark ``value`` as secret so it is masked everywhere it appears in output."""
    if not value:
        return
    value = value.strip()
    if len(value) >= _MIN_SECRET_LEN:
        _registered.add(value)


def register_secrets(*values: str | None) -> None:
    for value in values:
        register_secret(value)


def clear_registered_secrets() -> None:
    """Test hook. Not used in normal operation."""
    _registered.clear()


def redact(text: str) -> str:
    """Mask registered secrets and secret-shaped patterns in ``text``."""
    if not text:
        return text
    # Registered values first, longest-first, so overlapping registrations mask fully.
    for secret in sorted(_registered, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists/tuples for JSON output.

    Dict *keys* are left alone (they are field names, not values) but any key that
    names a credential has its value masked outright rather than pattern-matched,
    because a bare token as a dict value has no surrounding context to match on.
    """
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_secret_key(key):
                out[key] = MASK if value is not None else None
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v) for v in obj)
    return obj


_SECRET_KEY = re.compile(
    r"(?i)^(api[_-]?key|key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"authorization|cookie|cookies|set-cookie)$"
)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.match(key.strip()))
