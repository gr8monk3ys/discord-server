"""Give newcomers a short channel list. Safe to re-run; nothing is deleted or locked.

    python focus_mode.py --plan             # the plan for every channel, from layout.py alone
    python focus_mode.py                    # dry run against the server: the plan and the changes
    python focus_mode.py --apply            # do it
    python focus_mode.py --revert           # dry run of putting every channel back
    python focus_mode.py --revert --apply   # do that

What it changes, all from layout.py, in one Onboarding edit:
- the Onboarding default channels become the core (layout.ONBOARDING_DEFAULT_CHANNELS)
- one question is added under Channels & Roles (layout.FOCUS_PROMPT) whose answers are the
  opt-in groups (layout.FOCUS_GROUPS)

What it never changes: channel permissions, names, topics, categories, roles, the other
Onboarding questions, private channels, and default channels that layout.py does not know.

Who notices: default channels are what a member starts with when they join. Members who are
already here keep the channel list they have, and every channel stays open to @everyone, so
anyone can still add any of them under Channels & Roles (Browse Channels) or turn on
"Show All Channels". --revert puts layout.ONBOARDING_EVERYTHING back as the defaults and
leaves the question in place (it is harmless once everything is a default again).

Needs Manage Server and Manage Roles on Front Desk's role.
"""

import argparse
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

import layout
from names import slug

sys.stdout.reconfigure(encoding="utf-8")

NEEDED = ("manage_guild", "manage_roles")
# Discord's limits. The first two are in its developer docs (Modify Guild Onboarding).
MIN_DEFAULTS = 7  # default channels
MIN_POSTABLE = 5  # of those, channels where @everyone may send messages
MAX_PROMPTS = 15  # questions in all (the same number public_mode.py uses)
# Not in the docs, from the Onboarding editor: more than 12 answers turns the cards into a
# dropdown without descriptions; titles and descriptions are short text fields.
MAX_OPTIONS = 12
MAX_PROMPT_TITLE = 100
MAX_OPTION_TITLE = 50
MAX_OPTION_DESCRIPTION = 100
MAX_OPTION_CHANNELS = 50

CORE = "core"
OPT_IN = "opt-in"
EVENT = "event-driven"
GAME = "game"
UNLISTED = "unlisted"
PRIVATE = "private"
STAFF_CATEGORY = "00 · staff"


# ---------------------------------------------------------------- the plan (pure)
def layout_rows() -> list[dict]:
    """Every channel in layout.py, in sidebar order, with what it is."""
    rows = []
    for cat_spec in layout.CATEGORIES:
        for spec in cat_spec["channels"]:
            rows.append({
                "name": spec["name"],
                "category": cat_spec["name"],
                "type": spec.get("type", "text"),
                "private": bool(spec.get("private_to") or cat_spec.get("private_to")),
                "read_only": bool(spec.get("read_only")),
            })
    return rows


def staff_category(row) -> bool:
    """The staff category keeps the permissions it already has on the server (see layout.py),
    which hide it, so its channels count as private whether or not they say "private_to"."""
    return slug(row["category"]) == slug(STAFF_CATEGORY)


def game_channels() -> set[str]:
    return {slug(f"{emoji}・{channel}") for emoji, channel, _ in layout.GAMES}


def group_of() -> dict[str, list[str]]:
    """slug -> titles of the groups that list it (one title when layout.py is right)."""
    out: dict[str, list[str]] = {}
    for group in layout.FOCUS_GROUPS:
        for name in group["channels"]:
            out.setdefault(slug(name), []).append(group["title"])
    return out


