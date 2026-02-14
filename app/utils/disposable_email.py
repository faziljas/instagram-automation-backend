"""
Disposable/temporary email domain blocklist.
Used to reject sign-ups and sync from known temp-email providers.
"""
import os

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
    _BLOCKLIST = domains
    return _BLOCKLIST


def is_disposable_email(email: str) -> bool:
    """
    Return True if the email's domain is in the disposable/temp blocklist.
    """
    if not email or "@" not in email:
        return False
    domain = email.strip().lower().split("@")[-1]
    return domain in _load_blocklist()
