"""Second pass after setup_server.py: the built-in Discord features.

    python polish_server.py           # dry run
    python polish_server.py --apply

- trims empty channels listed in layout.TRIM (never ones with messages or people in them)
- safety defaults: @mentions-only notifications, Medium verification, media scanning
- turns on Community, then Onboarding so members pick their own games and channels
- Welcome Screen with the five channels new members should see first
- AutoMod: spam, mention raids, slurs and sexual content (trash talk is fine)
- revokes never-expiring invites the bot made (short-lived links are safer)
- media lock: @everyone can't post images/files/link embeds until @Verified
"""

import argparse
import datetime
import os
import sys

import discord
from dotenv import load_dotenv
from pathlib import Path

import layout
from names import slug

sys.stdout.reconfigure(encoding="utf-8")

# Discord allows one preset-word rule per server, so slurs + sexual content share one.
AUTOMOD_RULES = ["Front Desk: spam", "Front Desk: mention raids", "Front Desk: slurs"]


class Polish:
    def __init__(self, guild: discord.Guild, apply: bool):
        self.guild = guild
        self.apply = apply

    async def do(self, verb, what, action):
        print(f"  {verb:<7} {what}")
        if self.apply:
            return await action()
        return None

    def channel(self, name):
        target = slug(name)
        return next((c for c in self.guild.channels if slug(c.name) == target), None)

    def role(self, name):
        return discord.utils.get(self.guild.roles, name=name)

    # ------------------------------------------------------------ trim
    async def trim(self):
        print("\nTrim")
        for name in layout.TRIM:
            ch = next(
                (c for c in self.guild.channels if c.name == name and not isinstance(c, discord.ForumChannel)),
                None,
            )
            if ch is None:
                print(f"  ok      {name} (already gone)")
                continue
            if isinstance(ch, discord.VoiceChannel):
                busy = bool(ch.members)
            else:
                busy = bool([m async for m in ch.history(limit=1)])
            if busy:
                print(f"  keep    {name} (in use)")
                continue
            await self.do("delete", f"{name} (empty)", lambda: ch.delete(reason="Trimmed: empty"))

    # ------------------------------------------------------------ settings
    async def settings(self):
        print("\nSafety defaults")
        g = self.guild
        kwargs = {}
        if g.default_notifications != discord.NotificationLevel.only_mentions:
            kwargs["default_notifications"] = discord.NotificationLevel.only_mentions
        if g.verification_level < discord.VerificationLevel.medium:
            kwargs["verification_level"] = discord.VerificationLevel.medium
        if g.explicit_content_filter != discord.ContentFilter.all_members:
            kwargs["explicit_content_filter"] = discord.ContentFilter.all_members
        if not kwargs:
            print("  ok      notifications, verification, media scanning")
            return
        what = ", ".join(k.replace("_", " ") for k in kwargs)
        await self.do("update", what, lambda: g.edit(**kwargs))

    async def community(self):
        print("\nCommunity")
        rules = self.channel(layout.COMMUNITY["rules_channel"])
        updates = self.channel(layout.COMMUNITY["updates_channel"])
        if "COMMUNITY" in self.guild.features:
            print("  ok      already a Community server")
            return
        await self.do(
            "enable",
            f"Community (rules: #{rules.name}, admin notices: #{updates.name})",
            lambda: self.guild.edit(community=True, rules_channel=rules, public_updates_channel=updates),
        )

    # ------------------------------------------------------------ onboarding
    async def onboarding(self):
        print("\nOnboarding")
        defaults = [self.channel(n) for n in layout.ONBOARDING_DEFAULT_CHANNELS]
        missing = [n for n, c in zip(layout.ONBOARDING_DEFAULT_CHANNELS, defaults) if c is None]
        if missing:
            print(f"! missing default channels: {', '.join(missing)}")
            return

        prompts = []
        for p in layout.ONBOARDING_PROMPTS:
            options = []
            for o in p["options"]:
                ch = self.channel(o["channel"]) if o.get("channel") else None
                options.append(
                    discord.OnboardingPromptOption(
                        title=o["title"],
                        emoji=o.get("emoji", discord.utils.MISSING),
                        description=o.get("description"),
                        roles=[self.role(o["role"])] if o.get("role") else [],
                        channels=[ch] if ch else [],
                    )
                )
                role = f" (@{o['role']})" if o.get("role") else ""
                print(f"  option  {p['title']} -> {o['emoji']} {o['title']}{role}")
            prompts.append(
                discord.OnboardingPrompt(
                    type=discord.OnboardingPromptType.multiple_choice,
                    title=p["title"],
                    options=options,
                    single_select=not p.get("multi"),
                    required=p.get("required", False),
                )
            )
        await self.do(
            "enable",
            f"onboarding with {len(defaults)} default channels",
            lambda: self.guild.edit_onboarding(prompts=prompts, default_channels=defaults, enabled=True),
        )

    # ------------------------------------------------------------ automod
    async def automod(self):
        print("\nAutoMod")
        existing = {r.name: r for r in await self.guild.fetch_automod_rules()}
        alerts = self.channel(layout.AUTOMOD_ALERTS)
        exempt = [r for r in map(self.role, layout.AUTOMOD_EXEMPT_ROLES) if r]
        alert = discord.AutoModRuleAction(channel_id=alerts.id)
        block = discord.AutoModRuleAction(custom_message="Blocked by AutoMod. Ask a Keeper if that was a mistake.")

        rules = [
            (AUTOMOD_RULES[0], discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam), [block, alert]),
            (
                AUTOMOD_RULES[1],
                discord.AutoModTrigger(mention_limit=layout.MENTION_LIMIT, mention_raid_protection=True),
                [block, alert, discord.AutoModRuleAction(duration=datetime.timedelta(minutes=10))],
            ),
            (
                AUTOMOD_RULES[2],
                discord.AutoModTrigger(presets=discord.AutoModPresets(slurs=True, sexual_content=True)),
                [block, alert],
            ),
        ]
        for name, trigger, actions in rules:
            if name in existing:
                rule = existing[name]
                if trigger.presets is not None and rule.trigger.presets != trigger.presets:
                    await self.do("update", f"{name}: presets", lambda: rule.edit(trigger=trigger))
                else:
                    print(f"  ok      {name}")
                continue
            await self.do(
                "create",
                name,
                lambda: self.guild.create_automod_rule(
                    name=name,
                    event_type=discord.AutoModRuleEventType.message_send,
                    trigger=trigger,
                    actions=actions,
                    enabled=True,
                    exempt_roles=exempt,
                ),
            )

    # ------------------------------------------------------------ welcome screen
    async def welcome_screen(self):
        print("\nWelcome Screen")
        cfg = layout.WELCOME_SCREEN
        chans = []
        for name, emoji, desc in cfg["channels"]:
            ch = self.channel(name)
            if ch is None:
                print(f"! missing channel {name}")
                return
            chans.append(discord.WelcomeChannel(channel=ch, description=desc, emoji=emoji))
            print(f"  channel {emoji} {name}: {desc}")
        await self.do(
            "enable",
            f"welcome screen with {len(chans)} channels",
            lambda: self.guild.edit_welcome_screen(description=cfg["description"], welcome_channels=chans, enabled=True),
        )

    # ------------------------------------------------------------ media lock
    async def media_lock(self):
        print("\nMedia lock")
        media = discord.Permissions(attach_files=True, embed_links=True)
        everyone = self.guild.default_role
        if everyone.permissions.attach_files or everyone.permissions.embed_links:
            locked = discord.Permissions(everyone.permissions.value & ~media.value)
            await self.do("update", "@everyone: no images, files or link embeds", lambda: everyone.edit(permissions=locked))
        else:
            print("  ok      @everyone is locked")
        for name in layout.MEDIA_ROLES:
            role = self.role(name)
            if role is None:
                print(f"! missing role @{name} (run setup_server.py first)")
                continue
            if role.permissions.administrator or (role.permissions.value & media.value) == media.value:
                print(f"  ok      @{name} can post media")
                continue
            granted = discord.Permissions(role.permissions.value | media.value)
            await self.do("update", f"@{name}: allow images, files and embeds", lambda: role.edit(permissions=granted))

    # ------------------------------------------------------------ bot role
    async def bot_role(self):
        print("\nBots")
        role = self.role(layout.BOT_ROLE)
        async for m in self.guild.fetch_members(limit=None):
            if not m.bot or m.id == self.guild.me.id:
                continue
            if role in m.roles:
                print(f"  ok      {m.name} has @{role.name}")
            else:
                await self.do("give", f"@{role.name} to {m.name}", lambda: m.add_roles(role))

    # ------------------------------------------------------------ invites
    async def invites(self):
        print("\nInvites")
        if not layout.REVOKE_PERMANENT_INVITES:
            print("  ok      leaving invites alone")
            return
        permanent = [i for i in await self.guild.invites() if i.max_age == 0 and i.inviter and i.inviter.id == self.guild.me.id]
        if not permanent:
            print("  ok      no permanent invites from the bot")
        for inv in permanent:
            await self.do("revoke", f"permanent invite {inv.code}", lambda: inv.delete(reason="Use short-lived invites"))

    async def run(self) -> bool:
        mode = "APPLYING" if self.apply else "DRY RUN (nothing changes; add --apply to do it)"
        print(f"{self.guild.name}: {mode}")
        if not self.guild.me.guild_permissions.administrator:
            print("! The bot needs Administrator (Community can only be toggled by admins).")
            return False
        await self.trim()
        await self.settings()
        await self.community()
        await self.onboarding()
        await self.welcome_screen()
        await self.automod()
        await self.invites()
        await self.media_lock()
        await self.bot_role()
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

    intents = discord.Intents.default()
    intents.members = True  # to find bot members; enable "Server Members Intent" in the portal
    client = discord.Client(intents=intents)
    result = {"ok": False}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                print(f"The bot isn't in server {guild_id}. Invite it with:")
                print(
                    f"  https://discord.com/oauth2/authorize?client_id={client.application_id}"
                    "&scope=bot&permissions=8"
                )
                return
            result["ok"] = await Polish(guild, args.apply).run()
        except discord.HTTPException as e:
            print(f"\nDiscord refused a change: {e}")
        finally:
            await client.close()

    client.run(token, log_handler=None)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
