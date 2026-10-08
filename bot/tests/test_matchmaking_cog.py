"""Offline tests for cogs.matchmaking: a real in-memory SQLite database plus fakes for the bot,
guild, channels, members and interactions. No network."""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
from cogs import matchmaking as cogmod
from cogs.matchmaking import Matchmaking
from cogs.tempvoice import TempVoice
from logic import matchmaking as M

GUILD_ID = 999
T0 = 1_800_000_000
PLAYERS = list(range(101, 121))


def run(coro):
    return asyncio.run(coro)


def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.mention = f"<#{self.id}>"
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeVoice:
    _next = 700

    def __init__(self, guild, name, **kwargs):
        FakeVoice._next += 1
        self.id = FakeVoice._next
        self.guild = guild
        self.name = name
        self.kwargs = kwargs
        self.user_limit = kwargs.get("user_limit", 0)
        self.position = 0
        self.voice_states = {}
        self.mention = f"<#{self.id}>"
        self.deleted = False
        self.delete_fail = None

    async def delete(self, reason=None):
        if self.delete_fail is not None:
            raise self.delete_fail
        if self.deleted:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        self.deleted = True
        self.guild.voice_channels.remove(self)

    async def send(self, *a, **k):
        pass


class FakeCategory:
    def __init__(self, guild, name):
        self.guild = guild
        self.name = name
        self.overwrites = {"everyone": "view"}
        self.created = []
        self.fail = None

    async def create_voice_channel(self, name, **kwargs):
        if self.fail is not None:
            raise self.fail
        ch = FakeVoice(self.guild, name, **kwargs)
        self.created.append(ch)
        self.guild.voice_channels.append(ch)
        return ch


class FakeMember:
    def __init__(self, uid, guild, bot=False):
        self.id = uid
        self.guild = guild
        self.bot = bot
        self.display_name = f"player{uid}"
        self.mention = f"<@{uid}>"


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.games = FakeText(config.GAMES_CHANNEL)
        self.commands = FakeText(config.BOT_COMMANDS_CHANNEL)
        self.text_channels = [self.commands, self.games]
        self.category = FakeCategory(self, config.VOICE_CATEGORY)
        self.categories = [self.category]
        self.voice_channels = []
        self.members = {}
        self.chunked = False

    def get_channel(self, cid):
        return next((c for c in self.text_channels + self.voice_channels if c.id == cid), None)

    def get_member(self, uid):
        return self.members.get(uid)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.cogs = {}

    async def wait_until_ready(self):
        await asyncio.Event().wait()

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeResponse:
    def __init__(self, inter):
        self.inter = inter
        self.done = False

    async def send_message(self, content=None, **kwargs):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True
        self.inter.calls.append(dict(content=content, **kwargs))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, inter):
        self.inter = inter

    async def send(self, content=None, **kwargs):
        self.inter.calls.append(dict(content=content, **kwargs))


class FakeInteraction:
    def __init__(self, bot, user):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    @property
    def text(self):
        return "\n".join(c["content"] or "" for c in self.calls)


class Env:
    def __init__(self, db, guild, bot, monkeypatch):
        self.db, self.guild, self.bot = db, guild, bot
        self.t = T0
        monkeypatch.setattr(cogmod, "now", lambda: self.t)
        self.cog = Matchmaking(bot)
        bot.cogs["Matchmaking"] = self.cog

    def member(self, uid):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild)
        return self.guild.members[uid]

    async def join(self, uid, game="valorant", mode="Ranked", size=2):
        inter = FakeInteraction(self.bot, self.member(uid))
        await Matchmaking.join.callback(self.cog, inter, game, mode, size)
        return inter

    async def leave(self, uid):
        inter = FakeInteraction(self.bot, self.member(uid))
        await Matchmaking.leave.callback(self.cog, inter)
        return inter

    async def status(self, uid):
        inter = FakeInteraction(self.bot, self.member(uid))
        await Matchmaking.status.callback(self.cog, inter)
        return inter

    async def rows(self, sql, params=()):
        return [dict(r) for r in await self.db.fetchall(sql, params)]

    async def queued(self):
        return {r["user_id"] for r in await self.rows("SELECT user_id FROM mm_queue")}


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        env = Env(db, guild, bot, monkeypatch)
        for uid in PLAYERS:
            env.member(uid)
        try:
            await fn(env)
        finally:
            await db.close()

    run(go())


# ---------------------------------------------------------------- joining
def test_join_queues_and_reports(monkeypatch):
    async def body(env):
        inter = await env.join(101, size=3)
        assert "You're in the" in inter.text and "3 players" in inter.text
        assert inter.calls[0]["ephemeral"] is True
        rows = await env.rows("SELECT * FROM mm_queue")
        assert rows == [dict(user_id=101, game="valorant", mode="Ranked", size=3, joined_at=T0)]
        assert env.guild.games.sent == []
    with_env(body, monkeypatch)


