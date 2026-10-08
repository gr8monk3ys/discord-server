"""Staff-only commands are hidden from members' slash picker (the runtime gate stays)."""

import discord

from cogs.levels import Levels
from cogs.moderation import Moderation
from cogs.utility import Utility


def test_staff_commands_need_moderate_members_by_default():
    commands = [Moderation.warn, Moderation.timeout, Moderation.untimeout, Moderation.cases, Moderation.purge,
                Levels.xp, Utility.suggestion]
    for cmd in commands:
        perms = cmd.default_permissions
        assert perms is not None and perms.moderate_members, cmd.name
        assert perms.value == discord.Permissions(moderate_members=True).value, cmd.name
