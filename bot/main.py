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

sys.stdout.reconfigure(encoding="utf-8")
log = logging.getLogger("front_desk")

# Modules in build order. Each entry: (extension, privileged intents it needs).
MODULES = [
    ("cogs.lfg", set()),
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
)

def build_intents() -> discord.Intents:
    """Only ask for the privileged intents that loaded modules need, so the bot
    still connects before they're switched on in the Developer Portal."""
    intents = discord.Intents.default()
    for _, needs in MODULES:
        for name in needs:
            setattr(intents, name, True)
    return intents


class FrontDesk(commands.Bot):
    def __init__(self, settings: config.Settings):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.settings = settings
        self.guild_ref = discord.Object(id=settings.guild_id)
        self.db = Database(config.DATA_DIR / "front_desk.db")

    async def setup_hook(self) -> None:
        await self.db.connect()
        version = await self.db.migrate()
        log.info("database ready (schema v%s)", version)
        self.tree.on_error = self.on_app_command_error
        for extension, _ in MODULES:
            await self.load_extension(extension)
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
        await self.db.close()
        await super().close()

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
    discord.utils.setup_logging(level=logging.INFO)  # console
    discord.utils.setup_logging(handler=file_handler, level=logging.INFO, root=True)


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

    setup_logging()
    FrontDesk(settings).run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
