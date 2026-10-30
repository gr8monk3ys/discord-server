"""Faction sign-up for Monkey SMP: the rules of a registration. Pure: no Discord I/O, no database.

The rules are the SMP repo's docs/SEASONS.md, "Registering a faction":

- One post per faction in the SMP forum, tagged Faction: its name, its members' Minecraft names
  (up to four), its banner and a charter.
- The charter is one paragraph in the faction's own words (newlines are folded into one).
- A name held by last season's champion can't be taken, nor one a letter or two away
  (FACTIONS_RESERVED in server/.env: the held names, comma-separated).
- Once you join a faction you stay in it, so a Minecraft name is on one registration per season.

Staff approve or reject each one with the buttons on the post. FACTIONS_SEASON (default "1") keys
the registrations, so the next season starts empty when the owner bumps it.
"""

import json
import re
import unicodedata
from dataclasses import dataclass

MAX_MEMBERS = 4
NAME_LEN = (3, 32)
BANNER_LEN = (10, 300)
CHARTER_LEN = (20, 700)
RESERVED_DISTANCE = 2  # "nor one a letter or two away"

MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")  # Minecraft Java usernames
NAME_PUNCT = set(" '&.-")

ENV_FORUM, ENV_RESERVED, ENV_SEASON = "FACTIONS_FORUM", "FACTIONS_RESERVED", "FACTIONS_SEASON"
DEFAULT_SEASON = "1"

STATUS_WORDS = {"pending": "waiting for staff", "approved": "approved", "rejected": "not approved"}


@dataclass(frozen=True)
class Settings:
    forum: str  # the forum's name, as in config.py
    reserved: tuple[str, ...] = ()
    season: str = DEFAULT_SEASON


def read_settings(environ, default_forum: str) -> Settings:
    forum = str(environ.get(ENV_FORUM) or "").strip() or default_forum
    reserved = tuple(n for n in (clean_name(p) for p in str(environ.get(ENV_RESERVED) or "").split(",")) if n)
    season = str(environ.get(ENV_SEASON) or "").strip()[:20] or DEFAULT_SEASON
    return Settings(forum=forum, reserved=reserved, season=season)


# ---------------------------------------------------------------- text
def squash(text) -> str:
    """One line: every run of whitespace (newlines too) becomes one space."""
    return " ".join(str(text or "").split())


def clean_name(text) -> str:
    return squash(text)


def name_key(name: str) -> str:
    """What two names are compared by: letters and digits only, case and accents folded."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    return "".join(c for c in folded if c.isalnum())


def distance(a: str, b: str) -> int:
    """Levenshtein distance (one letter added, removed or changed is 1)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def parse_members(text) -> list[str]:
    """Minecraft names split on commas, semicolons and whitespace, a leading @ dropped."""
    return [p.lstrip("@") for p in re.split(r"[\s,;]+", str(text or "")) if p.lstrip("@")]


# ---------------------------------------------------------------- checks
def name_problem(name: str, reserved=()) -> str | None:
    lo, hi = NAME_LEN
    if not lo <= len(name) <= hi:
        return f"The faction name must be {lo} to {hi} characters."
    if any(not (c.isalnum() or c in NAME_PUNCT) for c in name) or not name[0].isalnum():
        return "The faction name can use letters, numbers, spaces and ' & . - (starting with a letter or number)."
    key = name_key(name)
    if len(key) < lo:
        return f"The faction name needs at least {lo} letters or numbers."
    for held in reserved:
        if distance(key, name_key(held)) <= RESERVED_DISTANCE:
            return (f"**{held}** is held by last season's champion, and names a letter or two away from it "
                    "can't be taken. Pick another name.")
    return None


def members_problem(names: list[str]) -> str | None:
    if not names:
        return "List your members' Minecraft Java names (yours too), separated by commas."
    if len(names) > MAX_MEMBERS:
        return f"A faction is at most {MAX_MEMBERS} players."
    bad = [n for n in names if not MC_NAME_RE.match(n)]
    if bad:
        return (f"`{bad[0][:20]}` isn't a Minecraft Java name (3 to 16 letters, numbers or _). "
                "Separate names with commas.")
    keys = [n.lower() for n in names]
    if len(set(keys)) != len(keys):
        return "A Minecraft name is listed twice."
    return None


def length_problem(what: str, text: str, bounds: tuple[int, int]) -> str | None:
    lo, hi = bounds
    if not lo <= len(text) <= hi:
        return f"The {what} must be {lo} to {hi} characters (yours is {len(text)})."
    return None


@dataclass(frozen=True)
class Entry:
    name: str
    members: tuple[str, ...]
    banner: str
    charter: str

    @property
    def key(self) -> str:
        return name_key(self.name)


def read_entry(raw: dict, reserved=()) -> tuple[Entry | None, str | None]:
    """The modal's answers checked against the rules: (entry, None) or (None, the problem)."""
    name = clean_name(raw.get("name"))
    members = parse_members(raw.get("members"))
    banner, charter = squash(raw.get("banner")), squash(raw.get("charter"))
    problem = (name_problem(name, reserved) or members_problem(members)
               or length_problem("banner description", banner, BANNER_LEN)
               or length_problem("charter", charter, CHARTER_LEN))
    if problem:
        return None, problem
    return Entry(name=name, members=tuple(members), banner=banner, charter=charter), None


def charter_problem(charter: str) -> str | None:
    return length_problem("charter", charter, CHARTER_LEN)


def clash(entry: Entry, others) -> str | None:
    """Against this season's other open registrations (rows with name, name_key, members):
    the same name, or a member who is already on one."""
    mine = {m.lower() for m in entry.members}
    for row in others:
        if row["name_key"] == entry.key:
            return f"There's already a faction called **{row['name']}** this season."
        theirs = {m.lower() for m in load_members(row["members"])}
        taken = sorted(mine & theirs)
        if taken:
            return (f"`{taken[0]}` is already on **{row['name']}**'s registration. Once you join a faction "
                    "you stay in it; leaving needs an admin.")
    return None


def dump_members(members) -> str:
    return json.dumps(list(members))


def load_members(text) -> list[str]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def can_decide(status: str) -> bool:
    return status == "pending"
