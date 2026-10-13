"""Focus: event-driven default channels. Pure: no Discord, no database.

The server keeps a short list of Onboarding default channels (server/layout.py, applied by
server/focus_mode.py). Two channels only matter while something is going on: the tournaments
channel while a tournament is open or running, and the realms channel while the Minecraft
server is online. While that is true the channel is a default, so someone who joins then
starts with it; afterwards it goes back to being opt-in.

Why the defaults and not the channel's permissions: hiding a channel from @everyone would
also take it away from members who opted in to it, and showing it does not put it in
anybody's channel list. So this module never touches a permission overwrite. By every
report we could find, the defaults are what a member starts with on joining, so a change
reaches people who join afterwards and leaves the lists of current members alone.

Rules:
- Only the channels in EVENT_CHANNELS are ever added or removed. Every other default is sent
  back exactly as it was.
- The module is inert unless the server is focused, meaning no ordinary opt-in channel is a
  default. Before focus_mode.py is applied, and after `focus_mode.py --revert`, every channel
  is a default and nothing here changes that.
- A channel is only removed if this module added it (a marker in the meta table).
- A removal never takes the list below Discord's minimum.
"""

import config

slug = config.slug

TOURNAMENT = "tournament"
REALMS = "realms"
# channel name (as in layout.py) -> what makes it live. The allowlist.
EVENT_CHANNELS: dict[str, str] = {
    config.TOURNAMENTS_CHANNEL: TOURNAMENT,
    config.REALMS_CHANNEL: REALMS,
}
TOURNAMENT_LIVE = ("signup", "running")  # tournaments.status values (cogs/tournaments.py ACTIVE)
WHEN_LIVE = {TOURNAMENT: "a tournament is open or running", REALMS: "the Minecraft server is online"}
WHEN_IDLE = {TOURNAMENT: "no tournament is open", REALMS: "the Minecraft server is offline"}

TICK = 60  # seconds between looks at the tournament table and the realms cog (no Discord calls)
RECHECK = 60 * 60  # read the Onboarding settings at least this often even if nothing changed
RETRY = 15 * 60  # after Discord refused or failed
HOLD = 15 * 60  # a channel changes at most once in this long, so a flapping server can't spam edits
MIN_DEFAULTS = 7  # Discord's minimum number of default channels

ADD, REMOVE, FORGET, NOTHING = "add", "remove", "forget", "nothing"


def marker_key(channel_name: str) -> str:
    """meta key that says "this module added the channel to the defaults"."""
    return f"focus:added:{slug(channel_name)}"


def opt_in_names() -> list[str]:
    """The ordinary opt-in channels: in a group in layout.py and not event-driven."""
    events = {slug(n) for n in EVENT_CHANNELS}
    return [n for group in config.layout.FOCUS_GROUPS for n in group["channels"] if slug(n) not in events]


def focused(default_ids, opt_in_ids) -> bool:
    """True when the short default list is in force: none of the opt-in channels is a default."""
    return not set(default_ids) & set(opt_in_ids)


def decide(live: bool | None, in_defaults: bool, marked: bool, is_focused: bool) -> str:
    """What to do about one event channel.

    live: True while its event is on, False when it is not, None when that isn't known (then
    nothing changes). marked: this module added it earlier."""
    if not is_focused:
        return FORGET if marked else NOTHING
    if live is None:
        return NOTHING
    if live:
        return NOTHING if in_defaults else ADD
    if not marked:
        return NOTHING  # a default somebody else chose: not ours to remove
    return REMOVE if in_defaults else FORGET


def held(action: str, changed_at: int | None, now: int) -> bool:
    """True when an add/remove must wait because the channel changed less than HOLD ago."""
    return action in (ADD, REMOVE) and changed_at is not None and now - changed_at < HOLD


def apply_actions(default_ids, actions: dict[int, str]) -> list[int] | None:
    """The new default id list (sorted, so the same input always sends the same edit), or
    None when nothing would change or a removal would go below Discord's minimum.
    `actions` maps a channel id to ADD or REMOVE; every other id is kept."""
    before = set(default_ids)
    after = (before | {i for i, a in actions.items() if a == ADD}) - {i for i, a in actions.items() if a == REMOVE}
    if after == before or len(after) < MIN_DEFAULTS:
        return None
    return sorted(after)


def log_line(channel_name: str, source: str, action: str) -> str:
    """One mod-log line for a change."""
    if action == ADD:
        return f"Focus: **{channel_name}** is a starting channel for new members while {WHEN_LIVE[source]}."
    return f"Focus: **{channel_name}** is opt-in again ({WHEN_IDLE[source]})."
