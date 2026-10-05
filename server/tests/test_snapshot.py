"""Tests for snapshot_lib, using SimpleNamespace fakes instead of discord.py objects."""

import json
from enum import Enum
from types import SimpleNamespace

import snapshot_lib as lib



class NS(SimpleNamespace):
    """SimpleNamespace that can be a dict key, like discord Roles/Members in channel.overwrites."""

    __hash__ = object.__hash__


TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.fake.token-value"


class Kind(Enum):
    text = 0
    voice = 2
    category = 4
    forum = 15


def perms(**flags):
    """A Permissions-like iterable of (name, bool), in a deliberately unsorted order."""
    base = {"view_channel": False, "administrator": False, "send_messages": False, "attach_files": False}
    base.update(flags)
    return list(reversed(list(base.items())))


def role(name, position, rid, color=0, **kw):
    return NS(
        id=rid,
        name=name,
        position=position,
        color=NS(value=color),
        hoist=kw.get("hoist", False),
        mentionable=kw.get("mentionable", False),
        managed=kw.get("managed", False),
        permissions=perms(**kw.get("perms", {})),
    )


def member(name, mid):
    return NS(id=mid, name=name, display_name=name.title())


def overwrite(allow=None, deny=None):
    allow, deny = perms(**(allow or {})), perms(**(deny or {}))
    return NS(pair=lambda: (allow, deny))


EVERYONE = role("@everyone", 0, 1000000000000000001)
SQUAD = role("Squad", 3, 1000000000000000003, color=0x397F5E, hoist=True, perms={"send_messages": True})
KEEPER = role("Keeper", 5, 1000000000000000005, color=0x42A979, perms={"administrator": True})
GUEST = role("Guest", 3, 1000000000000000002)  # same position as Squad: tie broken by id


def channel(name, kind, position, cid, category=None, **kw):
    return NS(
        id=cid,
        name=name,
        type=kind,
        position=position,
        category=category,
        topic=kw.get("topic"),
        nsfw=kw.get("nsfw", False),
        slowmode_delay=kw.get("slowmode", 0),
        user_limit=kw.get("user_limit", 0),
        bitrate=kw.get("bitrate", 64000),
        available_tags=kw.get("tags", []),
        overwrites=kw.get("overwrites", {}),
        permissions_synced=kw.get("synced", False),
        flags=NS(require_tag=kw.get("require_tag", False)),
    )


def build_channels():
    lobby = channel("02 · the lobby", Kind.category, 1, 2000000000000000002)
    desk = channel(
        "01 · front desk",
        Kind.category,
        0,
        2000000000000000001,
        overwrites={EVERYONE: overwrite(deny={"send_messages": True})},
    )
    general = channel(
        "💬・general",
        Kind.text,
        0,
        3000000000000000001,
        category=lobby,
        topic="Anything goes.",
        overwrites={
            member("someuser", 4000000000000000001): overwrite(allow={"attach_files": True}),
            SQUAD: overwrite(allow={"send_messages": True, "attach_files": True}),
            EVERYONE: overwrite(deny={"attach_files": True}),
        },
    )
    clips = channel("📸・clips", Kind.text, 1, 3000000000000000002, category=lobby, slowmode=10)
    rules = channel("📌・rules", Kind.text, 0, 3000000000000000003, category=desk, synced=True)
    voice = channel("🎮 Squad", Kind.voice, 0, 3000000000000000004, category=lobby, user_limit=5)
    forum = channel(
        "🎮・lfg",
        Kind.forum,
        2,
        3000000000000000005,
        category=lobby,
        tags=[
            NS(name="Wardogs", emoji=NS(name="🪖", id=None), moderated=False),
            NS(name="Ranked", emoji=NS(name="trophy", id=5000000000000000001), moderated=True),
            NS(name="Plain", emoji=None, moderated=False),
        ],
        require_tag=True,
    )
    loose = channel("loose", Kind.text, 9, 3000000000000000006)
    # Shuffled on purpose: output order must come from position, not input order.
    return [voice, forum, loose, clips, lobby, general, rules, desk]


# ------------------------------------------------------------ permissions


def test_perm_names_are_sorted_true_flags_only():
    p = perms(view_channel=True, send_messages=True, administrator=False)
    assert lib.perm_names(p) == ["send_messages", "view_channel"]


def test_perm_names_of_nothing_is_empty_list():
    assert lib.perm_names(perms()) == []


def test_color_hex():
    assert lib.color_hex(NS(value=0x42A979)) == "#42a979"
    assert lib.color_hex(NS(value=0)) is None


# ------------------------------------------------------------ roles


def test_roles_top_to_bottom_with_ties_broken_deterministically():
    out = lib.roles_list([GUEST, EVERYONE, KEEPER, SQUAD])
    assert [r["name"] for r in out] == ["Keeper", "Guest", "Squad", "@everyone"]
    assert lib.roles_list([SQUAD, GUEST, KEEPER, EVERYONE]) == out


