"""Monkey SMP faction recruitment: the rules. Pure: no Discord I/O, no database, no clock of its own.

The board (cogs/factions.py) is a forum where each approved faction of this season gets one post:
its name, a short pitch its leader writes, playstyle tags, a timezone, whether it is recruiting and
how many of its four places are taken, with a "Request to join" button that tells the leader.

- `/faction pitch` (the leader): the pitch, the tags and the timezone. Saving it makes or edits the post.
- `/faction recruit on|off` (the leader): open or closed. A full roster can't open.
- `/faction lfg on|off`: a solo player says they're looking (with an optional note and timezone);
  `/faction seekers`: leaders of approved factions, and staff, see who is.
- A join request goes to the leader by DM (or, if their DMs are shut, one ping in the faction's board
  post). One request per player per faction per REQUEST_COOLDOWN, and none from a player who is
  already on a registration this season.

Off unless RECRUIT_FORUM names a forum (server/.env): the commands then say the board isn't open.
"""

from dataclasses import dataclass

from logic import factions as F

ENV_FORUM = "RECRUIT_FORUM"

PITCH_LEN = (10, 200)
TIMEZONE_LEN = (2, 32)
NOTE_LEN = (0, 200)
MAX_TAGS = 4
# The playstyle tags a leader can pick (any case; commas between them).
PLAYSTYLES = ("builders", "pvp", "raiders", "traders", "farmers", "explorers", "redstone", "casual",
              "competitive", "chill", "night owls", "weekends")
REQUEST_COOLDOWN = 24 * 3600  # one join request per player per faction a day
SEEKER_DAYS = 14  # a /faction lfg entry not renewed in this long drops off the list
MAX_SEEKERS_SHOWN = 20


@dataclass(frozen=True)
class Settings:
    forum: str | None = None  # the board forum's name; None: the board is off

    @property
    def on(self) -> bool:
        return bool(self.forum)


def read_settings(environ) -> Settings:
    forum = str(environ.get(ENV_FORUM) or "").strip()
    return Settings(forum=forum or None)


# ---------------------------------------------------------------- the leader's answers
def parse_tags(text) -> tuple[list[str], str | None]:
    """Playstyle tags from a comma-separated line: (tags, None) or ([], the problem). Empty is fine."""
    raw = [F.squash(p).lower() for p in str(text or "").replace(";", ",").split(",")]
    tags: list[str] = []
    for tag in raw:
        if not tag:
            continue
        if tag not in PLAYSTYLES:
            return [], (f"`{tag[:20]}` isn't one of the playstyle tags. Pick up to {MAX_TAGS} of: "
                        + ", ".join(PLAYSTYLES) + ".")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        return [], f"Pick up to {MAX_TAGS} playstyle tags."
    return tags, None


@dataclass(frozen=True)
class Pitch:
    pitch: str
    tags: tuple[str, ...]
    timezone: str


def read_pitch(raw: dict) -> tuple[Pitch | None, str | None]:
    pitch = F.squash(raw.get("pitch"))
    timezone = F.squash(raw.get("timezone"))
    problem = (F.length_problem("pitch", pitch, PITCH_LEN)
               or F.length_problem("timezone", timezone, TIMEZONE_LEN))
    if problem:
        return None, problem
    tags, problem = parse_tags(raw.get("tags"))
    if problem:
        return None, problem
    return Pitch(pitch=pitch, tags=tuple(tags), timezone=timezone), None


def dump_tags(tags) -> str:
    return ",".join(tags)


def load_tags(text) -> list[str]:
    return [t for t in str(text or "").split(",") if t]


def read_seeker(note, timezone) -> tuple[tuple[str, str] | None, str | None]:
    note, timezone = F.squash(note), F.squash(timezone)
    problem = F.length_problem("note", note, NOTE_LEN)
    if not problem and timezone:
        problem = F.length_problem("timezone", timezone, TIMEZONE_LEN)
    return (None, problem) if problem else ((note, timezone), None)


# ---------------------------------------------------------------- what may happen
def roster_full(members) -> bool:
    return len(members) >= F.MAX_MEMBERS


def can_open(status: str, members) -> str | None:
    """Why a faction can't start recruiting, or None."""
    if status != "approved":
        return "Your faction has to be approved by staff before it can go on the recruitment board."
    if roster_full(members):
        return f"Your roster is full ({F.MAX_MEMBERS} of {F.MAX_MEMBERS}), so the faction can't recruit."
    return None


def request_problem(*, recruiting: bool, is_leader: bool, on_a_roster: bool, last_request_at: int | None,
                    now: int) -> str | None:
    """Why this join request can't go out, or None."""
    if is_leader:
        return "That's your own faction."
    if not recruiting:
        return "That faction isn't recruiting right now."
    if on_a_roster:
        return ("You're already on a faction's registration this season. Once you join a faction you stay in it; "
                "leaving needs an admin.")
    if last_request_at is not None and now - last_request_at < REQUEST_COOLDOWN:
        return f"You already asked them. Give the leader a day: you can ask again <t:{last_request_at + REQUEST_COOLDOWN}:R>."
    return None


def seeker_fresh(updated_at: int, now: int) -> bool:
    return now - updated_at <= SEEKER_DAYS * 86400


def can_browse(*, is_staff: bool, leads_approved: bool) -> bool:
    return is_staff or leads_approved


# ---------------------------------------------------------------- what is shown
def status_line(recruiting: bool, members) -> str:
    places = f"{len(members)} of {F.MAX_MEMBERS} places taken"
    return f"📯 Recruiting · {places}" if recruiting else f"Not recruiting · {places}"


def request_dm(requester_mention: str, mc_name: str | None, note: str, timezone: str, faction: str,
               jump_url: str | None, esc) -> str:
    """The leader's DM. `esc` escapes member-authored text."""
    who = requester_mention + (f" (Minecraft: `{esc(mc_name)}`)" if mc_name else "")
    lines = [f"📯 {who} asked to join **{esc(faction)}** on the Monkey SMP recruitment board."]
    if note:
        lines.append(f"Their note: {esc(note)}")
    if timezone:
        lines.append(f"Timezone: {esc(timezone)}")
    lines.append("DM them to talk it over. If you take them on, ask staff to add their name to your registration.")
    if jump_url:
        lines.append(f"-# Your board post: {jump_url}")
    return "\n".join(lines)


def seeker_line(mention: str, mc_name: str | None, note: str, timezone: str, since: int, esc) -> str:
    parts = [mention]
    if mc_name:
        parts.append(f"`{esc(mc_name)}`")
    if timezone:
        parts.append(esc(timezone))
    parts.append(f"since <t:{since}:d>")
    line = " · ".join(parts)
    return f"{line}\n  {esc(note)}" if note else line