def test_size_defaults_by_game(monkeypatch):
    async def body(env):
        await env.join(101, game="valorant", size=None)
        await env.join(102, game="fortnite", size=None)
        await env.join(103, game="minecraft", size=None)
        sizes = {r["user_id"]: r["size"] for r in await env.rows("SELECT * FROM mm_queue")}
        assert sizes == {101: 5, 102: 4, 103: 2}
    with_env(body, monkeypatch)


def test_unknown_game_and_mode_are_refused(monkeypatch):
    async def body(env):
        inter = await env.join(101, game="Chess")
        assert "Pick a game" in inter.text
        inter = await env.join(101, mode="Hardcore")
        assert "Mode must be" in inter.text
        assert await env.queued() == set()
    with_env(body, monkeypatch)


def test_typed_game_name_works(monkeypatch):
    async def body(env):
        await env.join(101, game="Counter-Strike 2", size=2)
        assert (await env.rows("SELECT game FROM mm_queue"))[0]["game"] == "counterstrike2"
    with_env(body, monkeypatch)


def test_joining_again_same_bucket_keeps_place(monkeypatch):
    async def body(env):
        await env.join(101, size=3)
        env.t += 100
        inter = await env.join(101, size=3)
        assert "already in" in inter.text
        assert (await env.rows("SELECT joined_at FROM mm_queue"))[0]["joined_at"] == T0
    with_env(body, monkeypatch)


def test_one_entry_per_member_join_moves(monkeypatch):
    async def body(env):
        await env.join(101, size=3)
        env.t += 10
        inter = await env.join(101, game="fortnite", mode="Casual", size=4)
        assert "Moved you out" in inter.text
        rows = await env.rows("SELECT * FROM mm_queue")
        assert rows == [dict(user_id=101, game="fortnite", mode="Casual", size=4, joined_at=T0 + 10)]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- popping
def test_full_bucket_pops_creates_channel_and_pings(monkeypatch):
    async def body(env):
        await env.join(101, size=3)
        await env.join(102, size=3)
        await env.join(150 + 0, game="fortnite", size=3)  # other bucket, untouched
        inter = await env.join(103, size=3)
        assert "Match found" in inter.text
        assert await env.queued() == {150}
        [match] = await env.rows("SELECT * FROM mm_matches")
        assert match["game"] == "valorant" and match["mode"] == "Ranked"
        assert M.decode_members(match["members"]) == [101, 102, 103]
        [ch] = env.guild.category.created
        assert match["channel_id"] == ch.id
        val = config.game_by_key("valorant")
        assert ch.name == f"{val.emoji} Valorant match"
        assert ch.kwargs["user_limit"] == 3
        assert ch.kwargs["overwrites"] == env.guild.category.overwrites
        [post] = env.guild.games.sent
        for uid in (101, 102, 103):
            assert f"<@{uid}>" in post["content"]
        assert ch.mention in post["content"]
        am = post["allowed_mentions"]
        assert am.everyone is False and am.roles is False
        assert sorted(o.id for o in am.users) == [101, 102, 103]
        # Registered as a temp voice channel, owned by the longest waiting member.
        assert await env.rows("SELECT channel_id, owner_id FROM temp_voice") == [
            dict(channel_id=ch.id, owner_id=101)]
    with_env(body, monkeypatch)


def test_pop_takes_longest_waiting(monkeypatch):
    async def body(env):
        for i, uid in enumerate((105, 101, 103)):
            env.t = T0 + i
            await env.join(uid, size=2)
        [match] = await env.rows("SELECT members FROM mm_matches")
        assert M.decode_members(match["members"]) == [105, 101]
        assert await env.queued() == {103}
    with_env(body, monkeypatch)


def test_concurrent_joins_never_double_match(monkeypatch):
    async def body(env):
        players = PLAYERS[:11]
        await asyncio.gather(*(env.join(uid, size=3) for uid in players))
        matches = await env.rows("SELECT members FROM mm_matches")
        assert len(matches) == 3
        matched = [u for m in matches for u in M.decode_members(m["members"])]
        assert len(matched) == len(set(matched)) == 9
        left = await env.queued()
        assert len(left) == 2 and not (left & set(matched))
        assert len(env.guild.category.created) == 3
    with_env(body, monkeypatch)


def test_concurrent_join_and_leave(monkeypatch):
    async def body(env):
        await env.join(101, size=2)
        left, _ = await asyncio.gather(env.leave(101), env.join(102, size=2))
        matches = await env.rows("SELECT members FROM mm_matches")
        q = await env.queued()
        # Either the leave won (102 waits alone) or the match won (101 matched, then leave is a no-op).
        if matches:
            assert M.decode_members(matches[0]["members"]) == [101, 102] and q == set()
            assert "not in a queue" in left.text
        else:
            assert q == {102} and "You left" in left.text
    with_env(body, monkeypatch)


