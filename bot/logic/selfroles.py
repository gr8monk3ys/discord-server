"""Self-assign role rules for the #roles panel. Pure: no Discord, no database.

Buttons and the games select carry only a section key and an index into that section's
list from config, never a role name or id, so a forged custom_id can't reach any role
that isn't listed here."""

from dataclasses import dataclass

import config

ROW_MAX = 5  # buttons per action row
SELECT_MAX = 25  # options per select menu

# A role carrying any of these is never self-assignable, even if it's listed by name.
DANGEROUS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages",
    "manage_webhooks", "manage_nicknames", "manage_expressions", "manage_events", "manage_threads",
    "ban_members", "kick_members", "moderate_members", "mention_everyone", "view_audit_log",
    "move_members", "mute_members", "deafen_members",
)


def has_dangerous_permissions(role) -> bool:
    """Any DANGEROUS permission on this role? Every role Front Desk hands out (self-roles,
    bought or earned) is checked, so a role edited to carry mod powers can't be farmed."""
    perms = getattr(role, "permissions", None)
    return perms is not None and any(getattr(perms, p, False) for p in DANGEROUS)


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    names: tuple[str, ...]
    single: bool = False  # picking one removes the others
    select: bool = False  # shown as a select menu instead of buttons
    hint: str = ""


@dataclass(frozen=True)
class Change:
    add: tuple[str, ...]
    remove: tuple[str, ...]


def sections(games=None) -> list[Section]:
    games = list(games) if games is not None else [g.role for g in config.GAMES]
    return [
        Section("platform", "Platform", tuple(config.PLATFORM_ROLES), hint="What you play on. Pick any."),
        Section("region", "Region", tuple(config.REGION_ROLES), single=True,
                hint="Where you are. One at a time."),
        Section("playtime", "Play time", tuple(config.PLAYTIME_ROLES), hint="When you're usually on."),
        Section("pings", "Pings", (config.GAMENIGHT_ROLE, config.LFG_ROLE, config.BUMPER_ROLE),
                hint="Opt-in pings: game nights, squads, Disboard bumps."),
        Section("games", "Games", tuple(games[:SELECT_MAX]), select=True,
                hint="Pick games to add or remove: you get their pings and channels."),
    ]


SECTIONS = {s.key: s for s in sections()}
BUTTON_SECTIONS = tuple(k for k, s in SECTIONS.items() if not s.select)


def section(key: str) -> Section | None:
    return SECTIONS.get(key)


def name_at(key: str, index: int) -> str | None:
    """The role name behind a button, or None for anything not on the panel."""
    s = SECTIONS.get(key)
    if s is None or s.select or not 0 <= index < len(s.names):
        return None
    return s.names[index]


def toggle(sec: Section, name: str, have: set[str], available: set[str] | None = None) -> Change:
    """What a button press does. `have` is the member's role names; `available` limits
    which other roles a single-choice section may take off (default: all of them)."""
    if name not in sec.names:
        return Change((), ())
    if name in have:
        return Change((), (name,))
    remove = ()
    if sec.single:
        remove = tuple(n for n in sec.names
                       if n != name and n in have and (available is None or n in available))
    return Change((name,), remove)


def select_values(sec: Section, values) -> list[str]:
    """Map select values (indexes as strings) to known names, dropping junk and repeats."""
    out = []
    for v in values:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(sec.names) and sec.names[i] not in out:
            out.append(sec.names[i])
    return out


def select(sec: Section, picked, have: set[str]) -> Change:
    """Each picked game toggles: added if missing, removed if held."""
    seen = []
    for n in picked:
        if n in sec.names and n not in seen:
            seen.append(n)
    return Change(tuple(n for n in seen if n not in have), tuple(n for n in seen if n in have))


def can_assign(*, position: int, bot_top: int, managed: bool, is_default: bool, dangerous: bool) -> bool:
    """Front Desk can only hand out plain roles strictly below its own top role."""
    return not managed and not is_default and not dangerous and position < bot_top


def describe(change: Change) -> str:
    parts = []
    if change.add:
        parts.append("Added " + ", ".join(change.add) + ".")
    if change.remove:
        parts.append("Removed " + ", ".join(change.remove) + ".")
    return " ".join(parts) or "Nothing changed."


def panel_ref(channel_id: int, message_id: int) -> str:
    return f"{channel_id}:{message_id}"


def parse_panel_ref(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        cid, mid = value.split(":")
        return int(cid), int(mid)
    except ValueError:
        return None
