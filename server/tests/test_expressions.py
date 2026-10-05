"""Emoji/soundboard pack: generated files meet Discord's limits; upload plan logic."""
import asyncio
import re
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import expressions as ex

EMOJI = ex.local_files(ex.EMOJI_DIR, ".png")
SOUNDS = ex.local_files(ex.SOUND_DIR, ".mp3")


# ---------------------------------------------------------------- generated files
def test_pack_is_generated():
    assert len(EMOJI) >= 20, "run assets/expressions/make_emoji.py"
    assert len(SOUNDS) == 8, "run assets/expressions/make_sounds.py"


@pytest.mark.parametrize("path", EMOJI, ids=lambda p: p.name)
def test_emoji_within_discord_limits(path):
    assert ex.check_emoji(path) == []
    with Image.open(path) as im:
        assert im.mode == "RGBA"
        alpha = im.getchannel("A")
        assert alpha.getextrema()[0] == 0, "background should be transparent"
        assert alpha.getpixel((0, 0)) == 0


@pytest.mark.parametrize("path", SOUNDS, ids=lambda p: p.name)
def test_sound_within_discord_limits(path):
    assert ex.check_sound(path) == []
    assert ex.mp3_duration(path.read_bytes()) <= 3.2  # 3 s + encoder padding
    assert path.stat().st_size <= ex.MAX_SOUND_BYTES


def test_names_unique_and_valid():
    names = [p.stem for p in EMOJI] + [p.stem for p in SOUNDS]
    assert all(ex.NAME_RE.match(n) for n in names)
    assert len({p.stem for p in EMOJI}) == len(EMOJI)
    assert len({p.stem for p in SOUNDS}) == len(SOUNDS)


def test_every_sound_has_an_emoji():
    assert {p.stem for p in SOUNDS} == set(ex.SOUND_EMOJI)


def test_name_regex():
    assert ex.NAME_RE.match("gg") and ex.NAME_RE.match("take_w")
    for bad in ("w", "GG", "has-dash", "x" * 33, "sp ace"):
        assert not ex.NAME_RE.match(bad)


def test_mp3_duration_rejects_garbage():
    assert ex.mp3_duration(b"not audio at all") == 0


# ---------------------------------------------------------------- plan logic
def test_plan_skips_existing():
    p = ex.plan(["gg", "ez", "rip"], {"ez"}, used=1, limit=50)
    assert p.create == ["gg", "rip"] and p.skip == ["ez"] and p.no_room == []


def test_plan_respects_slot_limit():
    p = ex.plan(["a1", "b2", "c3", "d4"], set(), used=6, limit=8)
    assert p.create == ["a1", "b2"] and p.no_room == ["c3", "d4"]


def test_plan_full_server_and_overfull():
    assert ex.plan(["a1"], set(), used=8, limit=8).no_room == ["a1"]
    assert ex.plan(["a1"], set(), used=9, limit=8).no_room == ["a1"]
    # existing names never count as needing room
    assert ex.plan(["a1"], {"a1"}, used=8, limit=8).skip == ["a1"]


def test_soundboard_limit_by_tier():
    g = lambda tier, features=(): SimpleNamespace(premium_tier=tier, features=list(features))
    assert [ex.soundboard_limit(g(t)) for t in range(4)] == [8, 24, 36, 48]
    assert ex.soundboard_limit(g(0, ["MORE_SOUNDBOARD"])) == 48


def test_missing_permissions():
    assert ex.missing_permissions(discord.Permissions.none()) == ["Create Expressions"]
    assert ex.missing_permissions(discord.Permissions(create_expressions=True)) == []
    assert ex.missing_permissions(discord.Permissions(administrator=True)) == []


# ---------------------------------------------------------------- run() with a fake guild
class FakeGuild:
    def __init__(self, perms, emojis=(), sounds=(), emoji_limit=50, tier=0):
        self.me = SimpleNamespace(guild_permissions=perms)
        self._emojis = [SimpleNamespace(name=n, animated=a) for n, a in emojis]
        self._sounds = [SimpleNamespace(name=n) for n in sounds]
        self.emoji_limit = emoji_limit
        self.premium_tier = tier
        self.features = []
        self.created = []

    async def fetch_emojis(self):
        return self._emojis

    async def fetch_soundboard_sounds(self):
        return self._sounds

    async def create_custom_emoji(self, *, name, image, reason=None):
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        self.created.append(("emoji", name))

    async def create_soundboard_sound(self, *, name, sound, emoji=None, reason=None):
        assert sound[:3] == b"ID3" and emoji
        self.created.append(("sound", name))


def test_run_without_permission_exits_nonzero(capsys):
    g = FakeGuild(discord.Permissions.none())
    assert asyncio.run(ex.run(g, apply=True)) != 0
    assert '"Create Expressions"' in capsys.readouterr().out
    assert g.created == []


def test_dry_run_changes_nothing(capsys):
    g = FakeGuild(discord.Permissions(create_expressions=True), emojis=[("gg", False)])
    assert asyncio.run(ex.run(g, apply=False)) == 0
    out = capsys.readouterr().out
    assert "ok      gg (already there)" in out and "upload  ez" in out
    assert g.created == []


def test_apply_skips_existing_and_stops_at_slot_limit(capsys):
    # 48 static emoji used out of 50, one animated (doesn't count), "gg" already there
    existing = [("gg", False)] + [(f"old{i}", False) for i in range(47)] + [("spin", True)]
    g = FakeGuild(discord.Permissions(create_expressions=True), emojis=existing, sounds=["ding"])
    assert asyncio.run(ex.run(g, apply=True)) == 0
    emoji_made = [n for k, n in g.created if k == "emoji"]
    sounds_made = [n for k, n in g.created if k == "sound"]
    assert len(emoji_made) == 2 and "gg" not in emoji_made
    assert "ding" not in sounds_made and len(sounds_made) == 7  # 8 slots, 1 used
    assert "no room for 17" in capsys.readouterr().out
