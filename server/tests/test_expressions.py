"""Emoji/soundboard pack: generated files meet Discord's limits; upload plan logic."""
import asyncio
import io
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


# ---------------------------------------------------------------- --replace: comparing pictures
def png(colour, size=ex.EMOJI_PX, **save):
    buf = io.BytesIO()
    Image.new("RGBA", (size, size), colour).save(buf, "PNG", **save)
    return buf.getvalue()


def reencoded(data):
    """The same pixels in a different file, as a CDN might hand them back."""
    buf = io.BytesIO()
    with Image.open(io.BytesIO(data)) as im:
        im.save(buf, "PNG", compress_level=1)
    assert buf.getvalue() != data
    return buf.getvalue()


LOCAL = {p.stem: p.read_bytes() for p in EMOJI}
OLD_ART = png((229, 83, 75, 255))  # stands in for whatever the server has now
BOT_ID, SOMEONE = 111, 222


def test_same_image_ignores_the_encoding():
    assert ex.image_distance(LOCAL["gg"], LOCAL["gg"]) == 0
    assert ex.image_distance(LOCAL["gg"], reencoded(LOCAL["gg"])) == 0
    assert ex.same_image(LOCAL["gg"], reencoded(LOCAL["gg"]))


def test_same_image_ignores_colour_under_transparent_pixels():
    assert ex.same_image(png((255, 0, 0, 0)), png((0, 0, 255, 0)))


def test_different_art_is_not_the_same_image():
    assert not ex.same_image(LOCAL["gg"], OLD_ART)
    assert not ex.same_image(LOCAL["gg"], LOCAL["ez"])
    assert not ex.same_image(png((9, 9, 9, 255)), png((9, 9, 9, 0)))  # only the coverage differs
    assert not ex.same_image(LOCAL["gg"], b"not a picture")


def test_same_image_survives_a_lossy_copy_at_another_size():
    buf = io.BytesIO()
    with Image.open(io.BytesIO(LOCAL["gg"])) as im:
        im.resize((96, 96), Image.LANCZOS).save(buf, "WEBP", quality=80)
    assert ex.same_image(LOCAL["gg"], buf.getvalue())


# ---------------------------------------------------------------- --replace: the plan
def remote_of(**images):
    """name -> [(emoji, image), ...] the way run_replace builds it; the emoji is just a label here."""
    return {name: [(f"{name}#{i}", image) for i, image in enumerate(many)] for name, many in images.items()}


def test_plan_replace_sorts_changed_same_and_absent():
    local = {"gg": LOCAL["gg"], "ez": LOCAL["ez"], "rip": LOCAL["rip"]}
    p = ex.plan_replace(local, remote_of(gg=[OLD_ART], ez=[reencoded(LOCAL["ez"])]), free=30)
    assert [(r.name, r.stale, r.upload, r.delete_first) for r in p.replace] == [("gg", ["gg#0"], True, False)]
    assert p.same == ["ez"] and p.absent == ["rip"]


def test_plan_replace_uploads_first_only_while_a_slot_is_free():
    local = {"gg": LOCAL["gg"], "ez": LOCAL["ez"]}
    remote = remote_of(gg=[OLD_ART], ez=[OLD_ART])
    assert [r.delete_first for r in ex.plan_replace(local, remote, free=1).replace] == [False, False]
    assert [r.delete_first for r in ex.plan_replace(local, remote, free=0).replace] == [True, True]
    assert [r.delete_first for r in ex.plan_replace(local, remote, free=-2).replace] == [True, True]  # overfull
    assert [r.delete_first for r in ex.plan_replace(local, remote, free=9, delete_first=True).replace] == [True, True]


def test_plan_replace_finishes_a_run_that_stopped_half_way():
    # the new gg went up but the old one was never deleted: delete it, upload nothing
    p = ex.plan_replace({"gg": LOCAL["gg"]}, remote_of(gg=[OLD_ART, reencoded(LOCAL["gg"])]), free=0)
    assert [(r.name, r.stale, r.upload, r.delete_first) for r in p.replace] == [("gg", ["gg#0"], False, False)]


