"""Offline tests for cogs.tournaments: a real in-memory SQLite database plus fakes for the
bot, guild, channels, messages, members and interactions. No network."""

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import config
import db as dbmod
import economy
from cogs import tournaments as cogmod
from cogs.tournaments import MatchButton, SignupButton, Tournaments
from logic import tournaments as T

GUILD_ID = 999
OWNER, MOD, KEEPER = 1, 20, 21
PLAYERS = list(range(101, 133))
TZ = ZoneInfo("America/Los_Angeles")
T0 = int(datetime(2026, 10, 7, 12, 0, tzinfo=TZ).timestamp())  # a Wednesday noon
OLD_ACCOUNT = datetime(2020, 1, 1, tzinfo=timezone.utc)
BOTUSER = 50


def run(coro):
    return asyncio.run(coro)


def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class FakeRole:
    _next = 5000

    def __init__(self, guild, name, position):
        FakeRole._next += 1
        self.id = FakeRole._next
        self.guild = guild
        self.name = name
        self.position = position
        self.mention = f"<@&{self.id}>"

    @property
    def members(self):
        return [m for m in self.guild.members.values() if self in m.roles]


class FakeMessage:
    def __init__(self, mid, channel, content=None, **kwargs):
        self.id = mid
        self.channel = channel
        self.content = content
        self.kwargs = kwargs
        self.edits = []
        self.deleted = False
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}/{mid}"

    async def edit(self, **kwargs):
        if self.deleted:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]
        self.kwargs.update({k: v for k, v in kwargs.items() if k != "content"})

    @property
    def view(self):
        return self.kwargs.get("view")

    @property
    def embed(self):
        return self.kwargs.get("embed")


class FakeText:
    _next = 300
    _mid = 9000

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.mention = f"<#{self.id}>"
        self.sent: list[FakeMessage] = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        FakeText._mid += 1
        msg = FakeMessage(FakeText._mid, self, content, **kwargs)
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        found = next((m for m in self.sent if m.id == mid), None)
        if found is None:
            found = FakeMessage(mid, self)
            found.deleted = True
        return found


class FakeMember:
    def __init__(self, uid, guild, roles=(), admin=False, created_at=OLD_ACCOUNT):
        self.id = uid
        self.guild = guild
        self.bot = False
        self.created_at = created_at
        self.name = f"user{uid}"
        self.display_name = f"player{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.role("@everyone")] + [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.role_calls = []

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)

    async def add_roles(self, *roles, reason=None):
        self.role_calls.append(("add", [r.name for r in roles]))
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.role_calls.append(("remove", [r.name for r in roles]))
        self.roles = [r for r in self.roles if r not in roles]


class FakeEvent:
    def __init__(self, eid, **kwargs):
        self.id = eid
        self.kwargs = kwargs
        self.name = kwargs.get("name")
        self.status = discord.EventStatus.scheduled
        self.calls = []

    async def start(self, reason=None):
        self.calls.append("start")
        self.status = discord.EventStatus.active

    async def end(self, reason=None):
        self.calls.append("end")
        self.status = discord.EventStatus.completed

    async def cancel(self, reason=None):
        self.calls.append("cancel")
        self.status = discord.EventStatus.cancelled


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.tourney = FakeText(config.TOURNAMENTS_CHANNEL)
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.tourney, self.mod_log]
        names = ["@everyone", config.TOURNEY_ROLE, "Front Desk", config.MOD_ROLE, config.KEEPER_ROLE]
        self.roles = [FakeRole(self, n, i) for i, n in enumerate(names)]
        self.members: dict[int, FakeMember] = {}
        self.me = FakeMember(BOTUSER, self, roles=["Front Desk"])
        self.events: dict[int, FakeEvent] = {}
        self.created_events: list[dict] = []
        self.event_fail = None

    def role(self, name):
        return config.match_by_name(self.roles, name)

    async def create_scheduled_event(self, **kwargs):
        if self.event_fail is not None:
            raise self.event_fail
        self.created_events.append(kwargs)
        ev = FakeEvent(7000 + len(self.created_events), **kwargs)
        self.events[ev.id] = ev
        return ev

    def get_scheduled_event(self, eid):
        return self.events.get(eid)

    async def fetch_scheduled_event(self, eid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        if uid in self.members:
            return self.members[uid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.dynamic = set()
        self.cogs = {}
        self.dispatched = []
        self.user = SimpleNamespace(id=BOTUSER)

    async def wait_until_ready(self):
        await asyncio.Event().wait()  # the tick never runs by itself in tests

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)

    def get_cog(self, name):
        return self.cogs.get(name)

    def dispatch(self, event, *args):
        self.dispatched.append((event, *args))


class FakeResponse:
    def __init__(self, inter):
        self.inter = inter
        self.done = False

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.inter.calls.append(("send_message", dict(content=content, **kwargs)))

    async def edit_message(self, **kwargs):
        self._finish()
        self.inter.calls.append(("edit_message", kwargs))
        if self.inter.message is not None:
            await self.inter.message.edit(**kwargs)

    async def defer(self, **kwargs):
        self._finish()
        self.inter.calls.append(("defer", kwargs))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, inter):
        self.inter = inter

    async def send(self, content=None, **kwargs):
        self.inter.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, bot, user, message=None):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.message = message
        self.channel = message.channel if message else None
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    def replies(self):
        return [kw.get("content") for k, kw in self.calls if k in ("send_message", "followup")]

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class Env:
    def __init__(self, db, guild, bot, monkeypatch):
        self.db, self.guild, self.bot = db, guild, bot
        self.t = T0
        monkeypatch.setattr(cogmod, "now", lambda: self.t)
        self.new_cog()

    def new_cog(self, seed=1):
        self.cog = Tournaments(self.bot, rng=random.Random(seed))
        self.bot.cogs["Tournaments"] = self.cog
        return self.cog

    def member(self, uid, **kw):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, **kw)
        return self.guild.members[uid]

    def inter(self, uid, message=None):
        return FakeInteraction(self.bot, self.member(uid), message)

    async def rows(self, sql, params=()):
        return [dict(r) for r in await self.db.fetchall(sql, params)]

    async def create(self, uid=MOD, name="Friday Cup", game="Valorant", size=8, starts_at=None):
        inter = self.inter(uid)
        await Tournaments.create.callback(self.cog, inter, name, game, size, starts_at)
        return inter

    def card(self):
        return self.guild.tourney.sent[0]

    async def join(self, uid, tid=1, action="join"):
        inter = self.inter(uid, message=self.card())
        await SignupButton(action, tid).callback(inter)
        return inter

    async def start(self, uid=MOD, tid=None):
        inter = self.inter(uid)
        await Tournaments.start.callback(self.cog, inter, tid)
        return inter

    async def setup(self, n, size=8, tid=1):
        await self.create(size=size)
        for uid in PLAYERS[:n]:
            await self.join(uid, tid)
        await self.start()

    def match_messages(self):
        return [m for m in self.guild.tourney.sent if m.view is not None and
                any(isinstance(i, MatchButton) for i in m.view.children)]

    async def message_for(self, match_id):
        value = await self.cog.meta(cogmod.match_key(match_id))
        _, mid = cogmod.unpack(value)
        return next(m for m in self.guild.tourney.sent if m.id == mid)

    async def match(self, round_, slot, tid=1):
        b = await self.cog.bracket(tid)
        return b.get(round_, slot)

    async def press(self, uid, match_id, pick):
        message = await self.message_for(match_id)
        inter = self.inter(uid, message=message)
        await MatchButton(match_id, pick).callback(inter)
        return inter

    async def play(self, round_, slot, winner_pick=1, tid=1):
        """Both players agree that P<winner_pick> won."""
        m = await self.match(round_, slot, tid)
        await self.press(m.p1, m.id, winner_pick)
        await self.press(m.p2, m.id, winner_pick)

    async def play_all(self, tid=1):
        while True:
            b = await self.cog.bracket(tid)
            if b.champion() is not None:
                return b
            for m in b.open_matches():
                await self.play(m.round, m.slot, 1 if m.p1 < m.p2 else 2, tid)


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        env = Env(db, guild, bot, monkeypatch)
        env.member(MOD, roles=[config.MOD_ROLE])
        env.member(KEEPER, roles=[config.KEEPER_ROLE])
        env.member(OWNER)
        for uid in PLAYERS:
            env.member(uid)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- load and buttons
def test_cog_load_registers_buttons(monkeypatch):
    async def go(env):
        await env.cog.cog_load()
        assert {SignupButton, MatchButton} <= env.bot.dynamic
        await env.cog.cog_unload()
        assert not env.bot.dynamic
    with_env(go, monkeypatch)


def test_custom_ids_match_templates():
    for item, cls in ((SignupButton("join", 7), SignupButton), (SignupButton("leave", 7), SignupButton),
                      (MatchButton(12, 2), MatchButton)):
        assert re.fullmatch(cls.__discord_ui_compiled_template__, item.item.custom_id)
    assert MatchButton(12, 1).item.label == "P1 won" and MatchButton(12, 2).item.label == "P2 won"

    async def go():
        m = re.fullmatch(MatchButton.__discord_ui_compiled_template__, "tourney:report:12:2")
        rebuilt = await MatchButton.from_custom_id(None, None, m)
        assert (rebuilt.match_id, rebuilt.pick) == (12, 2)
        m = re.fullmatch(SignupButton.__discord_ui_compiled_template__, "tourney:leave:3")
        rebuilt = await SignupButton.from_custom_id(None, None, m)
        assert (rebuilt.action, rebuilt.tid) == ("leave", 3)
    run(go())


# ---------------------------------------------------------------- create
def test_create_posts_signup_card(monkeypatch):
    async def go(env):
        inter = await env.create(name="Friday  Cup", game="valorant", size=8)
        (t,) = await env.rows("SELECT * FROM tournaments")
        assert (t["name"], t["game"], t["size"], t["status"], t["created_by"]) == (
            "Friday Cup", "Valorant", 8, "signup", MOD)
        card = env.card()
        assert (t["channel_id"], t["message_id"]) == (env.guild.tourney.id, card.id)
        assert "Entrants 0/8" in card.embed.description
        assert [i.item.custom_id for i in card.view.children] == ["tourney:join:1", "tourney:leave:1"]
        assert card.kwargs["allowed_mentions"].everyone is False
        assert "open for signups" in inter.replies()[0]
    with_env(go, monkeypatch)


def test_create_parses_start_time(monkeypatch):
    async def go(env):
        await env.create(starts_at="sat 8pm")
        (t,) = await env.rows("SELECT * FROM tournaments")
        expected = datetime(2026, 10, 10, 20, 0, tzinfo=TZ)
        assert t["starts_at"] == int(expected.timestamp())
        assert f"<t:{t['starts_at']}:F>" in env.card().embed.description
    with_env(go, monkeypatch)


def test_create_rejects_bad_time_and_non_staff(monkeypatch):
    async def go(env):
        inter = await env.create(starts_at="whenever")
        assert "couldn't read" in inter.replies()[0]
        inter = await env.create(uid=PLAYERS[0])
        assert "Only Keepers and Moderators" in inter.replies()[0]
        assert await env.rows("SELECT * FROM tournaments") == []
        assert env.guild.tourney.sent == []
    with_env(go, monkeypatch)


def test_create_escapes_name_markdown(monkeypatch):
    async def go(env):
        await env.create(name="[free nitro](https://x.example) **cup**")
        title = env.card().embed.title
        assert "\\[" in title and "\\*\\*" in title
    with_env(go, monkeypatch)


def test_create_without_channel_or_on_send_failure(monkeypatch):
    async def go(env):
        env.guild.tourney.fail = http_error()
        with pytest.raises(discord.HTTPException):
            await env.create()
        assert await env.rows("SELECT * FROM tournaments") == []
        env.guild.text_channels.remove(env.guild.tourney)
        inter = await env.create()
        assert "couldn't find" in inter.replies()[0]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- signups
def test_join_leave_and_limits(monkeypatch):
    async def go(env):
        await env.create(size=4)
        inter = await env.join(PLAYERS[0])
        assert inter.of("edit_message") and "Entrants 1/4" in env.card().embed.description
        assert f"<@{PLAYERS[0]}>" in env.card().embed.description
        inter = await env.join(PLAYERS[0])
        assert inter.replies() == ["You're already signed up."]
        for uid in PLAYERS[1:4]:
            await env.join(uid)
        join_button = env.card().view.children[0]
        assert join_button.item.disabled  # full
        inter = await env.join(PLAYERS[4])
        assert inter.replies() == ["This tournament is full."]
        inter = await env.join(PLAYERS[4], action="leave")
        assert inter.replies() == ["You're not signed up."]
        await env.join(PLAYERS[3], action="leave")
        assert "Entrants 3/4" in env.card().embed.description
        rows = await env.rows("SELECT user_id FROM tournament_entries")
        assert [r["user_id"] for r in rows] == PLAYERS[:3]
    with_env(go, monkeypatch)


def test_cannot_join_after_start(monkeypatch):
    async def go(env):
        await env.setup(3)
        inter = await env.join(PLAYERS[5])
        assert inter.replies() == ["Signups for this tournament are closed."]
        inter = await env.join(PLAYERS[0], action="leave")
        assert inter.replies() == ["Signups for this tournament are closed."]
        assert all(i.item.disabled for i in env.card().view.children)
        assert "Signups are closed" in env.card().embed.description
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- start
def test_start_needs_staff_and_two_entrants(monkeypatch):
    async def go(env):
        await env.create()
        inter = await env.start(uid=PLAYERS[0])
        assert "Only Keepers and Moderators" in inter.replies()[0]
        await env.join(PLAYERS[0])
        inter = await env.start()
        assert inter.replies() == ["A tournament needs at least two entrants."]
        (t,) = await env.rows("SELECT status FROM tournaments")
        assert t["status"] == "signup"
    with_env(go, monkeypatch)


def test_start_seeds_with_rng_and_posts_bracket_and_round_one(monkeypatch):
    async def go(env):
        await env.setup(5)  # 8-bracket: 3 byes, one real round-1 match, one open round-2 match
        entries = await env.rows("SELECT user_id, seed FROM tournament_entries ORDER BY seed")
        expected = T.seed(PLAYERS[:5], random.Random(1))
        assert [e["user_id"] for e in entries] == expected
        assert [e["seed"] for e in entries] == [1, 2, 3, 4, 5]
        matches = await env.rows("SELECT * FROM tournament_matches ORDER BY round, slot")
        assert len(matches) == 7
        assert sum(m["status"] == "bye" for m in matches) == 3
        bracket_msg = env.guild.tourney.sent[1]
        assert bracket_msg.embed.description.startswith("```") and "(bye)" in bracket_msg.embed.description
        posted = env.match_messages()
        b = await env.cog.bracket(1)
        assert len(posted) == len(b.open_matches()) == 2
        for msg in posted:
            am = msg.kwargs["allowed_mentions"]
            assert am.everyone is False and am.roles is False and len(am.users) == 2
            assert "P1 <@" in msg.content and "vs  P2 <@" in msg.content
        (t,) = await env.rows("SELECT status FROM tournaments")
        assert t["status"] == "running"
        inter = await env.start()
        assert "is running" in inter.replies()[0] or "no tournament" in inter.replies()[0].lower()
    with_env(go, monkeypatch)


def test_start_picks_tournament_when_several_open(monkeypatch):
    async def go(env):
        await env.create(name="A")
        await env.create(name="B")
        inter = await env.start()
        assert "More than one" in inter.replies()[0]
        inter = await env.start(tid=7)
        assert "no tournament #7" in inter.replies()[0]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- reports
def test_report_then_confirm_advances(monkeypatch):
    async def go(env):
        await env.setup(4)
        m = await env.match(1, 0)
        inter = await env.press(m.p1, m.id, 1)
        assert inter.of("edit_message")
        msg = await env.message_for(m.id)
        assert "press the same button to confirm" in msg.content
        assert (await env.match(1, 0)).status == T.REPORTED
        assert len(env.match_messages()) == 2  # nothing new yet
        await env.press(m.p2, m.id, 1)
        done = await env.match(1, 0)
        assert (done.status, done.winner) == (T.DONE, m.p1)
        assert all(i.item.disabled for i in msg.view.children) and "won." in msg.content
        final = await env.match(2, 0)
        assert final.p1 == m.p1 and final.status == T.PENDING
        await env.play(1, 1, 2)
        final = await env.match(2, 0)
        assert final.status == T.OPEN
        assert len(env.match_messages()) == 3  # the final got its own message
        bracket_msg = env.guild.tourney.sent[1]
        assert bracket_msg.edits and f"> player{m.p1}" in bracket_msg.embed.description
    with_env(go, monkeypatch)


def test_outsiders_cannot_report_and_done_is_done(monkeypatch):
    async def go(env):
        await env.setup(4)
        m = await env.match(1, 0)
        inter = await env.press(PLAYERS[10], m.id, 1)
        assert "Only the two players" in inter.replies()[0]
        await env.play(1, 0)
        inter = await env.press(m.p1, m.id, 2)
        assert inter.replies() == ["This match is already decided."]
        final = await env.match(2, 0)
        assert final.status == T.PENDING  # no message yet, but a press would say not ready
    with_env(go, monkeypatch)


def test_conflict_flags_staff_and_locks_players(monkeypatch):
    async def go(env):
        await env.setup(4)
        m = await env.match(1, 0)
        await env.press(m.p1, m.id, 1)
        await env.press(m.p2, m.id, 2)
        assert (await env.match(1, 0)).status == T.CONFLICT
        (flag,) = env.guild.mod_log.sent
        assert "disagree" in flag.content and (await env.message_for(m.id)).jump_url in flag.content
        am = flag.kwargs["allowed_mentions"]
        assert am.users is False and am.roles is False and am.everyone is False
        inter = await env.press(m.p1, m.id, 1)
        assert "Keeper or Moderator will decide" in inter.replies()[0]
        await env.press(KEEPER, m.id, 2)
        decided = await env.match(1, 0)
        assert (decided.status, decided.winner, decided.reported_by) == (T.DONE, m.p2, KEEPER)
        assert (await env.match(2, 0)).p1 == m.p2
    with_env(go, monkeypatch)


def test_staff_override_is_final_immediately(monkeypatch):
    async def go(env):
        await env.setup(4)
        m = await env.match(1, 1)
        await env.press(MOD, m.id, 2)
        decided = await env.match(1, 1)
        assert (decided.status, decided.winner) == (T.DONE, m.p2)
        assert env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- the champion
def test_full_tournament_pays_announces_and_dispatches(monkeypatch):
    async def go(env):
        await env.setup(6)
        b = await env.play_all()
        champ, second = b.champion(), b.runner_up()
        (t,) = await env.rows("SELECT * FROM tournaments")
        assert (t["status"], t["winner_id"]) == ("done", champ)
        assert await economy.balance(env.db, champ) == 1000
        assert await economy.balance(env.db, second) == 400
        refs = [r["ref"] for r in await env.rows("SELECT ref FROM ledger ORDER BY rowid")]
        assert refs == ["tourney:1:first", "tourney:1:second"]
        announce = env.guild.tourney.sent[-1]
        assert f"<@{champ}> won" in announce.content
        assert [u.id for u in announce.kwargs["allowed_mentions"].users] == [champ]
        assert announce.kwargs["allowed_mentions"].roles is False
        assert env.bot.dispatched == [("tournament_won", 1, champ)]
        assert env.guild.role(config.TOURNEY_ROLE) in env.guild.members[champ].roles
        assert "Champion: player" in env.guild.tourney.sent[1].embed.description
        assert await env.cog.meta(cogmod.announce_key(1)) is None
    with_env(go, monkeypatch)


def test_payouts_and_announcement_are_idempotent(monkeypatch):
    async def go(env):
        await env.setup(2, size=4)
        b = await env.play_all()
        champ = b.champion()
        sent = len(env.guild.tourney.sent)
        # A repeat close-out (e.g. a retry) can't pay twice.
        async with env.db.transaction() as tx:
            await env.cog.close_out_tx(tx, await env.cog.get(1, tx), b)
        await env.cog.sync(1)
        assert await economy.balance(env.db, champ) == 1000
        assert len(await env.rows("SELECT * FROM ledger")) == 2
        # ...but the re-armed marker announces again only once, then never.
        assert len(env.guild.tourney.sent) == sent + 1
        await env.cog.sync(1)
        await env.cog.on_ready()
        assert len(env.guild.tourney.sent) == sent + 1
        # Presses on the finished final do nothing.
        m = await env.match(1, 0)
        inter = await env.press(m.p1, m.id, 1)
        assert inter.replies() == ["This tournament is over."]
    with_env(go, monkeypatch)


def test_champion_role_moves_from_previous_champion(monkeypatch):
    async def go(env):
        role = env.guild.role(config.TOURNEY_ROLE)
        await env.setup(2, size=4)
        first = (await env.play_all(1)).champion()
        assert role in env.guild.members[first].roles
        # Second tournament: two different players.
        await env.create(name="Round two", size=4)
        card = env.guild.tourney.sent[-1]
        for uid in PLAYERS[5:7]:
            await SignupButton("join", 2).callback(env.inter(uid, message=card))
        await env.start(tid=2)
        second = (await env.play_all(2)).champion()
        assert second != first
        assert role in env.guild.members[second].roles
        assert role not in env.guild.members[first].roles
        assert ("remove", [config.TOURNEY_ROLE]) in env.guild.members[first].role_calls
    with_env(go, monkeypatch)


def test_role_above_bot_is_skipped_with_a_note(monkeypatch):
    async def go(env):
        role = env.guild.role(config.TOURNEY_ROLE)
        role.position = 99
        await env.setup(2, size=4)
        champ = (await env.play_all()).champion()
        assert role not in env.guild.members[champ].roles
        assert any("has to be above" in m.content for m in env.guild.mod_log.sent)
        assert await economy.balance(env.db, champ) == 1000  # still paid
        assert env.bot.dispatched == [("tournament_won", 1, champ)]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- restarts
def test_restart_mid_tournament_keeps_going(monkeypatch):
    async def go(env):
        await env.setup(4)
        m = await env.match(1, 0)
        await env.press(m.p1, m.id, 1)  # reported before the restart
        env.new_cog(seed=99)
        await env.press(m.p2, m.id, 1)  # confirmed after it: buttons rebuilt from custom ids
        assert (await env.match(1, 0)).status == T.DONE
        await env.play(1, 1, 1)
        b = await env.play_all()
        assert b.champion() is not None
        assert env.bot.dispatched[0][0] == "tournament_won"
    with_env(go, monkeypatch)


def test_resume_posts_missing_match_messages_and_owed_announcement(monkeypatch):
    async def go(env):
        await env.setup(4)
        b = await env.cog.bracket(1)
        # Simulate a crash before one match message went out.
        lost = b.open_matches()[1]
        await env.db.execute("DELETE FROM meta WHERE key = ?", (cogmod.match_key(lost.id),))
        before = len(env.match_messages())
        env.new_cog()
        await env.cog.on_ready()
        assert len(env.match_messages()) == before + 1
        await env.cog.on_ready()
        assert len(env.match_messages()) == before + 1  # never twice

        # Crash right after the final's transaction: the announcement is owed.
        real_finish = Tournaments.finish

        async def crashed(self, *a):
            return None
        monkeypatch.setattr(Tournaments, "finish", crashed)
        await env.play_all()
        monkeypatch.setattr(Tournaments, "finish", real_finish)
        assert env.bot.dispatched == []
        assert await env.cog.meta(cogmod.announce_key(1)) is not None
        env.new_cog()
        await env.cog.on_ready()
        assert len(env.bot.dispatched) == 1
        assert "won" in env.guild.tourney.sent[-1].content
    with_env(go, monkeypatch)


def test_on_ready_never_raises(monkeypatch):
    async def go(env):
        await env.setup(4)

        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "sync", boom)
        await env.cog.on_ready()
        monkeypatch.setattr(env.db, "fetchall", boom)
        await env.cog.on_ready()
    with_env(go, monkeypatch)


def test_discord_failures_while_posting_matches_are_retried(monkeypatch):
    async def go(env):
        await env.create(size=4)
        for uid in PLAYERS[:4]:
            await env.join(uid)
        env.guild.tourney.fail = http_error()
        await env.start()
        assert (await env.rows("SELECT status FROM tournaments"))[0]["status"] == "running"
        env.guild.tourney.fail = None
        await env.cog.on_ready()
        assert len(env.match_messages()) == 2
    with_env(go, monkeypatch)


def test_button_errors_reply_generically(monkeypatch):
    async def go(env):
        await env.setup(4)

        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "handle_report", boom)
        m = await env.match(1, 0)
        inter = await env.press(m.p1, m.id, 1)
        assert inter.replies() and "didn't work" in inter.replies()[0]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- cancel and bracket
def test_cancel_closes_everything_and_pays_nothing(monkeypatch):
    async def go(env):
        await env.setup(4)
        m = await env.match(1, 0)
        inter = FakeInteraction(env.bot, env.member(PLAYERS[0]))
        await Tournaments.cancel.callback(env.cog, inter, None)
        assert "Only Keepers and Moderators" in inter.replies()[0]
        inter = env.inter(MOD)
        await Tournaments.cancel.callback(env.cog, inter, None)
        assert "cancelled" in inter.replies()[-1]
        (t,) = await env.rows("SELECT status FROM tournaments")
        assert t["status"] == "cancelled"
        for msg in env.match_messages():
            assert all(i.item.disabled for i in msg.view.children)
            assert "cancelled" in msg.content
        assert "cancelled" in env.card().embed.description
        inter = await env.press(m.p1, m.id, 1)
        assert inter.replies() == ["This tournament is over."]
        assert await env.rows("SELECT * FROM ledger") == []
        inter = env.inter(MOD)
        await Tournaments.cancel.callback(env.cog, inter, 1)
        assert "cancelled, so that doesn't apply" in inter.replies()[0]
    with_env(go, monkeypatch)


def test_cancel_during_signup(monkeypatch):
    async def go(env):
        await env.create()
        await env.join(PLAYERS[0])
        inter = env.inter(KEEPER)
        await Tournaments.cancel.callback(env.cog, inter, None)
        assert all(i.item.disabled for i in env.card().view.children)
        inter = await env.join(PLAYERS[1])
        assert inter.replies() == ["Signups for this tournament are closed."]
    with_env(go, monkeypatch)


def test_bracket_command(monkeypatch):
    async def go(env):
        inter = env.inter(PLAYERS[0])
        await Tournaments.show_bracket.callback(env.cog, inter, None)
        assert inter.replies() == ["There's no tournament yet."]
        await env.create()
        await env.join(PLAYERS[0])
        inter = env.inter(PLAYERS[0])
        await Tournaments.show_bracket.callback(env.cog, inter, None)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] and "Entrants 1/8" in sent["embed"].description
        await env.join(PLAYERS[1])
        await env.start()
        inter = env.inter(PLAYERS[0])
        await Tournaments.show_bracket.callback(env.cog, inter, 1)
        (sent,) = inter.of("send_message")
        assert sent["embed"].description.startswith("```") and "Final" in sent["embed"].description
    with_env(go, monkeypatch)


def test_tournament_autocomplete(monkeypatch):
    async def go(env):
        await env.create(name="Alpha")
        await env.create(name="Beta")
        choices = await env.cog.tournament_choices(env.inter(MOD), "alp")
        assert [c.value for c in choices] == [1]
        games = await env.cog.game_choices(env.inter(MOD), "val")
        assert games and all("val" in c.name.lower() for c in games)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- automation
def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


NOV_FIRST_MONDAY = ts(2026, 11, 2, 12)
NOV_START = ts(2026, 11, 7, 19)  # the Saturday after
DAY = 86400


async def tick(env, at):
    env.t = at
    await Tournaments.tick.coro(env.cog)


async def make_auto(env):
    """First startup in October, then the November first Monday: tournament #1 is the auto one."""
    await tick(env, ts(2026, 10, 7, 12))
    await tick(env, NOV_FIRST_MONDAY)
    t = await env.cog.get(1)
    assert t is not None
    return t


def nudges(env):
    return [x for x in env.guild.tourney.sent if x.content and x.content.startswith("⏳")]


def test_first_run_only_marks_this_month_done(monkeypatch):
    async def go(env):
        await tick(env, ts(2026, 10, 7, 12))  # October's first Monday has passed
        assert await env.rows("SELECT * FROM tournaments") == []
        assert await env.rows("SELECT key FROM jobs WHERE key LIKE 'autotourney:%'") == [
            {"key": "autotourney:2026-10"}]
        await tick(env, NOV_FIRST_MONDAY - 60)
        assert await env.rows("SELECT * FROM tournaments") == []
    with_env(go, monkeypatch)


def test_monthly_auto_tournament_is_created_once(monkeypatch):
    async def go(env):
        t = await make_auto(env)
        assert (t["size"], t["status"], t["created_by"], t["starts_at"]) == (16, "signup", BOTUSER, NOV_START)
        assert "November 2026" in t["name"]
        assert env.card().embed is not None
        assert env.card().kwargs["allowed_mentions"].users is False
        assert (t["channel_id"], t["message_id"]) == (env.guild.tourney.id, env.card().id)
        [ev] = env.guild.created_events
        assert ev["entity_type"] == discord.EntityType.external
        assert ev["location"] == f"#{config.TOURNAMENTS_CHANNEL}"
        assert int(ev["start_time"].timestamp()) == NOV_START and ev["end_time"] > ev["start_time"]
        assert await env.cog.meta(cogmod.auto_key(1)) == "7001"
        env.new_cog()  # a restart changes nothing
        await tick(env, NOV_FIRST_MONDAY + 60)
        await tick(env, ts(2026, 11, 3, 12))
        assert len(await env.rows("SELECT * FROM tournaments")) == 1
        assert len(env.guild.created_events) == 1
    with_env(go, monkeypatch)


def test_auto_tournament_uses_the_most_played_game(monkeypatch):
    async def go(env):
        g = config.GAMES[-1]
        start = ts(2026, 10, 20, 20)
        for uid in PLAYERS[:3]:
            await env.db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                                 (uid, g.role, start, start + 3 * 3600))
        t = await make_auto(env)
        assert t["game"] == g.role and g.role in t["name"]
    with_env(go, monkeypatch)


def test_auto_tournament_rotates_games_without_play_data(monkeypatch):
    async def go(env):
        t = await make_auto(env)
        assert t["game"] == T.auto_game(None, 2026, 11, config.GAMES).role
    with_env(go, monkeypatch)


def test_no_auto_tournament_while_one_is_on(monkeypatch):
    async def go(env):
        await tick(env, ts(2026, 10, 7, 12))
        await env.create()
        await tick(env, NOV_FIRST_MONDAY)
        assert len(await env.rows("SELECT * FROM tournaments")) == 1
        assert env.guild.created_events == []
        assert await env.rows("SELECT key FROM jobs WHERE key = 'autotourney:2026-11'")
    with_env(go, monkeypatch)


def test_auto_tournament_retries_when_the_card_fails(monkeypatch):
    async def go(env):
        await tick(env, ts(2026, 10, 7, 12))
        env.guild.tourney.fail = http_error()
        await tick(env, NOV_FIRST_MONDAY)
        assert await env.rows("SELECT * FROM tournaments") == []
        assert await env.cog.meta(cogmod.auto_key(1)) is None
        assert not await env.rows("SELECT key FROM jobs WHERE key = 'autotourney:2026-11'")
        env.guild.tourney.fail = None
        await tick(env, NOV_FIRST_MONDAY + 60)
        assert len(await env.rows("SELECT * FROM tournaments")) == 1
    with_env(go, monkeypatch)


def test_auto_tournament_without_an_event_still_opens(monkeypatch):
    async def go(env):
        env.guild.event_fail = http_error(403)
        t = await make_auto(env)
        assert t["status"] == "signup"
        assert await env.cog.meta(cogmod.auto_key(t["id"])) == ""
        assert await env.rows("SELECT key FROM jobs WHERE key = 'autotourney:2026-11'")
    with_env(go, monkeypatch)


def test_too_late_after_downtime_skips_the_month(monkeypatch):
    async def go(env):
        await tick(env, ts(2026, 10, 7, 12))
        await tick(env, NOV_START - 3600)  # the bot was off all week
        assert await env.rows("SELECT * FROM tournaments") == []
        assert await env.rows("SELECT key FROM jobs WHERE key = 'autotourney:2026-11'")
    with_env(go, monkeypatch)


def test_fresh_accounts_cant_sign_up(monkeypatch):
    async def go(env):
        await env.create()
        fresh = 999_001
        env.member(fresh, created_at=datetime.fromtimestamp(env.t - 5 * DAY, timezone.utc))
        inter = await env.join(fresh)
        assert "days old" in inter.replies()[0]
        assert await env.cog.entrants(1) == []
    with_env(go, monkeypatch)


def test_reminder_pings_entrants_once_an_hour_before(monkeypatch):
    async def go(env):
        await make_auto(env)
        for uid in PLAYERS[:4]:
            await env.join(uid)
        before = len(env.guild.tourney.sent)
        await tick(env, NOV_START - 3600 - 60)
        assert len(env.guild.tourney.sent) == before
        await tick(env, NOV_START - 3500)
        [msg] = env.guild.tourney.sent[before:]
        assert all(f"<@{u}>" in msg.content for u in PLAYERS[:4])
        allowed = msg.kwargs["allowed_mentions"]
        assert {u.id for u in allowed.users} == set(PLAYERS[:4])
        assert allowed.roles is False and allowed.everyone is False
        await tick(env, NOV_START - 600)
        assert len(env.guild.tourney.sent) == before + 1
    with_env(go, monkeypatch)


def test_reminder_says_when_more_players_are_needed(monkeypatch):
    async def go(env):
        await make_auto(env)
        await env.join(PLAYERS[0])
        await tick(env, NOV_START - 1800)
        assert "needs 4 players" in env.guild.tourney.sent[-1].content
    with_env(go, monkeypatch)


def test_auto_tournament_starts_itself(monkeypatch):
    async def go(env):
        await make_auto(env)
        for uid in PLAYERS[:5]:
            await env.join(uid)
        await tick(env, NOV_START - 60)
        assert (await env.cog.get(1))["status"] == "signup"
        await tick(env, NOV_START)
        assert (await env.cog.get(1))["status"] == "running"
        assert len(env.match_messages()) == 2  # 5 players: three byes, so 4v5 and the 2v3 semifinal
        assert env.guild.events[7001].calls == ["start"]
        env.new_cog()
        await tick(env, NOV_START + 60)
        assert len(env.match_messages()) == 2
    with_env(go, monkeypatch)


def test_auto_tournament_with_too_few_players_is_cancelled(monkeypatch):
    async def go(env):
        await make_auto(env)
        for uid in PLAYERS[:3]:
            await env.join(uid)
        env.guild.tourney.fail = http_error()
        await tick(env, NOV_START)
        assert (await env.cog.get(1))["status"] == "cancelled"
        assert await env.cog.meta(cogmod.autocancel_key(1)) is not None  # the note is still owed
        assert env.guild.events[7001].calls == []
        env.guild.tourney.fail = None
        env.new_cog()
        await tick(env, NOV_START + 60)
        note = env.guild.tourney.sent[-1]
        assert "needed 4 players" in note.content and "3 signed up" in note.content
        assert note.kwargs["allowed_mentions"].users is False
        assert env.guild.events[7001].calls == ["cancel"]
        assert await env.cog.meta(cogmod.autocancel_key(1)) is None
        assert "cancelled" in env.card().embed.footer.text.lower()
        await tick(env, NOV_START + 120)
        assert env.guild.tourney.sent[-1] is note
    with_env(go, monkeypatch)


def test_staff_tournaments_are_not_auto_started(monkeypatch):
    async def go(env):
        await env.create(starts_at="sat 8pm")
        for uid in PLAYERS[:4]:
            await env.join(uid)
        t = await env.cog.get(1)
        assert t["starts_at"] is not None
        await tick(env, t["starts_at"] + 60)
        assert (await env.cog.get(1))["status"] == "signup"
    with_env(go, monkeypatch)


def test_staff_cancel_cancels_the_event(monkeypatch):
    async def go(env):
        await make_auto(env)
        inter = env.inter(MOD)
        await Tournaments.cancel.callback(env.cog, inter, None)
        assert env.guild.events[7001].calls == ["cancel"]
    with_env(go, monkeypatch)


def test_auto_tournament_finish_ends_the_event(monkeypatch):
    async def go(env):
        await make_auto(env)
        for uid in PLAYERS[:4]:
            await env.join(uid)
        await tick(env, NOV_START)
        await env.play_all()
        assert env.guild.events[7001].calls == ["start", "end"]
    with_env(go, monkeypatch)


def test_unreported_match_nudges_then_flags_staff(monkeypatch):
    async def go(env):
        await env.setup(4)
        await tick(env, T0 + DAY - 60)
        assert nudges(env) == [] and env.guild.mod_log.sent == []
        await tick(env, T0 + DAY)
        sent = nudges(env)
        assert len(sent) == 2
        b = await env.cog.bracket(1)
        for m, n in zip(b.round(1), sent):
            assert f"<@{m.p1}>" in n.content and f"<@{m.p2}>" in n.content
            assert {u.id for u in n.kwargs["allowed_mentions"].users} == {m.p1, m.p2}
            assert "https://discord.com/channels/" in n.content
        await tick(env, T0 + DAY + 3600)
        assert len(nudges(env)) == 2  # once
        await tick(env, T0 + 2 * DAY)
        assert len(env.guild.mod_log.sent) == 2
        assert all(f.kwargs["allowed_mentions"].users is False for f in env.guild.mod_log.sent)
        assert all("48 hours" in f.content for f in env.guild.mod_log.sent)
        await tick(env, T0 + 5 * DAY)
        assert len(env.guild.mod_log.sent) == 2  # flagged once; staff decide
        assert all(m.status == T.OPEN for m in (await env.cog.bracket(1)).round(1))
    with_env(go, monkeypatch)


def test_lone_report_is_flagged_to_staff_not_accepted(monkeypatch):
    """A player claiming a win the opponent never confirmed must not decide the match (or pay
    the prize) on a timer: staff are flagged in mod-log once and decide."""
    async def go(env):
        await env.setup(2)
        m = await env.match(1, 0)
        balance_p1 = await economy.balance(env.db, m.p1)
        balance_p2 = await economy.balance(env.db, m.p2)
        env.t = T0 + 3600
        await env.press(m.p2, m.id, 2)  # "I won"
        await tick(env, T0 + DAY)
        assert env.guild.mod_log.sent == [] and nudges(env) == []  # a report: no nudge
        await tick(env, T0 + 2 * DAY - 60)
        assert (await env.match(1, 0)).status == T.REPORTED
        await tick(env, T0 + 2 * DAY)
        still = await env.match(1, 0)
        assert still.status == T.REPORTED  # not decided
        assert (await env.cog.get(1))["status"] == "running"
        assert await economy.balance(env.db, m.p2) == balance_p2
        assert await economy.balance(env.db, m.p1) == balance_p1
        (flag,) = env.guild.mod_log.sent
        assert f"<@{m.p2}>" in flag.content and "confirm" in flag.content
        assert (await env.message_for(m.id)).jump_url in flag.content
        assert flag.kwargs["allowed_mentions"].users is False
        await tick(env, T0 + 5 * DAY)
        assert len(env.guild.mod_log.sent) == 1  # once
        assert (await env.match(1, 0)).status == T.REPORTED
        # staff can still settle it
        await env.press(MOD, m.id, 2)
        assert (await env.match(1, 0)).status == T.DONE
    with_env(go, monkeypatch)


def test_stale_clock_starts_for_matches_opened_before_tracking(monkeypatch):
    async def go(env):
        await env.setup(2)
        m = await env.match(1, 0)
        await env.db.execute("DELETE FROM meta WHERE key = ?", (cogmod.opened_key(m.id),))
        await tick(env, T0 + 3 * DAY)  # starts the clock instead of flagging at once
        assert env.guild.mod_log.sent == [] and nudges(env) == []
        await tick(env, T0 + 4 * DAY)
        assert len(nudges(env)) == 1 and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


def test_tick_never_raises(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(env.cog, "run_auto_create", boom)
        monkeypatch.setattr(env.cog, "sweep_matches", boom)
        await tick(env, T0)
    with_env(go, monkeypatch)
