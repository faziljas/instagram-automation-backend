"""
Instagram platform limits for automation rule config validation.
Used to enforce DM length, public comment length, trigger keyword count/length, and button text.
"""

# Maximum characters per Instagram direct message (safe for mobile & API).
INSTAGRAM_DM_MAX_CHARS = 1000

# Maximum characters per Instagram public comment.
INSTAGRAM_PUBLIC_COMMENT_MAX_CHARS = 2200

# Maximum number of trigger keywords per automation.
INSTAGRAM_TRIGGER_KEYWORDS_MAX_COUNT = 50

# Maximum characters per single trigger keyword.
INSTAGRAM_TRIGGER_KEYWORD_MAX_LENGTH = 100

# Maximum characters for quick reply / CTA button text.
INSTAGRAM_BUTTON_TEXT_MAX_CHARS = 20

# Config keys that hold DM-length text (each value must be <= INSTAGRAM_DM_MAX_CHARS).
DM_TEXT_KEYS = (
    "message_template",
    "ask_to_follow_message",
    "follow_recheck_message",
    "follow_no_exit_message",
    "ask_for_email_message",
    "email_success_message",
    "email_retry_message",
    "simple_flow_message",
    "simple_flow_email_question",
    "simple_flow_phone_message",
    "simple_flow_phone_question",
    "phone_invalid_retry_message",
)


def _check_str(value: str, max_len: int, field_name: str, errors: list[str]) -> None:
    if not value:
        return
    n = len(value)
    if n > max_len:
        errors.append(f"{field_name}: {n} characters (max {max_len})")


def validate_automation_config(config: dict) -> list[str]:
    """
    Validate automation rule config against Instagram limits.
    Returns a list of human-readable error messages; empty list means valid.
    """
    if not config or not isinstance(config, dict):
        return []

    errors: list[str] = []

    # Keywords: list length and per-keyword length
    keywords = config.get("keywords") or []
    if isinstance(keywords, list):
        if len(keywords) > INSTAGRAM_TRIGGER_KEYWORDS_MAX_COUNT:
            errors.append(
                f"Trigger keywords: {len(keywords)} keywords (max {INSTAGRAM_TRIGGER_KEYWORDS_MAX_COUNT})"
            )
        for i, kw in enumerate(keywords):
            if isinstance(kw, str) and len(kw) > INSTAGRAM_TRIGGER_KEYWORD_MAX_LENGTH:
                errors.append(
                    f"Trigger keyword #{i + 1}: {len(kw)} characters (max {INSTAGRAM_TRIGGER_KEYWORD_MAX_LENGTH})"
                )
    # Legacy single keyword
    single = config.get("keyword")
    if isinstance(single, str) and len(single) > INSTAGRAM_TRIGGER_KEYWORD_MAX_LENGTH:
        errors.append(
            f"Trigger keyword: {len(single)} characters (max {INSTAGRAM_TRIGGER_KEYWORD_MAX_LENGTH})"
        )

    # Comment replies (public comment limit)
    replies = config.get("comment_replies") or []
    if isinstance(replies, list):
        for i, r in enumerate(replies):
            if isinstance(r, str):
                _check_str(
                    r,
                    INSTAGRAM_PUBLIC_COMMENT_MAX_CHARS,
                    f"Public acknowledgement reply #{i + 1}",
                    errors,
                )

    # DM message variations
    for key in ("message_variations", "dm_messages"):
        messages = config.get(key) or []
        if isinstance(messages, list):
            for i, m in enumerate(messages):
                if isinstance(m, str):
                    _check_str(
                        m,
                        INSTAGRAM_DM_MAX_CHARS,
                        f"DM message variation #{i + 1}",
                        errors,
                    )

    # Single DM-text fields
    for key in DM_TEXT_KEYS:
        val = config.get(key)
        if isinstance(val, str):
            _check_str(
                val,
                INSTAGRAM_DM_MAX_CHARS,
                key.replace("_", " ").title(),
                errors,
            )
    if isinstance(config.get("message_template"), str):
        _check_str(
            config["message_template"],
            INSTAGRAM_DM_MAX_CHARS,
            "Primary DM message",
            errors,
        )

    # Buttons: text only (URL not length-limited by Instagram for this use case)
    buttons = config.get("buttons") or []
    if isinstance(buttons, list):
        for i, btn in enumerate(buttons):
            if isinstance(btn, dict):
                text = btn.get("text")
                if isinstance(text, str) and len(text) > INSTAGRAM_BUTTON_TEXT_MAX_CHARS:
                    errors.append(
                        f"Button #{i + 1} text: {len(text)} characters (max {INSTAGRAM_BUTTON_TEXT_MAX_CHARS})"
                    )

    return errors
