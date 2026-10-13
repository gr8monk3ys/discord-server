"""The quiet driver (bot/quietgate.py): the response signals against fakes, and the gate
against a real in-memory database. No network."""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
import quietgate
from logic import quiet as Q
from logic.quiet import Evidence, Post

GUILD_ID = 999
DAY = Q.DAY
T0 = 1_780_000_000
ON, OFF = {}, {"QUIET": "off"}


def run(coro):
    return asyncio.run(coro)


def http_error(status=500, cls=discord.HTTPException):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
def human(mid, reply_to=None, type=discord.MessageType.default):
    ref = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    return SimpleNamespace(id=mid, author=SimpleNamespace(bot=False), reference=ref, type=type)


def robot(mid, reply_to=None):
    ref = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    return SimpleNamespace(id=mid, author=SimpleNamespace(bot=True), reference=ref, type=discord.MessageType.default)


class FakeAnswer:
    """A poll answer and its voter list; `fail` makes the list unreadable."""

    def __init__(self, voters=0, fail=None):
        self.n, self.fail, self.asked = voters, fail, []

    def voters(self, limit=None):
        self.asked.append(limit)
        answer = self

        async def gen():
            if answer.fail is not None:
                raise answer.fail
            for i in range(answer.n if limit is None else min(answer.n, limit)):
                yield SimpleNamespace(id=7000 + i)
        return gen()


def prompt(mid, votes=None, reactions=(), thread_messages=None, answers=()):
    """A prompt as fetch_message returns it. reactions: (count, me) pairs."""
    return SimpleNamespace(
        id=mid,
        poll=None if votes is None else SimpleNamespace(total_votes=votes, answers=list(answers)),
        reactions=[SimpleNamespace(count=c, me=me) for c, me in reactions],
        thread=None if thread_messages is None else SimpleNamespace(message_count=thread_messages))


class FakeChannel:
    def __init__(self, name=config.GENERAL_CHANNEL):
        self.name = name
        self.messages = {}  # prompts by id
        self.log = []  # everything in the channel, oldest first
        self.fetch_fail = None
        self.history_fail = None
        self.calls = []
        self.sent = []
        self.fail = None

    def add(self, message):
        self.messages[message.id] = message
        return message

    async def fetch_message(self, mid):
        self.calls.append(("fetch", mid))
        if self.fetch_fail is not None:
            raise self.fetch_fail
        if mid not in self.messages:
            raise http_error(404, discord.NotFound)
        return self.messages[mid]

    def history(self, limit=100, after=None):
        self.calls.append(("history", limit, after.id))
        channel = self

        async def gen():
            if channel.history_fail is not None:
                raise channel.history_fail
            for m in [m for m in channel.log if m.id > after.id][:limit]:
                yield m
        return gen()

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeBot:
    def __init__(self, db, mod_log=True):
        self.db = db
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.mod_log = FakeChannel(config.MOD_LOG_CHANNEL)
        self.guild = SimpleNamespace(id=GUILD_ID, text_channels=[self.mod_log] if mod_log else [])

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


