"""Offline tests for cogs.wrapped: a real in-memory SQLite database plus small fakes for the
bot, guild, channels, members and interactions. No network; the clock is injected."""

import asyncio
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from PIL import Image

import config
import db as dbmod
import economy
from cogs import wrapped as cogmod
from cogs.wrapped import ShareButton, Wrapped
from logic import achievements as A
from logic import wrapped as W
from logic import wrappedcard as WC

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, U1, U2, U3, U4, BOTUSER = 10, 11, 12, 13, 14, 50
HOUR = 3600
DAY = 24 * HOUR


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


OPEN = ts(2026, 12, 10, 15, 0)  # inside the window
CLOSED = ts(2026, 10, 12, 15, 0)
MAY = ts(2026, 5, 1, 10, 0)


def run(coro):
    return asyncio.run(coro)


def png_size(file: discord.File) -> tuple[int, int]:
    file.fp.seek(0)
    data = file.fp.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return Image.open(io.BytesIO(data)).size


class FakeAsset:
    def __init__(self, key="abc"):
        self.key = key
        self.reads = 0

    def replace(self, **kwargs):
        return self

    async def read(self):
        self.reads += 1
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 80, 60)).save(buf, "PNG")
        return buf.getvalue()


class FakeText:
    _next = 300

    def __init__(self, name, category=None):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.category = category
        self.sent = []
        self.fail = None
        self.parent = None  # set to the parent channel to make this a thread
        self.muted = set()  # member ids who can read here but not post

    def permissions_for(self, member):
        can = member.id not in self.muted
        return discord.Permissions(view_channel=True, send_messages=can, send_messages_in_threads=can)

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, guild, bot=False, name=None):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.name = f"user{uid}"
        self.display_name = name or f"Member {uid}"
        self.mention = f"<@{uid}>"
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.display_avatar = FakeAsset(f"avatar{uid}")
        self.joined_at = datetime(2026, 2, 10, tzinfo=timezone.utc)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Chill Gaming"
        self.owner_id = OWNER
        self.icon = None
        self.afk_channel = None
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.announcements = FakeText(config.ANNOUNCEMENTS_CHANNEL)
        self.counting = FakeText(config.COUNTING_CHANNEL)
        self.mod = FakeText(config.MOD_CHANNEL, category=SimpleNamespace(name=config.STAFF_CATEGORY))
        self.text_channels = [self.general, self.announcements, self.counting, self.mod]
        self.members = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def defer(self, **kwargs):
        self._finish()
        self.calls.append(("defer", kwargs))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, bot, user, channel=None):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.channel = channel or bot.guild.general
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    @property
    def said(self) -> str:
        """The one thing the member was told."""
        texts = [kw["content"] for k, kw in self.calls if k in ("send_message", "followup")]
        assert len(texts) == 1, self.calls
        return texts[0]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = OPEN
        self.games = 0

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, **kw)
            self.guild.members.append(m)
        return m

    def click(self, uid, channel=None) -> FakeInteraction:
        return FakeInteraction(self.bot, self.member(uid), channel)

    async def wrapped(self, uid, year=None, channel=None) -> FakeInteraction:
        i = self.click(uid, channel)
        await Wrapped.wrapped.callback(self.cog, i, year)
        return i

    async def preview(self, uid, **kw) -> FakeInteraction:
        i = self.click(uid)
        await Wrapped.wrapped_preview.callback(self.cog, i, **kw)
        return i

    async def share(self, uid, year=2026, owner=None, channel=None) -> FakeInteraction:
        i = self.click(uid, channel)
        await self.cog.handle_share(i, year, uid if owner is None else owner)
        return i

    async def voice(self, uid, start, end, channel=5):
        await self.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                              (uid, channel, start, end))

    async def messages(self, uid, day, count):
        await self.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)", (uid, day, count))

    async def post(self, host, at=MAY, joiners=()):
        self.games += 1
        await self.db.execute("INSERT INTO lfg_posts (game, host_id, size, when_text, created_at)"
                              " VALUES (?, ?, 4, 'now', ?)", (f"game{self.games}", host, at))
        pid = (await self.db.fetchone("SELECT MAX(id) AS id FROM lfg_posts"))["id"]
        for uid in (host, *joiners):
            await self.db.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)", (pid, uid, at))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def busy_year(self, uid=U1):
        """Enough of a year for a full card: voice with two friends, chat, games, coins, a badge."""
        self.member(uid)
        for other in (U2, U3):
            self.member(other)
            await self.voice(other, MAY, MAY + 30 * HOUR)
        await self.voice(uid, MAY, MAY + 30 * HOUR)
        await self.messages(uid, "2026-05-01", 120)
        await self.messages(uid, "2026-05-02", 80)
        await self.db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                              (uid, "Alpha Strike", MAY, MAY + 12 * HOUR))
        await economy.apply(self.db, uid, 100, "daily", MAY, ref=f"daily:{uid}")
        await self.db.execute("INSERT INTO achievements (user_id, key, at) VALUES (?, 'chatty', ?)", (uid, MAY))
        await self.post(uid, joiners=(U2,))

    async def meta(self, key):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def jobs(self) -> set[str]:
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}

    async def tick(self, at):
        self.t = at
        await Wrapped.tick.coro(self.cog)


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Wrapped(bot)
        bot.cogs["Wrapped"] = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def no_pings(sent) -> None:
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users is False


