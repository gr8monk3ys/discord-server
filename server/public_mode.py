"""Open the server to the public, additively. Safe to re-run.

    python public_mode.py           # dry run: prints what would change
    python public_mode.py --apply   # do it

Unlike setup_server.py this never reorders or deletes anything, so it works
with Front Desk sitting mid-stack (below Keeper/Moderator) and without
Administrator. It needs Manage Roles, Manage Channels, Manage Server and
Create Invite on Front Desk's role.

Steps, all driven by layout.py:
- create the roles Front Desk hands out (Clip of the Week, Recruiter, Bumper, the house roles,
  Welcome Crew, Supporter, ...); new roles land at the bottom of the list and nothing is moved
- rename a channel still under one of its layout "was" names to its layout name (name only;
  e.g. #realms -> #monkey-smp)
- create any public layout channel that doesn't exist yet (never moves or edits existing ones),
  forums with their tags, layout, sort order, default reaction and slowmode, and the Stage channel
- create private categories/channels ("private_to") and keep them private
- set the server description and the welcome-screen text (layout.PUBLIC)
- refresh the rules and welcome posts in place
- keep one permanent invite for listings and print it
- add any missing Onboarding default channels
- add the self-assign Onboarding prompts (platform, region, Game Night pings) if missing
- games (layout.GAMES): create a listed game's role and channel if missing, add its tag to the
  forums that tag by game, and add it to the "What do you play?" question. When that edit adds
  a game, the options of retired games leave the question in the same edit (Discord caps the
  options of one question, so new ones may not fit beside them)
- restore_games: a listed game whose channel sits in the archive (it was retired, and is listed
  again) gets its channel moved back to the games category
- retire_games (layout.RETIRED_GAMES): the game leaves the "What do you play?" question, if the
  games step has not already taken it out, and its channel moves into the staff-only archive
  category (layout.GAME_ARCHIVE). RETIRING A GAME IS THE ONE THING HERE THAT TAKES SOMETHING
  AWAY, in those two places: the Onboarding option (games step or this one) and the channel
  (only this one). Nothing is deleted: the channel keeps its messages, the role stays on the
  server and on every member who has it, and old forum tags stay on old posts. The dry run
  prints every option that would leave and every channel it would hide. Moving the game's line
  back into GAMES undoes it.
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
from setup_server import Makeover, find, forum_options, layout_channels, private_overwrites

sys.stdout.reconfigure(encoding="utf-8")

BOT_ROLES = ("Season Champ", "Hype", "Birthday", "Tournament Champ", "Event Champ", "Counting Champ", "Clip of the Week", "Recruiter", "Bumper",
             "Game Night", "Mythic", "Legend", "Elite", "Veteran", "Regular",
             "PC", "PlayStation", "Xbox", "Switch", "Mobile", "NA", "EU", "LATAM", "Asia", "OCE",
             "Early Bird", "Night Owl", "Weekend Warrior",
             "Welcome Crew", "Supporter", "SMP Ping")  # roles Front Desk hands out
BOT_ROLES += tuple(layout.HOUSE_ROLES.values())  # the three house roles (created at the bottom)
BOT_ROLES += tuple(role for _, _, role in layout.GAMES)  # a newly listed game's ping role (never a retired one)
GAMES_PROMPT = "What do you play?"  # the Onboarding question whose options are the games
MAX_FORUM_TAGS = 20  # Discord's limit per forum
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
# Alerts go to layout.AUTOMOD_ALERTS, the same channel polish_server.py's rules use;
# Keeper/Moderator and the staff category are exempt. Front Desk itself has Manage Server, which
# AutoMod always exempts, so its partner posts are never blocked.
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
        # Roles made in this run: guild.roles only learns about them from the gateway a moment
        # later, and the private channels made further down need them straight away.
        self.made_roles: dict[str, discord.Role] = {}
        self.made_channels: dict[str, object] = {}  # slug -> text channel made in this run, same reason

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
                # No position: Discord puts a new role at the bottom, below Front Desk, and
                # nothing is ever moved.
                made = await self.do("create", f"@{name}", lambda: self.guild.create_role(
                    name=name, colour=discord.Colour(spec.get("color", 0)), hoist=hoist,
                    mentionable=spec.get("mentionable", False), reason="Public server"))
                if made is not None:
                    self.made_roles[name] = made
            elif role.hoist != hoist and role < self.guild.me.top_role:
                await self.do("update", f"@{name}: {'hoist' if hoist else 'unhoist'}",
                              lambda: role.edit(hoist=hoist, reason="Public server"))
            else:
                print(f"  ok      @{name}")

    # ------------------------------------------------------------ renamed channels
    async def renamed_channels(self):
        """A channel still under one of its layout "was" names gets its layout name. Name only:
        nothing is moved, restyled or re-permissioned, and messages stay where they are."""
        print("\nRenamed channels")
        for spec in layout_channels():
            if not spec.get("was") or self.channel(spec["name"]) is not None:
                continue
            pool = [*self.guild.text_channels, *self.guild.voice_channels, *self.guild.forums,
                    *self.guild.stage_channels]  # not categories
            old = next((c for c in pool if slug(c.name) in {slug(w) for w in spec["was"]}), None)
            if old is not None:
                await self.do("rename", f"#{old.name} -> #{spec['name']}",
                              lambda old=old, spec=spec: old.edit(name=spec["name"], reason="Public server: renamed"))

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
                if spec.get("type") == "stage":
                    await self.new_stage(spec, cat, cat_spec["name"])
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
                made = await self.do("create", f"{label} in {cat.name}" + (" (read-only)" if spec.get("read_only") else ""),
                                     lambda: create(spec["name"], category=cat, **kwargs))
                if made is not None and not voice:
                    self.made_channels[slug(spec["name"])] = made

    async def new_forum(self, spec, cat, cat_name):
        if find(self.guild.forums, spec) is not None:
            return
        label = f"#{spec['name']} (forum)"
        if cat is None:
            print(f"! skip    {label}: category {cat_name} doesn't exist (run setup_server.py)")
            return
        tags = [discord.ForumTag(name=n, emoji=e) for n, e in spec.get("tags", [])]
        options = forum_options(spec)
        extras = ", ".join(k for k in ("layout", "sort", "reaction", "post_slowmode", "slowmode") if k in spec)
        await self.do("create", f"{label} in {cat.name} with {len(tags)} tags" + (f" ({extras})" if extras else ""),
                      lambda: self.guild.create_forum(spec["name"], category=cat, topic=spec.get("topic", ""),
                                                      available_tags=tags, **options))

    # Scheduling a Stage event needs these in the stage channel, on top of Create Events.
    STAGE_EVENT_PERMS = ("manage_channels", "mute_members", "move_members")

    async def new_stage(self, spec, cat, cat_name):
        """Create a missing Stage channel (inherits the category's permissions, like the voice
        channels). Front Desk can't grant itself a permission it doesn't hold, so a missing
        one is only reported: the owner turns it on for the Front Desk role."""
        ch = find(getattr(self.guild, "stage_channels", []), spec)
        label = f"{spec['name']} (stage)"
        if ch is None:
            if cat is None:
                print(f"! skip    {label}: category {cat_name} doesn't exist (run setup_server.py)")
                return
            await self.do("create", f"{label} in {cat.name}",
                          lambda: self.guild.create_stage_channel(spec["name"], category=cat))
            perms = getattr(self.guild.me, "guild_permissions", None)
        else:
            perms = ch.permissions_for(self.guild.me)
        missing = [p for p in self.STAGE_EVENT_PERMS if not getattr(perms, p, False)]
        if missing:
            print(f"! note    {label}: Front Desk can't schedule Stage events without "
                  + ", ".join(p.replace("_", " ").title() for p in missing)
                  + " (Server Settings -> Roles -> Front Desk). Watch parties use the Lobby until then.")

    # ------------------------------------------------------------ private spaces
    def private_overwrites(self, role_names):
        return private_overwrites(self.guild, role_names, known=self.made_roles)

    @staticmethod
    def is_private(ch, ow) -> bool:
        return all(ch.overwrites_for(target) == want for target, want in ow.items())

    # ------------------------------------------------------------ bot can post
    BOT_POST = ("send_messages", "embed_links", "attach_files")
    # Extra permissions a feature needs in one read-only channel (tickets are private threads).
    BOT_POST_EXTRA = {"🆘・help": ("create_private_threads", "send_messages_in_threads", "manage_threads")}

    async def bot_posting(self):
        """Read-only channels made before Front Desk existed deny everyone but Keepers, the
        bot included: welcome cards, recaps and milestones then fail. Add (never remove) a
        member overwrite that lets the bot post there."""
        print("\nBot can post in read-only channels")
        me = self.guild.me
        for cat_spec in layout.CATEGORIES:
            for spec in cat_spec["channels"]:
                if not spec.get("read_only"):
                    continue
                ch = find(self.guild.text_channels, spec)
                if ch is None:
                    continue
                perms = ch.permissions_for(me)
                want = (*self.BOT_POST, *next(
                    (extra for name, extra in self.BOT_POST_EXTRA.items() if slug(name) == slug(spec["name"])), ()))
                missing = [p for p in want if not getattr(perms, p)]
                if not missing:
                    print(f"  ok      #{spec['name']}")
                    continue
                ow = ch.overwrites_for(me)
                ow.update(**{p: True for p in want})
                await self.do("allow", f"#{spec['name']}: Front Desk may " + ", ".join(
                    p.replace("_", " ") for p in missing),
                    lambda ch=ch, ow=ow: ch.set_permissions(me, overwrite=ow, reason="Front Desk posts here"))

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

    # ------------------------------------------------------------ games (layout.GAMES, layout.RETIRED_GAMES)
    @staticmethod
    def game_channel_name(game) -> str:
        emoji, channel, _role = game
        return f"{emoji}・{channel}"

    def game_channel(self, game):
        """The game's text channel wherever it sits (games category or archive), or one made in this run."""
        name = self.game_channel_name(game)
        return find(self.guild.text_channels, {"name": name}) or self.made_channels.get(slug(name))

    def archive(self):
        return find(self.guild.categories, layout.GAME_ARCHIVE)

    def games_category(self):
        """The category the layout puts game channels in."""
        if not layout.GAMES:
            return None
        first = slug(self.game_channel_name(layout.GAMES[0]))
        spec = next((c for c in layout.CATEGORIES if any(slug(s["name"]) == first for s in c["channels"])), None)
        return find(self.guild.categories, spec) if spec else None

    async def games_prompt(self):
        """(onboarding, the "What do you play?" prompt or None, readable). readable is False
        when Discord wouldn't show Onboarding at all; the reason is printed."""
        try:
            ob = await self.guild.onboarding()
        except discord.HTTPException as e:
            print(f"  skip    Onboarding ({e.text or e})")
            return None, None, False
        prompt = next((p for p in ob.prompts if slug(p.title) == slug(GAMES_PROMPT)), None)
        return ob, prompt, True

    async def game_tags(self):
        """Forums that tag by game get a tag for each listed game. Additive: a retired game's
        tag is left where it is, so posts that carry it keep it."""
        print("\nGame tags")
        listed = {slug(role) for _, _, role in layout.GAMES}
        retired = {slug(role) for _, _, role in layout.RETIRED_GAMES}
        for cat_spec in layout.CATEGORIES:
            for spec in cat_spec["channels"]:
                wanted = [(n, e) for n, e in spec.get("tags", []) if slug(n) in listed]
                forum = find(self.guild.forums, spec) if spec.get("type") == "forum" and wanted else None
                if forum is None:
                    continue  # no game tags, or the forum doesn't exist yet (new_channels makes it with all of them)
                have = {slug(t.name) for t in forum.available_tags}
                missing = [(n, e) for n, e in wanted if slug(n) not in have]
                kept = [t.name for t in forum.available_tags if slug(t.name) in retired]
                if kept:
                    print(f"  keep    #{forum.name} tags of retired games: {', '.join(kept)} (posts that carry them keep them)")
                if not missing:
                    print(f"  ok      #{forum.name} game tags")
                    continue
                if len(forum.available_tags) + len(missing) > MAX_FORUM_TAGS:
                    print(f"! skip    #{forum.name}: {len(missing)} more tags would pass Discord's limit of "
                          f"{MAX_FORUM_TAGS}. Remove a tag by hand, then re-run.")
                    continue
                # Existing tags go first, as they are, so tagged posts stay tagged.
                tags = [*forum.available_tags, *(discord.ForumTag(name=n, emoji=e) for n, e in missing)]
                await self.do("add", f"#{forum.name} tags: {', '.join(n for n, _ in missing)}",
                              lambda forum=forum, tags=tags: forum.edit(available_tags=tags))

    async def restore_games(self):
        """Un-retire: a listed game whose channel is in the archive gets it back in the games
        category, with that category's permissions."""
        print("\nGames listed again")
        archive = self.archive()
        back = []
        for game in layout.GAMES if archive is not None else ():
            ch = find(self.guild.text_channels, {"name": self.game_channel_name(game)})
            if ch is not None and ch.category_id == archive.id:
                back.append(ch)
        if not back:
            print("  ok      no listed game has its channel in the archive")
            return
        home = self.games_category()
        if home is None:
            print(f"! skip    {', '.join('#' + ch.name for ch in back)}: the games category doesn't exist "
                  "(run setup_server.py)")
            return
        for ch in back:
            await self.do("restore", f"#{ch.name}: move from {archive.name} back into {home.name} "
                                     "(public again, with that category's permissions)",
                          lambda ch=ch: ch.edit(category=home, sync_permissions=True,
                                                reason="Game listed again (layout.GAMES)"))

    async def game_options(self):
        """Every listed game is an option of the "What do you play?" question. Options that are
        already there are kept as they are, with one exception: when this adds a game, the
        options of retired games are dropped in the same edit (see below). With nothing to add
        it changes nothing, and retire_games() removes them."""
        print("\nGames in Onboarding")
        ob, prompt, readable = await self.games_prompt()
        if not readable:
            return
        if prompt is None:
            print(f"  skip    no \"{GAMES_PROMPT}\" question (polish_server.py makes it)")
            return
        specs = {slug(o["role"]): o for p in layout.ONBOARDING_PROMPTS if slug(p["title"]) == slug(GAMES_PROMPT)
                 for o in p["options"] if o.get("role")}
        archive = self.archive()
        have = {slug(o.title): o for o in prompt.options}
        new, names = {}, []
        for game in layout.GAMES:
            emoji, _channel, role_name = game
            key = slug(role_name)
            if key in have:
                continue
            role = self.made_roles.get(role_name) or self.role(role_name)
            ch = self.game_channel(game)
            if not self.apply:
                names.append(role_name)  # whatever is missing is created earlier in the same run
                continue
            if role is None or ch is None:
                print(f"! skip    {role_name}: its role or channel doesn't exist yet (re-run once it does)")
                continue
            if archive is not None and getattr(ch, "category_id", None) == archive.id:
                print(f"! skip    {role_name}: #{ch.name} is still in {archive.name} (re-run)")
                continue
            spec = specs.get(key, {})
            new[key] = discord.OnboardingPromptOption(
                title=spec.get("title", role_name), emoji=spec.get("emoji", emoji),
                description=spec.get("description"), roles=[role], channels=[ch])
            names.append(role_name)
        if not names:
            print("  ok      every listed game is an option")
            return
        # Listed games sit together, in layout order, where the first of them was; every other
        # option ("Just here to hang") stays in place, except a retired game's, dropped below.
        order = [slug(role) for _, _, role in layout.GAMES]
        block = [have.get(k) or new[k] for k in order if k in have or k in new]
        merged, placed = [], False
        for o in prompt.options:
            if slug(o.title) not in order:
                merged.append(o)
            elif not placed:
                merged += block
                placed = True
        if not placed:
            merged = [*block, *merged]
        # Discord caps the options of one question, and the cap is checked on every edit: adding
        # the new games while the retired ones are still there can be refused ("Too many
        # options"). So the retired games' options leave in this same edit; retire_games()
        # then finds them gone and only has the channels left to hide.
        retired = {slug(role) for _, _, role in layout.RETIRED_GAMES} - set(order)
        swapped = [o.title for o in merged if slug(o.title) in retired]
        merged = [o for o in merged if slug(o.title) not in retired]

        async def edit():
            prompt.options = merged
            # Only the prompts are sent: default channels and the enabled flag are left alone.
            return await self.guild.edit_onboarding(prompts=ob.prompts)

        what = f"\"{GAMES_PROMPT}\" options: {', '.join(names)}"
        if swapped:
            what += f" (in the same edit, retired options leave: {', '.join(swapped)}; members who picked one keep the role)"
        await self.do("add", what, edit)

    async def retire_games(self):
        """Takes something away (so does game_options(), which drops a retired game's option when
        it adds a new game). For each game in layout.RETIRED_GAMES: its option leaves the "What
        do you play?" question if it is still there, then its channel moves into the staff-only
        archive category. Never deletes: messages, the role and who holds it are untouched, and
        restore_games() undoes the move once the game is back in GAMES."""
        print("\nRetired games (hidden, never deleted)")
        if not layout.RETIRED_GAMES:
            print("  ok      no retired games")
            return
        retired = {slug(role) for _, _, role in layout.RETIRED_GAMES}
        ob, prompt, readable = await self.games_prompt()
        if not readable:
            # A hidden channel that is still an Onboarding option can make Discord refuse the
            # next Onboarding edit, so nothing is hidden until the question can be checked.
            print("! skip    nothing hidden: the question has to be checked first (re-run)")
            return
        gone = [o for o in prompt.options if slug(o.title) in retired] if prompt is not None else []
        if not gone:
            print(f"  ok      no retired game in \"{GAMES_PROMPT}\"")
        elif len(gone) == len(prompt.options):
            print(f"! skip    nothing hidden: \"{GAMES_PROMPT}\" would be left without options")
            return
        else:
            async def edit():
                # By identity: discord.py compares options by id, and every hand-built one has id 0.
                prompt.options = [o for o in prompt.options if not any(o is g for g in gone)]
                return await self.guild.edit_onboarding(prompts=ob.prompts)

            await self.do("remove", f"\"{GAMES_PROMPT}\" options: {', '.join(o.title for o in gone)} "
                                    "(members who picked one keep the role)", edit)

        in_layout = {slug(s["name"]) for c in layout.CATEGORIES for s in c["channels"]}
        archive = self.archive()
        hide = []
        for game in layout.RETIRED_GAMES:
            name, role_name = self.game_channel_name(game), game[2]
            if slug(name) in in_layout:
                print(f"! skip    #{name}: the layout still lists it (is {role_name} in both GAMES and RETIRED_GAMES?)")
                continue
            ch = find(self.guild.text_channels, {"name": name})
            if ch is None:
                print(f"  ok      {role_name}: no channel")
            elif archive is not None and ch.category_id == archive.id:
                print(f"  ok      #{ch.name} (already in {archive.name})")
            else:
                hide.append(ch)
            if self.role(role_name) is not None:
                print(f"  keep    @{role_name} (the role stays, and so does every member's copy of it)")
        if not hide:
            return
        who = ", ".join(f"@{n}" for n in layout.GAME_ARCHIVE["private_to"])
        ow = self.private_overwrites(layout.GAME_ARCHIVE["private_to"])
        label = layout.GAME_ARCHIVE["name"]
        if archive is None:
            archive = await self.do("create", f"category {label} (hidden from @everyone; visible to {who})",
                                    lambda: self.guild.create_category(label, overwrites=ow))
        elif not self.is_private(archive, ow):
            await self.do("lock", f"category {archive.name} (hidden from @everyone; visible to {who})",
                          lambda: archive.edit(overwrites=ow))
        for ch in hide:
            src = getattr(self.guild.get_channel(ch.category_id), "name", None) if ch.category_id else None
            await self.do("hide", f"#{ch.name}: move from {src or 'no category'} into {label} "
                                  f"(only {who} see it; messages are kept)",
                          lambda ch=ch: ch.edit(category=archive, sync_permissions=True,
                                                reason="Game retired (layout.RETIRED_GAMES)"))

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
        alerts = self.channel(layout.AUTOMOD_ALERTS)
        if alerts is None:
            print(f"! skip    no {layout.AUTOMOD_ALERTS} channel for alerts (run setup_server.py)")
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
        await self.renamed_channels()
        await self.new_channels()
        await self.bot_posting()
        await self.private()
        await self.description()
        await self.posts()
        await self.invite()
        await self.onboarding()
        await self.onboarding_prompts()
        # Games. Order matters: a channel is public again before it becomes an Onboarding
        # option, and an option is gone before its channel is hidden.
        await self.game_tags()
        await self.restore_games()
        await self.game_options()
        await self.retire_games()
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
            # The gateway's copy of the server leaves the description out (always None); REST has it.
            guild.description = (await client.fetch_guild(guild.id)).description
            result["ok"] = await PublicMode(guild, args.apply).run()
        except discord.HTTPException as e:
            print(f"\nDiscord refused a change: {e}")
        finally:
            await client.close()

    client.run(token, log_handler=None)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
