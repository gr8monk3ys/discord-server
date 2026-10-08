"""Front Desk: the server's always-on bot (while Lorenzo's PC is on).

    python main.py            # run the bot
    python main.py --invite   # print the invite link (see README)
"""

import argparse
import base64
import logging
import sys
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands

import config
from db import Database
from errors import reply_error

# Logs go to stderr; emoji in channel names break the Windows codepage when
# output is redirected. Under pythonw (the startup task) there is no console at all.
for stream in (sys.stdout, sys.stderr):
    if stream is not None:
        stream.reconfigure(encoding="utf-8")
log = logging.getLogger("front_desk")

# Modules in build order. Each entry: (extension, privileged intents it needs).
MODULES = [
    ("cogs.lfg", set()),
    ("cogs.stats", {"members", "presences"}),  # game time; runs without them, gaming off
    ("cogs.growth", {"members", "message_content"}),  # invite tracking, Disboard bumps
    ("cogs.community", {"members"}),  # welcome flow, /report, mod log
    ("cogs.starboard", {"message_content"}),  # hall of fame
    ("cogs.clips", {"message_content"}),  # clip of the week
    ("cogs.tempvoice", set()),  # join-to-create voice
    ("cogs.events", set()),  # /gamenight, free games
    ("cogs.economy", {"message_content"}),  # coins: /daily, /balance, /give, /coinflip, /richest
    ("cogs.games", set()),  # /slots, /blackjack, /trivia, /predict
    ("cogs.shop", {"members"}),  # /shop, /buy, /season
    ("cogs.engagement", {"members", "message_content"}),  # QOTD, daily poll, counting, auto game night, birthdays
    ("cogs.moderation", {"members", "message_content"}),  # /warn /timeout /cases /purge, anti-spam, anti-raid
    ("cogs.ops", set()),  # backups, health alerts, config drift, /status
    ("cogs.utility", {"presences"}),  # /remind, /afk, suggestions, stat channels, tickets
    ("cogs.cards", {"members"}),  # welcome cards after Onboarding
    ("cogs.tournaments", set()),  # /tournament create/start/cancel/bracket, brackets with prizes
    ("cogs.achievements", {"members"}),  # badges, /profile, /badges
    ("cogs.creators", set()),  # /creator link|verify|approve|unlink|list|remove, YouTube + Twitch alerts
    ("cogs.levels", {"members", "message_content"}),  # XP from chat + voice, level roles, /rank, /levels, /xp
    ("cogs.recap", {"members"}),  # weekly recap, owner digest, member milestones, monthly invite contest
    ("cogs.selfroles", set()),  # #roles self-assign panel + /roles
    ("cogs.partners", set()),  # /partner apply|remove, partner review cards, weekly dead-invite sweep
    ("cogs.quests", {"members"}),  # starter quest: /quest, 500-coin reward, day-1 nudge DM
    ("cogs.heartbeat", set()),  # heartbeat file for server/watchdog.py
]

PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    create_public_threads=True,
    manage_threads=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    add_reactions=True,
    manage_roles=True,
    send_polls=True,
    # Public server: invite tracking, temp voice, events, listings.
    manage_guild=True,
    manage_channels=True,
    create_instant_invite=True,
    move_members=True,
    manage_events=True,
    # Moderation autopilot: timeouts and spam cleanup.
    moderate_members=True,
    manage_messages=True,
)

def build_intents(privileged: bool = True) -> discord.Intents:
    """Only ask for the privileged intents that loaded modules need, so the bot
    still connects before they're switched on in the Developer Portal."""
    intents = discord.Intents.default()
    if privileged:
        for _, needs in MODULES:
            for name in needs:
                setattr(intents, name, True)
    return intents


class FrontDesk(commands.Bot):
    def __init__(self, settings: config.Settings, privileged: bool = True, sync: bool = True):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(privileged),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.settings = settings
        self.sync_commands = sync
        self.guild_ref = discord.Object(id=settings.guild_id)
        self.db = Database(config.DATA_DIR / "front_desk.db")

    async def setup_hook(self) -> None:
        await self.db.connect()
        version = await self.db.migrate()
        log.info("database ready (schema v%s)", version)
        self.tree.on_error = self.on_app_command_error
        for extension, _ in MODULES:
            await self.load_extension(extension)
        if self.sync_commands:  # the fallback restart reuses the commands just synced
            self.tree.copy_global_to(guild=self.guild_ref)
            synced = await self.tree.sync(guild=self.guild_ref)
            log.info("synced %d commands: %s", len(synced), ", ".join(c.name for c in synced))

    async def on_ready(self) -> None:
        if self.get_guild(self.settings.guild_id) is None:
            log.error("Front Desk isn't in the server (GUILD_ID %s). Invite it: python main.py --invite",
                      self.settings.guild_id)
        else:
            log.info("logged in as %s", self.user)

    async def close(self) -> None:
        await super().close()  # unloads cogs and stops loops first
        await self.db.close()

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        log.error("/%s failed", interaction.command.name if interaction.command else "?", exc_info=error)
        await reply_error(interaction)


def app_id_from_token(token: str) -> int:
    """The token's first segment is the bot's user id, base64-encoded."""
    first = token.split(".")[0]
    return int(base64.b64decode(first + "=" * (-len(first) % 4)))


def setup_logging() -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(config.DATA_DIR / "bot.log", maxBytes=1_000_000,
                                       backupCount=3, encoding="utf-8")
    if sys.stderr is not None:
        discord.utils.setup_logging(level=logging.INFO)  # console
    discord.utils.setup_logging(handler=file_handler, level=logging.INFO, root=True)


def single_instance_lock():
    """Hold an exclusive lock on data/bot.lock for the life of the process, so a
    second copy (startup task + run_bot.bat) can't answer every click twice."""
    config.DATA_DIR.mkdir(exist_ok=True)
    handle = open(config.DATA_DIR / "bot.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit("Front Desk is already running (another process holds data/bot.lock).")
    return handle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--invite", action="store_true", help="print the invite link and exit")
    args = parser.parse_args()
    settings = config.load_settings()

    if args.invite:
        print(discord.utils.oauth_url(
            app_id_from_token(settings.token),
            permissions=PERMISSIONS,
            guild=discord.Object(id=settings.guild_id),
            scopes=("bot", "applications.commands"),
            disable_guild_select=True,
        ))
        return

    lock = single_instance_lock()  # noqa: F841 (held until exit)
    setup_logging()
    try:
        FrontDesk(settings).run(settings.token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.warning("Server Members / Presence intents aren't enabled in the Developer Portal; "
                    "running without them (game time is off until they are).")
        FrontDesk(settings, privileged=False, sync=False).run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
