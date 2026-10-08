"""Offline integration tests for cogs.lfg.Lfg: a real in-memory SQLite database
plus lightweight fakes for the bot, guild, forum, threads and interactions.
No network. Every test drives the cog through asyncio.run."""

import asyncio
import re
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

import config
import db as dbmod
import errors
from cogs import lfg as cogmod
from cogs.lfg import Lfg, LfgButton
from logic import lfg as rules

GUILD_ID = 999
OWNER, HOST, A, B, C, KEEPER = 1, 10, 11, 12, 13, 20
VALORANT = config.game_by_key("valorant")


def run(coro):
    return asyncio.run(coro)


def game_choice(key="valorant"):
    g = config.game_by_key(key)
    return app_commands.Choice(name=g.role, value=g.key)


def mode_choice(mode):
    return app_commands.Choice(name=mode, value=mode)


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


# ---------------------------------------------------------------- fakes
class Named:
    _next = 5000

    def __init__(self, name):
        Named._next += 1
        self.id = Named._next
        self.name = name
        self.mention = f"<@&{self.id}>"

    def __repr__(self):
        return f"<{self.name}>"


class FakeMessage:
    def __init__(self, thread, message_id):
        self.thread = thread
        self.id = message_id

    async def edit(self, **kwargs):
        if self.thread.message_edit_error is not None:
            raise self.thread.message_edit_error
        self.thread.calls.append(("message.edit", kwargs))


class FakeThread:
    _next = 70000

    def __init__(self, name, applied_tags):
        FakeThread._next += 1
        self.id = FakeThread._next
        self.name = name
        self.applied_tags = list(applied_tags)
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{self.id}"
        self.locked = False
        self.archived = False
        self.calls = []  # ordered: ("message.edit"|"edit"|"send", kwargs)
        self.message_edit_error = None

    async def edit(self, **kwargs):
        self.calls.append(("edit", kwargs))
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "applied_tags" in kwargs:
            self.applied_tags = list(kwargs["applied_tags"])
        self.locked = kwargs.get("locked", self.locked)
        self.archived = kwargs.get("archived", self.archived)
        return self

    async def send(self, content=None, **kwargs):
        self.calls.append(("send", dict(content=content, **kwargs)))

    def get_partial_message(self, message_id):
        return FakeMessage(self, message_id)

    def sends(self):
        return [kw for kind, kw in self.calls if kind == "send"]


class FakeForum:
    def __init__(self, guild):
        self.guild = guild
        self.id = 4242
        self.name = config.LFG_FORUM
        self.available_tags = [Named(g.role) for g in config.GAMES] + [Named(m) for m in config.MODES]
        self.created = []  # kwargs of each create_thread call
        self.fail_next = None

    async def create_thread(self, **kwargs):
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err
        self.created.append(kwargs)
        thread = FakeThread(kwargs["name"], kwargs.get("applied_tags", []))
        self.guild.threads[thread.id] = thread
        message = SimpleNamespace(id=thread.id + 1)
        return SimpleNamespace(thread=thread, message=message)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.threads = {}  # every thread that exists on "Discord"
        self.cached = set()  # thread ids that guild.get_thread returns
        self.roles = [Named(g.role) for g in config.GAMES] + [Named(config.LFG_ROLE), Named(config.KEEPER_ROLE)]
        self.voice_channels = [Named(config.SQUAD_VOICE), Named(config.LOBBY_VOICE)]
        self.forum = FakeForum(self)
        self.forums = [self.forum]

    def role(self, name):
        return config.match_by_name(self.roles, name)

    def get_thread(self, thread_id):
        return self.threads.get(thread_id) if thread_id in self.cached else None


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.dispatched = []
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    async def fetch_channel(self, channel_id):
        thread = self.guild.threads.get(channel_id)
        if thread is None:
            raise not_found()
        return thread

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))

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

    async def edit_message(self, **kwargs):
        self._finish()
        self.calls.append(("edit_message", kwargs))

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
    def __init__(self, bot, user_id, channel=None, roles=(), admin=False):
        self.calls = []
        self.client = bot
        self.channel = channel
        self.user = SimpleNamespace(
            id=user_id,
            display_name=f"user{user_id}",
            mention=f"<@{user_id}>",
            roles=list(roles),
            guild=bot.guild,
            guild_permissions=SimpleNamespace(administrator=admin),
        )
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    def texts(self):
        return [kw.get("content") for _, kw in self.calls if kw.get("content")]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog

    def inter(self, user_id, thread=None, **kw):
        return FakeInteraction(self.bot, user_id, channel=thread, **kw)

    async def post(self, post_id=None):
        if post_id is None:
            return await self.db.fetchone("SELECT * FROM lfg_posts ORDER BY id DESC LIMIT 1")
        return await self.db.fetchone("SELECT * FROM lfg_posts WHERE id = ?", (post_id,))

    async def members(self, post_id):
        rows = await self.db.fetchall("SELECT user_id FROM lfg_members WHERE post_id = ? ORDER BY rowid",
                                      (post_id,))
        return [r["user_id"] for r in rows]

    def thread(self, post):
        return self.guild.threads[post["thread_id"]]

    async def create(self, host=HOST, game="valorant", players=3, mode=None, when="now", note=None):
        inter = self.inter(host)
        await Lfg.lfg.callback(self.cog, inter, game_choice(game), players,
                               mode_choice(mode) if mode else None, when, note)
        return inter, await self.post()

    async def press(self, user_id, action, post, **kw):
        inter = self.inter(user_id, thread=self.guild.threads.get(post["thread_id"]), **kw)
        await self.cog.handle_button(inter, action, post["id"])
        return inter

    def restart(self):
        """A new cog on the same database, as after a bot restart."""
        cog = Lfg(self.bot)
        cog.resolve()
        self.bot.cogs["Lfg"] = cog
        self.cog = cog
        return cog


async def make_env(ready=True):
    db = dbmod.Database(":memory:")
    await db.connect()
    await db.migrate()
    guild = FakeGuild()
    bot = FakeBot(db, guild)
    cog = Lfg(bot)
    bot.cogs["Lfg"] = cog
    if ready:
        cog.resolve()
        assert cog.ready
    return Env(db, guild, bot, cog)


def with_env(fn, ready=True):
    async def go():
        env = await make_env(ready)
        try:
            await fn(env)
        finally:
            await env.db.close()
    run(go())


def custom_ids(view):
    return [item.item.custom_id for item in view.children]


def button(view, action):
    return next(i.item for i in view.children if i.item.custom_id.startswith(f"lfg:{action}:"))


# ---------------------------------------------------------------- DynamicItem
def test_button_custom_id_round_trips_through_template():
    async def go():
        made = LfgButton("leave", 42)
        assert made.item.custom_id == "lfg:leave:42"
        pattern = LfgButton.__discord_ui_compiled_template__
        match = pattern.fullmatch("lfg:join:1234")
        parsed = await LfgButton.from_custom_id(None, None, match)
        assert (parsed.action, parsed.post_id) == ("join", 1234)
        assert parsed.item.custom_id == "lfg:join:1234"
        assert pattern.fullmatch("lfg:kick:1") is None
        assert pattern.fullmatch("lfg:join:abc") is None
    run(go())


# ---------------------------------------------------------------- /lfg create
def test_create_post_writes_rows_and_thread():
    async def go(env):
        inter, post = await env.create(players=3, mode="Ranked", when="9pm", note="mic pls")
        assert post["game"] == "valorant" and post["host_id"] == HOST and post["size"] == 3
        assert post["mode"] == "Ranked" and post["when_text"] == "9pm" and post["note"] == "mic pls"
        assert post["closed_at"] is None and post["thread_id"] and post["message_id"]
        assert await env.members(post["id"]) == [HOST]

        (made,) = env.guild.forum.created
        assert made["name"] == "Valorant · Ranked"
        assert [t.name for t in made["applied_tags"]] == ["Valorant", "Ranked"]
        content = made["content"]
        assert env.guild.role("Valorant").mention in content
        assert env.guild.role("LFG").mention in content
        assert "needs 2 more" in content and "9pm" in content
        am = made["allowed_mentions"]
        assert [r.name for r in am.roles] == ["Valorant", config.LFG_ROLE]  # only these two, never any role
        assert am.users is False and am.everyone is False
        pid = post["id"]
        assert custom_ids(made["view"]) == [f"lfg:join:{pid}", f"lfg:leave:{pid}", f"lfg:close:{pid}"]
        assert not any(i.item.disabled for i in made["view"].children)
        assert "1 / 3" in made["embed"].description
        assert f"<@{HOST}>  · host" in made["embed"].description

        assert inter.of("defer") and inter.of("defer")[0]["ephemeral"] is True
        (follow,) = inter.of("followup")
        assert env.thread(post).jump_url in follow["content"] and follow["ephemeral"] is True
    with_env(go)


def test_create_without_mode_has_plain_title_and_game_tag_only():
    async def go(env):
        _, post = await env.create(players=2)
        made = env.guild.forum.created[0]
        assert made["name"] == "Valorant"
        assert [t.name for t in made["applied_tags"]] == ["Valorant"]
        assert post["mode"] is None
    with_env(go)


def test_create_thread_failure_removes_rows():
    async def go(env):
        env.guild.forum.fail_next = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        with pytest.raises(discord.HTTPException):
            await env.create()
        assert await env.db.fetchall("SELECT * FROM lfg_posts") == []
        assert await env.db.fetchall("SELECT * FROM lfg_members") == []
    with_env(go)


@pytest.mark.parametrize("err", [OSError("network down"), asyncio.TimeoutError()],
                         ids=["oserror", "timeout"])
def test_create_thread_non_http_failure_removes_rows_and_unblocks(err):
    """Finding 3: discord.py re-raises raw OSError / TimeoutError (not HTTPException).
    The half-made row must still go, or the host is stuck on 'still being created'."""
    async def go(env):
        env.guild.forum.fail_next = err
        with pytest.raises(type(err)):
            await env.create()
        assert await env.db.fetchall("SELECT * FROM lfg_posts") == []
        assert await env.db.fetchall("SELECT * FROM lfg_members") == []
        inter, post = await env.create()
        assert post["thread_id"] is not None
        assert "still being created" not in " ".join(inter.texts())
    with_env(go)


def test_not_ready_cog_replies_ephemerally():
    async def go(env):
        inter = env.inter(HOST)
        await Lfg.lfg.callback(env.cog, inter, game_choice(), 3, None, "now", None)
        (msg,) = inter.of("send_message")
        assert msg["ephemeral"] is True and "starting up" in msg["content"]  # resolve() hasn't run yet
        assert await env.db.fetchall("SELECT * FROM lfg_posts") == []
    with_env(go, ready=False)


def test_missing_forum_replies_ephemerally():
    async def go(env):
        env.guild.forums = []
        env.cog.resolve()
        assert not env.cog.ready
        inter = env.inter(HOST)
        await Lfg.lfg.callback(env.cog, inter, game_choice(), 3, None, "now", None)
        (msg,) = inter.of("send_message")
        assert msg["ephemeral"] is True and config.LFG_FORUM in msg["content"]
    with_env(go, ready=False)


def test_resolve_drops_deleted_tags():
    async def go(env):
        env.guild.forum.available_tags = [t for t in env.guild.forum.available_tags if t.name != "Valorant"]
        env.cog.resolve()
        assert "valorant" not in env.cog.game_tags
        _, post = await env.create(game="valorant")
        assert [t.name for t in env.guild.forum.created[-1]["applied_tags"]] == []
    with_env(go)


def test_close_unarchives_auto_archived_thread_first():
    async def go(env):
        _, post = await env.create()
        thread = env.thread(post)
        thread.archived = True  # Discord auto-archived it while the bot was off
        await env.cog.close_post(post)
        kinds = [(k, kw) for k, kw in thread.calls]
        assert kinds[0] == ("edit", {"archived": False})
        assert kinds[1][0] == "message.edit"
        assert thread.archived and thread.locked
    with_env(go)


def flaky_lock(thread):
    """Make locking the thread fail with a 403 (e.g. Manage Threads missing) until restored."""
    real_edit = thread.edit

    async def edit(**kwargs):
        if kwargs.get("locked"):
            raise discord.HTTPException(SimpleNamespace(status=403, reason="x"), "Missing Permissions")
        return await real_edit(**kwargs)
    thread.edit = edit
    return real_edit


async def tidy_pending(env):
    rows = await env.db.fetchall("SELECT key FROM meta WHERE key LIKE 'lfg:tidy:%'")
    return [r["key"] for r in rows]


def test_transient_error_on_lock_keeps_post_closed_and_expiry_retries():
    async def go(env):
        _, post = await env.create()
        thread = env.thread(post)
        real_edit = flaky_lock(thread)
        await env.cog.close_post(post)
        assert (await env.post(post["id"]))["closed_at"] is not None  # stays closed
        assert await tidy_pending(env) == [f"lfg:tidy:{post['id']}"]
        assert not thread.locked
        thread.edit = real_edit
        await env.cog.expiry.coro(env.cog)
        assert thread.archived and thread.locked and thread.name.startswith("✓ ")
        assert await tidy_pending(env) == []
    with_env(go)


def test_early_manual_close_failure_is_retried_and_never_reopened():
    """Finding 2: a host closes 10 min in and the lock fails. The post must stay
    closed (Join refused, next /lfg is a fresh post) and expiry must retry the lock
    even though the post is far from its 3h expiry, including after a restart."""
    async def go(env):
        _, post = await env.create(host=HOST, players=3)
        thread = env.thread(post)
        real_edit = flaky_lock(thread)
        await env.press(HOST, "close", post)
        assert (await env.post(post["id"]))["closed_at"] is not None
        assert not thread.locked

        joiner = await env.press(A, "join", post)
        assert joiner.texts() == ["This squad is closed."]
        _, fresh = await env.create(host=HOST, players=4)
        assert fresh["id"] != post["id"] and fresh["thread_id"] != post["thread_id"]
        assert (await env.post(post["id"]))["closed_at"] is not None

        env.restart()
        await env.cog.expiry.coro(env.cog)  # still failing: marker survives
        assert await tidy_pending(env) == [f"lfg:tidy:{post['id']}"]
        thread.edit = real_edit
        await env.cog.expiry.coro(env.cog)
        assert thread.locked and thread.archived
        assert await tidy_pending(env) == []
        assert (await env.post(fresh["id"]))["closed_at"] is None  # young post untouched
    with_env(go)


def test_join_updates_embed_count():
    async def go(env):
        _, post = await env.create(players=3)
        inter = await env.press(A, "join", post)
        (edit,) = inter.of("edit_message")
        assert "2 / 3" in edit["embed"].description
        assert f"<@{A}>" in edit["embed"].description
        assert not button(edit["view"], "join").disabled
        assert await env.members(post["id"]) == [HOST, A]
        assert env.bot.dispatched == []
    with_env(go)


def test_join_until_full_disables_join_and_announces():
    async def go(env):
        _, post = await env.create(players=3)
        await env.press(A, "join", post)
        inter = await env.press(B, "join", post)
        (edit,) = inter.of("edit_message")
        assert "3 / 3" in edit["embed"].description
        assert button(edit["view"], "join").disabled
        assert not button(edit["view"], "leave").disabled
        assert not button(edit["view"], "close").disabled

        thread = env.thread(post)
        (sent,) = thread.sends()
        assert sent["content"].startswith("Squad's full:")
        for uid in (HOST, A, B):
            assert f"<@{uid}>" in sent["content"]
        assert config.SQUAD_VOICE in [v.name for v in env.guild.voice_channels]
        assert env.cog.voice["squad"].mention in sent["content"]  # size 3 -> Squad voice
        am = sent["allowed_mentions"]
        assert sorted(u.id for u in am.users) == sorted([HOST, A, B]) and am.roles is False and am.everyone is False

        ((event, args),) = env.bot.dispatched
        assert event == "lfg_squad_full" and args[0] == post["id"] and args[1].members == (HOST, A, B)
    with_env(go)


def test_big_squad_full_suggests_lobby():
    async def go(env):
        _, post = await env.create(players=6)
        for uid in range(100, 105):
            await env.press(uid, "join", post)
        (sent,) = env.thread(post).sends()
        assert env.cog.voice["lobby"].mention in sent["content"]
    with_env(go)


def test_join_when_full_and_already_in():
    async def go(env):
        _, post = await env.create(players=2)
        await env.press(A, "join", post)
        full = await env.press(B, "join", post)
        assert full.of("send_message")[0]["content"] == cogmod.JOIN_REPLIES[rules.Join.FULL]
        assert full.of("send_message")[0]["ephemeral"] is True
        again = await env.press(A, "join", post)
        assert again.of("send_message")[0]["content"] == cogmod.JOIN_REPLIES[rules.Join.ALREADY_IN]
        host = await env.press(HOST, "join", post)
        assert host.of("send_message")[0]["content"] == cogmod.JOIN_REPLIES[rules.Join.ALREADY_IN]
        assert await env.members(post["id"]) == [HOST, A]
    with_env(go)


def test_leave_reopens_spot():
    async def go(env):
        _, post = await env.create(players=2)
        await env.press(A, "join", post)
        inter = await env.press(A, "leave", post)
        (edit,) = inter.of("edit_message")
        assert "1 / 2" in edit["embed"].description
        assert not button(edit["view"], "join").disabled
        assert await env.members(post["id"]) == [HOST]
        rejoin = await env.press(B, "join", post)
        assert rejoin.of("edit_message")
        not_in = await env.press(C, "leave", post)
        assert not_in.of("send_message")[0]["content"] == cogmod.LEAVE_REPLIES[rules.Leave.NOT_IN]
    with_env(go)


def test_host_cannot_leave():
    async def go(env):
        _, post = await env.create()
        inter = await env.press(HOST, "leave", post)
        (msg,) = inter.of("send_message")
        assert msg["content"] == cogmod.LEAVE_REPLIES[rules.Leave.HOST] and msg["ephemeral"] is True
        assert await env.members(post["id"]) == [HOST]
    with_env(go)


def test_concurrent_joins_do_not_overfill():
    async def go(env):
        _, post = await env.create(players=2)
        inters = [env.inter(uid, thread=env.thread(post)) for uid in (A, B, C)]
        await asyncio.gather(*(env.cog.handle_button(i, "join", post["id"]) for i in inters))
        members = await env.members(post["id"])
        assert len(members) == 2 and members[0] == HOST
        assert sum(1 for i in inters if i.of("edit_message")) == 1
        assert sum(1 for i in inters if i.texts() == [cogmod.JOIN_REPLIES[rules.Join.FULL]]) == 2
        assert len(env.thread(post).sends()) == 1
        assert len(env.bot.dispatched) == 1
    with_env(go)


# ---------------------------------------------------------------- close
def test_non_host_non_keeper_cannot_close():
    async def go(env):
        _, post = await env.create()
        await env.press(A, "join", post)
        inter = await env.press(A, "close", post)
        (msg,) = inter.of("send_message")
        assert "host or a Keeper" in msg["content"] and msg["ephemeral"] is True
        assert (await env.post(post["id"]))["closed_at"] is None
        assert env.thread(post).calls == []
    with_env(go)


def test_host_close_disables_buttons_then_archives_once():
    async def go(env):
        _, post = await env.create(players=3, mode="Casual")
        inter = await env.press(HOST, "close", post)
        assert inter.of("defer")
        assert (await env.post(post["id"]))["closed_at"] is not None

        thread = env.thread(post)
        kinds = [k for k, _ in thread.calls]
        assert kinds == ["message.edit", "edit"]  # buttons off BEFORE archiving
        msg_edit = thread.calls[0][1]
        assert all(i.item.disabled for i in msg_edit["view"].children)
        assert msg_edit["embed"].color.value == cogmod.style.MUTED
        assert "CLOSED" in msg_edit["embed"].footer.text
        edit = thread.calls[1][1]
        assert edit["locked"] is True and edit["archived"] is True
        assert edit["name"].startswith("✓ ") and edit["name"] == "✓ Valorant · Casual"

        # Double close is a no-op (the post is closed, nothing else touches Discord).
        again = await env.press(HOST, "close", post)
        assert again.texts() == ["This squad is closed."]
        await env.cog.close_post(post)
        assert len(thread.calls) == 2
        # Buttons on a closed post just say so.
        late = await env.press(A, "join", post)
        assert late.texts() == ["This squad is closed."]
        assert await env.members(post["id"]) == [HOST]
    with_env(go)


@pytest.mark.parametrize("kind", ["owner", "admin", "keeper_role"])
def test_keepers_can_close(kind):
    async def go(env):
        _, post = await env.create()
        if kind == "owner":
            inter = await env.press(OWNER, "close", post)
        elif kind == "admin":
            inter = await env.press(B, "close", post, admin=True)
        else:
            inter = await env.press(KEEPER, "close", post, roles=[env.guild.role("Keeper")])
        assert inter.of("defer")
        assert (await env.post(post["id"]))["closed_at"] is not None
        assert env.thread(post).archived and env.thread(post).locked
    with_env(go)


def test_close_with_deleted_thread_still_marks_closed():
    async def go(env):
        _, post = await env.create()
        del env.guild.threads[post["thread_id"]]
        await env.press(HOST, "close", post)
        assert (await env.post(post["id"]))["closed_at"] is not None
    with_env(go)


def test_close_archives_even_if_starter_message_is_gone():
    async def go(env):
        _, post = await env.create()
        thread = env.thread(post)
        thread.message_edit_error = discord.NotFound(SimpleNamespace(status=404, reason="x"), "Unknown Message")
        await env.press(HOST, "close", post)
        assert (await env.post(post["id"]))["closed_at"] is not None
        assert thread.archived and thread.locked and thread.name.startswith("✓ ")
    with_env(go)


def test_join_racing_with_close_does_not_reopen_post():
    async def go(env):
        _, post = await env.create(players=3)
        # Join and Close clicked at the same moment: whichever order they run in,
        # a closed post must never gain a member or get its buttons re-enabled.
        join = asyncio.ensure_future(env.press(A, "join", post))
        await env.cog.close_post(post)
        inter = await join
        row = await env.post(post["id"])
        assert row["closed_at"] is not None
        if inter.of("edit_message"):  # join won the race: it ran before the close
            assert env.thread(post).archived  # ...and the close still archived afterwards
        else:
            assert await env.members(post["id"]) == [HOST]
            assert "closed" in inter.texts()[0]
    with_env(go)


def test_join_after_close_is_refused():
    async def go(env):
        _, post = await env.create(players=3)
        await env.cog.close_post(post)
        inter = await env.press(A, "join", post)
        assert await env.members(post["id"]) == [HOST]
        assert not inter.of("edit_message") and "closed" in inter.texts()[0]
    with_env(go)


# ---------------------------------------------------------------- expiry
def test_expiry_closes_posts_older_than_three_hours():
    async def go(env):
        _, old = await env.create(host=HOST, game="valorant")
        _, fresh = await env.create(host=A, game="minecraft")
        _, edge = await env.create(host=B, game="fortnite")
        current = cogmod.now()
        await env.db.execute("UPDATE lfg_posts SET created_at = ? WHERE id = ?",
                             (current - rules.EXPIRY_SECONDS - 60, old["id"]))
        await env.db.execute("UPDATE lfg_posts SET created_at = ? WHERE id = ?",
                             (current - rules.EXPIRY_SECONDS + 600, edge["id"]))
        await env.cog.expiry.coro(env.cog)
        assert (await env.post(old["id"]))["closed_at"] is not None
        assert (await env.post(fresh["id"]))["closed_at"] is None
        assert (await env.post(edge["id"]))["closed_at"] is None
        assert env.thread(old).archived and env.thread(old).name.startswith("✓ ")
        assert not env.thread(fresh).archived
    with_env(go)


def test_expiry_continues_after_a_deleted_thread():
    async def go(env):
        _, gone = await env.create(host=HOST, game="valorant")
        _, ok = await env.create(host=A, game="minecraft")
        await env.db.execute("UPDATE lfg_posts SET created_at = 0")
        del env.guild.threads[gone["thread_id"]]
        await env.cog.expiry.coro(env.cog)
        assert (await env.post(gone["id"]))["closed_at"] is not None
        assert (await env.post(ok["id"]))["closed_at"] is not None
        assert env.thread(ok).archived
    with_env(go)


# ---------------------------------------------------------------- re-running /lfg
def test_rerun_same_game_updates_same_post():
    async def go(env):
        _, post = await env.create(players=3, when="now", note="chill")
        thread = env.thread(post)
        inter, updated = await env.create(players=5, when="9pm", note="ranked grind")
        assert len(env.guild.forum.created) == 1  # no new thread
        assert updated["id"] == post["id"]
        assert (updated["size"], updated["when_text"], updated["note"]) == (5, "9pm", "ranked grind")
        assert len(await env.db.fetchall("SELECT * FROM lfg_posts")) == 1
        ((kind, kw),) = thread.calls  # only the message edit; no rename
        assert kind == "message.edit" and "1 / 5" in kw["embed"].description and "9pm" in kw["embed"].description
        (follow,) = inter.of("followup")
        assert thread.jump_url in follow["content"] and follow["ephemeral"] is True
        # A different game is a separate post.
        await env.create(game="minecraft")
        assert len(env.guild.forum.created) == 2
    with_env(go)


def test_rerun_resize_below_headcount_is_refused():
    async def go(env):
        _, post = await env.create(players=4)
        await env.press(A, "join", post)
        await env.press(B, "join", post)
        inter, after = await env.create(players=2)
        (follow,) = inter.of("followup")
        assert "already has 3 people" in follow["content"] and follow["ephemeral"] is True
        assert after["size"] == 4
        assert env.thread(post).calls == []
    with_env(go)


def test_rerun_mode_change_renames_and_retags():
    async def go(env):
        _, post = await env.create(players=3, mode="Casual")
        thread = env.thread(post)
        await env.create(players=3, mode="Ranked")
        assert thread.name == "Valorant · Ranked"
        assert [t.name for t in thread.applied_tags] == ["Valorant", "Ranked"]
        renames = [kw for k, kw in thread.calls if k == "edit"]
        assert len(renames) == 1
    with_env(go)


def test_rerun_with_deleted_thread_closes_post_with_helpful_message():
    async def go(env):
        _, post = await env.create()
        del env.guild.threads[post["thread_id"]]
        inter, after = await env.create(players=4)
        assert after["id"] == post["id"] and after["closed_at"] is not None
        (follow,) = inter.of("followup")
        assert "deleted" in follow["content"] and "/lfg" in follow["content"]
        # Running it again now makes a fresh post.
        _, fresh = await env.create(players=4)
        assert fresh["id"] != post["id"] and fresh["closed_at"] is None
        assert len(env.guild.forum.created) == 2
    with_env(go)


def test_rerun_shrink_to_full_announces():
    async def go(env):
        _, post = await env.create(players=4)
        await env.press(A, "join", post)
        await env.press(B, "join", post)
        await env.create(players=3)
        edit = env.thread(post).calls[0][1]
        assert button(edit["view"], "join").disabled  # this part works
        assert env.thread(post).sends(), "no Squad's full announcement"
        assert env.bot.dispatched and env.bot.dispatched[0][0] == "lfg_squad_full"
    with_env(go)


def test_concurrent_lfg_same_game_makes_one_post():
    async def go(env):
        i1, i2 = env.inter(HOST), env.inter(HOST)
        await asyncio.gather(
            Lfg.lfg.callback(env.cog, i1, game_choice(), 3, None, "now", None),
            Lfg.lfg.callback(env.cog, i2, game_choice(), 3, None, "now", None),
        )
        rows = await env.db.fetchall("SELECT * FROM lfg_posts WHERE closed_at IS NULL")
        assert len(rows) == 1
        assert len(env.guild.forum.created) == 1
    with_env(go)


# ---------------------------------------------------------------- restart
def test_buttons_work_after_restart():
    async def go(env):
        _, post = await env.create(players=2)
        cog = env.restart()  # new cog instance, same DB; no in-memory state carried over
        thread = env.thread(post)

        # Route through the DynamicItem exactly as discord.py would after a restart.
        match = LfgButton.__discord_ui_compiled_template__.fullmatch(f"lfg:join:{post['id']}")
        item = await LfgButton.from_custom_id(None, None, match)
        inter = env.inter(A, thread=thread)
        await item.callback(inter)
        (edit,) = inter.of("edit_message")
        assert "2 / 2" in edit["embed"].description and button(edit["view"], "join").disabled
        assert thread.sends() and env.bot.dispatched

        cog2 = env.restart()
        assert cog2 is not cog
        await env.press(HOST, "close", post)
        assert (await env.post(post["id"]))["closed_at"] is not None and thread.archived
    with_env(go)


def test_button_callback_errors_reply_generically():
    async def go(env):
        _, post = await env.create()

        async def boom(*a):
            raise RuntimeError("db exploded")
        env.cog.handle_button = boom
        inter = env.inter(A, thread=env.thread(post))
        await LfgButton("join", post["id"]).callback(inter)
        (msg,) = inter.of("send_message")
        assert msg["ephemeral"] is True and msg["content"] == errors.ERROR_REPLY
    with_env(go)


def test_user_text_cannot_widen_role_pings():
    """`/lfg when:<@&id>` puts a role mention in the post text; only the game role
    and @LFG may actually ping."""
    async def go(env):
        await env.create(when="<@&999> @everyone")
        kw = env.guild.forum.created[-1]
        assert "<@&999>" in kw["content"]  # text is shown as typed...
        am = kw["allowed_mentions"]
        assert [r.name for r in am.roles] == ["Valorant", config.LFG_ROLE]  # ...but can't ping
        assert am.everyone is False and am.users is False
    with_env(go)


# ---------------------------------------------------------------- ping limits, markdown
def test_role_pings_have_a_per_host_cooldown(monkeypatch):
    clock = {"t": 1_800_000_000}
    monkeypatch.setattr(cogmod, "now", lambda: clock["t"])

    async def go(env):
        _, first = await env.create(game="valorant")
        assert env.guild.forum.created[-1]["allowed_mentions"].roles
        await env.press(HOST, "close", first)
        clock["t"] += 60
        _, again = await env.create(game="valorant")  # close and re-post: still posted, no pings
        quiet = env.guild.forum.created[-1]
        assert again["id"] != first["id"] and again["thread_id"]
        assert env.guild.role("Valorant").mention not in quiet["content"]
        am = quiet["allowed_mentions"]
        assert am.roles is False and am.users is False and am.everyone is False
        await env.create(game="fortnite")  # another game inside the cooldown: quiet too
        assert env.guild.forum.created[-1]["allowed_mentions"].roles is False
        await env.create(host=A, game="minecraft")  # someone else: pings as usual
        assert env.guild.forum.created[-1]["allowed_mentions"].roles
        clock["t"] += cogmod.PING_COOLDOWN
        await env.create(game="wardogs")
        assert env.guild.forum.created[-1]["allowed_mentions"].roles
    with_env(go)


def test_member_text_is_markdown_escaped():
    async def go(env):
        await env.create(when="*9pm*", note="[Official giveaway](https://phish.example)")
        made = env.guild.forum.created[-1]
        desc = made["embed"].description
        assert not re.search(r"(?<!\\)\[Official giveaway\]\(", desc)  # the masked link is broken
        assert r"\[Official giveaway" in desc
        assert r"\*9pm\*" in desc and r"\*9pm\*" in made["content"]
    with_env(go)
