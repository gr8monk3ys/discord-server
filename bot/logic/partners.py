"""Partner program rules: parsing invite links, reading Discord's public invite
endpoint, who may apply, and the text of posts, review cards and DMs.
Pure: no Discord, no database, no network.

Every partner link the bot posts is rebuilt from a validated invite code
(`invite_url`), never copied from what the applicant typed."""

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from logic.schedule import Weekly

MIN_MEMBERS = 20
COOLDOWN_DAYS = 30
DAY = 24 * 60 * 60
COOLDOWN = COOLDOWN_DAYS * DAY
NAME_MAX = 100
INVITE_MAX = 100  # what the modal accepts for the invite field
DESC_MIN = 30
DESC_MAX = 600
SWEEP_JOB = Weekly("partners", weekday=0, hour=12, minute=0)  # Mondays at noon, Pacific

CODE = re.compile(r"[A-Za-z0-9-]{2,32}")
GG_HOSTS = {"discord.gg", "www.discord.gg"}
INVITE_HOSTS = {"discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com",
                "ptb.discord.com", "canary.discord.com"}
MENTION = re.compile(r"@(everyone|here)\b|<@[!&]?\d+>", re.IGNORECASE)
INVITE_IN_TEXT = re.compile(r"(discord(app)?\.com/invite|discord\.gg)/", re.IGNORECASE)

OPEN = ("pending", "approved")  # statuses that hold an invite code

REPLIES = {
    "invite_format": ("That doesn't look like a Discord invite. Paste a permanent invite like "
                      "`https://discord.gg/yourcode` or just the code."),
    "not_found": "That invite doesn't work. Make a new **permanent** invite (Expire after: Never) and try again.",
    "temporary": ("That invite expires. Make one with **Expire after: Never** so the partner post "
                  "keeps working."),
    "small": f"Partners need at least {MIN_MEMBERS} members. Come back once your server has grown a bit.",
    "self": "That's an invite to this server. Use an invite to the server you want to partner.",
    "not_server": "That invite isn't for a server.",
    "unreachable": "I couldn't check that invite with Discord just now. Try again in a few minutes.",
    "name": f"Give your server's name (up to {NAME_MAX} characters).",
    "name_mentions": "The server name can't contain pings.",
    "description_short": f"Write at least {DESC_MIN} characters about your server.",
    "description_long": f"Keep the description under {DESC_MAX} characters.",
    "description_mentions": "The description can't contain @everyone, @here or role/user pings.",
    "description_invite": "Leave invite links out of the description: I add your invite to the post myself.",
    "pending": "You already have an application waiting for review. The mods will get to it soon.",
    "cooldown": "Your last application wasn't approved. You can apply again in {days}.",
    "duplicate": "That server already has an application or a partner post here.",
}


def plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def reply(problem: str, days: int = 0) -> str:
    return REPLIES[problem].format(days=plural(days, "day"))