def with_bot(fn, mod_log=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        try:
            await fn(FakeBot(db, mod_log))
        finally:
            await db.close()
    run(go())


def notes(bot):
    return [m["embed"].description for m in bot.mod_log.sent]


# ---------------------------------------------------------------- signal: the prompt itself
def test_poll_votes_are_a_response():
    assert quietgate.responded(prompt(1, votes=1))
    assert not quietgate.responded(prompt(1, votes=0))


def test_someone_elses_reaction_is_a_response_the_bots_own_is_not():
    assert quietgate.responded(prompt(1, reactions=[(1, False)]))
    assert quietgate.responded(prompt(1, reactions=[(1, True), (2, True)]))
    assert not quietgate.responded(prompt(1, reactions=[(1, True)]))
    assert not quietgate.responded(prompt(1, reactions=[(0, False)]))


def test_a_message_in_the_answers_thread_is_a_response():
    assert quietgate.responded(prompt(1, thread_messages=1))
    assert not quietgate.responded(prompt(1, thread_messages=0))
    assert not quietgate.responded(prompt(1))  # the thread could not be opened


def test_a_bare_message_is_no_response():
    assert not quietgate.responded(SimpleNamespace(id=1))


# ---------------------------------------------------------------- signal: look
def looked(channel, mid=100):
    return run(quietgate.look(channel, Post(T0, mid)))


def test_look_answered_prompt_reads_no_history():
    ch = FakeChannel()
    ch.add(prompt(100, votes=3))
    ch.log = [human(101)]
    assert looked(ch) == Evidence(answered=True)
    assert ch.calls == [("fetch", 100)]


def test_look_a_poll_showing_no_votes_is_checked_against_its_voter_list():
    ch = FakeChannel()
    a, b = FakeAnswer(), FakeAnswer(voters=3)
    ch.add(prompt(100, votes=0, answers=[a, b]))  # the count on an open poll is approximate
    assert looked(ch) == Evidence(answered=True)
    assert a.asked == [1] and b.asked == [1] and ch.calls == [("fetch", 100)]

    a, b = FakeAnswer(), FakeAnswer()
    ch.add(prompt(100, votes=0, answers=[a, b]))
    assert looked(ch) == Evidence(answered=False, member_since=False)  # really nobody: on to the channel
    assert a.asked == [1] and b.asked == [1]

    ch.add(prompt(100, votes=0, answers=[FakeAnswer(fail=http_error(500)), FakeAnswer(voters=1)]))
    assert looked(ch) == Evidence()  # the list could not be read: could not tell


def test_look_a_poll_with_votes_does_not_read_the_voter_list():
    ch = FakeChannel()
    a = FakeAnswer(voters=1)
    ch.add(prompt(100, votes=1, answers=[a]))
    assert looked(ch) == Evidence(answered=True) and a.asked == []


def test_look_reply_in_the_channel_is_an_answer():
    ch = FakeChannel()
    ch.add(prompt(100))
    ch.log = [robot(101), human(102), human(103, reply_to=100)]
    assert looked(ch) == Evidence(answered=True, member_since=True)
    assert ch.calls == [("fetch", 100), ("history", quietgate.SCAN, 100)]  # one fetch, one bounded read


def test_look_member_message_that_is_not_a_reply_only_wakes():
    ch = FakeChannel()
    ch.add(prompt(100))
    ch.log = [human(90), robot(101), human(102, reply_to=55)]  # 90 was before the prompt
    assert looked(ch) == Evidence(answered=False, member_since=True)


def test_look_silent_room():
    ch = FakeChannel()
    ch.add(prompt(100, votes=0, reactions=[(1, True)], thread_messages=0))
    ch.log = [robot(101), robot(102, reply_to=100), human(103, type=discord.MessageType.new_member),
              human(104, type=discord.MessageType.pins_add)]
    assert looked(ch) == Evidence(answered=False, member_since=False)


def test_look_a_page_of_bot_posts_cannot_tell():
    ch = FakeChannel()
    ch.add(prompt(100))
    ch.log = [robot(101 + i) for i in range(quietgate.SCAN)] + [human(900)]
    assert looked(ch) == Evidence(answered=False, member_since=None)
    ch.log = ch.log[1:]  # one fewer bot post: the member is on the page
    assert looked(ch) == Evidence(answered=False, member_since=True)


@pytest.mark.parametrize("error", [http_error(404, discord.NotFound), http_error(403, discord.Forbidden),
                                   http_error(500), RuntimeError("x")])
def test_look_unreadable_prompt_cannot_tell(error):
    ch = FakeChannel()
    ch.add(prompt(100))
    ch.fetch_fail = error
    assert looked(ch) == Evidence()
    assert ch.calls == [("fetch", 100)]


def test_look_unreadable_history_cannot_tell():
    ch = FakeChannel()
    ch.add(prompt(100))
    ch.history_fail = http_error(403, discord.Forbidden)
    assert looked(ch) == Evidence(answered=False, member_since=None)


def test_look_without_a_message_id_or_a_readable_channel():
    assert looked(FakeChannel(), mid=None) == Evidence()
    assert looked(SimpleNamespace(), mid=5) == Evidence()  # a channel with no fetch_message at all


# ---------------------------------------------------------------- the gate
async def post(bot, ch, kind, at, mid, **prompt_kw):
    """Ask, and if allowed post prompt `mid`. Returns whether it was posted."""
    if not await quietgate.allowed(bot, kind, ch, at, environ=ON):
        return False
    await quietgate.posted(bot.db, kind, ch.add(prompt(mid, **prompt_kw)), at, environ=ON)
    return True


def test_gate_sleeps_after_three_ignored_wakes_on_a_member_and_probes():
    async def go(bot):
        ch = FakeChannel()
        for day in range(3):
            assert await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day, thread_messages=0)
        assert notes(bot) == []
        for day in range(3, 6):  # asleep: nothing posted, staff told once
            assert not await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day)
        assert notes(bot) == [Q.paused_line(Q.QOTD, Q.load(await bot.db.meta_get("quiet:qotd")), T0 + 3 * DAY)]
        assert "tries again in 6 days" in notes(bot)[0]
        assert bot.mod_log.sent[0]["allowed_mentions"].everyone is False
        assert await quietgate.status_lines(bot.db, ON) == [
            f"Question of the day: paused, tries again <t:{T0 + 9 * DAY}:R>"]

        ch.log.append(human(104))  # someone says something in the channel (after prompt 102)
        assert await post(bot, ch, Q.QOTD, T0 + 6 * DAY, 106)
        assert notes(bot)[1] == "Question of the day is back: someone posted in the channel."
        assert await quietgate.status_lines(bot.db, ON) == []

        assert not await post(bot, ch, Q.QOTD, T0 + 7 * DAY, 107)  # that one was ignored too
        assert len(notes(bot)) == 3
        for day in range(8, 13):
            assert not await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day)
        assert await post(bot, ch, Q.QOTD, T0 + 13 * DAY, 113)  # 7 days after the last one: a probe
        assert not await post(bot, ch, Q.QOTD, T0 + 14 * DAY, 114)
        assert len(notes(bot)) == 3  # neither the probe nor going back to sleep is news
    with_bot(go)


