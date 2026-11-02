"""New redaction pre-screens must stay false-negative-free.

The gates skip expensive ``re.sub`` passes on typical tool output. A miss
leaks a credential, so these lock the same match set the ungated regexes have.
"""

from __future__ import annotations

from agent.redact import (
    SECRET_HEADER_NAME_LIST,
    _PREFIX_SUBSTRINGS,
    _TELEGRAM_HINT_RE,
    _ZHIPU_HINT_RE,
    _has_known_prefix_substring,
    redact_sensitive_text,
)


def test_prefix_prescreen_matches_any_literal_substring():
    haystack = "plain text with no credentials at all"
    assert _has_known_prefix_substring(haystack) is any(p in haystack for p in _PREFIX_SUBSTRINGS)
    for prefix in _PREFIX_SUBSTRINGS:
        if not prefix:
            continue
        assert _has_known_prefix_substring(f"head {prefix} tail")


def test_telegram_hint_catches_bot_and_bare_ids():
    token = "12345678:" + ("A" * 30)
    assert _TELEGRAM_HINT_RE.search(token)
    assert _TELEGRAM_HINT_RE.search("bot" + token)
    redacted = redact_sensitive_text("Authorization unused " + token, force=True)
    assert "A" * 30 not in redacted


def test_zhipu_hint_requires_the_32_hex_dot_prefix():
    secret = "a" * 32 + "." + ("B" * 16)
    assert _ZHIPU_HINT_RE.search(secret)
    redacted = redact_sensitive_text("key " + secret, force=True)
    assert secret not in redacted
    assert _ZHIPU_HINT_RE.search("short.ab") is None


def test_secret_header_names_still_mask():
    for name in SECRET_HEADER_NAME_LIST:
        text = f"{name}: super-secret-value-12345"
        redacted = redact_sensitive_text(text, force=True)
        assert "super-secret-value-12345" not in redacted
