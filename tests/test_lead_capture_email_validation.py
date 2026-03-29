"""Tests for lead_capture.validate_email (no third-party HTTP APIs)."""

import pytest


@pytest.fixture
def no_dns(monkeypatch):
    monkeypatch.setenv("EMAIL_CHECK_DELIVERABILITY", "false")


def test_rejects_keyboard_mash_local(no_dns):
    from app.services.lead_capture import validate_email

    ok, msg, norm = validate_email("Hjdhjej@gmail.com")
    assert ok is False
    assert norm is None
    assert "real email" in msg.lower()


def test_rejects_vowel_padded_mash_repeated_rare_bigram(no_dns):
    """e.g. Hsjehebeb — enough vowels to pass ratio check but repeated uncommon pairs like 'eb'."""
    from app.services.lead_capture import validate_email

    ok, _, norm = validate_email("Hsjehebeb@gmail.com")
    assert ok is False
    assert norm is None


def test_rejects_hd_consonant_mash_with_repeated_dj(no_dns):
    from app.services.lead_capture import validate_email

    ok, _, norm = validate_email("Hdjjdjr@gmail.com")
    assert ok is False
    assert norm is None


def test_rejects_obvious_fake_local(no_dns):
    from app.services.lead_capture import validate_email

    ok, _, norm = validate_email("test@example.com")
    assert ok is False
    assert norm is None


def test_accepts_plausible_address_syntax_only(no_dns):
    from app.services.lead_capture import validate_email

    ok, msg, norm = validate_email("Jane.Doe+work@gmail.com")
    assert ok is True, msg
    assert norm == "jane.doe+work@gmail.com"


def test_accepts_rhythm_local_part_not_flagged_as_mash(no_dns):
    from app.services.lead_capture import validate_email

    ok, msg, norm = validate_email("rhythm@gmail.com")
    assert ok is True, msg
    assert norm == "rhythm@gmail.com"


def test_accepts_deeded_repeated_common_bigrams(no_dns):
    from app.services.lead_capture import validate_email

    ok, msg, norm = validate_email("deeded@gmail.com")
    assert ok is True, msg
    assert norm == "deeded@gmail.com"


def test_returns_normalized_on_success(no_dns):
    from app.services.lead_capture import validate_email

    ok, _, norm = validate_email("  Jane.Smith@DOMAIN.COM ")
    assert ok is True
    assert norm == "jane.smith@domain.com"
