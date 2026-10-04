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
"""

import argparse
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

import layout
from names import slug
from setup_server import Makeover, find

sys.stdout.reconfigure(encoding="utf-8")

BOT_ROLES = ("Season Champ", "Hype", "Clip of the Week", "Recruiter", "Bumper")  # roles Front Desk hands out
NEEDED = ("manage_roles", "manage_channels", "manage_guild", "create_instant_invite")


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
            if self.role(name):
                print(f"  ok      @{name}")
                continue
            await self.do("create", f"@{name}", lambda: self.guild.create_role(
                name=name, mentionable=spec.get("mentionable", False), reason="Public server"))

    # ------------------------------------------------------------ new public channels
    async def new_channels(self):
        print("\nNew channels")
        keeper = self.role("Keeper")
        for cat_spec in layout.CATEGORIES:
            if cat_spec.get("private_to"):
                continue  # handled by private()
            cat = find(self.guild.categories, cat_spec)
            for spec in cat_spec["channels"]:
                if spec.get("private_to") or spec.get("type") == "forum":
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

    # ------------------------------------------------------------ private spaces
    def private_overwrites(self, role_names):
        ow = {
            self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            self.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                       embed_links=True, connect=True),
        }
        for name in role_names:
            r = self.role(name)
            if r is None:
                print(f"! role @{name} not found; it won't see the private space")
                continue
            ow[r] = discord.PermissionOverwrite(view_channel=True)
        return ow

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
