"""Welcomers: get a regular into voice when someone new shows up there. Pure: no Discord, no
database, no clock of its own (the cog passes `now` and plain values in).

When a member who joined the server in the last NEW_DAYS days joins a public voice channel for
the first time ever and is still there HOLD seconds later, Front Desk posts one line in general
that pings the opt-in Welcomer role, mentions the newcomer without pinging them and names the
channel. Never:

- for a bot, or for a member with tracking off (`/privacy tracking:off`: the "first time in
  voice" answer comes from voice history, which they turned off, so they are skipped entirely)
- for someone who was in voice before (the stats module's voice history; HISTORY_SLACK keeps
  the row the stats module opens for this very join from counting) or was pinged for before
- when a Welcomer or a staff member is already in that channel
- when nobody else holds the Welcomer role
- within COOLDOWN of the last ping (a burst of joins is one ping), or in quiet hours (the Welcome
  Crew's setting, CREW_QUIET_HOURS, through logic/crew.py)

A first voice join that is skipped for any reason is not retried: a later join has voice
history, so it is not a first join any more.
"""

from datetime import tzinfo

from logic import crew

MIN = 60
HOUR = 60 * MIN
DAY = 24 * HOUR

NEW_DAYS = 14  # joined the server this recently
HOLD = 30  # seconds in the channel before the ping (someone who pops in and out is not pinged for)
COOLDOWN = 15 * MIN  # between two pings, whoever they are for
HISTORY_SLACK = MIN  # a voice row that started this close to the join is this join's own row
MARK_KEEP = 30 * DAY  # how long "pinged for this member" is kept (only a recent joiner can be new)

LAST_PING_KEY = "welcomers:last_ping"  # meta: when the last ping was claimed (unix)
MARK_PREFIX = "welcomers:pinged:"  # meta: welcomers:pinged:<user id> = when (unix)


def mark_key(user_id: int) -> str:
    return f"{MARK_PREFIX}{user_id}"


def is_newcomer(joined_at: int | None, now: int) -> bool:
    """Joined the server in the last NEW_DAYS days. Unknown join time: not a newcomer."""
    return joined_at is not None and 0 <= now - joined_at < NEW_DAYS * DAY


def held(joined_voice_at: int, now: int) -> bool:
    return now - joined_voice_at >= HOLD


# Why a first voice join gets no ping (for the log); None means send.
BOT = "a bot"
NOT_NEW = "not a newcomer"
OPTED_OUT = "tracking is off"
PINGED_BEFORE = "pinged for before"
BEEN_IN_VOICE = "was in voice before"
LEFT = "left the channel"
NO_WELCOMERS = "nobody holds the Welcomer role"
HOST_THERE = "a Welcomer or staff member is already there"
QUIET = "quiet hours"
COOLING = "cooldown"


def skip_reason(*, now: int, tz: tzinfo, quiet: tuple[int, int] | None, is_bot: bool,
                joined_at: int | None, opted_out: bool, pinged_before: bool, been_in_voice: bool,
                still_there: bool, welcomers: int, host_there: bool, last_ping_at: int | None) -> str | None:
    """Whether to ping for this newcomer now. `welcomers`: members holding the role, not counting
    bots or the newcomer. `host_there`: a Welcomer or staff member is in the channel with them."""
    if is_bot:
        return BOT
    if not is_newcomer(joined_at, now):
        return NOT_NEW
    if opted_out:
        return OPTED_OUT
    if pinged_before:
        return PINGED_BEFORE
    if been_in_voice:
        return BEEN_IN_VOICE
    if not still_there:
        return LEFT
    if welcomers <= 0:
        return NO_WELCOMERS
    if host_there:
        return HOST_THERE
    if crew.is_quiet(now, tz, quiet):
        return QUIET
    if last_ping_at is not None and now - last_ping_at < COOLDOWN:
        return COOLING
    return None


def ping_text(role_mention: str, newcomer_mention: str, channel_mention: str) -> str:
    """The one line in general. The newcomer's mention renders but doesn't ping (role ping only)."""
    return f"👋 {role_mention} {newcomer_mention} is new and just joined {channel_mention}. Come say hi."
