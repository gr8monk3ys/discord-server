"""Economy health for staff: /economy shows the coins in circulation, the last 7 days of coins
minted vs burned, and the biggest sources and sinks, all read from the wallets and the ledger.
Read-only: it never moves coins. Rules and wording live in logic/econstats.py."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import style
from logic import community as community_rules
from logic import econstats as ES

log = logging.getLogger(__name__)


def now() -> int:
    return int(time.time())


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    perms = getattr(user, "guild_permissions", None)
    return community_rules.can_handle(guild.owner_id == user.id, bool(getattr(perms, "administrator", False)),
                                      getattr(user, "roles", []))


class EconStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def report(self, t: int) -> discord.Embed:
        balances = [r["balance"] for r in await self.db.fetchall("SELECT balance FROM wallets WHERE balance > 0")]
        rows = await self.db.fetchall("SELECT reason, SUM(delta) AS net FROM ledger WHERE at >= ? AND at < ?"
                                      " GROUP BY reason", (t - ES.WINDOW, t))
        summary = ES.summarize((r["reason"], r["net"]) for r in rows)
        sup = ES.supply(balances)
        embed = style.embed(title="Economy · last 7 days", description=ES.verdict(summary, sup.total),
                            footer=style.label("economy", "staff"))
        embed.add_field(name="In circulation", value=f"{sup.total:,} coins\n{sup.holders:,} wallets", inline=True)
        embed.add_field(name="Typical wallet", value=f"median {sup.median:,}\ntop {ES.RICH} hold "
                                                     f"{ES.pct(sup.top_share, 1)}", inline=True)
        embed.add_field(name="Minted vs burned",
                        value=f"+{summary.minted:,} / -{summary.burned:,}\nnet {summary.net:+,}", inline=True)
        embed.add_field(name="Top sources", value=ES.flow_lines(summary.sources), inline=True)
        embed.add_field(name="Top sinks", value=ES.flow_lines(summary.sinks), inline=True)
        return embed

    @app_commands.command(name="economy", description="Coin supply, minted vs burned, top sources (staff only)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def economy(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use /economy.", ephemeral=True)
            return
        try:
            embed = await self.report(now())
        except Exception:
            log.exception("/economy failed")
            await interaction.response.send_message("Couldn't read the ledger. Check the logs.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(EconStats(bot))
