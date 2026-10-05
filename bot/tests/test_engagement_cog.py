"""Offline tests for cogs.engagement.Engagement: a real in-memory SQLite database plus
lightweight fakes for the bot, guild, channels, members and interactions. No network.
Time is controlled by patching cogs.engagement.now."""

import asyncio
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import config
import db as dbmod
import economy
from cogs import engagement as cogmod
from cogs.engagement import Engagement

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
A, B, C, BOTUSER = 1, 2, 3, 50
HOUR = 3600
DAY = 24 * HOUR


def run(coro):
    return asyncio.run(coro)


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


def http_error(status=500, cls=discord.HTTPException):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class FakeRole:
    _next = 7000

    def __init__(self, name):
        FakeRole._next += 1
        self.id = FakeRole._next
        self.name = name
        self.members = []


class FakeMessage:
    _next = 10_000

    def __init__(self, channel, content=None, author=None, **kwargs):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.channel = channel
        self.content = content
        self.author = author
        self.guild = getattr(channel, "guild", None)
        self.type = discord.MessageType.default
        self.kwargs = kwargs
        self.reactions = []
        self.threads = []
        self.thread_fail = None

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def create_thread(self, **kwargs):
        if self.thread_fail is not None:
            raise self.thread_fail
        self.threads.append(kwargs)


class FakeText:
    _next = 300

    def __init__(self, name, guild):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.guild = guild
        self.mention = f"<#{self.id}>"
        self.sent: list[FakeMessage] = []
        self.fail = None
        self.thread_fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        msg = FakeMessage(self, content, **kwargs)
        msg.thread_fail = self.thread_fail
        self.sent.append(msg)
        return msg


class FakeMember:
    def __init__(self, uid, guild, bot=False):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.name = self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = []
        self.role_fail = None

    async def add_roles(self, role, reason=None):
        if self.role_fail is not None:
            raise self.role_fail
        if role not in self.roles:
            self.roles.append(role)
            role.members.append(self)

    async def remove_roles(self, role, reason=None):
        if self.role_fail is not None:
            raise self.role_fail
        if role in self.roles:
            self.roles.remove(role)
            role.members.remove(self)


class FakeEvent:
    def __init__(self, eid, start_time, status=discord.EventStatus.scheduled, **kwargs):
        self.id = eid
        self.start_time = start_time
        self.status = status
        self.name = kwargs.get("name", "Game night")
        self.url = f"https://discord.com/events/{GUILD_ID}/{eid}"
        self.kwargs = kwargs


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.general = FakeText(config.GENERAL_CHANNEL, self)
        self.counting = FakeText(config.COUNTING_CHANNEL, self)
        self.gaming = FakeText(config.GAMING_CHANNEL, self)
        self.text_channels = [self.general, self.counting, self.gaming]
        self.squad = SimpleNamespace(id=101, name=config.SQUAD_VOICE, mention="<#101>")
        self.voice_channels = [self.squad]
        self.roles = [FakeRole(config.BIRTHDAY_ROLE), FakeRole(config.COUNTING_ROLE)]
        self.members: dict[int, FakeMember] = {}
        self.scheduled_events: list[FakeEvent] = []
        self.created: list[dict] = []
        self.create_fail = None

    def role(self, name):
        return config.match_by_name(self.roles, name)

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        if uid in self.members:
            return self.members[uid]
        raise http_error(404, discord.NotFound)

    async def create_scheduled_event(self, **kwargs):
        if self.create_fail is not None:
            raise self.create_fail
        self.created.append(kwargs)
        ev = FakeEvent(800 + len(self.created), **kwargs)
        self.scheduled_events.append(ev)
        return ev


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.intents = SimpleNamespace(members=True, message_content=True)
        self.user = SimpleNamespace(id=BOTUSER)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    async def send_message(self, content=None, **kwargs):
        assert not self.done
        self.done = True
        self.calls.append(dict(content=content, **kwargs))

    def is_done(self):
        return self.done