def test_role_dict_fields():
    out = lib.role_dict(KEEPER)
    assert out == {
        "name": "Keeper",
        "color": "#42a979",
        "hoist": False,
        "mentionable": False,
        "managed": False,
        "permissions": ["administrator"],
    }


# ------------------------------------------------------------ overwrites


def test_overwrites_keyed_by_role_and_member_name():
    ows = {
        member("someuser", 4000000000000000001): overwrite(allow={"attach_files": True}),
        SQUAD: overwrite(allow={"send_messages": True}, deny={"attach_files": True}),
    }
    out = lib.overwrites_dict(ows)
    assert out == {
        "member:someuser": {"allow": ["attach_files"], "deny": []},
        "role:Squad": {"allow": ["send_messages"], "deny": ["attach_files"]},
    }


def test_unresolved_member_overwrite_uses_name_map_or_placeholder_never_id():
    known = NS(id=4000000000000000009, type=NS(__name__="Member"))
    unknown_a = NS(id=4000000000000000008)
    unknown_b = NS(id=4000000000000000007)
    ows = {known: overwrite(), unknown_a: overwrite(), unknown_b: overwrite()}
    out = lib.overwrites_dict(ows, member_names={4000000000000000009: "pal"})
    assert sorted(out) == ["member:pal", "unresolved-1", "unresolved-2"]
    assert "4000000000000000008" not in json.dumps(out)


# ------------------------------------------------------------ channels


def test_channels_grouped_by_category_in_position_order():
    snap = lib.channels_snapshot(build_channels())
    assert [c["name"] for c in snap["categories"]] == ["01 · front desk", "02 · the lobby"]
    lobby = snap["categories"][1]
    # text-like channels before voice (Discord's sidebar buckets), then by position
    assert [c["name"] for c in lobby["channels"]] == ["💬・general", "📸・clips", "🎮・lfg", "🎮 Squad"]
    assert [c["name"] for c in snap["uncategorized"]] == ["loose"]


def test_channel_fields_by_type():
    snap = lib.channels_snapshot(build_channels())
    lobby = {c["name"]: c for c in snap["categories"][1]["channels"]}
    assert lobby["📸・clips"]["slowmode"] == 10
    assert lobby["📸・clips"]["type"] == "text"
    assert "user_limit" not in lobby["📸・clips"]
    assert lobby["🎮 Squad"]["user_limit"] == 5
    assert lobby["🎮 Squad"]["bitrate"] == 64000
    assert lobby["🎮・lfg"]["require_tag"] is True
    assert lobby["🎮・lfg"]["tags"] == [
        {"name": "Wardogs", "emoji": "🪖", "moderated": False},
        {"name": "Ranked", "emoji": ":trophy:", "moderated": True},
        {"name": "Plain", "emoji": None, "moderated": False},
    ]
    desk = snap["categories"][0]
    assert desk["overwrites"] == {"role:@everyone": {"allow": [], "deny": ["send_messages"]}}
    assert desk["channels"][0]["synced"] is True


# ------------------------------------------------------------ server, welcome, onboarding, automod


def test_server_dict_uses_names_and_sorted_features():
    afk = NS(name="💤 AFK")
    guild = NS(
        name="Squad",
        description=None,
        verification_level=Kind.text,  # any enum works
        explicit_content_filter=Kind.voice,
        default_notifications=Kind.category,
        afk_channel=afk,
        afk_timeout=900,
        system_channel=NS(name="👋・welcome"),
        rules_channel=None,
        public_updates_channel=None,
        features=["NEWS", "COMMUNITY", "WELCOME_SCREEN_ENABLED"],
        preferred_locale="en-US",
        mfa_level=Kind.text,
        nsfw_level=Kind.text,
        premium_progress_bar_enabled=False,
        id=999999999999999999,
        owner_id=888888888888888888,
    )
    out = lib.server_dict(guild)
    assert out["afk_channel"] == "💤 AFK"
    assert out["rules_channel"] is None
    assert out["features"] == ["COMMUNITY", "NEWS", "WELCOME_SCREEN_ENABLED"]
    assert out["verification_level"] == "text"
    assert lib.find_leaks(out, secrets=[TOKEN]) == []


def test_welcome_dict():
    ws = NS(
        description="Hi",
        enabled=True,
        welcome_channels=[
            NS(channel=NS(name="📌・rules"), description="Read me", emoji=NS(name="📌", id=None)),
            NS(channel=NS(name="💬・general"), description="Talk", emoji=None),
        ],
    )
    out = lib.welcome_dict(ws)
    assert out == {
        "description": "Hi",
        "enabled": True,
        "channels": [
            {"channel": "📌・rules", "description": "Read me", "emoji": "📌"},
            {"channel": "💬・general", "description": "Talk", "emoji": None},
        ],
    }