# ---------------------------------------------------------------- /wrapped
def test_wrapped_is_closed_outside_the_window(monkeypatch):
    async def body(env):
        env.t = CLOSED
        i = await env.wrapped(U1)
        assert i.said == W.CLOSED_TEXT and i.of("send_message")[0]["ephemeral"] is True
        assert env.cog.renders == 0
        env.t = ts(2027, 1, 16, 0, 1)
        assert (await env.wrapped(U2)).said == W.CLOSED_TEXT
    with_env(body, monkeypatch)


def test_wrapped_sends_a_private_card_with_a_share_button(monkeypatch):
    async def body(env):
        await env.busy_year()
        i = await env.wrapped(U1)
        assert i.of("defer") == [dict(ephemeral=True, thinking=True)]
        sent = i.of("followup")[0]
        assert sent["ephemeral"] is True and "Your Wrapped 2026" in sent["content"]
        no_pings(sent)
        width, height = png_size(sent["file"])
        assert width == WC.WIDTH and height > 900 and sent["file"].filename == "wrapped.png"
        (button,) = sent["view"].children
        assert isinstance(button, ShareButton) and button.custom_id == f"wrapped:share:2026:{U1}"
        assert sent["view"].timeout is None  # persistent
    with_env(body, monkeypatch)


def test_january_shows_the_year_just_ended_and_only_that(monkeypatch):
    async def body(env):
        await env.busy_year()
        env.t = ts(2027, 1, 10, 12)
        assert "Wrapped 2026" in (await env.wrapped(U1)).of("followup")[0]["content"]
        assert "Only Wrapped 2026" in (await env.wrapped(U2, year=2027)).said
    with_env(body, monkeypatch)


def test_member_with_no_data_gets_the_short_card(monkeypatch):
    async def body(env):
        sent = (await env.wrapped(U1)).of("followup")[0]
        assert png_size(sent["file"])[1] < 700
        assert "Fresh Ink" in sent["content"]  # FakeMember joined in February 2026
    with_env(body, monkeypatch)