def test_gate_keeps_posting_while_one_of_three_is_answered():
    async def go(bot):
        ch = FakeChannel()
        for day in range(12):
            assert await post(bot, ch, Q.POLL, T0 + day * DAY, 100 + day, votes=1 if day % 3 == 0 else 0)
        assert notes(bot) == []
    with_bot(go)


def test_gate_kinds_do_not_share_state():
    async def go(bot):
        ch = FakeChannel()
        for day in range(3):
            assert await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day)
            assert await post(bot, ch, Q.POLL, T0 + day * DAY, 200 + day, votes=2)
        assert not await post(bot, ch, Q.QOTD, T0 + 3 * DAY, 103)
        assert await post(bot, ch, Q.POLL, T0 + 3 * DAY, 203, votes=2)
        assert await quietgate.status_lines(bot.db, ON) == [
            Q.status_line(Q.QOTD, Q.load(await bot.db.meta_get("quiet:qotd")))]
    with_bot(go)


def test_gate_api_hiccup_never_silences():
    async def go(bot):
        ch = FakeChannel()
        ch.fetch_fail = http_error(503)
        for day in range(10):
            assert await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day)
        assert notes(bot) == []
    with_bot(go)


def test_gate_off_switch_always_posts_and_records_nothing():
    async def go(bot):
        ch = FakeChannel()
        for day in range(6):
            assert await quietgate.allowed(bot, Q.QOTD, ch, T0 + day * DAY, environ=OFF)
            await quietgate.posted(bot.db, Q.QOTD, ch.add(prompt(100 + day)), T0 + day * DAY, environ=OFF)
        assert ch.calls == [] and await bot.db.meta_get("quiet:qotd") is None
        # A kind that was asleep when the switch was thrown: it posts, and /status is silent.
        await bot.db.meta_set("quiet:qotd", Q.dump(Q.State((Post(T0, 1), Post(T0, 2), Post(T0, 3)), True)))
        assert await quietgate.allowed(bot, Q.QOTD, ch, T0 + DAY, environ=OFF)
        assert await quietgate.status_lines(bot.db, OFF) == []
        assert len(await quietgate.status_lines(bot.db, ON)) == 1
    with_bot(go)


def test_gate_reads_the_environment_by_default(monkeypatch):
    async def go(bot):
        await bot.db.meta_set("quiet:qotd", Q.dump(Q.State((Post(T0, 1), Post(T0, 2), Post(T0, 3)), True)))
        ch = FakeChannel()
        for mid in (1, 2, 3):
            ch.add(prompt(mid))
        monkeypatch.delenv("QUIET", raising=False)
        assert not await quietgate.allowed(bot, Q.QOTD, ch, T0 + DAY)
        monkeypatch.setenv("QUIET", "off")
        assert await quietgate.allowed(bot, Q.QOTD, ch, T0 + DAY)
    with_bot(go)


def test_gate_with_a_known_member_time_reads_nothing_from_discord():
    async def go(bot):
        ch = FakeChannel()
        for i in range(3):
            assert await quietgate.allowed(bot, Q.REVIVAL, ch, T0 + i * DAY, last_member_at=T0 - 1, environ=ON)
            await quietgate.posted(bot.db, Q.REVIVAL, None, T0 + i * DAY, environ=ON)  # send returned nothing
        assert not await quietgate.allowed(bot, Q.REVIVAL, ch, T0 + 3 * DAY, last_member_at=T0 - 1, environ=ON)
        assert await quietgate.allowed(bot, Q.REVIVAL, ch, T0 + 3 * DAY, last_member_at=T0 + 2 * DAY + 5, environ=ON)
        assert ch.calls == []
        assert notes(bot) == [Q.paused_line(Q.REVIVAL, Q.load(await bot.db.meta_get("quiet:revival")), T0 + 3 * DAY),
                              "Conversation starter is back: the last one got a response."]
    with_bot(go)


def test_gate_without_a_mod_log_or_with_a_failing_one_still_decides():
    async def go(bot):
        ch = FakeChannel()
        bot.mod_log.fail = http_error(403, discord.Forbidden)
        for day in range(3):
            assert await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day)
        assert not await post(bot, ch, Q.QOTD, T0 + 3 * DAY, 103)
        bot.guild.text_channels.clear()
        ch.log.append(human(500))
        assert await post(bot, ch, Q.QOTD, T0 + 4 * DAY, 104)
    with_bot(go)


def test_gate_fails_open_and_posted_never_raises():
    async def go(bot):
        ch = FakeChannel()

        async def boom(*args):
            raise RuntimeError("database is locked")
        broken = SimpleNamespace(db=SimpleNamespace(meta_get=boom, meta_set=boom), settings=bot.settings,
                                 get_guild=bot.get_guild)
        assert await quietgate.allowed(broken, Q.QOTD, ch, T0, environ=ON)
        await quietgate.posted(broken.db, Q.QOTD, prompt(1), T0, environ=ON)
    with_bot(go)


def test_state_holds_no_member_ids():
    async def go(bot):
        ch = FakeChannel()
        ch.log.append(human(424242))
        for day in range(4):
            await post(bot, ch, Q.QOTD, T0 + day * DAY, 100 + day)
        raw = await bot.db.meta_get("quiet:qotd")
        assert "424242" not in raw and set(raw) <= set('{}[]":,0123456789postalenu')
    with_bot(go)
