"""
Isolated lead-capture email validation (layers 1–5).

Kept separate from lead_capture.py and from Instagram/webhook code so validation
rules can ship without touching BAU automation paths. Import
`validate_lead_capture_email` from here for tests or other call sites if needed.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from typing import Optional, Tuple

from email_validator import EmailNotValidError, validate_email as validate_email_rfc


def _email_check_deliverability() -> bool:
    """DNS MX/A via email-validator (no third-party HTTP APIs). Disabled in tests via env."""
    return os.getenv("EMAIL_CHECK_DELIVERABILITY", "true").lower() in ("1", "true", "yes")


def _email_dns_timeout_sec() -> int:
    return max(3, min(30, int(os.getenv("EMAIL_DNS_TIMEOUT_SEC", "8"))))


_LAYER1_FORMAT_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Consonants for streak check (y must NOT be here — it acts as vowel in names like "rhythm").
_LAYER2_CONSONANT_STREAK = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}", re.IGNORECASE)
_LAYER2_VOWEL_RATIO_CHARS = frozenset("aeiouyAEIOUY")
_LAYER2_SAME_CHAR_RUN = re.compile(r"(.)\1{4,}", re.IGNORECASE)

_LAYER3_SUBSTRING_PATTERNS = (
    "qwerty",
    "asdfgh",
    "zxcvbn",
    "qazwsx",
    "abcdef",
    "123456",
    "aaaaaa",
    "xxxxxx",
)
_LAYER3_EXACT_LOCAL_PART = frozenset(
    {
        "test",
        "fake",
        "temp",
        "trash",
        "spam",
        "null",
        "none",
        "noreply",
        "nomail",
        "sample",
        "example",
        "demo",
        "user",
        "mail",
        "abc",
        "xyz",
    }
)

_LAYER4_DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "tempmail.com",
        "throwam.com",
        "yopmail.com",
        "sharklasers.com",
        "trashmail.com",
        "maildrop.cc",
        "dispostable.com",
        "fakeinbox.com",
        "spamgourmet.com",
        "10minutemail.com",
        "test.com",
        "example.com",
        "fake.com",
        "demo.com",
        "sample.com",
        "temp.com",
        "abc.com",
        "xyz.com",
    }
)


def _split_local_domain(raw: str) -> Optional[Tuple[str, str]]:
    s = raw.strip()
    if "@" not in s:
        return None
    local, _, domain = s.rpartition("@")
    if not local or not domain:
        return None
    return local, domain


def _layer1_format_ok(raw: str) -> bool:
    return bool(_LAYER1_FORMAT_RE.match(raw.strip()))


# Frequent English / name-like letter pairs (lowercase). Repeats of these are normal (e.g. deed, Tennessee).
_COMMON_LOCAL_BIGRAMS = frozenset(
    """
    th he in er an re at en es or te of ed is it al ar st to nt ng se ha as ou io le ve co me de hi ri ro ic ne ea ra ce li ch ma si om ur ca el ta la ns di fo ho pe ec pr no ct us ac wi tr ly yo un ow sh br wh ie na ni im em mi ai je jo ke ge ko ok be we po lo so go do mo vo fa ba ga pa ru lu tu su nu mu fu bu gu pu vu vi bi gi pi fi zi yi
    ab ad ag ap ay am av aw ax ep et ev ex ob od og op ot ov oz id ig ip ix ud ug up ut ib if iv ix um un ut
    oh ok oy oz za ze zo zu
    """.split()
)


def _local_part_repeated_rare_bigram(local: str) -> bool:
    """
    Catch mashing that adds vowels to pass the vowel-ratio check (e.g. Hsjehebeb with repeated 'eb').
    If the same uncommon 2-letter sequence appears twice+ and isn't a normal double (ll, ee), flag it.
    """
    if len(local) < 6 or len(local) > 24:
        return False
    if not re.match(r"^[a-zA-Z]+$", local):
        return False
    s = local.lower()
    pairs = [s[i : i + 2] for i in range(len(s) - 1)]
    for bg, count in Counter(pairs).items():
        if count < 2:
            continue
        if bg[0] == bg[1]:
            continue
        if bg in _COMMON_LOCAL_BIGRAMS:
            continue
        return True
    return False


def _layer2_gibberish_local(local: str) -> bool:
    if not local:
        return False
    low = local.lower()

    if _LAYER2_CONSONANT_STREAK.search(low):
        return True

    letters_only = [c for c in low if c.isalpha()]
    if len(letters_only) > 8:
        vowel_count = sum(1 for c in letters_only if c in _LAYER2_VOWEL_RATIO_CHARS)
        ratio = vowel_count / len(letters_only)
        if ratio < 0.25:
            return True

    if _LAYER2_SAME_CHAR_RUN.search(low):
        return True

    if _local_part_repeated_rare_bigram(local):
        return True

    return False


def _layer3_keyboard_pattern_local(local: str) -> bool:
    low = local.lower().strip()
    if low in _LAYER3_EXACT_LOCAL_PART:
        return True
    return any(p in low for p in _LAYER3_SUBSTRING_PATTERNS)


def _layer4_disposable_domain(domain: str) -> bool:
    return domain.strip().lower() in _LAYER4_DISPOSABLE_DOMAINS


_GIBBERISH_MSG = "That does not look like a real email. Please enter the address you actually use."
_DISPOSABLE_MSG = "Disposable or test email addresses are not allowed. Please use your real inbox."
_FORMAT_MSG = "Please enter a valid email address (check spelling and domain)."


def validate_lead_capture_email(email: str) -> Tuple[bool, str, Optional[str]]:
    """
    Layered validation (no third-party HTTP APIs):
      1) Basic format (single @, TLD dot, no spaces)
      2) Gibberish heuristics on local part (+ repeated rare bigram)
      3) Keyboard / throwaway local tokens
      4) Disposable domain blocklist
      5) RFC + optional DNS MX/A (email-validator)

    Returns: (is_valid, error_message, normalized_email_or_none)
    """
    if not email or not email.strip():
        return False, "Email cannot be empty.", None

    raw = email.strip()

    if not _layer1_format_ok(raw):
        return False, _FORMAT_MSG, None

    parsed = _split_local_domain(raw)
    if not parsed:
        return False, _FORMAT_MSG, None
    local_raw, domain_raw = parsed

    if len(local_raw) < 4:
        return (
            False,
            "Email address is too short before @. Please enter your full address.",
            None,
        )
    if not re.search(r"[a-zA-Z]", local_raw):
        return False, "Email address must contain at least one letter.", None

    if _layer2_gibberish_local(local_raw):
        return False, _GIBBERISH_MSG, None

    if _layer3_keyboard_pattern_local(local_raw):
        return False, _GIBBERISH_MSG, None

    if _layer4_disposable_domain(domain_raw):
        return False, _DISPOSABLE_MSG, None

    check_mx = _email_check_deliverability()
    timeout = _email_dns_timeout_sec() if check_mx else None
    try:
        info = validate_email_rfc(
            raw,
            check_deliverability=check_mx,
            timeout=timeout,
        )
        normalized_lower = info.normalized.lower()
    except EmailNotValidError:
        return False, _FORMAT_MSG, None

    return True, "", normalized_lower
