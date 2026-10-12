"""Offline tests for cogs.votehub: a real in-memory SQLite database plus small fakes for the
bot, guild, channels and interactions. No network."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import votehub as cogmod
from cogs.growth import BUMP_DUE_KEY
from cogs.votehub import VoteHub
from logic import growth as G
from logic import votehub as V

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
HOUR = 3600
DAY = 24 * HOUR


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


SUNDAY = ts(2026, 10, 11, 17, 0)  # the support:2026-W41 run
PREV = ts(2026, 10, 4, 17, 0)
KEY = "support:2026-W41"


def old(n: int) -> int:
    return discord.utils.time_snowflake(datetime(2020, 1, 1, tzinfo=timezone.utc)) + n


def young(n: int) -> int:
    return discord.utils.time_snowflake(datetime.fromtimestamp(SUNDAY - 5 * DAY, timezone.utc)) + n


A, B, C, D = old(1), old(2), old(3), old(4)


def run(coro):
    return asyncio.run(coro)


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


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.games = FakeText(config.GAMES_CHANNEL)
        self.botcmd = FakeText(config.BOT_COMMANDS_CHANNEL)
        self.text_channels = [self.botcmd, self.games]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class FakeResponse:
    def __init__(self, calls, fail=None):
        self.calls = calls
        self.done = False
        self.fail = fail

    async def send_message(self, content=None, **kwargs):
        if self.fail is not None:
            fail, self.fail = self.fail, None
            raise fail
        self.done = True
        self.calls.append(dict(content=content, **kwargs))

    def is_done(self):
        return self.done


class FakeInteraction:
    def __init__(self, bot, user_id, fail=None):
        self.calls = []
        self.client = bot
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.guild = bot.guild
        self.response = FakeResponse(self.calls, fail)
        self.followup = SimpleNamespace(send=self._followup)

    async def _followup(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = SUNDAY

    async def bump(self, uid, at, times=1):
        for i in range(times):
            await self.db.execute("INSERT INTO bumps (user_id, at) VALUES (?, ?)", (uid, at + i))

    async def seen(self, at):
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (cogmod.SEEN_KEY, str(at)))

    async def done(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def ledger(self):
        rows = await self.db.fetchall("SELECT user_id, delta, reason, ref FROM ledger ORDER BY id")
        return [(r["user_id"], r["delta"], r["reason"], r["ref"]) for r in rows]

    async def vote(self, uid, fail=None):
        inter = FakeInteraction(self.bot, uid, fail)
        await self.cog.vote.callback(self.cog, inter)
        return inter

    def fields(self, inter):
        embed = inter.calls[0]["embed"]
        return {f.name: f.value for f in embed.fields}


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        env = Env(db, guild, bot, VoteHub(bot))
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ------------------------------------------------------------ /vote

def test_vote_is_ephemeral_and_lists_every_place(monkeypatch):
    async def go(env):
        inter = await env.vote(A)
        (call,) = inter.calls
        assert call["ephemeral"] is True
        assert call["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        fields = env.fields(inter)
        assert list(fields) == ["Disboard", "discord.me", "top.gg", "Share"]
        assert f"`/bump` in {env.guild.botcmd.mention}" in fields["Disboard"]
        assert "Ready now." in fields["Disboard"] and "haven't bumped yet" in fields["Disboard"]
        assert V.DISCORD_ME_URL in fields["discord.me"]
        assert V.SHARE_URL in fields["Share"]
        assert "300/200/100" in call["embed"].description
        urls = [item.url for item in call["view"].children]
        assert V.DISCORD_ME_URL in urls and V.SHARE_URL in urls
        assert all(u.startswith("https://") for u in urls)
    with_env(go, monkeypatch)


def test_vote_topgg_is_coming_soon_until_the_url_is_set(monkeypatch):
    async def go(env):
        monkeypatch.setattr(V, "TOPGG_URL", "")
        inter = await env.vote(A)
        assert env.fields(inter)["top.gg"] == V.COMING_SOON
        assert len(inter.calls[0]["view"].children) == 2
        monkeypatch.setattr(V, "TOPGG_URL", "https://top.gg/discord/servers/example")
        inter = await env.vote(A)
        assert "(https://top.gg/discord/servers/example)" in env.fields(inter)["top.gg"]
        assert "https://top.gg/discord/servers/example" in [i.url for i in inter.calls[0]["view"].children]
    with_env(go, monkeypatch)


def test_vote_shows_my_bump_counts_from_the_growth_table(monkeypatch):
    async def go(env):
        env.t = SUNDAY + 2 * DAY
        await env.bump(A, SUNDAY + HOUR, times=2)  # since the last thanks
        await env.bump(A, SUNDAY - 3 * DAY)  # last week, inside 30 days
        await env.bump(A, SUNDAY - 40 * DAY)  # too old
        await env.bump(B, SUNDAY + HOUR, times=5)  # someone else
        text = env.fields(await env.vote(A))["Disboard"]
        assert "Your bumps: 2 this week · 3 in the past 30 days." in text
        assert "haven't bumped yet" in env.fields(await env.vote(C))["Disboard"]
    with_env(go, monkeypatch)


def test_vote_next_bump_uses_the_pending_reminder_or_the_last_bump(monkeypatch):
    async def go(env):
        await env.bump(B, env.t - HOUR)  # an hour ago: an hour to go
        due = env.t - HOUR + G.REMINDER_DELAY
        assert f"Ready <t:{due}:R>." in env.fields(await env.vote(A))["Disboard"]
        # a bump by someone Discord didn't name: no bumps row, only the reminder time
        await env.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (BUMP_DUE_KEY, str(due + 600)))
        assert f"Ready <t:{due + 600}:R>." in env.fields(await env.vote(A))["Disboard"]
        env.t = due + 601
        assert "Ready now." in env.fields(await env.vote(A))["Disboard"]
        await env.db.execute("UPDATE meta SET value = 'junk' WHERE key = ?", (BUMP_DUE_KEY,))
        assert "Ready now." in env.fields(await env.vote(A))["Disboard"]
    with_env(go, monkeypatch)


def test_vote_without_the_bot_channel_still_answers(monkeypatch):
    async def go(env):
        env.guild.text_channels = [env.guild.games]
        assert "in the bot channel" in env.fields(await env.vote(A))["Disboard"]
    with_env(go, monkeypatch)


def test_vote_never_raises(monkeypatch):
    async def go(env):
        inter = await env.vote(A, fail=RuntimeError("boom"))
        (call,) = inter.calls  # the generic error reply
        assert call.get("ephemeral") is True and call.get("embed") is None
    with_env(go, monkeypatch)


# ------------------------------------------------------------ weekly bump thanks

def test_first_run_only_marks_the_week_done(monkeypatch):
    async def go(env):
        await env.bump(A, SUNDAY - DAY)
        assert await env.cog.run_support() is False
        assert await env.done() == {KEY}
        assert env.guild.games.sent == [] and await env.ledger() == []
    with_env(go, monkeypatch)


def test_thanks_top_three_and_pays_with_refs(monkeypatch):
    async def go(env):
        await env.seen(PREV)
        await env.bump(A, SUNDAY - 3 * DAY, times=2)
        await env.bump(B, SUNDAY - 2 * DAY, times=4)
        await env.bump(C, SUNDAY - DAY)
        await env.bump(D, SUNDAY - HOUR)  # ties C, but got there later
        await env.bump(D, PREV - HOUR, times=9)  # the week before: not counted
        await env.bump(D, SUNDAY + 5, times=9)  # after the cut-off: next week's
        env.t = SUNDAY + 60
        assert await env.cog.run_support() is True
        (msg,) = env.guild.games.sent
        assert msg["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        text = msg["embed"].description
        assert msg["embed"].title == "Bump thanks · 2026-W41"
        # "Supporter" now means the Supporter tier: the bump thanks never uses the word
        shown = f"{msg['embed'].title} {text} {msg['embed'].footer.text}".lower()
        assert "supporter" not in shown and "bump thanks" in msg["embed"].footer.text.lower()
        assert text.index(f"<@{B}>") < text.index(f"<@{A}>") < text.index(f"<@{C}>")
        assert f"<@{D}>" not in text
        assert "4 bumps · +300 coins" in text and "2 bumps · +200 coins" in text and "1 bump · +100 coins" in text
        assert await env.ledger() == [(B, 300, "support", "support:2026-W41:1"),
                                      (A, 200, "support", "support:2026-W41:2"),
                                      (C, 100, "support", "support:2026-W41:3")]
        assert (await env.balance(B), await env.balance(A), await env.balance(C), await env.balance(D)) == (300, 200, 100, 0)
        assert KEY in await env.done()
        # the next ticks do nothing
        assert await env.cog.run_support() is False
        assert len(env.guild.games.sent) == 1 and len(await env.ledger()) == 3
    with_env(go, monkeypatch)


def test_fresh_accounts_are_thanked_but_not_paid(monkeypatch):
    async def go(env):
        alt = young(7)
        await env.seen(PREV)
        await env.bump(alt, SUNDAY - DAY, times=3)
        await env.bump(A, SUNDAY - DAY)
        assert await env.cog.run_support() is True
        text = env.guild.games.sent[0]["embed"].description
        assert f"<@{alt}>  3 bumps · coins need a" in text
        assert await env.ledger() == [(A, 200, "support", "support:2026-W41:2")]
        assert await env.balance(alt) == 0
    with_env(go, monkeypatch)


def test_nobody_bumped_posts_nothing_and_is_done(monkeypatch):
    async def go(env):
        await env.seen(PREV)
        await env.bump(A, PREV - HOUR)  # an older week
        assert await env.cog.run_support() is True
        assert env.guild.games.sent == [] and await env.ledger() == []
        assert KEY in await env.done()
    with_env(go, monkeypatch)


def test_no_games_channel_waits_and_pays_nothing_yet(monkeypatch):
    async def go(env):
        await env.seen(PREV)
        await env.bump(A, SUNDAY - DAY)
        env.guild.text_channels = [env.guild.botcmd]
        assert await env.cog.run_support() is False
        assert await env.ledger() == [] and KEY not in await env.done()
        env.guild.text_channels.append(env.guild.games)
        assert await env.cog.run_support() is True
        assert len(env.guild.games.sent) == 1 and await env.balance(A) == 300
    with_env(go, monkeypatch)


def test_failed_post_retries_without_paying_twice(monkeypatch):
    async def go(env):
        await env.seen(PREV)
        await env.bump(A, SUNDAY - DAY)
        env.guild.games.fail = discord.HTTPException(SimpleNamespace(status=500, reason="oops"), "server error")
        assert await env.cog.run_support() is None  # logged, not raised
        assert KEY not in await env.done()
        env.guild.games.fail = discord.Forbidden(SimpleNamespace(status=403, reason="no"), "Missing Permissions")
        assert await env.cog.run_support() is False
        assert KEY not in await env.done()
        env.guild.games.fail = None
        env.t = SUNDAY + 10 * 60
        assert await env.cog.run_support() is True
        assert len(env.guild.games.sent) == 1
        assert "+300 coins" in env.guild.games.sent[0]["embed"].description
        assert await env.ledger() == [(A, 300, "support", "support:2026-W41:1")]
    with_env(go, monkeypatch)


def test_catches_up_once_after_downtime(monkeypatch):
    async def go(env):
        await env.seen(PREV - 30 * DAY)
        await env.bump(A, SUNDAY - DAY)
        await env.bump(B, PREV - DAY, times=3)  # a week missed while the bot was off
        env.t = SUNDAY + 2 * DAY  # Tuesday: the bot comes back
        assert await env.cog.run_support() is True
        (msg,) = env.guild.games.sent
        assert f"<@{A}>" in msg["embed"].description and f"<@{B}>" not in msg["embed"].description
        done = await env.done()
        assert KEY in done and "support:2026-W40" in done  # the missed week is closed, not replayed
        assert await env.balance(B) == 0
        assert await env.cog.run_support() is False
    with_env(go, monkeypatch)


def test_before_sunday_five_nothing_runs(monkeypatch):
    async def go(env):
        await env.seen(PREV + 60)
        await env.bump(A, SUNDAY - DAY)
        env.t = SUNDAY - 60
        assert await env.cog.run_support() is False
        assert env.guild.games.sent == [] and await env.ledger() == []
    with_env(go, monkeypatch)


def test_guild_missing_does_not_raise(monkeypatch):
    async def go(env):
        await env.seen(PREV)
        await env.bump(A, SUNDAY - DAY)
        env.bot.guild = None
        assert await env.cog.run_support() is False
        assert await env.ledger() == []
    with_env(go, monkeypatch)


def test_setup_registers_the_cog_and_command():
    async def go():
        added = []
        bot = SimpleNamespace(add_cog=lambda cog: added.append(cog) or asyncio.sleep(0))
        await cogmod.setup(bot)
        (cog,) = added
        assert isinstance(cog, VoteHub) and cog.vote.name == "vote"
    run(go())