def classify() -> list[dict]:
    """layout_rows() plus "status" (CORE, OPT_IN, EVENT, GAME, UNLISTED or PRIVATE), "group"
    (the opt-in group's title, if any) and "note". Raises ValueError when a public channel is
    in no list or in more than one, so a new channel cannot be forgotten."""
    core = {slug(n) for n in layout.ONBOARDING_DEFAULT_CHANNELS}
    groups = group_of()
    events = {slug(n): why for n, why in layout.FOCUS_EVENT_CHANNELS.items()}
    unlisted = {slug(n): why for n, why in layout.FOCUS_UNLISTED.items()}
    games = game_channels()
    problems, rows = [], []
    for row in layout_rows():
        key = slug(row["name"])
        if row["private"] or staff_category(row):
            homes = [w for w, hit in (("the core", key in core), ("a group", key in groups),
                                      ("the event-driven list", key in events),
                                      ("the unlisted list", key in unlisted)) if hit]
            if homes:
                problems.append(f"{row['name']} is private but is in {' and '.join(homes)}")
            rows.append({**row, "status": PRIVATE, "group": None, "note": "unchanged"})
            continue
        homes = [*(["core"] if key in core else []), *groups.get(key, []),
                 *(["a game channel"] if key in games else []), *(["unlisted"] if key in unlisted else [])]
        if not homes:
            problems.append(f"{row['name']} is public but is not in the core, a group or FOCUS_UNLISTED")
            continue
        if len(homes) > 1:
            problems.append(f"{row['name']} is listed more than once: {', '.join(homes)}")
            continue
        if key in events and key not in groups:
            problems.append(f"{row['name']} is event-driven but is in no opt-in group")
            continue
        if key in core:
            rows.append({**row, "status": CORE, "group": None, "note": "everyone starts with it"})
        elif key in games:
            rows.append({**row, "status": GAME, "group": None, "note": "picked under \"What do you play?\""})
        elif key in unlisted:
            rows.append({**row, "status": UNLISTED, "group": None, "note": unlisted[key]})
        elif key in events:
            rows.append({**row, "status": EVENT, "group": groups[key][0],
                         "note": f"a default while {events[key]}"})
        else:
            rows.append({**row, "status": OPT_IN, "group": groups[key][0], "note": ""})
    known = {slug(r["name"]) for r in layout_rows()}
    for label, names in (("ONBOARDING_DEFAULT_CHANNELS", layout.ONBOARDING_DEFAULT_CHANNELS),
                         ("ONBOARDING_EVERYTHING", layout.ONBOARDING_EVERYTHING),
                         ("FOCUS_GROUPS", [n for g in layout.FOCUS_GROUPS for n in g["channels"]]),
                         ("FOCUS_EVENT_CHANNELS", layout.FOCUS_EVENT_CHANNELS),
                         ("FOCUS_UNLISTED", layout.FOCUS_UNLISTED),
                         ("FOCUS_RESERVE", layout.FOCUS_RESERVE)):
        names = list(names)
        problems += [f"{label} names {n}, which is not a channel in layout.py" for n in names if slug(n) not in known]
        if len({slug(n) for n in names}) != len(names):
            problems.append(f"{label} lists a channel twice")
    if problems:
        raise ValueError("; ".join(problems))
    return rows


def limit_problems() -> list[str]:
    """What Discord would refuse, as far as layout.py alone can tell. A forum or a voice
    channel may or may not count as "postable" for Discord; that is reported as a note by
    the script and covered by layout.FOCUS_RESERVE, not treated as a problem here."""
    problems = []
    rows = {slug(r["name"]): r for r in layout_rows()}
    core = [rows[slug(n)] for n in layout.ONBOARDING_DEFAULT_CHANNELS]
    if len(core) < MIN_DEFAULTS:
        problems.append(f"the core has {len(core)} channels; Discord needs at least {MIN_DEFAULTS}")
    if len(postable(core)) < MIN_POSTABLE:
        problems.append(f"the core has {len(postable(core))} channels members can post or talk in; "
                        f"Discord needs {MIN_POSTABLE} that @everyone can send messages in")
    for row in core:
        if row["type"] not in ("text", "forum", "voice"):
            problems.append(f"{row['name']} is a {row['type']} channel and cannot be a default")
    if len(layout.FOCUS_PROMPT) > MAX_PROMPT_TITLE:
        problems.append("the question is too long")
    if not 0 < len(layout.FOCUS_GROUPS) <= MAX_OPTIONS:
        problems.append(f"the question has {len(layout.FOCUS_GROUPS)} answers; keep it to {MAX_OPTIONS}")
    titles = [g["title"] for g in layout.FOCUS_GROUPS]
    if len({slug(t) for t in titles}) != len(titles):
        problems.append("two groups share a title")
    for g in layout.FOCUS_GROUPS:
        if not 0 < len(g["title"]) <= MAX_OPTION_TITLE:
            problems.append(f"group title too long: {g['title']}")
        if len(g.get("description", "")) > MAX_OPTION_DESCRIPTION:
            problems.append(f"group description too long: {g['title']}")
        if not 0 < len(g["channels"]) <= MAX_OPTION_CHANNELS:
            problems.append(f"group {g['title']} needs between 1 and {MAX_OPTION_CHANNELS} channels")
    return problems


