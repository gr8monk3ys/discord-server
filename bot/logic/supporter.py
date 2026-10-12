"""Supporter tier: a cosmetic thank-you for people who chip in. Pure: no Discord, no database.

There is no payment code here. Ko-fi's Discord integration (or Discord Server Subscriptions)
gives members the Supporter role; the bot only reacts to the role. Perks are cosmetic or
convenience only: a flair on /profile, the private lounge channel (Discord permissions on the
role) and a small monthly coin stipend that is no season-point reason, so it can't win a season.

SUPPORT_URL (the page /support links to) is only accepted as https on a short host allowlist.
"""

from collections import deque
from urllib.parse import urlsplit

import config
from logic.recap import Monthly

MIN = 60
DAY = 24 * 60 * MIN

# ---------------------------------------------------------------- the support link
ALLOWED_HOSTS = frozenset({"ko-fi.com", "patreon.com", "discord.com"})
URL_MAX = 200
NOT_OPEN = "Supporting the server isn't open yet."


def support_url(raw: str | None) -> str | None:
    """SUPPORT_URL as a safe link, or None. Only plain-ASCII https URLs on ALLOWED_HOSTS (with
    or without "www."), no credentials, no odd port. The link is rebuilt from its parts and the
    fragment is dropped."""
    text = (raw or "").strip()
    if not text or len(text) > URL_MAX or "\\" in text or any(not "!" <= c <= "~" for c in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return None
    host = (parts.hostname or "").lower()
    if host.removeprefix("www.") not in ALLOWED_HOSTS:
        return None
    return f"https://{host}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


# ---------------------------------------------------------------- stipend
STIPEND_COINS = 250  # about one /daily: a thank-you, not an income
STIPEND_REASON = "supporterstipend"  # ledger reason (not in shop.SEASON_REASONS)
STIPEND_JOB = Monthly("supporterstipend", day=1, hour=12, minute=15)


def stipend_ref(month: str, user_id: int) -> str:
    """One stipend per member per local month, however often the role comes and goes."""
    return f"supporterstipend:{month}:{user_id}"


# ---------------------------------------------------------------- thank-you
RETHANK_AFTER = 60 * DAY  # a supporter who comes back after this long is thanked again
THANKS_BURST = 3  # public thank-yous per THANKS_WINDOW; the rest are recorded quietly
THANKS_WINDOW = 10 * MIN


def should_thank(thanked_at: int | None, now: int) -> bool:
    return thanked_at is None or now - thanked_at >= RETHANK_AFTER


class Burst:
    """At most THANKS_BURST public thank-yous per window, so an integration that hands the
    role to many members at once doesn't flood general."""

    def __init__(self, limit: int = THANKS_BURST, window: int = THANKS_WINDOW):
        self.limit, self.window = limit, window
        self.sent: deque[int] = deque()

    def allow(self, now: int) -> bool:
        while self.sent and now - self.sent[0] >= self.window:
            self.sent.popleft()
        if len(self.sent) >= self.limit:
            return False
        self.sent.append(now)
        return True


def thanks_text(mention: str, coins: int) -> str:
    extra = f" **{coins:,} coins** are in your wallet as a thank-you." if coins else ""
    return f"💜 {mention} is now a **{config.SUPPORTER_ROLE}** of the server. Thank you!{extra}"


# ---------------------------------------------------------------- perks and flair
def perks_text() -> str:
    return "\n".join([
        f"💜 The **{config.SUPPORTER_ROLE}** role, shown in the member list",
        "🪪 A supporter flair on your `/profile`",
        f"🛋️ The private {config.SUPPORTERS_CHANNEL} lounge",
        f"🪙 {STIPEND_COINS:,} coins on the 1st of each month",
        "",
        "Perks are a thank-you, not an advantage: nothing here changes seasons, tournaments or "
        "anything you can win. They stop when the role does; coins already paid are yours.",
    ])


def flair(since: int | None) -> str:
    return f"💜 **{config.SUPPORTER_ROLE}**" + (f" since <t:{since}:D>" if since else "")