# ---------------------------------------------------------------- invites
def parse_invite(text: str) -> str | None:
    """The invite code in a bare code, discord.gg/<code> or discord.com/invite/<code>
    link; None for anything else (other hosts, query strings, ports, extra path)."""
    text = text.strip().strip("<>").strip()
    if CODE.fullmatch(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.query or parts.fragment:
        return None
    host = parts.netloc.lower()  # user@host and host:port never match below
    path = [p for p in parts.path.split("/") if p]
    if host in GG_HOSTS and len(path) == 1:
        code = path[0]
    elif host in INVITE_HOSTS and len(path) == 2 and path[0] == "invite":
        code = path[1]
    else:
        return None
    return code if CODE.fullmatch(code) else None


def _checked(code: str) -> str:
    if not CODE.fullmatch(code or ""):
        raise ValueError(f"not an invite code: {code!r}")
    return code


def invite_url(code: str) -> str:
    return f"https://discord.gg/{_checked(code)}"


def api_url(code: str) -> str:
    return f"https://discord.com/api/v10/invites/{_checked(code)}"


@dataclass(frozen=True)
class InviteCheck:
    problem: str | None  # None = good to partner
    guild_id: int | None = None
    guild_name: str | None = None
    members: int | None = None

    @property
    def definitive(self) -> bool:
        """False when Discord didn't answer usefully: don't act on it."""
        return self.problem != "unreachable"


def check_invite(status: int, body: bytes, our_guild_id: int) -> InviteCheck:
    """Read GET /invites/<code>?with_counts=true. A partner invite must exist, be for
    a server other than ours, never expire and show at least MIN_MEMBERS members."""
    if status == 404:
        return InviteCheck("not_found")
    if status != 200:
        return InviteCheck("unreachable")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return InviteCheck("unreachable")
    if not isinstance(data, dict):
        return InviteCheck("unreachable")
    guild = data.get("guild")
    gid = guild.get("id") if isinstance(guild, dict) else None
    if data.get("type", 0) != 0 or not isinstance(gid, str) or not gid.isdigit():
        return InviteCheck("not_server")
    name = guild.get("name")
    name = name.strip()[:NAME_MAX] if isinstance(name, str) else None
    members = data.get("approximate_member_count")
    members = members if isinstance(members, int) and not isinstance(members, bool) else None
    found = dict(guild_id=int(gid), guild_name=name, members=members)
    if int(gid) == our_guild_id:
        return InviteCheck("self", **found)
    if data.get("expires_at") is not None:
        return InviteCheck("temporary", **found)
    if members is None or members < MIN_MEMBERS:
        return InviteCheck("small", **found)
    return InviteCheck(None, **found)


def is_dead(check: InviteCheck) -> bool:
    """An approved partner's invite is gone for good (deleted, or now expiring). A server
    that shrank or a Discord hiccup doesn't count."""
    return check.problem in ("not_found", "temporary")


# ---------------------------------------------------------------- what applicants type
def clean_text(text: str) -> str:
    """One paragraph: every run of whitespace (newlines too) becomes one space."""
    return " ".join((text or "").split())


def name_problem(name: str) -> str | None:
    name = clean_text(name)
    if not name or len(name) > NAME_MAX:
        return "name"
    if MENTION.search(name):
        return "name_mentions"
    return None


def description_problem(desc: str) -> str | None:
    """`desc` is already clean_text()ed."""
    if MENTION.search(desc):
        return "description_mentions"
    if INVITE_IN_TEXT.search(desc):
        return "description_invite"
    if len(desc) < DESC_MIN:
        return "description_short"
    if len(desc) > DESC_MAX:
        return "description_long"
    return None


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def names_match(given: str, real: str | None) -> bool:
    return _letters(given) == _letters(real or "")


# ---------------------------------------------------------------- who may apply
def application_problem(rows: Iterable[Mapping], now: int) -> tuple[str, int] | None:
    """(problem, days left) if this member can't apply now: one pending application at
    a time, and COOLDOWN_DAYS after their latest denial. `rows` are their partners rows."""
    rows = list(rows)
    if any(r["status"] == "pending" for r in rows):
        return ("pending", 0)
    denials = [r["decided_at"] or r["created_at"] for r in rows if r["status"] == "denied"]
    if denials:
        left = max(denials) + COOLDOWN - now
        if left > 0:
            return ("cooldown", math.ceil(left / DAY))
    return None


def duplicate(rows: Iterable[Mapping], code: str) -> bool:
    """Another pending or approved application already uses this invite code."""
    return any(r["status"] in OPEN and r["invite_code"] == code for r in rows)


# ---------------------------------------------------------------- text (callers escape names)
def post_lines(name: str, desc: str, members: int | None, code: str, dead: bool = False) -> str:
    """Body of the partner post. `name` and `desc` must already be escaped."""
    if dead:
        return f"{desc}\n\n⚠️ This invite has expired, so the link was removed."
    lines = [desc, ""]
    if members is not None:
        lines.append(f"👥 {members:,} members")
    lines.append(f"🔗 {invite_url(code)}")
    return "\n".join(lines)


def review_lines(app_id: int, applicant: str, given_name: str, invite_name: str | None,
                 members: int | None, code: str, desc: str) -> str:
    """Body of the staff review card. Names and description must already be escaped."""
    lines = [f"**Applicant** {applicant}", f"**Server** {given_name}"]
    if invite_name and not names_match(given_name, invite_name):
        lines.append(f"⚠️ The invite's server name doesn't match: **{invite_name}**")
    if members is not None:
        lines.append(f"**Members** {members:,}")
    lines += [f"**Invite** <{invite_url(code)}>", "", desc]
    return "\n".join(lines)


def dm_text(kind: str, partner_name: str, our_name: str) -> str:
    if kind == "approved":
        return (f"Approved: **{partner_name}** is now a partner of **{our_name}**. "
                "Your server is listed in the partners channel. Thanks for applying!")
    return (f"Your partner application for **{partner_name}** with **{our_name}** wasn't approved this time. "
            f"You can apply again in {COOLDOWN_DAYS} days.")