def test_onboarding_dict_uses_names_and_sorts_inner_sets():
    opt = NS(
        title="Wardogs",
        description=None,
        emoji=NS(name="🪖", id=None),
        roles=[NS(name="Wardogs"), NS(name="LFG")],
        channels=[NS(name="🪖・wardogs")],
        id=7000000000000000001,
    )
    prompt = NS(
        title="What do you play?",
        type=Kind.text,
        single_select=False,
        required=False,
        in_onboarding=True,
        options=[opt],
        id=7000000000000000002,
    )
    ob = NS(
        enabled=True,
        mode=Kind.voice,
        default_channels=[NS(name="📌・rules"), NS(name="💬・general")],
        prompts=[prompt],
    )
    out = lib.onboarding_dict(ob)
    assert out["default_channels"] == ["💬・general", "📌・rules"]
    assert out["prompts"][0]["options"][0]["roles"] == ["LFG", "Wardogs"]
    assert lib.find_leaks(out, secrets=[]) == []


def test_automod_rules_sorted_and_named():
    trig = NS(
        type=Kind.text,
        keyword_filter=["b", "a"],
        regex_patterns=[],
        presets=[("slurs", True), ("profanity", False), ("sexual_content", True)],
        allow_list=[],
        mention_limit=None,
        mention_raid_protection=None,
    )
    rule_b = NS(
        name="Front Desk: spam",
        enabled=True,
        event_type=Kind.text,
        trigger=trig,
        actions=[NS(type=Kind.voice, channel_id=3000000000000000099, duration=None, custom_message=None)],
        exempt_roles=[NS(name="Moderator"), NS(name="Keeper")],
        exempt_channels=[],
    )
    rule_a = NS(**{**vars(rule_b), "name": "Front Desk: mention raids", "actions": []})
    names = {3000000000000000099: "🛡️・mod"}
    out = lib.automod_list([rule_b, rule_a], channel_name=names.get)
    assert [r["name"] for r in out] == ["Front Desk: mention raids", "Front Desk: spam"]
    spam = out[1]
    assert spam["trigger"]["keyword_filter"] == ["a", "b"]
    assert spam["trigger"]["presets"] == ["sexual_content", "slurs"]
    assert spam["actions"][0]["channel"] == "🛡️・mod"
    assert spam["exempt_roles"] == ["Keeper", "Moderator"]
    assert lib.find_leaks(out, secrets=[]) == []


def test_error_note_is_a_note_not_a_crash():
    note = lib.error_note("automod", RuntimeError("403 Forbidden (error code: 50013): Missing Permissions"))
    assert note == {"error": "could not fetch automod: 403 Forbidden (error code: 50013): Missing Permissions"}


# ------------------------------------------------------------ safety + determinism


def test_find_leaks_flags_token_snowflakes_and_invites():
    bad = {"a": TOKEN, "b": [1234567890123456789], "c": "join discord.gg/abc123", "d": "id 123456789012345678"}
    problems = lib.find_leaks(bad, secrets=[TOKEN])
    assert len(problems) == 4
    assert TOKEN not in " ".join(problems)  # the report itself must not echo the secret


def test_scrub_replaces_mentions_with_names():
    names = {3000000000000000001: "📌・rules", 1000000000000000003: "Squad"}
    data = {"topic": "Read <#3000000000000000001>, ping <@&1000000000000000003>, ask <@!4000000000000000001> <:pog:5000000000000000001> <#3000000000000000999>"}
    out = lib.scrub(data, names)
    assert out == {"topic": "Read #📌・rules, ping @Squad, ask @user :pog: #unknown-channel"}
    assert lib.find_leaks(out) == []


def test_full_snapshot_has_no_ids_or_secrets():
    snap = lib.channels_snapshot(build_channels())
    roles = lib.roles_list([GUEST, EVERYONE, KEEPER, SQUAD])
    assert lib.find_leaks({"channels": snap, "roles": roles}, secrets=[TOKEN]) == []


def test_dumps_is_stable_across_runs_and_input_order():
    a = lib.dumps(lib.channels_snapshot(build_channels()))
    shuffled = list(reversed(build_channels()))
    b = lib.dumps(lib.channels_snapshot(shuffled))
    assert a == b
    assert a.endswith("\n")
    assert "💬・general" in a  # ensure_ascii=False


# ------------------------------------------------------------ compare


def test_compare_by_slug():
    missing, extra = lib.compare(["💬・general", "Squad", "📸・clips"], ["general", "squad", "random"])
    assert missing == ["📸・clips"]
    assert extra == ["random"]


def test_layout_names_reads_roles_and_channels():
    layout = NS(
        ROLES=[{"name": "Keeper"}, {"name": "Squad"}],
        CATEGORIES=[{"name": "01 · front desk", "channels": [{"name": "📌・rules"}]}],
    )
    roles, channels = lib.layout_names(layout)
    assert roles == ["Keeper", "Squad"]
    assert channels == ["01 · front desk", "📌・rules"]
