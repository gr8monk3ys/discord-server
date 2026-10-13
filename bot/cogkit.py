"""Small helpers the cogs share. Imports only `logic/`, never a cog, so any cog can import it."""

from logic import community


def is_staff(member) -> bool:
    """Owner, administrators, Keepers and Moderators: the rule for handling reports
    (`logic.community.can_handle`), so the staff roles are matched by slug and a role whose
    case or emoji changed still counts. False for anyone who is not a member of a server
    (a user in a DM has no guild)."""
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    return community.can_handle(getattr(guild, "owner_id", None) == member.id,
                                bool(getattr(perms, "administrator", False)),
                                getattr(member, "roles", None) or [])
