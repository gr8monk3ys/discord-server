"""Offline tests for the recruitment board in cogs.factions: a real in-memory SQLite database plus the
fakes from test_factions_cog. No network."""

import asyncio
import re
from types import SimpleNamespace

import discord

import config
import db as dbmod
from cogs.factions import BOARD_OFF, Factions, JoinButton, PitchModal
from logic import factions as F
from logic import recruit as RC
from test_factions_cog import (MOD, FakeBot, FakeForum, FakeGuild, FakeInteraction, FakeMember, FakeThread,
                               http_error)

BOARD = "📯・recruiting"
ENV = {"RECRUIT_FORUM": BOARD}
LEADER, LEADER2, SOLO, OTHER = 11, 12, 30, 31
PITCH = {"pitch": "River traders with a big farm; we want a builder.", "tags": "Builders, chill", "timezone": "US Pacific"}


def run(coro):
    return asyncio.run(coro)


class Thread(FakeThread):
    def __init__(self, tid, name):
        super().__init__(tid, name)
        self.tag_edits = []
        self.fail_send = None

    async def edit(self, **kwargs):
        self.tag_edits.append(kwargs)

    async def send(self, content=None, **kwargs):
        if self.fail_send is not None:
            raise self.fail_send
        await super().send(content, **kwargs)


class Board(FakeForum):
    async def create_thread(self, name, **kwargs):
        made = await super().create_thread(name, **kwargs)
        thread = Thread(made.thread.id, name)
        thread.messages, thread.kwargs = made.thread.messages, made.thread.kwargs
        self.threads[-1] = thread
        return SimpleNamespace(thread=thread, message=made.message)


class Guild(FakeGuild):
    def __init__(self):
        super().__init__()
        self.board = Board(BOARD)
        self.forums = [self.forum, self.board]

    def get_thread(self, tid):
        return next((t for t in self.forum.threads + self.board.threads if t.id == tid), None)


class User:
    def __init__(self, uid, closed=False):
        self.id = uid
        self.dms = []
        self.closed = closed

    async def send(self, content=None, **kwargs):
        if self.closed:
            raise http_error(discord.Forbidden, 403)
        self.dms.append(dict(content=content, **kwargs))


class Bot(FakeBot):
    def __init__(self, db, guild):
        super().__init__(db, guild)
        self.users = {}

    def get_user(self, uid):
        return self.users.setdefault(uid, User(uid))

    async def fetch_user(self, uid):
        return self.get_user(uid)


class Env:
    def __init__(self, db, guild, cog, bot):
        self.db, self.guild, self.cog, self.bot = db, guild, cog, bot

    def user(self, uid):
        return FakeMember(uid, self.guild, roles=[config.MOD_ROLE] if uid == MOD else [])

    def inter(self, uid):
        return FakeInteraction(self.guild, self.user(uid))

    async def faction(self, leader, name, members=("Steve",), status="approved", season="1"):
        await self.db.execute(
            "INSERT INTO factions (season, name, name_key, leader_id, members, banner, charter, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'a banner here', 'a charter that is long enough', ?, 0)",
            (season, name, F.name_key(name), leader, F.dump_members(members), status))
        return (await self.db.fetchone("SELECT id FROM factions WHERE name = ?", (name,)))["id"]

    async def link(self, uid, name):
        await self.db.execute("INSERT INTO monkey_links (name_key, name, user_id, state, linked_at, updated_at)"
                              " VALUES (?, ?, ?, 'ok', 0, 0)", (name.lower(), name, uid))

    async def pitch(self, uid, **answers):
        inter = self.inter(uid)
        await Factions.pitch.callback(self.cog, inter)
        modals = inter.of("send_modal")
        if not modals:
            return inter
        modal = modals[0]["modal"]
        assert isinstance(modal, PitchModal)
        for key, value in {**PITCH, **answers}.items():
            modal.inputs[key]._value = value
        submit = self.inter(uid)
        await modal.on_submit(submit)
        return submit

    async def toggle(self, uid, on):
        inter = self.inter(uid)
        await Factions.recruit_toggle.callback(self.cog, inter, SimpleNamespace(value="on" if on else "off"))
        return inter

    async def lfg(self, uid, on=True, note="", timezone=""):
        inter = self.inter(uid)
        await Factions.lfg.callback(self.cog, inter, SimpleNamespace(value="on" if on else "off"), note, timezone)
        return inter

    async def seekers(self, uid):
        inter = self.inter(uid)
        await Factions.seekers.callback(self.cog, inter)
        return inter

    async def join(self, uid, fid):
        inter = self.inter(uid)
        await self.cog.request_join(inter, fid)
        return inter

    async def rec(self, fid):
        row = await self.db.fetchone("SELECT * FROM faction_recruit WHERE faction_id = ?", (fid,))
        return dict(row) if row else None


def with_env(fn, environ=ENV):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = Guild()
        bot = Bot(db, guild)
        cog = Factions(bot, environ=environ)
        try:
            await fn(Env(db, guild, cog, bot))
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- wiring
def test_join_button_template_round_trips_and_is_registered():
    assert JoinButton(42).item.custom_id == "recruit:join:42"
    pattern = JoinButton.__discord_ui_compiled_template__
    assert re.fullmatch(pattern, "recruit:join:7")["id"] == "7"
    assert re.fullmatch(pattern, "recruit:kick:7") is None

    async def go():
        bot = FakeBot(None, FakeGuild())
        cog = Factions(bot, environ={})
        await cog.cog_load()
        assert JoinButton in bot.dynamic
        await cog.cog_unload()
        assert JoinButton not in bot.dynamic
    run(go())


def test_everything_says_closed_while_the_board_is_off():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        for inter in (await env.pitch(LEADER), await env.toggle(LEADER, True), await env.lfg(SOLO),
                      await env.seekers(MOD), await env.join(SOLO, fid)):
            assert inter.texts() == [BOARD_OFF]
        assert env.guild.board.threads == []
    with_env(go, environ={})


def test_a_board_named_but_missing_is_off_too():
    async def go(env):
        await env.faction(LEADER, "River Kings")
        inter = await env.pitch(LEADER)
        assert "isn't open" in inter.texts()[0]
    with_env(go, environ={"RECRUIT_FORUM": "no-such-forum"})


# ---------------------------------------------------------------- the pitch and the post
def test_only_the_leader_of_an_approved_faction_can_pitch():
    async def go(env):
        await env.faction(LEADER, "River Kings", status="pending")
        assert "Only the leader of an approved faction" in (await env.pitch(LEADER)).texts()[0]
        assert "Only the leader" in (await env.pitch(SOLO)).texts()[0]
        await env.faction(LEADER2, "Old Kings", season="0")  # last season's
        assert "Only the leader" in (await env.pitch(LEADER2)).texts()[0]
    with_env(go)


def test_pitch_makes_one_open_post_then_edits_it():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings", members=("Steve", "Alex"))
        inter = await env.pitch(LEADER, pitch="We **trade** with @everyone, and farm.")
        assert "Your board post:" in inter.texts()[-1] and "open for requests" in inter.texts()[-1]
        (thread,) = env.guild.board.threads
        kw = thread.kwargs
        assert thread.name == "River Kings"
        assert [t.name for t in kw["applied_tags"]] == [config.RECRUITING_TAG]
        assert kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        embed = kw["embed"]
        assert "\\*\\*trade\\*\\*" in embed.description and "@everyone" not in embed.description.replace("@​", "")
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Playstyle"] == "builders, chill" and fields["Timezone"] == "US Pacific"
        assert fields["Status"] == "📯 Recruiting · 2 of 4 places taken" and fields["Leader"] == f"<@{LEADER}>"
        assert [c.item.custom_id for c in kw["view"].children] == [f"recruit:join:{fid}"]
        rec = await env.rec(fid)
        assert rec["recruiting"] == 1 and rec["thread_id"] == thread.id and rec["tags"] == "builders,chill"

        inter = await env.pitch(LEADER, pitch="Now we mostly build castles together.", tags="")
        assert "open for requests" not in inter.texts()[-1]
        assert len(env.guild.board.threads) == 1
        (edit,) = thread.messages[thread.id].edits
        assert edit["embed"].description == "Now we mostly build castles together."
        assert {f.name: f.value for f in edit["embed"].fields}["Playstyle"] == "Not set"
    with_env(go)


def test_the_modal_is_prefilled_and_bad_answers_are_refused():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        inter = await env.pitch(LEADER, tags="griefers")
        assert "isn't one of the playstyle tags" in inter.texts()[0] and env.guild.board.threads == []
        await env.pitch(LEADER)
        inter = env.inter(LEADER)
        await Factions.pitch.callback(env.cog, inter)
        modal = inter.of("send_modal")[0]["modal"]
        assert modal.inputs["pitch"].default == PITCH["pitch"]
        assert modal.inputs["tags"].default == "builders, chill" and not modal.inputs["tags"].required
        assert modal.faction_id == fid
    with_env(go)


def test_a_full_roster_posts_closed_and_cannot_open():
    async def go(env):
        fid = await env.faction(LEADER, "Full House", members=("A_1", "B_2", "C_3", "D_4"))
        inter = await env.pitch(LEADER)
        assert "open for requests" not in inter.texts()[-1]
        (thread,) = env.guild.board.threads
        assert thread.kwargs["applied_tags"] == [] and thread.kwargs["view"] is None
        assert (await env.rec(fid))["recruiting"] == 0
        assert "roster is full" in (await env.toggle(LEADER, True)).texts()[0]
    with_env(go)


def test_recruit_on_off_edits_the_post_and_its_tag():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        assert "Write your pitch first" in (await env.toggle(LEADER, False)).texts()[0]
        await env.pitch(LEADER)
        (thread,) = env.guild.board.threads
        inter = await env.toggle(LEADER, False)
        assert "is closed" in inter.texts()[-1]
        edit = thread.messages[thread.id].edits[-1]
        assert edit["view"] is None and edit["embed"].color.value == 0xABB0BA
        assert thread.tag_edits[-1] == {"applied_tags": []}
        assert (await env.rec(fid))["recruiting"] == 0
        inter = await env.toggle(LEADER, True)
        assert "open for requests" in inter.texts()[-1]
        assert [t.name for t in thread.tag_edits[-1]["applied_tags"]] == [config.RECRUITING_TAG]
        assert thread.messages[thread.id].edits[-1]["view"] is not None
        assert "Only the leader" in (await env.toggle(SOLO, True)).texts()[0]
    with_env(go)


def test_a_deleted_post_is_made_again():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        await env.pitch(LEADER)
        env.guild.board.threads.clear()  # deleted by hand: get_thread misses, fetch_channel 404s
        await env.toggle(LEADER, False)
        (thread,) = env.guild.board.threads
        assert (await env.rec(fid))["thread_id"] == thread.id
    with_env(go)


# ---------------------------------------------------------------- join requests
def test_request_to_join_dms_the_leader_once_a_day():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        await env.pitch(LEADER)
        await env.link(SOLO, "Solo_Sam")
        await env.lfg(SOLO, note="I build *big* things", timezone="EU")
        inter = await env.join(SOLO, fid)
        assert "Sent to the faction's leader" in inter.texts()[-1]
        (dm,) = env.bot.users[LEADER].dms
        assert f"<@{SOLO}> (Minecraft: `Solo\\_Sam`)" in dm["content"] and "\\*big\\*" in dm["content"]
        assert "Timezone: EU" in dm["content"] and env.guild.board.threads[0].jump_url in dm["content"]
        assert dm["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        inter = await env.join(SOLO, fid)
        assert "already asked" in inter.texts()[0] and len(env.bot.users[LEADER].dms) == 1
        await env.db.execute("UPDATE recruit_requests SET at = at - ?", (RC.REQUEST_COOLDOWN,))
        await env.join(SOLO, fid)
        assert len(env.bot.users[LEADER].dms) == 2
    with_env(go)


def test_request_refusals():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        assert "isn't on the board" in (await env.join(SOLO, fid)).texts()[0]  # no pitch yet
        await env.pitch(LEADER)
        assert "your own faction" in (await env.join(LEADER, fid)).texts()[0]
        await env.faction(LEADER2, "Red Monkeys", members=("Other_1",))
        assert "already on a faction" in (await env.join(LEADER2, fid)).texts()[0]
        await env.link(OTHER, "Other_1")  # listed on Red Monkeys by Minecraft name
        assert "already on a faction" in (await env.join(OTHER, fid)).texts()[0]
        await env.toggle(LEADER, False)
        assert "isn't recruiting" in (await env.join(SOLO, fid)).texts()[0]
        assert "isn't on the board" in (await env.join(SOLO, 999)).texts()[0]
        assert env.bot.users.get(LEADER) is None or env.bot.users[LEADER].dms == []
    with_env(go)


def test_closed_dms_ping_the_leader_on_the_post_only():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        await env.pitch(LEADER)
        env.bot.users[LEADER] = User(LEADER, closed=True)
        inter = await env.join(SOLO, fid)
        assert "pinged them on the board post" in inter.texts()[-1]
        thread = env.guild.board.threads[0]
        (sent,) = thread.sent
        assert sent["content"].startswith(f"<@{LEADER}> <@{SOLO}>")
        assert sent["allowed_mentions"].to_dict()["parse"] == []
        assert [o.id for o in sent["allowed_mentions"].users] == [LEADER]

        thread.fail_send = http_error()
        await env.db.execute("DELETE FROM recruit_requests")
        inter = await env.join(SOLO, fid)
        assert "couldn't reach the leader" in inter.texts()[-1]
        assert await env.db.fetchall("SELECT * FROM recruit_requests") == []  # not counted against them
    with_env(go)


# ---------------------------------------------------------------- /faction lfg and /faction seekers
def test_lfg_list_and_who_can_see_it():
    async def go(env):
        await env.faction(LEADER, "River Kings")
        await env.faction(LEADER2, "Pending Ones", status="pending")
        assert "Nobody's looking" in (await env.seekers(LEADER)).texts()[0]
        await env.link(SOLO, "Solo_Sam")
        inter = await env.lfg(SOLO, note="  redstone   nerd ", timezone="US East")
        assert "on the list for 14 days" in inter.texts()[0]
        assert "on the list" in (await env.lfg(OTHER, note="x" * 10)).texts()[0]
        assert "Only the leader" in (await env.seekers(SOLO)).texts()[0]
        assert "Only the leader" in (await env.seekers(LEADER2)).texts()[0]  # not approved
        for uid in (LEADER, MOD):
            inter = await env.seekers(uid)
            (sent,) = inter.of("send_message")
            text = sent["embed"].description
            assert f"<@{SOLO}> · `Solo\\_Sam` · US East" in text and "redstone nerd" in text and f"<@{OTHER}>" in text
            assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        await env.db.execute("UPDATE smp_seekers SET updated_at = updated_at - ? WHERE user_id = ?",
                             (RC.SEEKER_DAYS * 86400 + 60, OTHER))
        text = (await env.seekers(LEADER)).of("send_message")[0]["embed"].description
        assert f"<@{OTHER}>" not in text
        assert "off the list" in (await env.lfg(SOLO, on=False)).texts()[0]
        assert "weren't on the list" in (await env.lfg(SOLO, on=False)).texts()[0]
    with_env(go)


def test_lfg_refuses_players_on_a_roster_and_long_notes():
    async def go(env):
        await env.faction(LEADER, "River Kings", members=("Steve",))
        assert "already on a faction" in (await env.lfg(LEADER)).texts()[0]
        await env.link(SOLO, "steve")
        assert "already on a faction" in (await env.lfg(SOLO)).texts()[0]
        assert "timezone must be" in (await env.lfg(OTHER, timezone="x")).texts()[0]
        assert await env.db.fetchall("SELECT * FROM smp_seekers") == []
    with_env(go)



def test_leaving_or_a_ban_forgets_the_seeker_and_their_requests():
    async def go(env):
        fid = await env.faction(LEADER, "River Kings")
        await env.pitch(LEADER)
        for uid in (SOLO, OTHER):
            await env.lfg(uid)
            await env.join(uid, fid)
        assert len(await env.db.fetchall("SELECT * FROM recruit_requests")) == 2
        await env.cog.on_member_remove(SimpleNamespace(id=SOLO, guild=env.guild))
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=OTHER))
        await env.cog.on_member_remove(SimpleNamespace(id=LEADER, guild=SimpleNamespace(id=-1)))  # another server
        assert await env.db.fetchall("SELECT * FROM smp_seekers") == []
        assert await env.db.fetchall("SELECT * FROM recruit_requests") == []
        assert await env.rec(fid) is not None
    with_env(go)