def test_plan_replace_counts_the_slots_it_frees():
    # full server: tidying gg frees a slot, so ez can go up before its old one comes down
    local = {"gg": LOCAL["gg"], "ez": LOCAL["ez"]}
    p = ex.plan_replace(local, remote_of(gg=[OLD_ART, LOCAL["gg"]], ez=[OLD_ART]), free=0)
    assert [(r.name, r.upload, r.delete_first) for r in p.replace] == [("gg", False, False), ("ez", True, False)]
    # two old copies of one name: both go, one comes back
    p = ex.plan_replace(local, remote_of(gg=[OLD_ART, OLD_ART], ez=[OLD_ART]), free=0)
    assert [(r.name, len(r.stale), r.delete_first) for r in p.replace] == [("gg", 2, True), ("ez", 1, False)]


def test_plan_replace_force_swaps_an_identical_picture():
    remote = remote_of(gg=[LOCAL["gg"]], ez=[LOCAL["ez"]])
    p = ex.plan_replace({"gg": LOCAL["gg"], "ez": LOCAL["ez"]}, remote, free=5, force=frozenset({"gg"}))
    assert [(r.name, r.upload) for r in p.replace] == [("gg", True)] and p.same == ["ez"]


def test_replace_permissions():
    mine = [SimpleNamespace(user=SimpleNamespace(id=BOT_ID))]
    theirs = mine + [SimpleNamespace(user=SimpleNamespace(id=SOMEONE))]
    unknown = [SimpleNamespace(user=None)]
    create = discord.Permissions(create_expressions=True)
    both = discord.Permissions(create_expressions=True, manage_expressions=True)
    assert ex.replace_missing_permissions(create, mine, BOT_ID) == []
    assert ex.replace_missing_permissions(create, theirs, BOT_ID) == ["Manage Expressions"]
    assert ex.replace_missing_permissions(create, unknown, BOT_ID) == ["Manage Expressions"]
    assert ex.replace_missing_permissions(both, theirs, BOT_ID) == []
    assert ex.replace_missing_permissions(discord.Permissions.none(), theirs, BOT_ID) == [
        "Create Expressions", "Manage Expressions"]
    assert ex.replace_missing_permissions(discord.Permissions(administrator=True), theirs, BOT_ID) == []


# ---------------------------------------------------------------- run_replace() with a fake guild
class FakeEmoji:
    def __init__(self, guild, name, image, user_id, animated):
        self.guild, self.name, self.image, self.animated = guild, name, image, animated
        self.user = SimpleNamespace(id=user_id)
        self.roles = []

    async def read(self):
        return self.image

    async def delete(self, *, reason=None):
        assert reason
        self.guild.emojis.remove(self)
        self.guild.log.append(("delete", self.name, self.image == OLD_ART))


class ReplaceGuild:
    """Keeps emoji like Discord does: by slot, with no rule against two of one name.

    It has no soundboard methods on purpose: --replace must not go near the sounds.
    """

    def __init__(self, perms, emoji_limit=50, refuse_uploads=False):
        self.me = SimpleNamespace(id=BOT_ID, guild_permissions=perms)
        self.emoji_limit = emoji_limit
        self.refuse_uploads = refuse_uploads
        self.emojis = []
        self.log = []

    def has(self, name, image, user_id=BOT_ID, animated=False):
        self.emojis.append(FakeEmoji(self, name, image, user_id, animated))
        return self

    def images(self, name):
        return [e.image for e in self.emojis if e.name == name]

    async def fetch_emojis(self):
        return list(self.emojis)

    async def create_custom_emoji(self, *, name, image, roles=(), reason=None):
        if self.refuse_uploads:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "no")
        assert sum(not e.animated for e in self.emojis) < self.emoji_limit, "uploaded with no free slot"
        assert reason and image == LOCAL[name]
        self.has(name, image)
        self.log.append(("create", name))


CREATE = discord.Permissions(create_expressions=True)


def replace(g, **kw):
    return asyncio.run(ex.run_replace(g, **kw))