class FakeInteraction:
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        self.response = FakeResponse(self.calls)


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = ts(2026, 10, 4, 8)

    def member(self, uid, **kw):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, **kw)
        return self.guild.members[uid]

    def inter(self, uid):
        return FakeInteraction(self.member(uid), self.guild)

    async def tick(self, at=None):
        if at is not None:
            self.t = at
        await Engagement.tick.coro(self.cog)

    async def say(self, uid, content, channel=None, bot=False):
        ch = channel or self.guild.counting
        msg = FakeMessage(ch, content, author=self.member(uid, bot=bot))
        await self.cog.on_message(msg)
        return msg

    async def jobs(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}

    async def meta(self, key):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def counting(self):
        row = await self.db.fetchone("SELECT * FROM counting")
        return dict(row) if row else None

    async def run_counts(self):
        return {r["user_id"]: r["n"] for r in await self.db.fetchall("SELECT * FROM counting_run")}


QUESTIONS = ["Q one?", "Q two?", "Q three?"]
PAIRS = [("Cats", "Dogs"), ("Tea", "Coffee")]


def with_env(fn, monkeypatch, questions=QUESTIONS, pairs=PAIRS):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Engagement(bot, questions=list(questions), pairs=list(pairs), rng=random.Random(5))
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def embeds_text(channel):
    return [m.kwargs["embed"].description for m in channel.sent if m.kwargs.get("embed")]


# ---------------------------------------------------------------- question of the day
def test_qotd_first_run_marks_done_then_posts_daily_with_thread(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 4, 13))  # very first startup, after noon
        assert env.guild.general.sent == []
        assert "qotd:2026-10-04" in await env.jobs()

        await env.tick(ts(2026, 10, 5, 11, 59))  # (the 4th's 18:00 poll has gone out by now)
        assert embeds_text(env.guild.general) == []
        await env.tick(ts(2026, 10, 5, 12, 0))
        posted = [m for m in env.guild.general.sent if m.kwargs.get("embed")]
        assert len(posted) == 1
        msg = posted[0]
        assert msg.kwargs["embed"].description in QUESTIONS
        assert msg.kwargs["embed"].title == "Question of the day"
        assert msg.kwargs["allowed_mentions"].users is False
        assert msg.threads and "answers" in msg.threads[0]["name"]
        assert "qotd:2026-10-05" in await env.jobs()
        await env.tick(ts(2026, 10, 5, 12, 5))
        assert len([m for m in env.guild.general.sent if m.kwargs.get("embed")]) == 1
    with_env(go, monkeypatch)


def test_qotd_no_repeats_until_exhausted_then_reshuffles(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 1, 13))
        seen = []
        for d in range(2, 9):
            await env.tick(ts(2026, 10, d, 12, 1))
            seen.append([m.kwargs["embed"].description for m in env.guild.general.sent
                         if m.kwargs.get("embed")][-1])
        assert sorted(seen[:3]) == sorted(QUESTIONS)
        assert sorted(seen[3:6]) == sorted(QUESTIONS)
        for a, b in zip(seen, seen[1:]):
            assert a != b  # never the same question two days running, even across a reshuffle
    with_env(go, monkeypatch)


def test_qotd_send_failure_retries_and_thread_failure_does_not_repost(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 4, 13))
        env.guild.general.fail = http_error()
        await env.tick(ts(2026, 10, 5, 12, 1))
        assert "qotd:2026-10-05" not in await env.jobs()
        assert await env.db.fetchall("SELECT * FROM qotd_used") == []
        env.guild.general.fail = None
        env.guild.general.thread_fail = http_error(403, discord.Forbidden)
        await env.tick(ts(2026, 10, 5, 12, 2))
        assert "qotd:2026-10-05" in await env.jobs()
        await env.tick(ts(2026, 10, 5, 12, 3))
        assert len([m for m in env.guild.general.sent if m.kwargs.get("embed")]) == 1
        assert len(await env.db.fetchall("SELECT * FROM qotd_used")) == 1
    with_env(go, monkeypatch)


def test_qotd_without_general_channel_is_skipped(monkeypatch):
    async def go(env):
        env.guild.text_channels.remove(env.guild.general)
        await env.tick(ts(2026, 10, 4, 13))
        await env.tick(ts(2026, 10, 5, 12, 1))
        assert "qotd:2026-10-05" in await env.jobs()
    with_env(go, monkeypatch)


def test_real_banks_load():
    cog = Engagement(SimpleNamespace())
    assert len(cog.questions) >= 365 and len(cog.pairs) >= 200


# ---------------------------------------------------------------- daily poll
def test_poll_posts_native_poll_for_24h(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 4, 19))
        await env.tick(ts(2026, 10, 5, 18, 0))
        polls = [m.kwargs["poll"] for m in env.guild.general.sent if m.kwargs.get("poll")]
        assert len(polls) == 1
        p = polls[0]
        assert isinstance(p, discord.Poll)
        assert p.duration == timedelta(hours=24)
        assert tuple(a.text for a in p.answers) in PAIRS
        assert "thisorthat:2026-10-05" in await env.jobs()
        await env.tick(ts(2026, 10, 6, 18, 0))
        polls = [m.kwargs["poll"] for m in env.guild.general.sent if m.kwargs.get("poll")]
        assert len(polls) == 2
        assert {tuple(a.text for a in p.answers) for p in polls} == set(PAIRS)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- counting
def test_counting_happy_path_and_best(monkeypatch):
    async def go(env):
        msgs = [await env.say(u, str(n)) for u, n in ((A, 1), (B, 2), (A, 3), (C, 4))]
        assert [m.reactions for m in msgs] == [["✅"]] * 4
        state = await env.counting()
        assert (state["current"], state["last_user"], state["best"]) == (4, C, 4)
        assert await env.run_counts() == {A: 2, B: 1, C: 1}
    with_env(go, monkeypatch)


def test_double_count_resets_with_note(monkeypatch):
    async def go(env):
        await env.say(A, "1")
        await env.say(B, "2")
        bad = await env.say(B, "3")
        assert bad.reactions == ["❌"]
        note = env.guild.counting.sent[-1]
        assert "twice in a row" in note.content and "**2**" in note.content
        assert note.kwargs["allowed_mentions"].users is False
        state = await env.counting()
        assert (state["current"], state["last_user"], state["best"]) == (0, None, 2)
        assert await env.run_counts() == {}
        ok = await env.say(B, "1")  # after a reset anyone, even the breaker, starts over
        assert ok.reactions == ["✅"]
    with_env(go, monkeypatch)


def test_wrong_number_resets(monkeypatch):
    async def go(env):
        await env.say(A, "1")
        bad = await env.say(B, "3")
        assert bad.reactions == ["❌"]
        assert "next number was **2**" in env.guild.counting.sent[-1].content
        assert (await env.counting())["current"] == 0
    with_env(go, monkeypatch)


@pytest.mark.parametrize("text", ["hello", "1!", "two", "", "1 2", "-1", "4.0", "🎉"])
def test_non_integers_are_ignored(monkeypatch, text):
    async def go(env):
        await env.say(A, "1")
        msg = await env.say(B, text)
        assert msg.reactions == []
        assert (await env.counting())["current"] == 1
        assert env.guild.counting.sent == []
    with_env(go, monkeypatch)


def test_bots_other_channels_and_threads_are_ignored(monkeypatch):
    async def go(env):
        await env.say(A, "1", bot=True)
        await env.say(B, "1", channel=env.guild.general)
        thread = SimpleNamespace(id=555, name=config.COUNTING_CHANNEL, parent=env.guild.counting, guild=env.guild)
        await env.say(C, "1", channel=thread)
        assert await env.counting() is None
    with_env(go, monkeypatch)


def test_edits_and_deletes_have_no_listener():
    listeners = {name for name, _ in Engagement.__cog_listeners__}
    assert "on_message" in listeners
    assert not listeners & {"on_message_edit", "on_raw_message_edit", "on_message_delete",
                            "on_raw_message_delete"}


def test_champ_role_goes_to_top_counter_of_best_run_and_swaps(monkeypatch):
    async def go(env):
        role = env.guild.role(config.COUNTING_ROLE)
        for u, n in ((A, 1), (B, 2), (A, 3)):
            await env.say(u, str(n))
        assert [m.id for m in role.members] == [A]
        assert await env.meta("counting_champ") == str(A)
        await env.say(C, "9")  # reset; best stays 3 with A as champ
        for u, n in ((B, 1), (C, 2), (B, 3)):
            await env.say(u, str(n))
        assert [m.id for m in role.members] == [A]  # 3 only ties the best
        await env.say(C, "4")  # B 2, C 2: tie, the old champ isn't in it -> lowest id (B)
        assert [m.id for m in role.members] == [B]
        assert await env.meta("counting_champ") == str(B)
    with_env(go, monkeypatch)


def test_opted_out_member_counts_but_is_not_recorded(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, 0)", (A,))
        a = await env.say(A, "1")
        await env.say(B, "2")
        assert a.reactions == ["✅"]
        assert await env.run_counts() == {B: 1}
        assert [m.id for m in env.guild.role(config.COUNTING_ROLE).members] == [B]
    with_env(go, monkeypatch)


def test_counting_listener_never_raises(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "count", boom)
        await env.say(A, "1")  # logged, not raised
    with_env(go, monkeypatch)


def test_reaction_failure_still_counts(monkeypatch):
    async def go(env):
        msg = FakeMessage(env.guild.counting, "1", author=env.member(A))

        async def fail(emoji):
            raise http_error(403, discord.Forbidden)
        msg.add_reaction = fail
        await env.cog.on_message(msg)
        assert (await env.counting())["current"] == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- birthday commands
def test_birthday_set_validates_and_saves(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await env.cog.set_birthday(inter, 2, 30)
        assert "doesn't have a day 30" in inter.calls[0]["content"] and inter.calls[0]["ephemeral"]
        assert await env.db.fetchall("SELECT * FROM birthdays") == []
        inter = env.inter(A)
        await env.cog.set_birthday(inter, 2, 29)
        assert "February 29" in inter.calls[0]["content"] and "28th" in inter.calls[0]["content"]
        inter = env.inter(A)
        await env.cog.set_birthday(inter, 10, 12)  # replaces
        rows = [dict(r) for r in await env.db.fetchall("SELECT * FROM birthdays")]
        assert rows == [{"user_id": A, "month": 10, "day": 12}]
    with_env(go, monkeypatch)


def test_birthday_remove(monkeypatch):
    async def go(env):
        await env.cog.set_birthday(env.inter(A), 5, 5)
        inter = env.inter(A)
        await env.cog.remove_birthday(inter)
        assert "forgotten" in inter.calls[0]["content"]
        inter = env.inter(A)
        await env.cog.remove_birthday(inter)
        assert "don't have" in inter.calls[0]["content"]
    with_env(go, monkeypatch)


def test_birthday_list_next_five_no_pings(monkeypatch):
    async def go(env):
        env.t = ts(2026, 10, 4, 10)
        dates = {A: (10, 4), B: (12, 1), C: (1, 2), 4: (10, 10), 5: (11, 11), 6: (3, 3), 7: (10, 5)}
        for uid, (m, d) in dates.items():
            await env.cog.set_birthday(env.inter(uid), m, d)
        await env.db.execute("INSERT INTO birthdays VALUES (99, 10, 6)")  # left the server
        inter = env.inter(A)
        await env.cog.list_birthdays(inter)
        call = inter.calls[0]
        lines = call["embed"].description.splitlines()
        assert [ln.split("<@")[1].split(">")[0] for ln in lines] == ["1", "7", "4", "5", "2"]
        assert "today" in lines[0]
        assert call["allowed_mentions"].users is False
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- birthday day
async def first_seen_birthdays(env, day):
    await env.tick(ts(*day, 10))  # first startup after 09:00 marks that day done


def test_birthday_shoutout_role_coins_and_expiry(monkeypatch):
    async def go(env):
        await env.cog.set_birthday(env.inter(A), 10, 5)
        await env.cog.set_birthday(env.inter(B), 10, 5)
        await env.cog.set_birthday(env.inter(C), 10, 6)
        await first_seen_birthdays(env, (2026, 10, 4))
        await env.tick(ts(2026, 10, 5, 9, 0))
        role = env.guild.role(config.BIRTHDAY_ROLE)
        assert sorted(m.id for m in role.members) == [A, B]
        shout = [m for m in env.guild.general.sent if m.content and "birthday" in m.content.lower()]
        assert len(shout) == 1
        assert "<@1>" in shout[0].content and "<@2>" in shout[0].content and "<@3>" not in shout[0].content
        allowed = shout[0].kwargs["allowed_mentions"]
        assert sorted(u.id for u in allowed.users) == [A, B] and allowed.roles is False
        assert allowed.everyone is False
        assert await economy.balance(env.db, A) == 250 and await economy.balance(env.db, B) == 250
        refs = {r["ref"] for r in await env.db.fetchall("SELECT ref FROM ledger")}
        assert refs == {"bday:2026:1", "bday:2026:2"}
        # role stays for 24 h, then comes off
        await env.tick(ts(2026, 10, 6, 8, 59))
        assert sorted(m.id for m in role.members) == [A, B]
        await env.tick(ts(2026, 10, 6, 9, 1))
        assert sorted(m.id for m in role.members) == [C]
        assert await env.meta("bday_role:1") is None
    with_env(go, monkeypatch)


def test_birthday_retry_after_failed_post_does_not_double_pay(monkeypatch):
    async def go(env):
        await env.cog.set_birthday(env.inter(A), 10, 5)
        await first_seen_birthdays(env, (2026, 10, 4))
        env.guild.general.fail = http_error()
        await env.tick(ts(2026, 10, 5, 9, 0))
        assert "birthdays:2026-10-05" not in await env.jobs()
        env.guild.general.fail = None
        await env.tick(ts(2026, 10, 5, 9, 1))
        assert "birthdays:2026-10-05" in await env.jobs()
        assert await economy.balance(env.db, A) == 250
        assert len([m for m in env.guild.general.sent if m.content and "birthday" in m.content.lower()]) == 1
        # role expiry counts from the first grant
        assert await env.meta("bday_role:1") == str(ts(2026, 10, 5, 9, 0) + DAY)
    with_env(go, monkeypatch)


def test_leap_day_birthday_in_common_year(monkeypatch):
    async def go(env):
        await env.cog.set_birthday(env.inter(A), 2, 29)
        await first_seen_birthdays(env, (2027, 2, 27))
        await env.tick(ts(2027, 2, 28, 9, 0))
        assert await economy.balance(env.db, A) == 250
        assert {r["ref"] for r in await env.db.fetchall("SELECT ref FROM ledger")} == {"bday:2027:1"}
    with_env(go, monkeypatch)


def test_birthday_once_a_year_and_skips_leavers_and_optouts(monkeypatch):
    async def go(env):
        await env.cog.set_birthday(env.inter(A), 10, 5)
        await env.cog.set_birthday(env.inter(B), 10, 5)
        await env.db.execute("INSERT INTO birthdays VALUES (99, 10, 5)")  # not in the server
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, 0)", (B,))
        await first_seen_birthdays(env, (2026, 10, 4))
        await env.tick(ts(2026, 10, 5, 9, 0))
        assert await economy.balance(env.db, B) == 0  # opted out: shout-out and role, no ledger row
        assert B in [m.id for m in env.guild.role(config.BIRTHDAY_ROLE).members]
        # A moves their birthday to tomorrow: no second party this year
        await env.cog.set_birthday(env.inter(A), 10, 6)
        await env.tick(ts(2026, 10, 6, 9, 0))
        shouts = [m for m in env.guild.general.sent if m.content and "birthday" in m.content.lower()]
        assert len(shouts) == 1 and "<@99>" not in shouts[0].content
    with_env(go, monkeypatch)


