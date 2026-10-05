"""Community rules: welcome text, report checks and mod-log lines.
Pure: no Discord, no database, so it's all unit-tested."""

from collections.abc import Iterable, Mapping, Sequence
from enum import Enum

import config

MAX_GAMES = 3  # games listed in a welcome
REASON_MIN = 5
REASON_MAX = 300
QUOTE_LIMIT = 300
RATE_LIMIT = 3  # reports per reporter...
RATE_WINDOW = 10 * 60  # ...per 10 minutes
NEW_ACCOUNT_DAYS = 7
HOUR = 60 * 60
DAY = 24 * HOUR

# Picked by user id, so a member always gets the same one (and tests can pin it).
OPENERS = [
    "Welcome in, {mention}.",
    "Hey {mention}, glad you found us.",
    "{mention} just pulled up. Welcome!",
    "Good to have you here, {mention}.",
    "Hey {mention}, welcome to the server.",
]


# ---------------------------------------------------------------- welcome
def opener(user_id: int, mention: str) -> str:
    return OPENERS[user_id % len(OPENERS)].format(mention=mention)


def picked_games(member_roles: Iterable, games: Sequence[config.Game]) -> list[config.Game]:
    """The member's game roles, in server order, at most MAX_GAMES."""
    member_roles = list(member_roles)
    picked = [g for g in games if config.match_by_name(member_roles, g.role) is not None]
    return picked[:MAX_GAMES]


def pick_open_post(games: Sequence[config.Game], open_posts: Mapping[str, int]):
    """(game, thread_id) for the first picked game with an open LFG post, else None.
    `open_posts` maps game key -> thread id."""
    for game in games:
        if game.key in open_posts:
            return game, open_posts[game.key]
    return None


def thread_link(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def join_names(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def welcome_text(user_id: int, mention: str, games: Sequence[config.Game],
                 open_posts: Mapping[str, int], guild_id: int, lfg_ref: str) -> str:
    """The welcome post. `lfg_ref` is the forum's mention (or its name if it's missing)."""
    lines = [opener(user_id, mention)]
    if games:
        lines.append(f"Saw you're into {join_names([g.role for g in games])}.")
    post = pick_open_post(games, open_posts)
    if post:
        game, thread_id = post
        lines.append(f"{game.role} squad looking for people right now: {thread_link(guild_id, thread_id)}")
    else:
        lines.append(f"Looking for people to play with? Check {lfg_ref} or start a squad with /lfg.")
    return "\n".join(lines)


def onboarding_completed(before: bool, after: bool) -> bool:
    """True only on the update where Onboarding flips to done."""
    return not before and after


def welcome_on_join(completed: bool, onboarding_enabled: bool) -> bool:
    """Welcome straight away if they're already through Onboarding, or there is none."""
    return completed or not onboarding_enabled


# ---------------------------------------------------------------- reports
class ReportProblem(Enum):
    SELF = "self"
    BOT = "bot"
    REASON_SHORT = "reason_short"
    REASON_LONG = "reason_long"
    RATE_LIMITED = "rate_limited"


REPORT_REPLIES = {
    ReportProblem.SELF: "You can't report yourself.",
    ReportProblem.BOT: "Bots can't be reported. If one is misbehaving, ping a mod.",
    ReportProblem.REASON_SHORT: f"Add a bit more detail (at least {REASON_MIN} characters).",
    ReportProblem.REASON_LONG: f"Keep the reason under {REASON_MAX} characters.",
    ReportProblem.RATE_LIMITED: ("You've sent a few reports in the last 10 minutes. The mods have them; "
                                 "if it's urgent, message a Moderator directly."),
}

THANKS = "Thanks — the mods have it."


def check_target(reporter_id: int, target_id: int, target_is_bot: bool) -> ReportProblem | None:
    """Who can be reported (checked before the context menu opens its form)."""
    if reporter_id == target_id:
        return ReportProblem.SELF
    if target_is_bot:
        return ReportProblem.BOT
    return None


def check_report(reporter_id: int, target_id: int, target_is_bot: bool, reason: str) -> ReportProblem | None:
    problem = check_target(reporter_id, target_id, target_is_bot)
    if problem:
        return problem
    reason = reason.strip()
    if len(reason) < REASON_MIN:
        return ReportProblem.REASON_SHORT
    if len(reason) > REASON_MAX:
        return ReportProblem.REASON_LONG
    return None


def rate_limited(recent_count: int) -> bool:
    """`recent_count`: this reporter's reports in the last RATE_WINDOW seconds."""
    return recent_count >= RATE_LIMIT


def quote(text: str | None, limit: int = QUOTE_LIMIT) -> str | None:
    """A block-quoted excerpt of a reported message, or None if it has no text."""
    if not text or not text.strip():
        return None
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return "\n".join(f"> {line}" for line in text.split("\n"))


def can_handle(is_owner: bool, is_admin: bool, member_roles: Iterable) -> bool:
    """Who may Resolve/Dismiss reports: owner, administrators, Moderators and Keepers."""
    if is_owner or is_admin:
        return True
    member_roles = list(member_roles)
    return any(config.match_by_name(member_roles, name) is not None
               for name in (config.MOD_ROLE, config.KEEPER_ROLE))


# ---------------------------------------------------------------- mod log
def plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def account_age(created_ts: int, now_ts: int) -> str:
    age = max(0, now_ts - created_ts)
    if age < HOUR:
        return "under an hour"
    if age < DAY:
        return plural(age // HOUR, "hour")
    if age < 365 * DAY:
        return plural(age // DAY, "day")
    return plural(age // (365 * DAY), "year")


def is_new_account(created_ts: int, now_ts: int) -> bool:
    return now_ts - created_ts < NEW_ACCOUNT_DAYS * DAY


def join_line(mention: str, name: str, user_id: int, created_ts: int, now_ts: int) -> str:
    line = f"📥 {mention} ({name} · `{user_id}`) joined · account {account_age(created_ts, now_ts)} old"
    if is_new_account(created_ts, now_ts):
        line += " · ⚠️ new account"
    return line


def automod_line(rule: str, user: str, channel: str | None, keyword: str | None) -> str:
    line = f"🤖 AutoMod **{rule}** · {user}"
    if channel:
        line += f" in {channel}"
    if keyword:
        line += f" · matched `{keyword}`"
    return line