def test_expired_entries_are_not_matched(monkeypatch):
    async def body(env):
        await env.join(101, size=2)
        env.t += M.QUEUE_TTL
        inter = await env.join(102, size=2)
        assert "Match found" not in inter.text
        assert await env.rows("SELECT * FROM mm_matches") == []
    with_env(body, monkeypatch)


def test_members_gone_from_a_chunked_guild_are_skipped(monkeypatch):
    async def body(env):
        await env.join(101, size=2)
        del env.guild.members[101]
        env.guild.chunked = True
        await env.join(102, size=2)
        assert await env.rows("SELECT * FROM mm_matches") == []
        await env.join(103, size=2)
        [m] = await env.rows("SELECT members FROM mm_matches")
        assert M.decode_members(m["members"]) == [102, 103]
    with_env(body, monkeypatch)


def test_no_category_still_records_and_pings(monkeypatch):
    async def body(env):
        env.guild.categories = []
        await env.join(101)
        await env.join(102)
        [m] = await env.rows("SELECT channel_id FROM mm_matches")
        assert m["channel_id"] is None
        [post] = env.guild.games.sent
        assert config.NEW_SQUAD_VOICE in post["content"]
    with_env(body, monkeypatch)


def test_channel_create_failure_still_pings(monkeypatch):
    async def body(env):
        env.guild.category.fail = http_error()
        await env.join(101)
        inter = await env.join(102)
        assert "Match found" in inter.text
        assert len(env.guild.games.sent) == 1
    with_env(body, monkeypatch)


def test_falls_back_to_bot_commands(monkeypatch):
    async def body(env):
        env.guild.text_channels = [env.guild.commands]
        await env.join(101)
        await env.join(102)
        assert len(env.guild.commands.sent) == 1
    with_env(body, monkeypatch)


def test_ping_failure_does_not_raise(monkeypatch):
    async def body(env):
        env.guild.games.fail = http_error()
        await env.join(101)
        inter = await env.join(102)
        assert "Match found" in inter.text
        assert len(await env.rows("SELECT * FROM mm_matches")) == 1
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- leave / status
def test_leave(monkeypatch):
    async def body(env):
        inter = await env.leave(101)
        assert "not in a queue" in inter.text
        await env.join(101)
        inter = await env.leave(101)
        assert "You left" in inter.text
        assert await env.queued() == set()
    with_env(body, monkeypatch)


def test_status_counts_only_and_names_in_my_bucket(monkeypatch):
    async def body(env):
        env.member(101).display_name = "*bold*"
        await env.join(101, size=4)
        await env.join(102, size=4)
        await env.join(103, game="fortnite", size=4)
        inter = await env.status(102)
        text = inter.text
        assert "**2**/4 waiting" in text and "**1**/4 waiting" in text
        assert "you're in this one" in text
        assert r"\*bold\*" in text  # escaped
        assert "player103" not in text  # other buckets: counts only
        assert inter.calls[0]["ephemeral"] is True
        assert inter.calls[0]["allowed_mentions"].users is False or not inter.calls[0]["allowed_mentions"].users
        inter = await env.status(103)
        assert "player101" not in inter.text and "bold" not in inter.text
    with_env(body, monkeypatch)


def test_status_empty(monkeypatch):
    async def body(env):
        inter = await env.status(101)
        assert "Nobody's queued" in inter.text
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- expiry
def test_sweep_expires_and_notes_once(monkeypatch):
    async def body(env):
        await env.join(101)
        env.t += 30
        await env.join(102, game="fortnite")
        env.t = T0 + M.QUEUE_TTL
        assert await env.cog.expire(env.t) == [101]
        assert await env.queued() == {102}
        inter = await env.status(101)
        assert "expired" in inter.text
        inter = await env.status(101)
        assert "expired" not in inter.text
        # 102 hasn't expired: no note.
        inter = await env.leave(102)
        assert "expired" not in inter.text and "You left" in inter.text
    with_env(body, monkeypatch)


def test_expiry_note_shown_on_join(monkeypatch):
    async def body(env):
        await env.join(101)
        env.t += M.QUEUE_TTL
        await env.cog.expire(env.t)
        inter = await env.join(101)
        assert "expired" in inter.text and "You're in the" in inter.text
    with_env(body, monkeypatch)


def test_old_notes_are_pruned(monkeypatch):
    async def body(env):
        await env.join(101)
        env.t += M.QUEUE_TTL
        await env.cog.expire(env.t)
        env.t += cogmod.NOTICE_KEEP
        await env.cog.expire(env.t)
        assert await env.rows("SELECT * FROM meta WHERE key LIKE 'mm:%'") == []
    with_env(body, monkeypatch)