def test_tracking_off_gets_an_explanation_not_a_card(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.optout(U1)
        i = await env.wrapped(U1)
        assert i.said == W.OPTOUT_SELF and "/privacy" in i.said
        assert i.of("send_message")[0]["ephemeral"] is True and i.of("defer") == []
        assert env.cog.renders == 0
        shared = await env.share(U1)
        assert shared.said == W.OPTOUT_SELF and env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_opting_out_drops_the_cached_card(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.wrapped(U1)
        assert any(k[1] == U1 for k in env.cog.cache)
        await env.optout(U1)
        env.t += W.COOLDOWN
        await env.wrapped(U1)
        assert not any(k[1] == U1 for k in env.cog.cache)
    with_env(body, monkeypatch)


def test_cooldown_per_member(monkeypatch):
    async def body(env):
        await env.wrapped(U1)
        again = await env.wrapped(U1)
        assert "still warm" in again.said and f"{W.COOLDOWN} seconds" in again.said
        assert (await env.wrapped(U2)).of("followup")  # someone else isn't held up
        env.t += W.COOLDOWN
        assert (await env.wrapped(U1)).of("followup")
    with_env(body, monkeypatch)


def test_card_is_drawn_once_until_the_stats_change(monkeypatch):
    async def body(env):
        await env.busy_year()
        avatar = env.member(U1).display_avatar
        first = (await env.wrapped(U1)).of("followup")[0]["file"]
        env.t += W.COOLDOWN
        second = (await env.wrapped(U1)).of("followup")[0]["file"]
        assert env.cog.renders == 1 and avatar.reads == 1  # served from the cache
        first.fp.seek(0)
        second.fp.seek(0)
        assert first.fp.read() == second.fp.read()

        await env.messages(U1, "2026-06-01", 500)  # the numbers changed
        env.t += W.COOLDOWN
        await env.wrapped(U1)
        assert env.cog.renders == 2

        env.member(U1).display_name = "Renamed"  # so did the name
        env.t += W.COOLDOWN
        await env.wrapped(U1)
        assert env.cog.renders == 3

        env.member(U1).display_avatar = FakeAsset("new-avatar")
        env.t += W.COOLDOWN
        await env.wrapped(U1)
        assert env.cog.renders == 4
        env.t += W.COOLDOWN
        await env.wrapped(U1)
        assert env.cog.renders == 4
    with_env(body, monkeypatch)


def test_cache_is_bounded(monkeypatch):
    async def body(env):
        for n in range(W.CACHE_SIZE + 3):
            await env.cog.cached(("member", n, 2026), "x", WC.CardSpec(("a",), "n", None, "Member", "t", "l"), None)
        assert len(env.cog.cache) == W.CACHE_SIZE and ("member", 0, 2026) not in env.cog.cache
    with_env(body, monkeypatch)


def test_rendering_runs_off_the_event_loop(monkeypatch):
    async def body(env):
        seen = []
        real = asyncio.to_thread

        async def spy(fn, *args, **kwargs):
            seen.append(fn)
            return await real(fn, *args, **kwargs)
        monkeypatch.setattr(cogmod.asyncio, "to_thread", spy)
        await env.wrapped(U1)
        assert WC.render in seen and W.summarize in seen
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- what the card is built from
def test_summary_reads_the_year_from_the_tables(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.messages(U1, "2025-12-31", 999)  # last year
        await env.voice(U1, ts(2025, 3, 1), ts(2025, 3, 2))
        await economy.apply(env.db, U1, 5000, "daily", ts(2025, 3, 1), ref="daily:old")
        await env.db.execute("INSERT INTO xp (user_id, xp, level) VALUES (?, 5000, 9)", (U1,))
        summary, names = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.messages == 200 and summary.voice_seconds == 30 * HOUR
        assert summary.top_games == (("Alpha Strike", 12 * HOUR),) and summary.coins_earned == 100
        assert summary.badges == ("chatty",) and summary.squads_hosted == 1
        assert summary.level == 9 and summary.levels_gained is None  # no January snapshot
        assert summary.joined_month == 2  # from member.joined_at when the joins table has no row
        assert {m.user_id for m in summary.teammates} == {U2, U3}
        assert names[U2] == "Member 12"
    with_env(body, monkeypatch)


def test_opted_out_teammates_never_appear_on_someone_elses_card(monkeypatch):
    async def body(env):
        await env.busy_year()
        env.member(U4)
        await env.voice(U4, MAY, MAY + 30 * HOUR)
        await env.post(U4, joiners=(U1,))  # the ledger-free tables still hold their squad
        await env.optout(U4)
        summary, names = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert U4 not in {m.user_id for m in summary.teammates} and U4 not in names
        await env.optout(U3)  # now only one teammate qualifies: the section goes
        summary, names = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.teammates == ()
        spec = WC.member_spec(summary, "A", "a", "S", names)
        assert "Favourite teammates" not in [s.title for s in spec.sections]
    with_env(body, monkeypatch)


def test_teammates_who_left_or_are_bots_are_not_named(monkeypatch):
    async def body(env):
        await env.busy_year()
        env.guild.members = [m for m in env.guild.members if m.id != U3]  # left
        env.member(BOTUSER, bot=True)
        await env.voice(BOTUSER, MAY, MAY + 30 * HOUR)
        summary, _ = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.teammates == ()  # only U2 is left to name
    with_env(body, monkeypatch)


def test_levels_gained_uses_the_january_snapshots(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO xp (user_id, xp, level) VALUES (?, 5000, 14)", (U1,))
        await env.cog.set_meta(W.levels_key(2026), json.dumps({str(U1): 6}))
        summary, _ = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.levels_gained == 8
        await env.cog.set_meta(W.levels_key(2027), json.dumps({str(U1): 12}))  # where the year ended
        summary, _ = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.levels_gained == 6
        await env.cog.set_meta(W.levels_key(2026), "not json")
        summary, _ = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.levels_gained is None
    with_env(body, monkeypatch)


def test_house_is_the_current_house_by_its_display_name(monkeypatch):
    async def body(env):
        place = "INSERT INTO house_members (user_id, house, joined_at, left_at) VALUES (?, ?, ?, ?)"
        await env.db.execute(place, (U1, "fern", MAY, None))
        assert await env.cog.house_of(U1) is None  # the houses module isn't loaded
        env.bot.cogs["Houses"] = object()
        assert await env.cog.house_of(U1) == "Fern"  # the name, not the stored key
        assert await env.cog.house_of(U2) is None  # never sorted
        summary, _ = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.house == "Fern"
        await env.db.execute(place, (U2, "otter", MAY, MAY + DAY))
        assert await env.cog.house_of(U2) is None  # left their house: a former member
        await env.db.execute(place, (U3, "ember_fall", MAY, None))
        assert await env.cog.house_of(U3) is None  # not one of the houses
    with_env(body, monkeypatch)


def test_house_is_left_out_when_the_table_cannot_be_read(monkeypatch):
    async def body(env):
        env.bot.cogs["Houses"] = object()
        await env.db.execute("DROP TABLE house_members")
        assert await env.cog.house_of(U1) is None
        summary, _ = await env.cog.member_summary(env.guild, env.member(U1), 2026)
        assert summary.house is None
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- sharing
def test_share_posts_the_card_publicly_without_pings(monkeypatch):
    async def body(env):
        await env.busy_year()
        i = await env.share(U1)
        (sent,) = env.guild.general.sent
        assert sent["content"].startswith(f"📓 <@{U1}>'s Wrapped 2026: **")
        no_pings(sent)
        assert png_size(sent["file"])[0] == WC.WIDTH and "view" not in sent
        assert i.of("defer") == [dict(ephemeral=True, thinking=True)]
        assert "Shared." in i.said and "Wrapped 2026** badge" in i.said
        assert await env.meta(W.share_key(2026, U1)) == str(env.t)
    with_env(body, monkeypatch)


def test_share_grants_the_badge_once_and_outside_the_catalogue_total(monkeypatch):
    async def body(env):
        await env.share(U1)
        env.t += W.SHARE_COOLDOWN
        again = await env.share(U1)
        assert again.said == "Shared."  # no second badge
        rows = await env.db.fetchall("SELECT key FROM achievements WHERE user_id = ?", (U1,))
        assert [r["key"] for r in rows] == ["wrapped_2026"]
        held = {"wrapped_2026", "chatty"}
        assert [b.name for b in A.held_badges(held)] == ["Chatty", "Wrapped 2026"]
        assert A.progress(held) == f"1/{A.TOTAL}" and "wrapped_2026" not in A.BY_KEY
        assert "📓" in A.grid(held)
    with_env(body, monkeypatch)


def test_only_the_owner_can_share_a_card(monkeypatch):
    async def body(env):
        i = await env.share(U2, owner=U1)
        assert "not your card" in i.said and env.guild.general.sent == []
        assert await env.meta(W.share_key(2026, U1)) is None
    with_env(body, monkeypatch)


def test_share_cooldown(monkeypatch):
    async def body(env):
        await env.share(U1)
        env.t += HOUR
        i = await env.share(U1)
        assert "again in about 5 hours" in i.said and len(env.guild.general.sent) == 1
        env.t += W.SHARE_COOLDOWN - HOUR - 60
        assert "about 1 minute" in (await env.share(U1)).said
        env.t += 60
        await env.share(U1)
        assert len(env.guild.general.sent) == 2
        await env.share(U2)  # per member
        assert len(env.guild.general.sent) == 3
    with_env(body, monkeypatch)


def test_share_cooldown_outlives_a_privacy_round_trip(monkeypatch):
    from cogs.stats import opt_out

    async def body(env):
        async def round_trip():
            await opt_out(env.db, U1, env.t)  # /privacy tracking:off ...
            await env.db.execute("DELETE FROM privacy_optout WHERE user_id = ?", (U1,))  # ... and on again
        await env.share(U1)
        shared_at = env.t
        env.t += HOUR
        await round_trip()
        assert await env.meta(W.share_key(2026, U1)) == str(shared_at)  # kept while the cooldown runs
        i = await env.share(U1)
        assert "again in about 5 hours" in i.said and len(env.guild.general.sent) == 1
        env.t = shared_at + W.SHARE_COOLDOWN
        await round_trip()
        assert await env.meta(W.share_key(2026, U1)) is None  # the cooldown is over: nothing left to keep
        await env.share(U1)
        assert len(env.guild.general.sent) == 2
        rows = await env.db.fetchall("SELECT key FROM achievements WHERE user_id = ?", (U1,))
        assert [r["key"] for r in rows] == ["wrapped_2026"]  # and the badge is still granted once
    with_env(body, monkeypatch)


def test_share_needs_the_member_to_be_able_to_post_there(monkeypatch):
    async def body(env):
        general = env.guild.general
        general.muted = {U1}  # can read general, can't write in it (a timeout-style overwrite)
        i = await env.share(U1)
        assert "can't post in this channel" in i.said and general.sent == []
        assert await env.meta(W.share_key(2026, U1)) is None  # no cooldown burned
        await env.share(U2)  # someone who can post is not affected
        assert len(general.sent) == 1
        # in a thread the thread permission is what counts
        thread = FakeText("a thread")
        thread.parent = general
        thread.permissions_for = lambda m: discord.Permissions(send_messages=True, send_messages_in_threads=False)
        assert "can't post in this channel" in (await env.share(U3, channel=thread)).said and thread.sent == []
        thread.permissions_for = lambda m: discord.Permissions(send_messages=False, send_messages_in_threads=True)
        assert "Shared." in (await env.share(U3, channel=thread)).said and len(thread.sent) == 1
        # a channel that can't say what the member may do is refused, not trusted
        odd = FakeText("odd")
        del FakeText.permissions_for
        try:
            assert "can't post in this channel" in (await env.share(U4, channel=odd)).said and odd.sent == []
        finally:
            FakeText.permissions_for = permissions_for
    permissions_for = FakeText.permissions_for
    with_env(body, monkeypatch)


def test_share_is_refused_in_quiet_and_staff_channels(monkeypatch):
    async def body(env):
        for channel in (env.guild.counting, env.guild.announcements, env.guild.mod):
            i = await env.share(U1, channel=channel)
            assert "can't be shared in this channel" in i.said and channel.sent == []
        assert await env.meta(W.share_key(2026, U1)) is None  # no cooldown burned
    with_env(body, monkeypatch)


def test_share_after_the_window_closed(monkeypatch):
    async def body(env):
        env.t = ts(2027, 2, 1)
        i = await env.share(U1)  # an old button, pressed in February
        assert "has closed" in i.said and env.guild.general.sent == []
        owner = await env.share(OWNER)  # staff can still post one (previews)
        assert "Shared." in owner.said
    with_env(body, monkeypatch)


def test_failed_share_gives_the_cooldown_back(monkeypatch):
    async def body(env):
        env.guild.general.fail = discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Permissions")
        i = await env.share(U1)
        assert "couldn't post there" in i.said
        assert await env.meta(W.share_key(2026, U1)) is None
        assert await env.db.fetchone("SELECT 1 FROM achievements WHERE user_id = ?", (U1,)) is None
        env.guild.general.fail = None
        assert "Shared." in (await env.share(U1)).said
    with_env(body, monkeypatch)


def test_share_button_round_trips_its_custom_id(monkeypatch):
    async def body(env):
        button = ShareButton(2026, U1)
        match = ShareButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)
        rebuilt = await ShareButton.from_custom_id(None, None, match)
        assert (rebuilt.year, rebuilt.user_id) == (2026, U1)
        # Pressing it goes through the cog, so it works after a restart.
        i = env.click(U1)
        await rebuilt.callback(i)
        assert len(env.guild.general.sent) == 1
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- /wrapped-preview
def test_preview_is_staff_only(monkeypatch):
    async def body(env):
        env.t = CLOSED
        i = await env.preview(U1)
        assert "Only staff" in i.said and env.cog.renders == 0
        perms = Wrapped.wrapped_preview.default_permissions
        assert perms is not None and perms.value == discord.Permissions(moderate_members=True).value
        assert Wrapped.wrapped.default_permissions is None
    with_env(body, monkeypatch)


def test_staff_can_preview_any_time_any_member_and_the_server(monkeypatch):
    async def body(env):
        env.t = CLOSED
        await env.busy_year()
        mine = (await env.preview(OWNER)).of("followup")[0]
        assert "Your Wrapped 2026" in mine["content"] and mine["ephemeral"] is True
        theirs = (await env.preview(OWNER, member=env.member(U1))).of("followup")[0]
        assert "Member 11's Wrapped 2026" in theirs["content"] and "view" not in theirs  # can't share another's
        server = (await env.preview(OWNER, server=True)).of("followup")[0]
        assert server["file"].filename == "wrapped-server.png" and server["ephemeral"] is True
        assert "nothing to wrap for 2031" in (await env.preview(OWNER, year=2031)).said
        await env.optout(U1)
        assert "stat tracking turned off" in (await env.preview(OWNER, member=env.member(U1))).said
        assert "Bots" in (await env.preview(OWNER, member=env.member(BOTUSER, bot=True))).said
    with_env(body, monkeypatch)


def test_previewed_names_are_escaped(monkeypatch):
    async def body(env):
        env.t = CLOSED
        sneaky = env.member(U1, name="**bold** @everyone")
        sent = (await env.preview(OWNER, member=sneaky)).of("followup")[0]
        assert "**bold**" not in sent["content"] and "@everyone" not in sent["content"]
        no_pings(sent)
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- the server summary
def test_server_summary_names_only_tracking_members(monkeypatch):
    async def body(env):
        await env.busy_year()
        env.member(U4)
        await env.messages(U4, "2026-05-01", 5000)
        await env.optout(U4)
        summary, names = await env.cog.server_summary(env.guild, 2026)
        assert summary.messages == 5200  # totals count everything recorded
        chat = next(s for s in summary.superlatives if s.key == "chat")
        assert chat.user_ids == (U1,) and U4 not in names
        spec = WC.server_spec(summary, env.guild.name, names)
        assert "Member 14" not in repr(spec)
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- the two posts
DEC1 = ts(2026, 12, 1, 10, 0)
DEC20 = ts(2026, 12, 20, 10, 0)


def test_announcement_posts_once_on_december_first_with_the_server_card(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.tick(CLOSED)  # first sight of the jobs: last year's periods are just marked
        assert env.guild.announcements.sent == [] and await env.jobs() == {"wrapped:2025", "wrappedreminder:2025"}
        await env.tick(DEC1 - 60)
        assert env.guild.announcements.sent == []
        await env.tick(DEC1)
        (sent,) = env.guild.announcements.sent
        assert "Wrapped 2026 is open" in sent["content"] and "@everyone" not in sent["content"]
        no_pings(sent)
        assert sent["file"].filename == "wrapped-server.png" and png_size(sent["file"])[0] == WC.WIDTH
        await env.tick(DEC1 + 300)
        await env.tick(DEC1 + DAY)
        assert len(env.guild.announcements.sent) == 1  # never twice
        assert "wrapped:2026" in await env.jobs()
    with_env(body, monkeypatch)


def test_reminder_on_december_twentieth_and_nothing_else(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.tick(CLOSED)
        await env.tick(DEC1)
        await env.tick(DEC20 - 60)
        assert len(env.guild.announcements.sent) == 1
        await env.tick(DEC20)
        assert len(env.guild.announcements.sent) == 2
        reminder = env.guild.announcements.sent[1]
        assert "still open" in reminder["content"] and "file" not in reminder
        no_pings(reminder)
        for at in (DEC20 + 300, ts(2026, 12, 31, 12), ts(2027, 1, 15, 12), ts(2027, 3, 1), ts(2027, 11, 30)):
            await env.tick(at)
        assert len(env.guild.announcements.sent) == 2
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_announcement_catches_up_once_after_downtime_and_skips_the_reminder_if_close(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.tick(CLOSED)
        await env.tick(ts(2026, 12, 18, 9))  # the PC was off from before Dec 1
        assert len(env.guild.announcements.sent) == 1 and "is open" in env.guild.announcements.sent[0]["content"]
        await env.tick(DEC20)  # two days later: a reminder would be noise
        assert len(env.guild.announcements.sent) == 1
        assert "wrappedreminder:2026" in await env.jobs()
    with_env(body, monkeypatch)


def test_jobs_switched_on_mid_december_do_not_backfill_the_announcement(monkeypatch):
    async def body(env):
        await env.tick(ts(2026, 12, 5, 12))  # the very first tick is after Dec 1
        await env.tick(ts(2026, 12, 6, 12))
        assert env.guild.announcements.sent == []
        await env.tick(DEC20)  # the reminder still goes out
        assert len(env.guild.announcements.sent) == 1 and "still open" in env.guild.announcements.sent[0]["content"]
    with_env(body, monkeypatch)


def test_announcement_retries_when_discord_fails_or_the_channel_is_missing(monkeypatch):
    async def body(env):
        await env.busy_year()
        await env.tick(CLOSED)
        channels, env.guild.text_channels = env.guild.text_channels, [env.guild.general]
        await env.tick(DEC1)
        assert "wrapped:2026" not in await env.jobs()
        env.guild.text_channels = channels
        env.guild.announcements.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        await env.tick(DEC1 + 300)
        assert "wrapped:2026" not in await env.jobs()
        env.guild.announcements.fail = None
        await env.tick(DEC1 + 600)
        assert len(env.guild.announcements.sent) == 1 and "wrapped:2026" in await env.jobs()
    with_env(body, monkeypatch)


def test_empty_year_announces_without_an_empty_card(monkeypatch):
    async def body(env):
        await env.tick(CLOSED)
        await env.tick(DEC1)
        (sent,) = env.guild.announcements.sent
        assert "file" not in sent and "is open" in sent["content"] and not sent["content"].endswith("went:")
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- level snapshot
def test_level_snapshot_is_taken_once_in_january_without_opted_out_members(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO xp (user_id, xp, level) VALUES (?, 100, 4), (?, 900, 9)", (U1, U2))
        await env.optout(U2)
        await env.tick(ts(2026, 12, 31, 23, 50))
        assert await env.meta(W.levels_key(2027)) is None and await env.meta(W.levels_key(2026)) is None
        await env.tick(ts(2027, 1, 1, 0, 5))
        assert json.loads(await env.meta(W.levels_key(2027))) == {str(U1): 4}
        await env.db.execute("UPDATE xp SET level = 30 WHERE user_id = ?", (U1,))
        await env.tick(ts(2027, 1, 20))
        assert json.loads(await env.meta(W.levels_key(2027))) == {str(U1): 4}  # the start stays the start
    with_env(body, monkeypatch)


def test_opting_out_later_removes_the_member_from_every_snapshot(monkeypatch):
    async def body(env):
        await env.cog.set_meta(W.levels_key(2026), json.dumps({str(U1): 4, str(U2): 9}))
        await env.cog.set_meta(W.levels_key(2027), json.dumps({str(U2): 12}))
        await env.optout(U2)
        await env.tick(ts(2027, 6, 1))
        assert json.loads(await env.meta(W.levels_key(2026))) == {str(U1): 4}
        assert json.loads(await env.meta(W.levels_key(2027))) == {}
    with_env(body, monkeypatch)


def test_nothing_about_a_members_year_is_stored(monkeypatch):
    async def body(env):
        await env.busy_year()
        before = {r["key"] for r in await env.db.fetchall("SELECT key FROM meta")}
        await env.wrapped(U1)
        assert {r["key"] for r in await env.db.fetchall("SELECT key FROM meta")} == before
        await env.share(U1)
        new = {r["key"] for r in await env.db.fetchall("SELECT key FROM meta")} - before
        assert new == {W.share_key(2026, U1)}
    with_env(body, monkeypatch)
