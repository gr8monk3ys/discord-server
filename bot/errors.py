"""The replies members see when something breaks; the details go to data/bot.log (and
the ops module raises repeated errors in the mod log)."""

import discord
from discord import app_commands

ERROR_REPLY = ("Something went wrong on my end. It's been logged; try again in a minute, "
               "or ask a mod if it keeps happening.")
MEMBER_NOT_FOUND = "I couldn't find that member here. They may have left the server."
BAD_OPTION = "One of those options didn't look right. Check it and try again."


def reply_for(error: BaseException) -> str:
    """The member-facing reply for an app command error."""
    if isinstance(error, app_commands.TransformerError):
        if error.type in (discord.AppCommandOptionType.user, discord.AppCommandOptionType.mentionable):
            return MEMBER_NOT_FOUND
        return BAD_OPTION
    return ERROR_REPLY


async def reply_error(interaction: discord.Interaction, text: str = ERROR_REPLY) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass
