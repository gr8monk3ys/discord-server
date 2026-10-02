"""The one reply users see when something breaks; the details go to data/bot.log."""

import discord

ERROR_REPLY = "That didn't work. Lorenzo, check bot.log."


async def reply_error(interaction: discord.Interaction, text: str = ERROR_REPLY) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass
