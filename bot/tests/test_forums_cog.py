"""Offline tests for cogs.forums.Forums: a real in-memory SQLite database plus fakes for the
bot, guild, forums, posts and their starter messages. Time is controlled by patching
cogs.forums.now; nothing sleeps."""

import asyncio
from types import SimpleNamespace

import discord

import config
import db as dbmod
import economy
from cogs import forums as cogmod
from cogs.forums import Forums
from logic import forums as F
from logic import quests as Q

GUILD_ID = 999
BOT_ID = 50
A, B = 1, 2  # 2015 snowflakes: established accounts
DAY = 24 * 60 * 60
T0 = 1_791_400_000

GUIDE = ("Start every round by buying light armor and a ghost, then hold the angle from "
         "heaven instead of pushing main. Lower your sensitivity until a full swipe turns you "
         "exactly half a circle, and practise ten minutes of deathmatch before ranked games. "
         "It took me from silver to platinum in about a month.")


def young(at: int, days: int = 3) -> int:
    return ((at - days * DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22


def run(coro):
    return asyncio.run(coro)


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


# ---------------------------------------------------------------- fakes
class FakeStarter:
    def __init__(self, thread):
        self.thread = thread
        self.id = thread.id

    async def add_reaction(self, emoji):
        t = self.thread
        if t.react_failures:
            raise t.react_failures.pop(0)
        t.reactions.append(emoji)


class FakeThread:
    def __init__(self, guild, tid, parent, owner_id, name="A post", content="", attachments=()):
        self.guild, self.id, self.parent, self.owner_id, self.name = guild, tid, parent, owner_id, name
        self.locked = False
        self.reactions = []
        self.react_failures = []
        self.message = SimpleNamespace(author=SimpleNamespace(id=owner_id), content=content,
                                       attachments=list(attachments))
        self.fetch_error = None

    def get_partial_message(self, mid):
        assert mid == self.id
        return FakeStarter(self)

    async def fetch_message(self, mid):
        assert mid == self.id
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.message is None:
            raise http_error(discord.NotFound, 404)
        return self.message


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.guides = SimpleNamespace(id=10, name=config.GUIDES_FORUM)
        self.setups = SimpleNamespace(id=11, name=config.SETUPS_FORUM)
        self.lfg = SimpleNamespace(id=12, name=config.LFG_FORUM)
        self.forums = [self.guides, self.setups, self.lfg]
        self.threads: dict[int, FakeThread] = {}
        self.cached: set[int] = set()
        self.members: dict[int, SimpleNamespace] = {}
        self.fetches = 0
        self.next_id = 7000

    def get_member(self, uid):
        return self.members.get(uid)

    def get_thread(self, tid):
        return self.threads.get(tid) if tid in self.cached else None

    async def fetch_channel(self, tid):
        self.fetches += 1
        if tid not in self.threads:
            raise http_error(discord.NotFound, 404)
        return self.threads[tid]


def image(name="desk.png", content_type="image/png"):
    return SimpleNamespace(filename=name, content_type=content_type)


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0

    async def post(self, forum=None, owner=A, name="A post", content=GUIDE, attachments=(), announce=True):
        g = self.guild
        g.next_id += 1
        thread = FakeThread(g, g.next_id, forum or g.guides, owner, name, content, attachments)
        g.threads[thread.id] = thread
        g.cached.add(thread.id)
        if announce:
            await self.cog.on_thread_create(thread)
        return thread

    async def row(self, thread):
        r = await self.db.fetchone("SELECT * FROM forum_posts WHERE thread_id = ?", (thread.id,))
        return dict(r) if r else None

    async def state(self, thread):
        r = await self.row(thread)
        return r["state"] if r else None

    async def bal(self, uid):
        return await economy.balance(self.db, uid)

    async def later(self, seconds):
        self.t += seconds
        await self.cog.run_settle()


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = SimpleNamespace(db=db, settings=SimpleNamespace(guild_id=GUILD_ID), user=SimpleNamespace(id=BOT_ID),
                              get_guild=lambda gid: guild if gid == GUILD_ID else None)
        cog = Forums(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)

        async def no_wait(seconds):
            return None
        monkeypatch.setattr(cog, "pause", no_wait)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- new posts
def test_new_post_gets_the_forums_reaction_and_a_pending_row(monkeypatch):
    async def go(env):
        guide = await env.post(name="Crosshairs")
        setup = await env.post(forum=env.guild.setups, owner=B, name="My desk", content="", attachments=[image()])
        assert guide.reactions == ["👍"] and setup.reactions == ["🔥"]
        assert await env.row(guide) == dict(thread_id=guide.id, kind="guides", author_id=A, title="Crosshairs",
                                            created_at=T0, state="pending", settled_at=None)
        assert (await env.row(setup))["kind"] == "setups"
        assert await env.bal(A) == 0  # nothing is paid at creation
    with_env(go, monkeypatch)


def test_other_forums_other_servers_and_bots_are_ignored(monkeypatch):
    async def go(env):
        g = env.guild
        lfg = await env.post(forum=g.lfg)
        same_name_text = await env.post(forum=SimpleNamespace(id=77, name=config.GUIDES_FORUM))  # not in guild.forums
        by_bot = await env.post(owner=BOT_ID)
        g.members[30] = SimpleNamespace(bot=True)
        by_other_bot = await env.post(owner=30)
        elsewhere = FakeThread(SimpleNamespace(id=5, forums=[g.guides]), 1, g.guides, A)
        await env.cog.on_thread_create(elsewhere)
        for t in (lfg, same_name_text, by_bot, by_other_bot, elsewhere):
            assert t.reactions == []
        assert await env.db.fetchall("SELECT * FROM forum_posts") == []
    with_env(go, monkeypatch)


def test_opted_out_author_gets_the_reaction_but_no_record(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (A, T0))
        thread = await env.post()
        assert thread.reactions == ["👍"]
        assert await env.row(thread) is None
        await env.later(2 * DAY)
        assert await env.bal(A) == 0
    with_env(go, monkeypatch)


def test_reaction_waits_for_the_starter_message_and_survives_errors(monkeypatch):
    async def go(env):
        g = env.guild
        slow = FakeThread(g, 1, g.guides, A)
        slow.react_failures = [http_error(discord.NotFound, 404)] * 2
        await env.cog.on_thread_create(slow)
        assert slow.reactions == ["👍"]

        never = FakeThread(g, 2, g.guides, A)
        never.react_failures = [http_error(discord.NotFound, 404)] * 5
        await env.cog.on_thread_create(never)
        denied = FakeThread(g, 3, g.guides, A)
        denied.react_failures = [http_error(discord.Forbidden, 403)]
        await env.cog.on_thread_create(denied)
        assert never.reactions == [] and denied.reactions == []
        # No reaction doesn't cost the author the record.
        assert {r["thread_id"] for r in await env.db.fetchall("SELECT thread_id FROM forum_posts")} == {1, 2, 3}
    with_env(go, monkeypatch)


def test_listener_never_raises(monkeypatch):
    async def go(env):
        await env.cog.on_thread_create(SimpleNamespace(guild=env.guild))  # no parent, no owner: logged
        await env.db.close()
        await env.cog.on_raw_thread_delete(SimpleNamespace(thread_id=1))  # database gone: logged
        await env.db.connect()
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- rewards
def test_post_is_paid_once_after_surviving_a_day(monkeypatch):
    async def go(env):
        thread = await env.post(name="Old title")
        await env.later(F.SURVIVE - 60)
        assert await env.state(thread) == "pending" and await env.bal(A) == 0
        thread.name = "New title"  # renamed since: the row follows
        await env.later(60)
        assert await env.bal(A) == F.REWARD
        row = await env.row(thread)
        assert row["state"] == "paid" and row["settled_at"] == env.t and row["title"] == "New title"
        ledger = await env.db.fetchall("SELECT * FROM ledger")
        assert [(r["user_id"], r["delta"], r["reason"], r["ref"]) for r in ledger] == [
            (A, F.REWARD, "forum_post", f"forum:{thread.id}")]
        await env.later(DAY)
        await env.later(DAY)
        assert await env.bal(A) == F.REWARD
    with_env(go, monkeypatch)


def test_setup_needs_a_picture_and_a_guide_needs_text(monkeypatch):
    async def go(env):
        g = env.guild
        pic = await env.post(forum=g.setups, owner=A, content="", attachments=[image()])
        not_a_pic = await env.post(forum=g.setups, owner=B, content="look", attachments=[image("x.zip", "application/zip")])
        empty_guide = await env.post(owner=B, content="first")
        await env.later(F.SURVIVE)
        assert await env.state(pic) == "paid" and await env.bal(A) == F.REWARD
        assert await env.state(not_a_pic) == "skipped" and await env.state(empty_guide) == "skipped"
        assert await env.bal(B) == 0
    with_env(go, monkeypatch)


def test_content_is_judged_when_it_pays_not_when_it_was_posted(monkeypatch):
    async def go(env):
        gutted = await env.post(owner=A)
        gutted.message.content = "."  # a full guide, emptied after posting
        filled = await env.post(owner=B, content="wip")
        filled.message.content = GUIDE  # a stub that was finished within the day
        await env.later(F.SURVIVE)
        assert await env.state(gutted) == "skipped" and await env.bal(A) == 0
        assert await env.state(filled) == "paid" and await env.bal(B) == F.REWARD
    with_env(go, monkeypatch)


def test_deleted_or_locked_posts_never_pay(monkeypatch):
    async def go(env):
        g = env.guild
        deleted = await env.post()
        await env.cog.on_raw_thread_delete(SimpleNamespace(thread_id=deleted.id))
        assert await env.state(deleted) == "deleted"
        gone = await env.post()  # deleted while the bot was offline: no event
        del g.threads[gone.id]
        g.cached.discard(gone.id)
        no_starter = await env.post()
        no_starter.message = None
        locked = await env.post()
        locked.locked = True
        hidden = await env.post()
        hidden.fetch_error = http_error(discord.Forbidden, 403)
        await env.later(F.SURVIVE)
        assert [await env.state(t) for t in (deleted, gone, no_starter, locked, hidden)] == [
            "deleted", "deleted", "deleted", "skipped", "skipped"]
        assert await env.bal(A) == 0 and await env.db.fetchall("SELECT * FROM ledger") == []
    with_env(go, monkeypatch)


def test_deleting_a_paid_post_changes_nothing(monkeypatch):
    async def go(env):
        thread = await env.post()
        await env.later(F.SURVIVE)
        await env.cog.on_raw_thread_delete(SimpleNamespace(thread_id=thread.id))
        assert await env.state(thread) == "paid" and await env.bal(A) == F.REWARD
    with_env(go, monkeypatch)


def test_weekly_cap_per_member(monkeypatch):
    async def go(env):
        posts = [await env.post(owner=A) for _ in range(F.WEEKLY_CAP + 2)]
        other = await env.post(owner=B)
        await env.later(F.SURVIVE)
        states = [await env.state(t) for t in posts]
        assert states == ["paid"] * F.WEEKLY_CAP + ["unpaid"] * 2
        assert await env.bal(A) == F.REWARD * F.WEEKLY_CAP
        assert await env.state(other) == "paid"  # the cap is per member
        # A week after those payouts the member can earn again.
        env.t += F.WEEK
        again = await env.post(owner=A)
        await env.later(F.SURVIVE)
        assert await env.state(again) == "paid" and await env.bal(A) == F.REWARD * (F.WEEKLY_CAP + 1)
    with_env(go, monkeypatch)


def test_weekly_cap_outlives_a_privacy_round_trip(monkeypatch):
    """/privacy off deletes the member's forum_posts rows; the cap is counted in the ledger."""
    from cogs.stats import opt_out

    async def go(env):
        paid = [await env.post(owner=A) for _ in range(F.WEEKLY_CAP)]
        await env.later(F.SURVIVE)
        waiting = await env.post(owner=A)  # not judged yet when they opt out
        assert await env.bal(A) == F.REWARD * F.WEEKLY_CAP
        await opt_out(env.db, A, env.t)  # /privacy tracking:off ...
        assert [await env.row(t) for t in (*paid, waiting)] == [None] * (F.WEEKLY_CAP + 1)
        await env.db.execute("DELETE FROM privacy_optout WHERE user_id = ?", (A,))  # ... and on again
        again = await env.post(owner=A)  # same week
        await env.later(F.SURVIVE)
        assert await env.state(again) == "unpaid" and await env.bal(A) == F.REWARD * F.WEEKLY_CAP
        assert await env.row(waiting) is None  # a forgotten post is never judged, so never paid
        rows = await env.db.fetchall("SELECT ref FROM ledger WHERE user_id = ? ORDER BY id", (A,))
        assert [r["ref"] for r in rows] == [F.ref(t.id) for t in paid]  # one payout per post, no more
        env.t += F.WEEK  # the cap window passes like for anyone else
        later = await env.post(owner=A)
        await env.later(F.SURVIVE)
        assert await env.state(later) == "paid"
    with_env(go, monkeypatch)


def test_young_accounts_are_not_paid(monkeypatch):
    async def go(env):
        alt = young(T0)
        thread = await env.post(owner=alt)
        await env.later(F.SURVIVE)
        assert await env.state(thread) == "unpaid" and await env.bal(alt) == 0
    with_env(go, monkeypatch)


def test_opting_out_before_payout_drops_the_record(monkeypatch):
    async def go(env):
        thread = await env.post()
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (A, T0 + 5))
        await env.later(F.SURVIVE)
        assert await env.row(thread) is None and await env.bal(A) == 0
    with_env(go, monkeypatch)


def test_someone_elses_starter_or_a_moved_post_is_skipped(monkeypatch):
    async def go(env):
        swapped = await env.post()
        swapped.message.author = SimpleNamespace(id=B)
        moved = await env.post()
        moved.parent = env.guild.lfg
        await env.later(F.SURVIVE)
        assert await env.state(swapped) == "skipped" and await env.state(moved) == "skipped"
        assert await env.bal(A) == 0 and await env.bal(B) == 0
    with_env(go, monkeypatch)


def test_discord_errors_retry_then_give_up(monkeypatch):
    async def go(env):
        thread = await env.post()
        thread.fetch_error = http_error(discord.HTTPException, 500)
        await env.later(F.SURVIVE)
        assert await env.state(thread) == "pending"
        thread.fetch_error = None
        await env.later(600)
        assert await env.state(thread) == "paid"

        stuck = await env.post(owner=B)
        stuck.fetch_error = http_error(discord.HTTPException, 500)
        await env.later(F.SURVIVE + F.GIVE_UP - 1)
        assert await env.state(stuck) == "pending"
        await env.later(1)
        assert await env.state(stuck) == "skipped" and await env.bal(B) == 0
    with_env(go, monkeypatch)


def test_archived_post_is_fetched_and_already_paid_ref_is_not_paid_again(monkeypatch):
    async def go(env):
        thread = await env.post()
        env.guild.cached.discard(thread.id)  # archived: not in the cache
        await economy.apply(env.db, A, F.REWARD, F.REASON, T0, ref=F.ref(thread.id))  # as if paid before a crash
        await env.later(F.SURVIVE)
        assert env.guild.fetches == 1
        assert await env.state(thread) == "paid" and await env.bal(A) == F.REWARD
    with_env(go, monkeypatch)


def test_nothing_is_judged_without_the_message_content_intent(monkeypatch):
    async def go(env):
        env.bot.intents = discord.Intents.default()  # message_content off: posts read as empty
        thread = await env.post()
        await env.later(F.SURVIVE)
        assert await env.state(thread) == "pending"
        env.bot.intents.message_content = True
        await env.later(600)
        assert await env.state(thread) == "paid"
    with_env(go, monkeypatch)


def test_settle_without_a_guild_and_loop_never_raises(monkeypatch):
    async def go(env):
        await env.post()
        env.bot.get_guild = lambda gid: None
        await env.later(F.SURVIVE)  # no guild: nothing happens, nothing raises

        async def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "run_settle", boom)
        await env.cog.settle.coro(env.cog)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- recap hook
def test_fresh_line_covers_the_window_and_leaves_out_deleted_thin_and_opted_out(monkeypatch):
    async def go(env):
        g = env.guild
        assert await env.cog.fresh_line(T0 - DAY, T0 + 7 * DAY) is None
        guide = await env.post(name="Rotations")
        thin = await env.post(owner=B, name="first", content="first")
        deleted = await env.post(owner=B, name="oops")
        await env.cog.on_raw_thread_delete(SimpleNamespace(thread_id=deleted.id))
        env.t += 3600
        setup = await env.post(forum=g.setups, owner=B, name="My **desk**", content="", attachments=[image()])
        await env.later(F.SURVIVE)
        assert await env.state(thin) == "skipped"
        line = await env.cog.fresh_line(T0 - DAY, T0 + 7 * DAY)
        assert line == rf"Fresh in the forums: 1 new guide and 1 new setup. Latest: **My \*\*desk\*\*** (<#{setup.id}>)"
        # Half-open window on the creation time.
        assert await env.cog.fresh_line(T0, T0 + 3600) == f"Fresh in the forums: 1 new guide. Latest: **Rotations** (<#{guide.id}>)"
        assert await env.cog.fresh_line(T0 + 3601, T0 + 7 * DAY) is None
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (B, env.t))
        assert "setup" not in await env.cog.fresh_line(T0 - DAY, T0 + 7 * DAY)
    with_env(go, monkeypatch)


def test_setup_registers_the_cog():
    async def go():
        added = []

        async def add_cog(cog):
            added.append(cog)
        await cogmod.setup(SimpleNamespace(add_cog=add_cog))
        assert isinstance(added[0], Forums)
    run(go())
