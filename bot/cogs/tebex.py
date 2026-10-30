"""Tebex cancellation alerts (rules in logic/tebex.py): a small HTTP receiver for Tebex's webhooks.

Off unless TEBEX_WEBHOOK_SECRET is set in server/.env. Then it listens on TEBEX_WEBHOOK_HOST
(default 127.0.0.1) and TEBEX_WEBHOOK_PORT (default 8790), at POST /tebex/webhook only. Making it
reachable from the internet (a tunnel or reverse proxy with HTTPS) is the owner's step:
docs/TEBEX_WEBHOOK.md.

- Every request must carry a valid X-Signature (HMAC of the body with the secret); anything else is
  401 and nothing is read. The secret is never logged, and request lines are not logged at all.
- `validation.webhook` is answered with its id, as Tebex asks.
- `recurring-payment.cancellation.requested`: one post in 🛡️・mod with the player, the package and the
  end date. The webhook id is stored first, so Tebex's retries post once; if the post fails the id
  is dropped again and the answer is 500, so Tebex retries later.
- Other types: 200, ignored.
"""

import logging
import os
import time

import discord
from aiohttp import web
from discord.ext import commands

import config
from logic import tebex as L

log = logging.getLogger(__name__)


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


class Tebex(commands.Cog):
    def __init__(self, bot, environ=None):
        self.bot = bot
        self.settings, self.problems = L.read_settings(os.environ if environ is None else environ)
        self.runner: web.AppRunner | None = None
        self.bound_port: int | None = None

    async def cog_load(self) -> None:
        for problem in self.problems:
            log.warning("tebex: %s", problem)
        if self.settings is None:
            log.info("tebex: webhook receiver off (no %s)", L.ENV_SECRET)
            return
        await self.start_server()

    async def cog_unload(self) -> None:
        await self.stop_server()

    @property
    def db(self):
        return self.bot.db

    def app(self) -> web.Application:
        app = web.Application(client_max_size=L.MAX_BODY)
        app.router.add_post(L.PATH, self.handle)
        return app

    async def start_server(self) -> None:
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.settings.host, self.settings.port)
        try:
            await site.start()
        except OSError as exc:  # port in use: the rest of the bot still runs
            log.error("tebex: couldn't listen on %s:%s (%s); the webhook receiver is off",
                      self.settings.host, self.settings.port, type(exc).__name__)
            await self.stop_server()
            return
        sockets = getattr(getattr(site, "_server", None), "sockets", None) or []
        self.bound_port = sockets[0].getsockname()[1] if sockets else self.settings.port
        log.info("tebex: webhook receiver on %s:%s%s", self.settings.host, self.bound_port, L.PATH)

    async def stop_server(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    # ------------------------------------------------------------ requests
    async def handle(self, request) -> web.Response:
        body = await request.read()
        if not L.verify(self.settings.secret, body, request.headers.get("X-Signature")):
            log.warning("tebex: refused a request with a missing or wrong signature")
            return web.Response(status=401, text="bad signature")
        try:
            event = L.parse(body)
        except L.BadBody as exc:
            log.warning("tebex: refused a signed request (%s)", exc)
            return web.Response(status=400, text="bad body")
        if event.type == L.VALIDATION:
            return web.json_response({"id": event.id})
        if event.type != L.CANCELLATION:
            return web.Response(status=200, text="ignored")
        try:
            posted = await self.alert(event)
        except Exception:
            log.exception("tebex: handling webhook %s failed", event.id)
            posted = False
        return web.Response(status=200, text="ok") if posted else web.Response(status=500, text="try again")

    async def alert(self, event: L.Event) -> bool:
        """True once the alert is out (or was already sent for this webhook id)."""
        ending = L.ending(event.subject)
        async with self.db.transaction() as tx:
            seen = await tx.fetchone("SELECT 1 FROM tebex_webhooks WHERE webhook_id = ?", (event.id,))
            if seen is None:
                await tx.execute("INSERT INTO tebex_webhooks (webhook_id, type, reference, received_at)"
                                 " VALUES (?, ?, ?, ?)", (event.id, event.type, ending.reference or None, now()))
        if seen is not None:
            return True
        if await self.post(L.alert_text(ending, esc)):
            return True
        await self.db.execute("DELETE FROM tebex_webhooks WHERE webhook_id = ?", (event.id,))
        return False

    async def post(self, text: str) -> bool:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        channel = config.match_by_name(guild.text_channels, config.MOD_CHANNEL) if guild is not None else None
        if channel is None:
            log.warning("tebex: no %s channel; the alert waits for Tebex's retry", config.MOD_CHANNEL)
            return False
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("tebex: couldn't post in %s (%s)", config.MOD_CHANNEL, type(exc).__name__)
            return False
        return True


async def setup(bot) -> None:
    await bot.add_cog(Tebex(bot))
