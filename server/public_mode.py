"""Open the server to the public, additively. Safe to re-run.

    python public_mode.py           # dry run: prints what would change
    python public_mode.py --apply   # do it

Unlike setup_server.py this never reorders or deletes anything, so it works
with Front Desk sitting mid-stack (below Keeper/Moderator) and without
Administrator. It needs Manage Roles, Manage Channels, Manage Server and
Create Invite on Front Desk's role.

Steps, all driven by layout.py:
- create the roles Front Desk hands out (Clip of the Week, Recruiter, Bumper)
- create any public layout channel that doesn't exist yet (never moves or edits existing ones)
- create private categories/channels ("private_to") and keep them private
- set the server description and the welcome-screen text (layout.PUBLIC)
- refresh the rules and welcome posts in place
- keep one permanent invite for listings and print it
- add any missing Onboarding default channels
- add the self-assign Onboarding prompts (platform, region, Game Night pings) if missing
- fill AutoMod gaps: create Discord's built-in rules the server doesn't have yet (spam,
  mention spam, slur/sexual-content preset, invite links to other servers); never edits or
  deletes an existing rule
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

import layout
from names import slug
from setup_server import Makeover, find, private_overwrites

sys.stdout.reconfigure(encoding="utf-8")

BOT_ROLES = ("Season Champ", "Hype", "Birthday", "Tournament Champ", "Counting Champ", "Clip of the Week", "Recruiter", "Bumper",
             "Game Night", "Mythic", "Legend", "Elite", "Veteran", "Regular",
             "PC", "PlayStation", "Xbox", "Switch", "Mobile", "NA", "EU", "LATAM", "Asia", "OCE",
             "Early Bird", "Night Owl", "Weekend Warrior")  # roles Front Desk hands out
NEEDED = ("manage_roles", "manage_channels", "manage_guild", "create_instant_invite")

# Onboarding questions added after the existing ones (matched by title; existing prompts are
# never removed or reordered). The same roles are on Front Desk's 🎭・roles panel.
ONBOARDING_PROMPTS = [
    {"title": "What do you play on?", "multi": True, "pre_join": True, "options": [
        {"title": "PC", "emoji": "🖥️", "role": "PC"},
        {"title": "PlayStation", "emoji": "🟦", "role": "PlayStation"},
        {"title": "Xbox", "emoji": "🟩", "role": "Xbox"},
        {"title": "Switch", "emoji": "🟥", "role": "Switch"},
        {"title": "Mobile", "emoji": "📱", "role": "Mobile"},
    ]},
    {"title": "Where are you?", "multi": False, "options": [
        {"title": "North America", "emoji": "🌎", "role": "NA"},
        {"title": "Europe", "emoji": "🌍", "role": "EU"},
        {"title": "Latin America", "emoji": "🌎", "role": "LATAM"},
        {"title": "Asia", "emoji": "🌏", "role": "Asia"},
        {"title": "Oceania", "emoji": "🌏", "role": "OCE"},
    ]},
    {"title": "Want pings?", "multi": True, "options": [
        {"title": "Game nights", "emoji": "🎉", "role": "Game Night",
         "description": "Get @Game Night pings when a game night starts."},
    ]},
]
MAX_PROMPTS = 15  # Discord's limit
# Discord also caps the questions shown before joining; the others go to Channels & Roles
# (post-join). If Discord still says there are too many, every new one goes post-join.
TOO_MANY_QUESTIONS = "Too many questions"


# AutoMod gaps (matched by trigger type, not name, so rules made by hand in Server Settings count).
# Alerts go to the mod log; Keeper/Moderator and the staff category are exempt. Front Desk itself
# has Manage Server, which AutoMod always exempts, so its partner posts are never blocked.
AUTOMOD_ALERTS = "📋・mod-log"
AUTOMOD_EXEMPT_CATEGORY = "00 · staff"
AUTOMOD_BLOCKED = "Blocked by AutoMod. Ask a Keeper if that was a mistake."
AUTOMOD_INVITES = "Front Desk: invite links"
AUTOMOD_INVITE_BLOCKED = "Invite links to other servers aren't allowed. Partners apply with /partner apply."
# Discord's regex flavour (Rust); at most 260 characters each.
INVITE_PATTERNS = [r"(?i)discord(?:app)?\.com/invite/[a-z0-9-]+", r"(?i)discord\.gg/[a-z0-9-]+", r"(?i)dsc\.gg/[a-z0-9-]+"]
INVITE_MARKERS = ("discord.gg", r"discord\.gg", "/invite")  # an existing rule with any of these counts
MAX_KEYWORD_RULES = 6  # Discord's per-server limit for keyword rules
MAX_ALLOW_LIST = 100


class PublicMode:
    def __init__(self, guild: discord.Guild, apply: bool):
        self.guild = guild
        self.apply = apply

    async def do(self, verb, what, action):
        print(f"  {verb:<7} {what}")
        if self.apply:
            return await action()
        return None

    def role(self, name):
        return discord.utils.find(lambda r: slug(r.name) == slug(name), self.guild.roles)

    def channel(self, name):
        return discord.utils.find(lambda c: slug(c.name) == slug(name), self.guild.channels)

    # ------------------------------------------------------------ checks
    def preflight(self) -> bool:
        perms = self.guild.me.guild_permissions
        missing = [p for p in NEEDED if not getattr(perms, p)]
        if missing:
            print("! Front Desk's role is missing: " + ", ".join(p.replace("_", " ") for p in missing))
            print("  Server Settings -> Roles -> Front Desk -> Permissions.")
            return False
        return True

    # ------------------------------------------------------------ roles
    async def roles(self):
        print("\nRoles")
        for name in BOT_ROLES:
            spec = next(r for r in layout.ROLES if r.get("name") == name)
            hoist = spec.get("hoist", False)
            role = self.role(name)
            if role is None:
                await self.do("create", f"@{name}", lambda: self.guild.create_role(
                    name=name, colour=discord.Colour(spec.get("color", 0)), hoist=hoist,
                    mentionable=spec.get("mentionable", False), reason="Public server"))
            elif role.hoist != hoist and role < self.guild.me.top_role:
                await self.do("update", f"@{name}: {'hoist' if hoist else 'unhoist'}",
                              lambda: role.edit(hoist=hoist, reason="Public server"))
            else:
                print(f"  ok      @{name}")

    # ------------------------------------------------------------ new public channels
    async def new_channels(self):
        print("\nNew channels")
        keeper = self.role("Keeper")
        for cat_spec in layout.CATEGORIES:
            if cat_spec.get("private_to"):
                continue  # handled by private()
            cat = find(self.guild.categories, cat_spec)
            for spec in cat_spec["channels"]:
                if spec.get("private_to"):
                    continue
                if spec.get("type") == "forum":
                    await self.new_forum(spec, cat, cat_spec["name"])
                    continue
                voice = spec.get("type") == "voice"
                pool = self.guild.voice_channels if voice else self.guild.text_channels
                label = spec["name"] if voice else f"#{spec['name']}"
                if find(pool, spec) is not None:
                    continue
                if cat is None:
                    print(f"! skip    {label}: category {cat_spec['name']} doesn't exist (run setup_server.py)")
                    continue
                kwargs = {}
                if not voice:
                    kwargs["topic"] = spec.get("topic", "")
                    kwargs["slowmode_delay"] = spec.get("slowmode", 0)
                if spec.get("read_only"):
                    ow = {
                        self.guild.default_role: discord.PermissionOverwrite(
                            send_messages=False, create_public_threads=False,
                            create_private_threads=False, add_reactions=True),
                        self.guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True),
                    }
                    if keeper:
                        ow[keeper] = discord.PermissionOverwrite(send_messages=True)
                    kwargs["overwrites"] = {**cat.overwrites, **ow}
                create = self.guild.create_voice_channel if voice else self.guild.create_text_channel
                await self.do("create", f"{label} in {cat.name}" + (" (read-only)" if spec.get("read_only") else ""),
                              lambda: create(spec["name"], category=cat, **kwargs))

    async def new_forum(self, spec, cat, cat_name):
        if find(self.guild.forums, spec) is not None:
            return
        label = f"#{spec['name']} (forum)"
        if cat is None:
            print(f"! skip    {label}: category {cat_name} doesn't exist (run setup_server.py)")
            return
        tags = [discord.ForumTag(name=n, emoji=e) for n, e in spec.get("tags", [])]
        await self.do("create", f"{label} in {cat.name} with {len(tags)} tags",
                      lambda: self.guild.create_forum(spec["name"], category=cat, topic=spec.get("topic", ""),
                                                      available_tags=tags))

    # ------------------------------------------------------------ private spaces
    def private_overwrites(self, role_names):
        return private_overwrites(self.guild, role_names)

    @staticmethod
    def is_private(ch, ow) -> bool:
        return all(ch.overwrites_for(target) == want for target, want in ow.items())

    async def private(self):
        print("\nPrivate spaces")
        for cat_spec in layout.CATEGORIES:
            cat = find(self.guild.categories, cat_spec)
            cat_private = cat_spec.get("private_to")
            if cat_private:
                ow = self.private_overwrites(cat_private)
                if cat is None:
                    cat = await self.do("create", f"category {cat_spec['name']} (private)",
                                        lambda: self.guild.create_category(cat_spec["name"], overwrites=ow))
                elif not self.is_private(cat, ow):
                    await self.do("lock", f"category {cat.name}", lambda: cat.edit(overwrites=ow))
                else:
                    print(f"  ok      category {cat.name} (private)")
            for spec in cat_spec["channels"]:
                who = spec.get("private_to") or cat_private
                if not who:
                    continue
                await self.private_channel(spec, cat, self.private_overwrites(who), cat_spec["name"])

    async def private_channel(self, spec, cat, ow, cat_name):
        voice = spec.get("type") == "voice"
        pool = self.guild.voice_channels if voice else self.guild.text_channels
        label = spec["name"] if voice else f"#{spec['name']}"
        ch = find(pool, spec)
        if cat is not None and not cat.permissions_for(self.guild.me).view_channel:
            print(f"! skip    {label}: Front Desk can't see {cat.name}. Edit the category's permissions, "
                  "add Front Desk, and allow View Channel, then re-run.")
            return
        if ch is None:
            if cat is None and not self.apply:
                print(f"  create  {label} in {cat_name} (private)")
                return
            create = self.guild.create_voice_channel if voice else self.guild.create_text_channel
            kwargs = {} if voice else {"topic": spec.get("topic", "")}
            await self.do("create", f"{label} (private)",
                          lambda: create(spec["name"], category=cat, overwrites=ow, **kwargs))
        elif not self.is_private(ch, ow):
            await self.do("lock", label, lambda: ch.edit(overwrites=ow))
        else:
            print(f"  ok      {label} (private)")

    # ------------------------------------------------------------ text
    async def description(self):
        print("\nDescription")
        want = layout.PUBLIC["description"]
        if self.guild.description == want:
            print("  ok      server description")
        else:
            await self.do("update", f"server description: {want}", lambda: self.guild.edit(description=want))
        try:
            screen = await self.guild.welcome_screen()
        except discord.HTTPException as e:
            print(f"  skip    welcome screen ({e.text or e})")
            return
        if screen.description == layout.WELCOME_SCREEN["description"]:
            print("  ok      welcome screen text")
        else:
            await self.do("update", "welcome screen text",
                          lambda: screen.edit(description=layout.WELCOME_SCREEN["description"]))

    async def posts(self):
        # Reuse setup_server's in-place refresh of the rules/welcome posts.
        m = Makeover(self.guild, self.apply)
        m.channels = {slug(c.name): c for c in self.guild.channels}
        m.roles = {r.name: r for r in self.guild.roles}
        await m.make_posts()

    # ------------------------------------------------------------ invite
    async def invite(self):
        print("\nInvite")
        ch = self.channel(layout.PUBLIC["invite_channel"])
        if ch is None:
            print(f"! no channel {layout.PUBLIC['invite_channel']}")
            return
        mine = [i for i in await self.guild.invites()
                if i.max_age == 0 and i.max_uses == 0 and i.inviter and i.inviter.id == self.guild.me.id]
        if mine:
            print(f"  ok      permanent invite {mine[0].url}")
            return
        made = await self.do("create", f"permanent invite to #{ch.name}",
                             lambda: ch.create_invite(max_age=0, max_uses=0, unique=False,
                                                      reason="Public listing invite"))
        if made:
            print(f"          {made.url}")

    # ------------------------------------------------------------ onboarding
    async def onboarding(self):
        print("\nOnboarding")
        try:
            ob = await self.guild.onboarding()
        except discord.HTTPException as e:
            print(f"  skip    ({e.text or e})")
            return
        have = {c.id for c in ob.default_channels}
        add = [c for c in (self.channel(n) for n in layout.ONBOARDING_DEFAULT_CHANNELS)
               if c is not None and c.id not in have]
        if not add:
            print("  ok      default channels")
            return
        await self.do("add", "default channels: " + ", ".join(c.name for c in add),
                      lambda: self.guild.edit_onboarding(default_channels=[*ob.default_channels, *add]))

    async def onboarding_prompts(self):
        print("\nOnboarding prompts")
        try:
            ob = await self.guild.onboarding()
        except discord.HTTPException as e:
            print(f"  skip    ({e.text or e})")
            return
        have = {slug(p.title) for p in ob.prompts}
        add = []
        for spec in ONBOARDING_PROMPTS:
            if slug(spec["title"]) in have:
                print(f"  ok      {spec['title']}")
                continue
            options = []
            for o in spec["options"]:
                role = self.role(o["role"])
                if role is None:
                    print(f"! skip    {spec['title']} -> {o['title']}: no @{o['role']} role yet "
                          "(re-run once it exists)")
                    continue
                options.append(discord.OnboardingPromptOption(
                    title=o["title"], emoji=o["emoji"], description=o.get("description"), roles=[role]))
            if options:
                add.append((spec, options))
        if not add:
            return
        if len(ob.prompts) + len(add) > MAX_PROMPTS:
            print(f"! skip    Onboarding already has {len(ob.prompts)} prompts (Discord allows {MAX_PROMPTS})")
            return
        def build(pre_join_ok):
            # edit_onboarding replaces the whole list, so the existing prompts go first, as they are.
            return [*ob.prompts, *(
                discord.OnboardingPrompt(type=discord.OnboardingPromptType.multiple_choice, title=spec["title"],
                                         options=options, single_select=not spec["multi"], required=False,
                                         in_onboarding=pre_join_ok and spec.get("pre_join", False))
                for spec, options in add)]

        async def edit():
            try:
                return await self.guild.edit_onboarding(prompts=build(True))
            except discord.HTTPException as e:
                if TOO_MANY_QUESTIONS not in (e.text or str(e)):
                    raise
                print("  retry   Discord caps pre-join questions: adding them under Channels & Roles instead")
                return await self.guild.edit_onboarding(prompts=build(False))

        await self.do("add", "onboarding prompts: " + "; ".join(
            f"{spec['title']} ({', '.join(o.title for o in options)})" for spec, options in add), edit)

    # ------------------------------------------------------------ automod gaps
    @staticmethod
    def blocks_invites(rule) -> bool:
        t = rule.trigger
        if t.type != discord.AutoModRuleTriggerType.keyword:
            return False
        text = " ".join([*t.keyword_filter, *t.regex_patterns]).lower()
        return any(m in text for m in INVITE_MARKERS)

    async def own_invite_codes(self):
        """This server's permanent invites (and vanity URL): sharing those is fine."""
        codes = []
        try:
            codes = [i.code for i in await self.guild.invites() if i.max_age == 0]
        except discord.HTTPException as e:
            print(f"  note    couldn't list invites ({e.text or e}); own invites won't be allow-listed")
        vanity = getattr(self.guild, "vanity_url_code", None)
        if vanity:
            codes.append(vanity)
        return [f"discord.gg/{c}" for c in dict.fromkeys(codes)][:MAX_ALLOW_LIST]

    async def automod_gaps(self):
        print("\nAutoMod gaps")
        try:
            rules = await self.guild.fetch_automod_rules()
        except discord.HTTPException as e:
            print(f"  skip    ({e.text or e})")
            return
        T = discord.AutoModRuleTriggerType
        have = {r.trigger.type for r in rules}
        alerts = self.channel(AUTOMOD_ALERTS)
        if alerts is None:
            print(f"! skip    no {AUTOMOD_ALERTS} channel for alerts (run setup_server.py)")
            return
        exempt_roles = [r for r in map(self.role, layout.AUTOMOD_EXEMPT_ROLES) if r]
        staff = self.channel(AUTOMOD_EXEMPT_CATEGORY)
        exempt_channels = [staff] if staff is not None else []
        alert = discord.AutoModRuleAction(channel_id=alerts.id)

        def block(msg=AUTOMOD_BLOCKED):
            return discord.AutoModRuleAction(custom_message=msg)

        wanted = []  # (name, trigger, actions, summary)
        if T.spam in have:
            print("  ok      spam filter")
        else:
            wanted.append(("Front Desk: spam", discord.AutoModTrigger(type=T.spam), [block(), alert],
                           "suspected spam"))
        if T.mention_spam in have:
            print("  ok      mention spam limit")
        else:
            wanted.append(("Front Desk: mention raids",
                           discord.AutoModTrigger(mention_limit=layout.MENTION_LIMIT, mention_raid_protection=True),
                           [block(), alert, discord.AutoModRuleAction(duration=datetime.timedelta(minutes=10))],
                           f"more than {layout.MENTION_LIMIT} mentions, raid protection, 10 min timeout"))
        preset = next((r for r in rules if r.trigger.type == T.keyword_preset), None)
        if preset is None:
            wanted.append(("Front Desk: slurs",
                           discord.AutoModTrigger(presets=discord.AutoModPresets(slurs=True, sexual_content=True)),
                           [block(), alert], "slurs and sexual content presets"))
        else:
            lacking = [p for p in ("slurs", "sexual_content") if not getattr(preset.trigger.presets, p)]
            if lacking:  # Discord allows one preset rule; this step never edits it
                print(f"! note    {preset.name} lacks the {' and '.join(p.replace('_', ' ') for p in lacking)} "
                      "preset: turn it on in Server Settings -> AutoMod")
            else:
                print("  ok      slur and sexual-content presets")
        if any(self.blocks_invites(r) for r in rules):
            print("  ok      invite links")
        elif sum(r.trigger.type == T.keyword for r in rules) >= MAX_KEYWORD_RULES:
            print(f"! skip    invite links: the server already has {MAX_KEYWORD_RULES} keyword rules (Discord's limit)")
        else:
            allow = await self.own_invite_codes()
            wanted.append((AUTOMOD_INVITES,
                           discord.AutoModTrigger(type=T.keyword, regex_patterns=INVITE_PATTERNS, allow_list=allow),
                           [block(AUTOMOD_INVITE_BLOCKED), alert],
                           f"invite links to other servers ({len(allow)} of ours allowed)"))
        exempt = ", ".join([*(f"@{r.name}" for r in exempt_roles), *(c.name for c in exempt_channels)]) or "nobody"
        for name, trigger, actions, summary in wanted:
            await self.do("create", f"{name}: {summary}; alerts to #{alerts.name}; exempt {exempt}",
                          lambda name=name, trigger=trigger, actions=actions: self.guild.create_automod_rule(
                              name=name, event_type=discord.AutoModRuleEventType.message_send, trigger=trigger,
                              actions=actions, enabled=True, exempt_roles=exempt_roles,
                              exempt_channels=exempt_channels, reason="Public server: AutoMod gaps"))

    async def run(self) -> bool:
        mode = "APPLYING" if self.apply else "DRY RUN (nothing changes; add --apply to do it)"
        print(f"{self.guild.name}: {mode}")
        if not self.preflight():
            return False
        await self.roles()
        await self.new_channels()
        await self.private()
        await self.description()
        await self.posts()
        await self.invite()
        await self.onboarding()
        await self.onboarding_prompts()
        await self.automod_gaps()
        print("\nDone." if self.apply else "\nThat's the plan. Run again with --apply.")
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    args = parser.parse_args()
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
            result["ok"] = await PublicMode(guild, args.apply).run()
        except discord.HTTPException as e:
            print(f"\nDiscord refused a change: {e}")
        finally:
            await client.close()

    client.run(token, log_handler=None)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
