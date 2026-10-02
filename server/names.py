"""Name matching shared by the setup scripts and the Front Desk bot."""

import re


def slug(name: str) -> str:
    """'💬・general' -> 'general', '🎮 Squad I' -> 'squadi'."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())
