import importlib
from types import SimpleNamespace

import pytest

import cogkit
import config

OWNER, SOMEONE = 1, 2
# Cogs whose staff check is the shared one. Deliberately different, and not listed:
# helpdesk (by permission: Moderate Members or Administrator), and the methods Games.is_staff,
# Moderation.is_staff and Ops.is_staff.
SHARED = ("catchup", "clipcast", "creators", "crew", "econstats", "events", "gamelink", "houses", "levels", "monkey", "news",
          "partners", "sitedata", "staffapps", "supporter", "tournaments", "utility", "wrapped")


def member(uid=SOMEONE, roles=(), admin=False, **kw):
    guild = SimpleNamespace(owner_id=OWNER)
    return SimpleNamespace(id=uid, guild=guild, guild_permissions=SimpleNamespace(administrator=admin),
                           roles=[SimpleNamespace(name=n) for n in roles], **kw)


def test_owner_and_administrators_are_staff():
    assert cogkit.is_staff(member(uid=OWNER))
    assert cogkit.is_staff(member(admin=True))
    assert not cogkit.is_staff(member())


@pytest.mark.parametrize("name,expected", [
    (config.KEEPER_ROLE, True),
    (config.MOD_ROLE, True),
    ("🛡️ moderator", True),  # matched by slug: emoji, case and separators are ignored
    ("KEEPER", True),
    ("Moderators", False),
    ("Keeper of Memes", False),
    ("Overwatch", False),
])
def test_staff_roles_are_matched_by_slug(name, expected):
    assert cogkit.is_staff(member(roles=["LFG", name])) is expected


def test_someone_who_is_not_a_member_is_never_staff():
    # a user in a DM: no guild, and discord.py gives a User neither roles nor guild permissions
    assert not cogkit.is_staff(SimpleNamespace(id=OWNER))
    assert not cogkit.is_staff(SimpleNamespace(id=SOMEONE, guild=None, roles=[SimpleNamespace(name=config.MOD_ROLE)]))


def test_missing_attributes_mean_not_staff_rather_than_an_error():
    guild = SimpleNamespace(owner_id=OWNER)
    assert not cogkit.is_staff(SimpleNamespace(id=SOMEONE, guild=guild))
    assert not cogkit.is_staff(SimpleNamespace(id=SOMEONE, guild=guild, roles=None, guild_permissions=None))
    assert cogkit.is_staff(SimpleNamespace(id=SOMEONE, guild=guild, roles=[SimpleNamespace(name=config.MOD_ROLE)]))


@pytest.mark.parametrize("name", SHARED)
def test_these_cogs_use_the_shared_predicate(name):
    assert importlib.import_module(f"cogs.{name}").is_staff is cogkit.is_staff