def test_member_remove_drops_entry(monkeypatch):
    async def body(env):
        await env.join(101)
        await env.cog.on_member_remove(env.member(101))
        assert await env.queued() == set()
        other = SimpleNamespace(id=102, guild=SimpleNamespace(id=1))
        await env.join(102, game="fortnite")
        await env.cog.on_member_remove(other)  # another guild: ignored
        assert await env.queued() == {102}
        await env.cog.on_member_remove(None)  # never raises
    with_env(body, monkeypatch)


def test_sweep_loop_never_raises(monkeypatch):
    async def body(env):
        async def boom(*a):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "expire", boom)
        monkeypatch.setattr(env.cog, "clean_channels", boom)
        await env.cog.sweep.coro(env.cog)
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- channel cleanup
async def make_match(env):
    await env.join(101)
    await env.join(102)
    return env.guild.category.created[-1]


def test_never_joined_channel_deleted_after_grace(monkeypatch):
    async def body(env):
        ch = await make_match(env)
        env.t += M.EMPTY_GRACE - 1
        assert await env.cog.clean_channels() == 0
        env.t += 1
        assert await env.cog.clean_channels() == 1
        assert ch.deleted
        assert await env.rows("SELECT * FROM temp_voice") == []
    with_env(body, monkeypatch)


def test_occupied_channel_is_kept_and_timer_resets(monkeypatch):
    async def body(env):
        ch = await make_match(env)
        env.t += M.EMPTY_GRACE - 60
        ch.voice_states = {101: object()}
        await env.cog.clean_channels()
        ch.voice_states = {}
        env.t += 120
        assert await env.cog.clean_channels() == 0  # empty again only just now
        env.t += M.EMPTY_GRACE
        assert await env.cog.clean_channels() == 1
    with_env(body, monkeypatch)


def test_bots_dont_keep_a_channel(monkeypatch):
    async def body(env):
        ch = await make_match(env)
        env.guild.members[50] = FakeMember(50, env.guild, bot=True)
        ch.voice_states = {50: object()}
        env.t += M.EMPTY_GRACE
        assert await env.cog.clean_channels() == 1
    with_env(body, monkeypatch)


def test_delete_failure_is_logged_and_retried(monkeypatch):
    async def body(env):
        ch = await make_match(env)
        ch.delete_fail = http_error()
        env.t += M.EMPTY_GRACE
        assert await env.cog.clean_channels() == 0
        ch.delete_fail = None
        assert await env.cog.clean_channels() == 1
    with_env(body, monkeypatch)


def test_registers_with_tempvoice_and_cleans_through_it(monkeypatch):
    async def body(env):
        tv = TempVoice(env.bot)
        await tv.cog_load()
        env.bot.cogs["TempVoice"] = tv
        ch = await make_match(env)
        assert tv.owners[ch.id] == 101
        env.t += M.EMPTY_GRACE
        assert await env.cog.clean_channels() == 1
        assert ch.deleted and ch.id not in tv.owners
        assert await env.rows("SELECT * FROM temp_voice") == []
        # A restarted tempvoice picks match channels up from its table.
        ch2 = await make_match(env)
        tv2 = TempVoice(env.bot)
        await tv2.cog_load()
        assert tv2.owners == {ch2.id: 101}
    with_env(body, monkeypatch)


def test_deleted_channel_is_forgotten(monkeypatch):
    async def body(env):
        ch = await make_match(env)
        await ch.delete()
        env.t += M.EMPTY_GRACE
        assert await env.cog.clean_channels() == 0
        assert ch.id not in env.cog.empty_since
    with_env(body, monkeypatch)


def test_autocomplete(monkeypatch):
    async def body(env):
        out = await env.cog.game_choices(None, "val")
        assert [c.value for c in out] == ["valorant"]
        assert len(await env.cog.game_choices(None, "")) == len(config.GAMES)
    with_env(body, monkeypatch)


def test_joiner_is_told_the_voice_channel_directly(monkeypatch):
    async def body(env):
        await env.join(101, size=2)
        inter = await env.join(102, size=2)
        [ch] = env.guild.category.created
        assert ch.mention in inter.text
        assert all(c.get("ephemeral") for c in inter.calls)
    with_env(body, monkeypatch)


def test_no_post_channel_dms_the_players(monkeypatch):
    async def body(env):
        env.guild.text_channels = []
        dms = []
        for uid in (101, 102):
            m = env.member(uid)

            async def send(content=None, _uid=uid, **kw):
                dms.append((_uid, content))
            m.send = send
        await env.join(101, size=2)
        await env.join(102, size=2)
        [ch] = env.guild.category.created
        assert sorted(u for u, _ in dms) == [101, 102]
        assert all(ch.mention in text for _, text in dms)
    with_env(body, monkeypatch)
