"""Fly.io tokens (``FlyV1 fm2_…``, bare ``fm2_``/``fo1_``) are masked. Fake tokens only."""

from agent.redact import redact_sensitive_text, redact_terminal_output

# Fake bodies shaped like Fly macaroons: standard base64 with ``+``, ``/`` and ``=`` padding.
_FM2_A = "fm2_" + "lJPECAAAAAAAAFAKExBAKEFAKE+FAKE/FAKEfakeFAKEfake0123456789=="
_FM2_B = "fm2_" + "lJPETmFAKEFAKEFAKE/0987654321+zyxwvuFAKEfake="
_FO1 = "fo1_" + "FAKEfakeFAKEfake0123456789_-FAKEfake"
_SECRET_BITS = ("FAKExBAKE", "FAKEfakeFAKEfake0123", "TmFAKEFAKE", "0987654321+zyx")


def _assert_masked(out: str) -> None:
    for bit in _SECRET_BITS:
        assert bit not in out, out


def test_flyv1_header_with_comma_joined_macaroons_is_masked():
    out = redact_sensitive_text(f"export creds: FlyV1 {_FM2_A},{_FM2_B} done", force=True)
    _assert_masked(out)
    assert out.startswith("export creds: FlyV1 ") and out.endswith(" done"), out


def test_bare_fly_tokens_are_masked():
    out = redact_sensitive_text(f"token {_FM2_A}\nother {_FO1}\n", force=True)
    _assert_masked(out)


def test_fly_token_in_terminal_output_is_masked():
    out = redact_terminal_output(f"$ fly tokens create org\nFlyV1 {_FM2_A}\n", "fly tokens create org")
    _assert_masked(out)


def test_file_read_masks_fly_token_non_reusably():
    out = redact_sensitive_text(f"FLY: {_FM2_B}", file_read=True)
    _assert_masked(out)
    assert "«redacted" in out, out


def test_short_or_embedded_lookalikes_are_untouched():
    benign = "fm2_short fo1_x myfm2_" + "A" * 30 + " FlyV1 docs"
    assert redact_sensitive_text(benign, force=True) == benign
