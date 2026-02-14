"""
Disposable/temporary email domain blocklist.
Used to reject sign-ups and sync from known temp-email providers.
"""
import logging
import os

logger = logging.getLogger(__name__)

# Load once at module import
_BLOCKLIST: set[str] = set()
_BLOCKLIST_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "disposable_email_blocklist.txt"
)


def _load_blocklist() -> set[str]:
    global _BLOCKLIST
    if _BLOCKLIST:
        return _BLOCKLIST
    domains = set()
    if os.path.isfile(_BLOCKLIST_PATH):
        with open(_BLOCKLIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    domains.add(line)
        logger.info(
            "Disposable email blocklist loaded: %s domains from %s",
            len(domains),
            _BLOCKLIST_PATH,
        )
    else:
        logger.warning(
            "Disposable email blocklist file not found at %s; no domains will be blocked.",
            _BLOCKLIST_PATH,
        )
    _BLOCKLIST = domains
    return _BLOCKLIST


def ensure_blocklist_loaded() -> int:
    """Load blocklist at startup so we fail fast if file is missing. Returns count."""
    return len(_load_blocklist())


def is_disposable_email(email: str) -> bool:
    """
    Return True if the email's domain is in the disposable/temp blocklist.
    """
    if not email or "@" not in email:
        return False
    domain = email.strip().lower().split("@")[-1]
    return domain in _load_blocklist()
