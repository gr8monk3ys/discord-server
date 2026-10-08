"""Help desk: /help built from the bot's live command tree, the rotating presence and /about.

Pure rules, no Discord I/O. `collect()` reads app_commands Command/Group objects (anything with
the same attributes works) so the menu always lists exactly what's synced: a new cog or command
shows up in /help without touching this file. Unknown modules land in Utility."""

import re
from dataclasses import dataclass
from datetime import date

SITE_URL = "https://discord.lscaturchio.xyz"
INVITE_URL = "https://discord.gg/C25hy9Ca9W"
PRESENCE_MINUTES = 5
VIEW_TIMEOUT = 10 * 60  # the select menu stops answering after this (ephemeral, so no cleanup)
MAX_DESCRIPTION = 4000  # embed descriptions cap at 4096
MAX_CHOICES = 25  # Discord's autocomplete and select-option limit


@dataclass(frozen=True)
class Category:
    key: str
    emoji: str
    title: str
    blurb: str


CATEGORIES = (
    Category("squad", "🎮", "Squad up", "Find people to play with and get your own voice channel."),
    Category("stats", "📈", "Stats & levels", "Your activity, XP, ranks and badges."),
    Category("coins", "🪙", "Coins & shop", "Earn coins every day and spend them on perks."),
    Category("games", "🎲", "Games", "Quick games for coins, plus the Daily Word."),
    Category("events", "🏆", "Events & tournaments", "Game nights and brackets with prizes."),
    Category("community", "👋", "Community", "Roles, quests, invites, birthdays, partners and reports."),
    Category("creators", "📺", "Creators", "Get your streams and uploads spotlighted."),
    Category("utility", "🧰", "Utility", "Help, server info, reminders and AFK."),
)
BY_KEY = {c.key: c for c in CATEGORIES}
FALLBACK = "utility"

# cogs.<module> -> category. Anything not listed falls back to Utility, so nothing goes missing.
MODULE_CATEGORY = {
    "lfg": "squad", "tempvoice": "squad", "matchmaking": "squad",
    "stats": "stats", "levels": "stats", "achievements": "stats",
    "economy": "coins", "shop": "coins", "econstats": "coins",
    "games": "games", "wordgame": "games",
    "events": "events", "tournaments": "events",
    "community": "community", "growth": "community", "engagement": "community", "selfroles": "community",
    "partners": "community", "quests": "community", "challenges": "community", "vibes": "community",
    "recap": "community", "cards": "community", "starboard": "community",
    "creators": "creators", "clips": "creators",
    "utility": "utility", "moderation": "utility", "ops": "utility", "helpdesk": "utility", "heartbeat": "utility",
}

# Staff commands say so in their description: "Staff: ...", "... (mods only)", "... (staff only)",
# "... (mods)". A flag on a group covers its subcommands. Discord-side default_permissions count too.
STAFF_RE = re.compile(r"^\s*staff\b|\((?:mods?|staff)(?: only)?\)\s*$", re.IGNORECASE)

OPTION_TYPES = {
    "string": "text", "integer": "whole number", "number": "number", "boolean": "yes/no", "user": "member",
    "channel": "channel", "role": "role", "mentionable": "member or role", "attachment": "file",
}


@dataclass(frozen=True)
class Param:
    name: str
    description: str
    required: bool
    kind: str


@dataclass(frozen=True)
class Entry:
    name: str  # qualified: "word guess"
    description: str
    category: str
    staff: bool
    group: bool = False
    params: tuple[Param, ...] = ()
    subcommands: tuple[str, ...] = ()  # qualified names, for groups

    @property
    def root(self) -> str:
        return self.name.split(" ")[0]


def module_category(module: str | None) -> str:
    leaf = (module or "").rsplit(".", 1)[-1]
    return MODULE_CATEGORY.get(leaf, FALLBACK)


def staff_text(description: str | None) -> bool:
    return bool(STAFF_RE.search(description or ""))


def _has_perms(cmd) -> bool:
    perms = getattr(cmd, "default_permissions", None)
    return perms is not None and getattr(perms, "value", 0) != 0


def _kind(param) -> str:
    t = getattr(param, "type", None)
    name = getattr(t, "name", str(t or ""))
    return OPTION_TYPES.get(name, name or "text")


def _params(cmd) -> tuple[Param, ...]:
    return tuple(Param(p.name, getattr(p, "description", "") or "", bool(getattr(p, "required", False)), _kind(p))
                 for p in getattr(cmd, "parameters", []) or [])


def collect(commands) -> list[Entry]:
    """Every slash command and group in the tree, parents before their subcommands, sorted by name.
    Context menus (no description) are skipped."""
    out: list[Entry] = []
    seen: set[str] = set()

    def walk(cmd, inherited_staff: bool, category: str | None):
        if not hasattr(cmd, "description") or cmd.qualified_name in seen:
            return
        seen.add(cmd.qualified_name)
        cat = category or module_category(getattr(cmd, "module", None))
        own = inherited_staff or _has_perms(cmd) or staff_text(cmd.description)
        subs = sorted(getattr(cmd, "commands", None) or [], key=lambda c: c.name)
        if hasattr(cmd, "commands"):  # a group
            start = len(out)
            out.append(None)  # placeholder, filled once the children are known
            for sub in subs:
                walk(sub, own, cat)
            children = [e for e in out[start + 1:] if not e.group]
            staff = own or (bool(children) and all(e.staff for e in children))
            out[start] = Entry(cmd.qualified_name, cmd.description or "", cat, staff, group=True,
                               subcommands=tuple(e.name for e in children))
        else:
            out.append(Entry(cmd.qualified_name, cmd.description or "", cat, own, params=_params(cmd)))

    for cmd in sorted(commands, key=lambda c: c.name):
        walk(cmd, False, None)
    return out