def test_replace_dry_run_prints_the_plan_and_changes_nothing(capsys):
    g = ReplaceGuild(CREATE).has("gg", OLD_ART).has("ez", reencoded(LOCAL["ez"])).has("party", OLD_ART)
    assert replace(g, apply=False) == 0
    out = capsys.readouterr().out
    assert "replace gg (upload the new one, then delete the old one)" in out
    assert "same    ez" in out
    assert "absent  rip (not on the server; a run without --replace adds it)" in out
    assert "party" not in out  # not one of ours
    assert "new ID" in out and "--replace --apply" in out
    assert g.log == [] and len(g.emojis) == 3


def test_replace_apply_uploads_then_deletes_and_leaves_the_rest(capsys):
    g = ReplaceGuild(CREATE).has("gg", OLD_ART).has("ez", LOCAL["ez"]).has("party", OLD_ART)
    g.has("lfg", OLD_ART, animated=True)  # an animated namesake is somebody else's business
    assert replace(g, apply=True) == 0
    assert g.log == [("create", "gg"), ("delete", "gg", True)]
    assert g.images("gg") == [LOCAL["gg"]] and g.images("ez") == [LOCAL["ez"]]
    assert g.images("party") == [OLD_ART] and g.images("lfg") == [OLD_ART]
    assert replace(g, apply=True) == 0  # a second run finds nothing to do
    assert len(g.log) == 2 and "Nothing to replace." in capsys.readouterr().out


def test_replace_apply_on_a_full_server_deletes_first():
    g = ReplaceGuild(CREATE, emoji_limit=3).has("gg", OLD_ART).has("ez", OLD_ART).has("party", OLD_ART)
    assert replace(g, apply=True) == 0
    # one emoji at a time, in file order
    assert g.log == [("delete", "ez", True), ("create", "ez"), ("delete", "gg", True), ("create", "gg")]
    assert g.images("gg") == [LOCAL["gg"]] and g.images("ez") == [LOCAL["ez"]]


def test_replace_delete_first_flag_and_force():
    g = ReplaceGuild(CREATE).has("gg", LOCAL["gg"]).has("ez", LOCAL["ez"])
    assert replace(g, apply=True, delete_first=True, force=frozenset({"ez"})) == 0
    assert g.log == [("delete", "ez", False), ("create", "ez")]


def test_replace_finishes_a_half_done_swap():
    g = ReplaceGuild(CREATE).has("gg", OLD_ART).has("gg", LOCAL["gg"])
    assert replace(g, apply=True) == 0
    assert g.log == [("delete", "gg", True)] and g.images("gg") == [LOCAL["gg"]]


def test_replace_refused_upload_deletes_nothing(capsys):
    g = ReplaceGuild(CREATE, refuse_uploads=True).has("gg", OLD_ART).has("ez", OLD_ART)
    assert replace(g, apply=True) == 1
    assert g.log == [] and g.images("gg") == [OLD_ART] and g.images("ez") == [OLD_ART]
    assert "--replace --delete-first --apply" in capsys.readouterr().out


def test_replace_refused_upload_after_delete_says_how_to_recover(capsys):
    g = ReplaceGuild(CREATE, refuse_uploads=True).has("gg", OLD_ART).has("ez", OLD_ART)
    assert replace(g, apply=True, delete_first=True) == 1
    assert g.log == [("delete", "ez", True)] and g.images("gg") == [OLD_ART]  # stopped at the first failure
    assert "python expressions.py --apply" in capsys.readouterr().out


def test_replace_needs_manage_expressions_for_someone_elses_emoji(capsys):
    g = ReplaceGuild(CREATE).has("gg", OLD_ART, user_id=SOMEONE)
    assert replace(g, apply=True) == 2
    assert '"Manage Expressions"' in capsys.readouterr().out
    assert g.log == [] and g.images("gg") == [OLD_ART]
    g.me.guild_permissions = discord.Permissions(create_expressions=True, manage_expressions=True)
    assert replace(g, apply=True) == 0 and g.images("gg") == [LOCAL["gg"]]


def test_replace_rejects_an_unknown_force_name(capsys):
    g = ReplaceGuild(CREATE).has("gg", OLD_ART)
    assert replace(g, apply=True, force=frozenset({"nope"})) == 1
    assert "nope" in capsys.readouterr().out and g.log == []