def test_birthday_role_failure_still_posts_and_retries_removal(monkeypatch):
    async def go(env):
        await env.cog.set_birthday(env.inter(A), 10, 5)
        await first_seen_birthdays(env, (2026, 10, 4))
        await env.tick(ts(2026, 10, 5, 9, 0))
        env.member(A).role_fail = http_error(403, discord.Forbidden)
        await env.tick(ts(2026, 10, 6, 9, 5))
        assert await env.meta("bday_role:1") is not None  # kept for a retry
        env.member(A).role_fail = None
        await env.tick(ts(2026, 10, 6, 9, 6))
        assert await env.meta("bday_role:1") is None
        assert env.guild.role(config.BIRTHDAY_ROLE).members == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- auto game night
async def add_game_time(env, uid, game, start, hours):
    await env.db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                         (uid, game, start, start + int(hours * HOUR)))


def test_auto_gamenight_creates_event_for_top_game(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 2, 13))  # first startup: Friday 10/2 marked done
        await add_game_time(env, A, "VALORANT", ts(2026, 10, 5, 18), 3)
        await add_game_time(env, B, "Minecraft", ts(2026, 10, 6, 18), 2)
        await add_game_time(env, C, "Minecraft Launcher", ts(2026, 10, 7, 18), 2)
        await add_game_time(env, C, "Unlisted Indie", ts(2026, 10, 7, 21), 20)
        await env.tick(ts(2026, 10, 9, 12, 0))
        assert len(env.guild.created) == 1
        ev = env.guild.created[0]
        assert ev["name"] == "Minecraft game night"
        assert ev["channel"] is env.guild.squad
        assert ev["entity_type"] is discord.EntityType.voice
        assert int(ev["start_time"].timestamp()) == ts(2026, 10, 9, 21)
        rows = [dict(r) for r in await env.db.fetchall("SELECT * FROM gamenights")]
        assert rows == [{"event_id": 801, "host_id": BOTUSER, "game": "minecraft",
                         "starts_at": ts(2026, 10, 9, 21), "reminded": 0}]
        assert "autogamenight:2026-W41" in await env.jobs()
        await env.tick(ts(2026, 10, 9, 12, 5))
        assert len(env.guild.created) == 1
    with_env(go, monkeypatch)