def visible(entries, staff: bool) -> list[Entry]:
    return [e for e in entries if staff or not e.staff]


def leaves(entries) -> list[Entry]:
    """What people actually run: plain commands and subcommands (groups themselves can't be run)."""
    return [e for e in entries if not e.group]


def by_category(entries) -> dict[str, list[Entry]]:
    """Category key -> runnable commands, in CATEGORIES order; empty categories left out."""
    grouped = {c.key: [] for c in CATEGORIES}
    for e in leaves(entries):
        grouped.setdefault(e.category, []).append(e)
    return {k: v for k, v in grouped.items() if v}


def mention(entry_name: str, ids: dict[str, int] | None = None) -> str:
    """A clickable </name:id> mention when the command's id is known, else `/name`."""
    root = entry_name.split(" ")[0]
    cid = (ids or {}).get(root)
    return f"</{entry_name}:{cid}>" if cid else f"`/{entry_name}`"


def command_line(entry: Entry, ids=None) -> str:
    tag = " · staff" if entry.staff else ""
    return f"{mention(entry.name, ids)} {entry.description}{tag}"


def clip(text: str, limit: int = MAX_DESCRIPTION) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit("\n", 1)[0]
    return cut + "\n…"


def category_text(key: str, entries, ids=None) -> str:
    cat = BY_KEY.get(key)
    rows = by_category(entries).get(key, [])
    head = f"{cat.blurb}\n\n" if cat else ""
    if not rows:
        return head + "Nothing here yet."
    return clip(head + "\n".join(command_line(e, ids) for e in rows))


def overview_rows(entries) -> list[tuple[str, str]]:
    """(field name, field value) per category for the front page."""
    rows = []
    for key, items in by_category(entries).items():
        cat = BY_KEY.get(key) or BY_KEY[FALLBACK]
        sample = ", ".join(f"`/{e.name}`" for e in items[:4]) + (" …" if len(items) > 4 else "")
        noun = "command" if len(items) == 1 else "commands"
        rows.append((f"{cat.emoji} {cat.title} · {len(items)} {noun}", f"{cat.blurb}\n{sample}"))
    return rows


def normalize(query: str) -> str:
    return " ".join((query or "").strip().lstrip("/").lower().split())


def find(entries, query: str) -> Entry | None:
    q = normalize(query)
    if not q:
        return None
    exact = next((e for e in entries if e.name == q), None)
    if exact:
        return exact
    starts = [e for e in entries if e.name.startswith(q)]
    return starts[0] if len(starts) == 1 else None


def suggest(entries, current: str, limit: int = MAX_CHOICES) -> list[Entry]:
    """Autocomplete: names starting with what's typed first, then names containing it."""
    q = normalize(current)
    first = [e for e in entries if e.name.startswith(q)]
    then = [e for e in entries if q and q in e.name and e not in first]
    if q:
        then += [e for e in entries if q in e.description.lower() and e not in first and e not in then]
    return (first + then)[:limit]


def detail_lines(entry: Entry, ids=None) -> list[str]:
    cat = BY_KEY.get(entry.category) or BY_KEY[FALLBACK]
    lines = [entry.description or "No description.", "", f"Category: {cat.emoji} {cat.title}"]
    if entry.staff:
        lines.append("Staff only.")
    if entry.group:
        lines += ["", "Subcommands:"] + [f"• {mention(s, ids)}" for s in entry.subcommands]
    elif entry.params:
        lines += ["", "Options:"]
        for p in entry.params:
            need = "required" if p.required else "optional"
            desc = f": {p.description}" if p.description else ""
            lines.append(f"• `{p.name}` ({p.kind}, {need}){desc}")
    else:
        lines += ["", "No options: just run it."]
    return lines


def usage(entry: Entry) -> str:
    if entry.group:
        return f"/{entry.name} <{'|'.join(s.split(' ')[-1] for s in entry.subcommands)}>"
    opts = " ".join(f"{p.name}:<{p.kind}>" if p.required else f"[{p.name}]" for p in entry.params)
    return f"/{entry.name} {opts}".rstrip()


# ------------------------------------------------------------ presence

def presence_lines(members: int, word_no: int) -> list[tuple[str, str]]:
    """(activity kind, text) in rotation order. Kinds: watching, playing, listening."""
    noun = "member" if members == 1 else "members"
    return [
        ("watching", f"{members:,} {noun}"),
        ("playing", "/queue to find a squad"),
        ("listening", "/help"),
        ("playing", f"Daily Word #{word_no}"),
    ]


def presence_at(tick: int, members: int, word_no: int) -> tuple[str, str]:
    lines = presence_lines(members, word_no)
    return lines[tick % len(lines)]


# ------------------------------------------------------------ /about

def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'}"


def age_text(created: date, today: date) -> str:
    """'1 year, 2 months', '3 months, 4 days', '5 days' or 'today'."""
    if today <= created:
        return "today"
    months = (today.year - created.year) * 12 + today.month - created.month
    if today.day < created.day:
        months -= 1
    years, months = divmod(months, 12)
    anchor_month = created.month + (years * 12 + months)
    y, m = created.year + (anchor_month - 1) // 12, (anchor_month - 1) % 12 + 1
    day = min(created.day, _month_days(y, m))
    days = (today - date(y, m, day)).days
    parts = [(years, "year"), (months, "month"), (days, "day")]
    shown = [_plural(n, u) for n, u in parts if n][:2]
    return ", ".join(shown) or "today"


def _month_days(year: int, month: int) -> int:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - date(year, month, 1)).days


def boost_text(boosts: int, tier: int) -> str:
    level = f"level {tier}" if tier else "no level yet"
    return f"{boosts:,} ({level})"
