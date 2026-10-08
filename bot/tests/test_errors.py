"""errors.py: the replies members see when a command fails."""

import discord
from discord import app_commands

import errors


def transformer_error(kind):
    return app_commands.TransformerError("123", kind, app_commands.Transformer())


def test_generic_reply_is_written_for_members():
    text = errors.ERROR_REPLY
    assert "bot.log" not in text and "Lorenzo" not in text
    assert "mod" in text.lower() and "try again" in text.lower()


def test_reply_for_picks_the_message():
    assert errors.reply_for(RuntimeError("x")) == errors.ERROR_REPLY
    assert errors.reply_for(transformer_error(discord.AppCommandOptionType.user)) == errors.MEMBER_NOT_FOUND
    assert errors.reply_for(transformer_error(discord.AppCommandOptionType.string)) == errors.BAD_OPTION
    assert "member" in errors.MEMBER_NOT_FOUND.lower()