def test_auto_gamenight_falls_back_to_anything(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 2, 13))
        await env.tick(ts(2026, 10, 9, 12, 0))
        assert env.guild.created[0]["name"] == "Game night"
        row = await env.db.fetchone("SELECT game FROM gamenights")
        assert row["game"] is None
    with_env(go, monkeypatch)


def test_auto_gamenight_skips_when_member_has_one(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 2, 13))
        await env.db.execute("INSERT INTO gamenights VALUES (5, ?, 'valorant', ?, 0)", (A, ts(2026, 10, 9, 20)))
        await env.tick(ts(2026, 10, 9, 12, 0))
        assert env.guild.created == []
        assert "autogamenight:2026-W41" in await env.jobs()
    with_env(go, monkeypatch)


def test_auto_gamenight_skips_when_discord_event_exists(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 2, 13))
        start = datetime.fromtimestamp(ts(2026, 10, 9, 22), timezone.utc)
        env.guild.scheduled_events.append(FakeEvent(77, start))
        await env.tick(ts(2026, 10, 9, 12, 0))
        assert env.guild.created == []
    with_env(go, monkeypatch)


def test_auto_gamenight_ignores_other_evenings(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 2, 13))
        await env.db.execute("INSERT INTO gamenights VALUES (5, ?, NULL, ?, 0)", (A, ts(2026, 10, 8, 21)))
        await env.tick(ts(2026, 10, 9, 12, 0))
        assert len(env.guild.created) == 1
    with_env(go, monkeypatch)


def test_auto_gamenight_retries_on_error_then_gives_up_when_too_late(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 2, 13))
        env.guild.create_fail = http_error(403, discord.Forbidden)
        await env.tick(ts(2026, 10, 9, 12, 0))
        assert "autogamenight:2026-W41" not in await env.jobs()
        await env.tick(ts(2026, 10, 9, 20, 45))
        assert "autogamenight:2026-W41" in await env.jobs()
        assert env.guild.created == []
    with_env(go, monkeypatch)


def test_one_failing_job_does_not_stop_the_others(monkeypatch):
    async def go(env):
        await env.tick(ts(2026, 10, 4, 13))

        async def boom(period):
            raise RuntimeError("nope")
        monkeypatch.setattr(env.cog, "post_qotd", boom)
        await env.tick(ts(2026, 10, 5, 18, 0))  # qotd raises; the poll still goes out
        assert [m for m in env.guild.general.sent if m.kwargs.get("poll")]
        assert "qotd:2026-10-05" not in await env.jobs()
    with_env(go, monkeypatch)