def postable(rows) -> list[dict]:
    """Rows that are not read-only in layout.py: text, forum (posting is Send Messages) and
    voice (its text chat)."""
    return [r for r in rows if not r["read_only"]]


def surely_postable(rows) -> list[dict]:
    """The ones Discord certainly counts: plain text channels @everyone may write in."""
    return [r for r in rows if r["type"] == "text" and not r["read_only"]]


def plan_lines() -> list[str]:
    """One line per channel, in sidebar order: what a newcomer gets."""
    rows = classify()
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        if r["status"] == CORE:
            what = "core"
        elif r["status"] == OPT_IN:
            what = f"opt-in group \"{r['group']}\""
        elif r["status"] == EVENT:
            what = f"event-driven: opt-in group \"{r['group']}\", {r['note']}"
        elif r["status"] == GAME:
            what = f"game channel ({r['note']}; not changed here)"
        elif r["status"] == UNLISTED:
            what = f"unlisted ({r['note']}; not changed here)"
        else:
            what = "private (unchanged)"
        lines.append(f"{r['name']:<{width}}  {what}")
    return lines


# ---------------------------------------------------------------- applying it
class FocusMode:
    def __init__(self, guild: discord.Guild, apply: bool, revert: bool = False):
        self.guild = guild
        self.apply = apply
        self.revert = revert

    def channel(self, name):
        return discord.utils.find(lambda c: slug(c.name) == slug(name), self.guild.channels)

    def preflight(self) -> bool:
        perms = self.guild.me.guild_permissions
        missing = [p for p in NEEDED if not getattr(perms, p)]
        if missing:
            print("! Front Desk's role is missing: " + ", ".join(p.replace("_", " ") for p in missing))
            print("  Server Settings -> Roles -> Front Desk -> Permissions.")
            return False
        return True

    def print_plan(self) -> None:
        print("\nPlan, channel by channel")
        for line in plan_lines():
            print(f"  {line}")
        rows = {slug(r["name"]): r for r in layout_rows()}
        core = [rows[slug(n)] for n in layout.ONBOARDING_DEFAULT_CHANNELS]
        sure = surely_postable(core)
        print(f"\n  The core is {len(core)} channels. Discord needs {MIN_DEFAULTS}, {MIN_POSTABLE} of them "
              "open for @everyone to send messages.")
        print(f"  {len(sure)} are plain text channels ({', '.join(r['name'] for r in sure)}); the other "
              f"{len(postable(core)) - len(sure)} postable ones are a forum and voice channels.")
        if len(sure) < MIN_POSTABLE:
            print("  If Discord does not count those, this script adds "
                  + ", then ".join(layout.FOCUS_RESERVE) + " to the core until it accepts the edit.")

    # ------------------------------------------------------------ defaults
    def managed(self) -> set[str]:
        """Slugs of the channels this script decides about. Other defaults are kept as they are."""
        return {slug(r["name"]) for r in classify() if r["status"] in (CORE, OPT_IN, EVENT)}

    def wanted_defaults(self, current, extra=()):
        """(channels, missing names): the default list to set. Defaults layout.py does not
        manage come first, untouched; then the core (or everything, for --revert), in
        layout.py's order."""
        names = layout.ONBOARDING_EVERYTHING if self.revert else [*layout.ONBOARDING_DEFAULT_CHANNELS, *extra]
        if self.revert:  # never drop anything on the way back
            out = list(current)
        else:
            managed = self.managed()
            out = [c for c in current if slug(c.name) not in managed]
        missing = []
        for name in names:
            ch = self.channel(name)
            if ch is None:
                missing.append(name)
            elif ch.id not in {c.id for c in out}:
                out.append(ch)
        return out, missing

    # ------------------------------------------------------------ the question
    def wanted_options(self):
        """[(group, [channels])] for the groups with at least one channel on the server."""
        out = []
        for group in layout.FOCUS_GROUPS:
            found = []
            for name in group["channels"]:
                ch = self.channel(name)
                if ch is None:
                    print(f"! note    {group['title']}: no {name} channel yet (re-run once it exists)")
                else:
                    found.append(ch)
            if found:
                out.append((group, found))
        return out

    @staticmethod
    def option_ids(option) -> set[int]:
        return {int(i) for i in option.channel_ids}

    def prompt_matches(self, prompt, wanted) -> bool:
        have = {slug(o.title): self.option_ids(o) for o in prompt.options}
        want = {slug(g["title"]): {c.id for c in channels} for g, channels in wanted}
        return have == want and not prompt.in_onboarding

    def build_prompt(self, wanted):
        return discord.OnboardingPrompt(
            type=discord.OnboardingPromptType.multiple_choice, title=layout.FOCUS_PROMPT,
            options=[discord.OnboardingPromptOption(title=g["title"], emoji=g["emoji"],
                                                    description=g.get("description"), channels=channels)
                     for g, channels in wanted],
            single_select=False, required=False, in_onboarding=False)

    def wanted_prompts(self, ob):
        """The full prompt list to send, or None when the question is already right. The other
        questions are passed back exactly as they are, in the same order."""
        if self.revert:
            return None
        wanted = self.wanted_options()
        if not wanted:
            return None
        at = next((i for i, p in enumerate(ob.prompts) if slug(p.title) == slug(layout.FOCUS_PROMPT)), None)
        if at is not None and self.prompt_matches(ob.prompts[at], wanted):
            print(f"  ok      question \"{layout.FOCUS_PROMPT}\"")
            return None
        prompts = list(ob.prompts)
        if at is None:
            if len(prompts) + 1 > MAX_PROMPTS:
                print(f"! skip    Onboarding already has {len(prompts)} questions (Discord allows {MAX_PROMPTS})")
                return None
            prompts.append(self.build_prompt(wanted))
        else:
            prompts[at] = self.build_prompt(wanted)
        verb = "add" if at is None else "update"
        print(f"  {verb:<7} question \"{layout.FOCUS_PROMPT}\" under Channels & Roles (asked after joining)")
        for group, channels in wanted:
            print(f"          {group['emoji']} {group['title']}: {', '.join(c.name for c in channels)}")
        return prompts

    # ------------------------------------------------------------ the edit
    async def onboarding(self) -> bool:
        print("\nOnboarding")
        try:
            ob = await self.guild.onboarding()
        except discord.HTTPException as e:
            print(f"  skip    ({e.text or e})")
            return False
        if not ob.enabled:
            print("  skip    Onboarding is off on this server (polish_server.py turns it on)")
            return False
        current = list(ob.default_channels)
        defaults, missing = self.wanted_defaults(current)
        if missing:
            print(f"! stop    these channels don't exist yet: {', '.join(missing)} (run public_mode.py first)")
            return False
        prompts = self.wanted_prompts(ob)
        if not self.revert and prompts is None and not any(
                slug(p.title) == slug(layout.FOCUS_PROMPT) for p in ob.prompts):
            print("! stop    the opt-in question can't be added, so the defaults stay as they are: "
                  "without it the other channels would be hard to find")
            return False
        have, want = {c.id for c in current}, {c.id for c in defaults}
        added = [c for c in defaults if c.id not in have]
        removed = [c for c in current if c.id not in want]
        if not added and not removed:
            print(f"  ok      default channels ({len(current)})")
        else:
            if removed:
                print(f"  opt-in  no longer a default ({len(removed)}): {', '.join(c.name for c in removed)}")
            if added:
                print(f"  default added ({len(added)}): {', '.join(c.name for c in added)}")
            print(f"          default channels: {len(current)} -> {len(defaults)}")
        if prompts is None and not added and not removed:
            return True
        if not self.apply:
            return True
        await self.send(prompts, None if not (added or removed) else defaults, current)
        return True

    async def send(self, prompts, defaults, current) -> None:
        """One Onboarding edit. If Discord refuses the shorter default list, add the reserve
        channels one at a time and try again; a refused edit changes nothing."""
        kwargs = {}
        if prompts is not None:
            kwargs["prompts"] = prompts
        if defaults is None:
            await self.guild.edit_onboarding(**kwargs, reason="Focus: opt-in question")
            return
        reserve = [] if self.revert else list(layout.FOCUS_RESERVE)
        extra: list[str] = []
        while True:
            try:
                await self.guild.edit_onboarding(**kwargs, default_channels=defaults,
                                                 reason="Focus: revert" if self.revert else "Focus: core channels")
                break
            except discord.HTTPException as e:
                if e.status != 400 or not reserve:
                    raise
                extra.append(reserve.pop(0))
                print(f"  retry   Discord refused ({e.text or e}); adding {extra[-1]} to the core")
                defaults, missing = self.wanted_defaults(current, extra)
                if missing:
                    raise
        if extra:
            print(f"! note    Discord needed {', '.join(extra)} as well. Add "
                  f"{'it' if len(extra) == 1 else 'them'} to ONBOARDING_DEFAULT_CHANNELS in layout.py "
                  "(and take it out of its group) so the plan above matches the server.")

    async def run(self) -> bool:
        what = "REVERT: every channel back in the default list" if self.revert else "FOCUS: a short default list"
        mode = "APPLYING" if self.apply else "DRY RUN (nothing changes; add --apply to do it)"
        print(f"{self.guild.name}: {what}. {mode}")
        if not self.preflight():
            return False
        try:
            problems = limit_problems()
            classify()
        except ValueError as e:
            print(f"! layout.py: {e}")
            return False
        if problems:
            for p in problems:
                print(f"! layout.py: {p}")
            return False
        if not self.revert:
            self.print_plan()
        ok = await self.onboarding()
        if self.apply:
            print("\nDone. Run snapshot_server.py and commit the new server/snapshot/onboarding.json.")
        else:
            print("\nThat's the plan. Run again with --apply.")
        return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    parser.add_argument("--revert", action="store_true", help="put every channel back in the default list")
    parser.add_argument("--plan", action="store_true",
                        help="print the channel-by-channel plan from layout.py and stop (no Discord connection)")
    args = parser.parse_args()
    if args.plan:
        try:
            lines, problems = plan_lines(), limit_problems()
        except ValueError as e:
            sys.exit(f"layout.py: {e}")
        print("\n".join(lines))
        sys.exit("\n".join(f"layout.py: {p}" for p in problems) if problems else 0)
    load_dotenv(Path(__file__).with_name(".env"))
    token, guild_id = os.getenv("DISCORD_TOKEN"), os.getenv("GUILD_ID")
    if not token or not guild_id:
        sys.exit("Set DISCORD_TOKEN and GUILD_ID in server/.env (copy .env.example).")

    client = discord.Client(intents=discord.Intents.default())
    result = {"ok": False}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                print(f"Front Desk isn't in server {guild_id}.")
                return
            result["ok"] = await FocusMode(guild, args.apply, args.revert).run()
        except discord.HTTPException as e:
            print(f"\nDiscord refused a change: {e}")
        finally:
            await client.close()

    client.run(token, log_handler=None)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
