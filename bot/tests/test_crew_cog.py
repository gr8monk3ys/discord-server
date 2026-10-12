"""Offline tests for cogs.crew: a real in-memory SQLite database plus small fakes for the bot,
guild, roles, channels, members, messages and interactions. No network, no real clock."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import crew as cogmod
from cogs.crew import LAST_PING_KEY, Crew, CrewButton, build_view
from cogs.moderation import RAID_KEY
from logic import crew as C
from logic import moderation as M
from logic import quests as Q

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
BOT_ID = 50
OWNER_ID = 1
DAY = 24 * 3600


def run(coro):
    return asyncio.run(coro)


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


def at(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)


def http_error(cls=discord.Forbidden, status=403):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


def young(now, days=5):
    """A snowflake for an account created `days` before `now` (too new to count)."""
    return ((now - days * DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22


class FakeRole:
    _next = 1000

    def __init__(self, name, guild, position=5, managed=False, perms=None, rid=None):
        FakeRole._next += 1
        self.id = rid or FakeRole._next
        self.name, self.guild, self.position, self.managed = name, guild, position, managed
        self.permissions = discord.Permissions(**(perms or {}))
        self.mention = f"<@&{self.id}>"

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    def __hash__(self):
        return self.id


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, guild, roles=(), bot=False, joined_at=None, status=discord.Status.online,
                 onboarded=True):
        self.id, self.guild, self.bot = uid, guild, bot
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.default, *roles]
        self.joined_at = joined_at
        self.created_at = at(Q.account_created(uid))
        self.status = status
        self.flags = SimpleNamespace(completed_onboarding=onboarded)
        self.guild_permissions = discord.Permissions.none()
        self.added, self.removed, self.fail = [], [], None

    async def add_roles(self, *roles, reason=None):
        if self.fail is not None:
            raise self.fail
        self.added.extend(roles)
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        if self.fail is not None:
            raise self.fail
        self.removed.extend(roles)
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, with_role=True):
        self.id, self.owner_id = GUILD_ID, OWNER_ID
        self.members = []
        self.default = FakeRole("@everyone", self, position=0, rid=GUILD_ID)
        self.top = FakeRole("Front Desk", self, position=50)
        self.crew = FakeRole(config.CREW_ROLE, self)
        self.mod = FakeRole(config.MOD_ROLE, self, position=60)
        self.roles = [self.default, self.top, self.mod] + ([self.crew] if with_role else [])
        self.me = SimpleNamespace(id=BOT_ID, top_role=self.top,
                                  guild_permissions=discord.Permissions(manage_roles=True))
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.text_channels = [self.general, self.mod_log]
        self.onboarding_on = True

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def onboarding(self):
        return SimpleNamespace(enabled=self.onboarding_on)


class FakeBot:
    def __init__(self, db, guild):
        self.db, self.guild = db, guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.intents = discord.Intents.all()
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = ts(2026, 10, 7, 12, 0)

    def member(self, uid, crew=False, roles=(), **kw):
        kw.setdefault("joined_at", at(self.t - 100 * DAY))
        m = FakeMember(uid, self.guild, roles=[*roles, *([self.guild.crew] if crew else [])], **kw)
        self.guild.members.append(m)
        return m

    def newcomer(self, uid, **kw):
        return self.member(uid, joined_at=at(self.t), **kw)

    def say(self, author, reply_to=None, mentions=(), channel=None):
        ref = SimpleNamespace(resolved=reply_to) if reply_to is not None else None
        return SimpleNamespace(author=author, guild=self.guild, channel=channel or self.guild.general,
                               type=discord.MessageType.reply if ref else discord.MessageType.default,
                               reference=ref, mentions=list(mentions), webhook_id=None, created_at=at(self.t))

    def click(self, user):
        return SimpleNamespace(guild=self.guild, user=user, response=FakeResponse())

    async def arrivals(self):
        return {r["user_id"]: dict(r) for r in await self.db.fetchall("SELECT * FROM crew_arrivals")}

    async def greetings(self):
        return {r["newcomer_id"]: (r["crew_id"], r["coins"])
                for r in await self.db.fetchall("SELECT * FROM crew_greetings")}

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def exchange(self, crew, newcomer):
        """The crew member says hi, the newcomer replies to that message a minute later."""
        hello = self.say(crew, mentions=[newcomer])
        await self.cog.on_message(hello)
        self.t += 60
        await self.cog.on_message(self.say(newcomer, reply_to=hello))


def with_env(fn, monkeypatch, with_role=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(with_role)
        bot = FakeBot(db, guild)
        cog = Crew(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        monkeypatch.setattr(cogmod, "clock", lambda: float(env.t))
        monkeypatch.delenv("CREW_PING_MINUTES", raising=False)
        monkeypatch.delenv("CREW_QUIET_HOURS", raising=False)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def role_ping_only(sent, role):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.users is False
    assert [r.id for r in am.roles] == [role.id]


# ---------------------------------------------------------------- arrivals
def test_arrival_is_recorded_on_join_when_onboarding_is_done_or_off(monkeypatch):
    async def body(env):
        done = env.newcomer(101, onboarded=True)
        await env.cog.on_member_join(done)
        waiting = env.newcomer(102, onboarded=False)
        await env.cog.on_member_join(waiting)  # still in Onboarding: not yet
        assert set(await env.arrivals()) == {101}
        after = SimpleNamespace(**{**vars(waiting), "flags": SimpleNamespace(completed_onboarding=True)})
        await env.cog.on_member_update(waiting, after)
        await env.cog.on_member_update(after, after)  # any later update: nothing new
        rows = await env.arrivals()
        assert set(rows) == {101, 102} and all(r["fresh"] == 1 and r["pinged_at"] is None for r in rows.values())
        env.guild.onboarding_on = False
        await env.cog.on_member_join(env.newcomer(103, onboarded=False))
        assert 103 in await env.arrivals()
        await env.cog.on_member_join(env.newcomer(104, bot=True))
        assert 104 not in await env.arrivals()
    with_env(body, monkeypatch)


def test_arrival_uses_the_community_cogs_onboarding_answer(monkeypatch):
    async def body(env):
        asked = []

        async def enabled(guild):
            asked.append(guild)
            return False
        env.bot.cogs["Community"] = SimpleNamespace(onboarding_enabled=enabled)
        await env.cog.on_member_join(env.newcomer(101, onboarded=False))
        assert asked == [env.guild] and 101 in await env.arrivals()
    with_env(body, monkeypatch)


def test_join_and_onboarding_events_record_one_arrival(monkeypatch):
    async def body(env):
        m = env.newcomer(101)
        assert await env.cog.arrived(m)
        env.t += 120
        assert not await env.cog.arrived(m)
        assert (await env.arrivals())[101]["at"] == env.t - 120
    with_env(body, monkeypatch)


def test_rejoin_within_30_days_is_not_fresh(monkeypatch):
    async def body(env):
        t = env.t
        # 101 left 3 days ago (the growth cog's join log); 102 left long ago; 103 only has this join.
        await env.db.execute("INSERT INTO joins (user_id, joined_at, left_at) VALUES (101, ?, ?)",
                             (t - 60 * DAY, t - 3 * DAY))
        await env.db.execute("INSERT INTO joins (user_id, joined_at, left_at) VALUES (102, ?, ?)",
                             (t - 200 * DAY, t - 90 * DAY))
        for uid in (101, 102, 103):
            await env.db.execute("INSERT INTO joins (user_id, joined_at) VALUES (?, ?)", (uid, t))
            await env.cog.arrived(env.newcomer(uid))
        rows = await env.arrivals()
        assert (rows[101]["fresh"], rows[102]["fresh"], rows[103]["fresh"]) == (0, 1, 1)
        # 103 leaves and comes back two days later: our own earlier arrival makes it a rejoin.
        env.t += 2 * DAY
        await env.cog.arrived(env.guild.get_member(103))
        assert (await env.arrivals())[103]["fresh"] == 0
    with_env(body, monkeypatch)


def test_opted_out_newcomer_is_not_recorded(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (101, 1)")
        assert not await env.cog.arrived(env.newcomer(101))
        assert await env.arrivals() == {}
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- the crew ping
def test_one_ping_batches_arrivals_and_pings_the_role_only(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        for uid in (101, 102):
            await env.cog.arrived(env.newcomer(uid))
        assert await env.cog.flush()
        sent = env.guild.general.sent
        assert len(sent) == 1
        assert sent[0]["content"] == f"👋 {env.guild.crew.mention} <@101> and <@102> are new here. Come say hi!"
        role_ping_only(sent[0], env.guild.crew)
        assert not await env.cog.flush()  # nothing waiting any more
        assert len(sent) == 1 and all(r["pinged_at"] == env.t for r in (await env.arrivals()).values())
    with_env(body, monkeypatch)


def test_at_most_one_ping_per_gap(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        await env.cog.arrived(env.newcomer(101))
        assert await env.cog.flush()
        env.t += 5 * 60
        await env.cog.arrived(env.newcomer(102))
        env.t += 60
        await env.cog.arrived(env.newcomer(103))
        assert not await env.cog.flush()
        env.t += 23 * 60  # 29 minutes after the first ping
        assert not await env.cog.flush()
        env.t += 60
        assert await env.cog.flush()
        sent = env.guild.general.sent
        assert len(sent) == 2 and "<@102> and <@103>" in sent[1]["content"]
        assert await env.cog.meta(LAST_PING_KEY) == str(env.t)
    with_env(body, monkeypatch)


def test_gap_and_quiet_hours_come_from_the_environment(monkeypatch):
    async def body(env):
        monkeypatch.setenv("CREW_PING_MINUTES", "10")
        monkeypatch.setenv("CREW_QUIET_HOURS", "12-14")
        env.member(20, crew=True)
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()  # 12:00 is quiet now
        env.t = ts(2026, 10, 7, 14, 0)
        assert await env.cog.flush()
        env.t += 9 * 60
        await env.cog.arrived(env.newcomer(102))
        assert not await env.cog.flush()
        env.t += 60
        assert await env.cog.flush()
    with_env(body, monkeypatch)


def test_no_ping_in_quiet_hours_then_one_in_the_morning(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        env.t = ts(2026, 10, 7, 23, 30)
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()
        env.t = ts(2026, 10, 8, 8, 59)
        assert not await env.cog.flush()
        env.t = ts(2026, 10, 8, 9, 0)
        assert await env.cog.flush()
        assert "<@101> just joined" in env.guild.general.sent[0]["content"]
    with_env(body, monkeypatch)


def test_arrivals_nobody_was_pinged_for_go_stale(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        env.t = ts(2026, 10, 7, 20, 30)
        gone = env.newcomer(102)
        await env.cog.arrived(env.newcomer(101))
        await env.cog.arrived(gone)
        env.guild.members.remove(gone)  # left before anyone could be pinged
        env.t = ts(2026, 10, 7, 22, 30)
        assert not await env.cog.flush()
        assert (await env.arrivals())[102]["skipped"] == 1
        env.t = ts(2026, 10, 8, 9, 0)  # 12.5 hours later: too late to say "just joined"
        assert not await env.cog.flush()
        assert env.guild.general.sent == [] and (await env.arrivals())[101]["skipped"] == 1
    with_env(body, monkeypatch)


def test_no_ping_while_no_crew_member_is_online(monkeypatch):
    async def body(env):
        crew = env.member(20, crew=True, status=discord.Status.offline)
        env.member(21, status=discord.Status.online)  # online, but not on the crew
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()
        crew.status = discord.Status.idle
        env.t += 60
        assert await env.cog.flush()
    with_env(body, monkeypatch)


def test_without_the_presence_intent_the_online_check_is_skipped(monkeypatch):
    async def body(env):
        env.bot.intents = discord.Intents.default()
        env.member(20, crew=True, status=discord.Status.offline)
        await env.cog.arrived(env.newcomer(101))
        assert await env.cog.flush()
    with_env(body, monkeypatch)


def test_no_ping_during_a_moderation_lockdown(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        state = M.lockdown(env.t - 60, 1, None)
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (RAID_KEY, state.dumps()))
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()
        assert env.guild.general.sent == [] and (await env.arrivals())[101]["skipped"] == 1
        # Afterwards the next arrival is pinged for; the raid joiners never are.
        env.t = state.until
        await env.cog.arrived(env.newcomer(102))
        assert await env.cog.flush()
        assert "<@102> just joined" in env.guild.general.sent[0]["content"]
    with_env(body, monkeypatch)


def test_a_join_burst_counts_as_a_raid_here_too(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        for uid in range(101, 101 + M.RAID_WEIGHT):  # the moderation cog's own threshold
            await env.cog.on_member_join(env.newcomer(uid))
            env.t += 1
        assert await env.cog.raid_active()
        assert not await env.cog.flush()
        assert env.guild.general.sent == []
        assert all(r["skipped"] == 1 for r in (await env.arrivals()).values())
        env.t += M.RAID_LOCK
        assert not await env.cog.raid_active()
    with_env(body, monkeypatch)


def test_without_the_role_or_the_channel_nothing_is_posted(monkeypatch):
    async def body(env):
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()
        assert env.guild.general.sent == []
    with_env(body, monkeypatch, with_role=False)

    async def no_channel(env):
        env.member(20, crew=True)
        env.guild.text_channels = [env.guild.mod_log]
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()
        assert (await env.arrivals())[101]["pinged_at"] is None  # still waiting
    with_env(no_channel, monkeypatch)


def test_a_failed_post_is_not_retried_every_minute(monkeypatch):
    async def body(env):
        env.member(20, crew=True)
        env.guild.general.fail = http_error()
        await env.cog.arrived(env.newcomer(101))
        assert not await env.cog.flush()
        env.guild.general.fail = None
        env.t += 60
        assert not await env.cog.flush()
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- greetings
def test_reply_to_a_crew_member_pays_once(monkeypatch):
    async def body(env):
        crew, new = env.member(20, crew=True), env.newcomer(101)
        await env.cog.arrived(new)
        await env.exchange(crew, new)
        assert await env.greetings() == {101: (20, C.COINS)}
        assert await env.balance(20) == C.COINS and await env.balance(101) == 0
        row = await env.db.fetchone("SELECT reason, ref FROM ledger")
        assert (row["reason"], row["ref"]) == ("crew", "crew:greet:101")
        await env.exchange(crew, new)  # more chatting: nothing more
        other = env.member(21, crew=True)
        await env.exchange(other, new)  # the first crew member won
        assert await env.greetings() == {101: (20, C.COINS)}
        assert await env.balance(20) == C.COINS and await env.balance(21) == 0
    with_env(body, monkeypatch)


def test_the_crew_member_talking_at_the_newcomer_is_not_enough(monkeypatch):
    async def body(env):
        crew, new = env.member(20, crew=True), env.newcomer(101)
        await env.cog.arrived(new)
        for _ in range(5):
            await env.cog.on_message(env.say(crew, mentions=[new]))
            await env.cog.on_message(env.say(crew, reply_to=env.say(new)))
        await env.cog.on_message(env.say(new))  # the newcomer talks, but to nobody
        await env.cog.on_message(env.say(new, mentions=[env.member(30)]))  # or to a non-crew member
        assert await env.greetings() == {}
    with_env(body, monkeypatch)


def test_mention_counts_only_after_the_crew_member_said_hi(monkeypatch):
    async def body(env):
        crew, new = env.member(20, crew=True), env.newcomer(101)
        await env.cog.arrived(new)
        await env.cog.on_message(env.say(new, mentions=[crew]))
        assert await env.greetings() == {}
        await env.cog.on_message(env.say(crew, mentions=[new]))
        await env.cog.on_message(env.say(new, mentions=[crew]))
        assert await env.greetings() == {101: (20, C.COINS)}
    with_env(body, monkeypatch)


def test_reply_to_an_old_or_deleted_message_does_not_count(monkeypatch):
    async def body(env):
        crew, new = env.member(20, crew=True), env.newcomer(101)
        old = env.say(crew)  # sent before the newcomer arrived
        env.t += 600
        await env.cog.arrived(new)
        await env.cog.on_message(env.say(new, reply_to=old))
        await env.cog.on_message(env.say(new, reply_to=SimpleNamespace(id=5)))  # deleted: no author
        assert await env.greetings() == {}
    with_env(body, monkeypatch)


def test_window_closes_after_48_hours(monkeypatch):
    async def body(env):
        crew, new = env.member(20, crew=True), env.newcomer(101)
        await env.cog.arrived(new)
        env.t += C.WINDOW - 30
        await env.exchange(crew, new)  # the reply lands 30 s too late
        assert await env.greetings() == {}
    with_env(body, monkeypatch)


def test_rejoins_and_fresh_alts_are_never_rewarded(monkeypatch):
    async def body(env):
        crew = env.member(20, crew=True)
        back = env.newcomer(101)
        await env.db.execute("INSERT INTO joins (user_id, joined_at, left_at) VALUES (101, ?, ?)",
                             (env.t - 40 * DAY, env.t - DAY))
        await env.cog.arrived(back)
        await env.exchange(crew, back)
        alt = env.newcomer(young(env.t))
        await env.cog.arrived(alt)
        await env.exchange(crew, alt)
        assert await env.greetings() == {} and await env.balance(20) == 0
    with_env(body, monkeypatch)


def test_a_hand_assigned_crew_member_with_a_new_account_earns_nothing(monkeypatch):
    async def body(env):
        crew, new = env.member(young(env.t), crew=True), env.newcomer(101)
        await env.cog.arrived(new)
        await env.exchange(crew, new)
        assert await env.greetings() == {}
    with_env(body, monkeypatch)


def test_daily_cap_per_crew_member(monkeypatch):
    async def body(env):
        crew = env.member(20, crew=True)
        for uid in range(101, 101 + C.DAILY_REWARDS + 1):
            new = env.newcomer(uid)
            await env.cog.arrived(new)
            await env.exchange(crew, new)
        got = await env.greetings()
        assert len(got) == C.DAILY_REWARDS + 1  # every greeting counts...
        assert sorted(c for _, c in got.values()) == [0] + [C.COINS] * C.DAILY_REWARDS  # ...the pay is capped
        assert await env.balance(20) == C.COINS * C.DAILY_REWARDS
        env.t += DAY
        new = env.newcomer(200)
        await env.cog.arrived(new)
        await env.exchange(crew, new)
        assert await env.balance(20) == C.COINS * (C.DAILY_REWARDS + 1)
    with_env(body, monkeypatch)


def test_opt_out_on_either_side_records_nothing(monkeypatch):
    async def body(env):
        crew, quiet_crew = env.member(20, crew=True), env.member(21, crew=True)
        a, b = env.newcomer(101), env.newcomer(102)
        await env.cog.arrived(a)
        await env.cog.arrived(b)
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (21, 1)")
        await env.exchange(quiet_crew, a)
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (102, 1)")  # after arriving
        await env.exchange(crew, b)
        assert await env.greetings() == {} and await env.db.fetchall("SELECT 1 FROM ledger") == []
        await env.exchange(crew, a)  # the newcomer is still open for someone who is tracked
        assert await env.greetings() == {101: (20, C.COINS)}
    with_env(body, monkeypatch)


def test_newcomer_list_is_rebuilt_after_a_restart(monkeypatch):
    async def body(env):
        crew, new = env.member(20, crew=True), env.newcomer(101)
        await env.cog.arrived(new)
        hello = env.say(crew, mentions=[new])
        env.cog = Crew(env.bot)  # a restart: memory is gone, the table is not
        env.t += 60
        await env.cog.on_message(env.say(new, reply_to=hello))
        assert await env.greetings() == {101: (20, C.COINS)}
    with_env(body, monkeypatch)


def test_fifth_greeting_grants_the_badge_once(monkeypatch):
    async def body(env):
        crew = env.member(20, crew=True)
        for i in range(C.BADGE_AT + 1):
            new = env.newcomer(101 + i)
            await env.cog.arrived(new)
            await env.exchange(crew, new)
            env.t += DAY
            held = await env.db.fetchall("SELECT key FROM achievements WHERE user_id = 20")
            assert [r["key"] for r in held] == ([C.BADGE_KEY] if i >= C.BADGE_AT - 1 else [])
        posts = [s for s in env.guild.general.sent if "Welcoming Committee" in (s["content"] or "")]
        assert len(posts) == 1 and "<@20>" in posts[0]["content"]
        am = posts[0]["allowed_mentions"]
        assert am.roles is False and [u.id for u in am.users] == [20]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- /crew and the buttons
def test_crew_command_shows_roster_counts_and_hides_opted_out(monkeypatch):
    async def body(env):
        a, b, c = env.member(20, crew=True), env.member(21, crew=True), env.member(22, crew=True)
        env.member(23, crew=True, bot=True)
        for uid, crew_id in ((101, 20), (102, 20), (103, 21), (104, 22)):
            await env.db.execute("INSERT INTO crew_greetings (newcomer_id, crew_id, at, coins) VALUES (?, ?, ?, 0)",
                                 (uid, crew_id, env.t))
        await env.db.execute("INSERT INTO crew_greetings (newcomer_id, crew_id, at, coins) VALUES (99, 21, ?, 0)",
                             (ts(2026, 9, 30, 23, 0),))  # last month
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (22, 1)")
        i = env.click(a)
        await Crew.crew.callback(env.cog, i)
        sent = i.response.sent[0]
        embed = sent["embed"]
        assert embed.description.splitlines() == ["<@20> · 2 greetings this month", "<@21> · 1 greeting this month",
                                                  "<@22>"]
        assert "3 ON THE CREW" in embed.footer.text and "Join the crew" in embed.fields[0].value
        am = sent["allowed_mentions"]
        assert am.everyone is False and am.roles is False and am.users is False
        assert sorted(item.custom_id for item in sent["view"].children) == ["crew:join", "crew:leave"]
        assert b and c
    with_env(body, monkeypatch)


def test_crew_command_says_when_it_is_not_set_up(monkeypatch):
    async def body(env):
        i = env.click(env.member(20))
        await Crew.crew.callback(env.cog, i)
        assert i.response.sent == [dict(content=C.NOT_SET_UP, ephemeral=True)]
        j = env.click(env.member(21))
        await env.cog.handle_button(j, "join")
        assert j.response.sent[0]["content"] == C.NOT_SET_UP
    with_env(body, monkeypatch, with_role=False)


def test_join_button_checks_the_bar(monkeypatch):
    async def body(env):
        async def press(member, action="join"):
            i = env.click(member)
            await env.cog.handle_button(i, action)
            assert i.response.sent[0]["ephemeral"] is True
            return i.response.sent[0]["content"]

        new = env.member(30, joined_at=at(env.t - 13 * DAY))
        assert "Come back in 1 day." in await press(new)
        alt = env.member(young(env.t))
        assert "30 days old" in await press(alt)
        warned = env.member(31)
        await env.db.execute("INSERT INTO cases (user_id, mod_id, kind, reason, at) VALUES (31, 1, 'warn', 'x', ?)",
                             (env.t - 29 * DAY,))
        assert "clean record" in await press(warned)
        assert new.added == alt.added == warned.added == []
        ok = env.member(32)
        # Old cases and cases that aren't against them don't block.
        await env.db.execute("INSERT INTO cases (user_id, mod_id, kind, reason, at) VALUES (32, 1, 'warn', 'x', ?)",
                             (env.t - 31 * DAY,))
        await env.db.execute("INSERT INTO cases (user_id, mod_id, kind, reason, at)"
                             " VALUES (32, 1, 'untimeout', 'x', ?)", (env.t,))
        assert "Welcome to the crew" in await press(ok)
        assert ok.added == [env.guild.crew]
        assert "already on the crew" in await press(ok)
        assert "left the crew" in await press(ok, "leave") and ok.removed == [env.guild.crew]
        assert "not on the crew" in await press(ok, "leave")
    with_env(body, monkeypatch)


def test_join_button_refuses_a_role_it_must_not_hand_out(monkeypatch):
    async def body(env):
        m = env.member(30)
        for change in (dict(position=50), dict(managed=True),
                       dict(permissions=discord.Permissions(moderate_members=True))):
            saved = {k: getattr(env.guild.crew, k) for k in change}
            for k, v in change.items():
                setattr(env.guild.crew, k, v)
            i = env.click(m)
            await env.cog.handle_button(i, "join")
            assert "can't hand out" in i.response.sent[0]["content"], change
            for k, v in saved.items():
                setattr(env.guild.crew, k, v)
        m.fail = http_error()
        i = env.click(m)
        await env.cog.handle_button(i, "join")
        assert "couldn't change that role" in i.response.sent[0]["content"]
        assert m.added == []
    with_env(body, monkeypatch)


def test_buttons_are_persistent_dynamic_items():
    async def body():
        view = build_view()
        assert view.timeout is None
        assert [type(i) for i in view.children] == [CrewButton, CrewButton]
        item = await CrewButton.from_custom_id(None, None, {"action": "leave"})
        assert item.action == "leave" and item.custom_id == "crew:leave"
    run(body())


# ---------------------------------------------------------------- upkeep
def test_upkeep_removes_crew_with_a_new_case_and_logs_it(monkeypatch):
    async def body(env):
        clean, warned = env.member(20, crew=True), env.member(21, crew=True)
        await env.db.execute("INSERT INTO cases (user_id, mod_id, kind, reason, at) VALUES (21, 1, 'spam', 'x', ?)",
                             (env.t - DAY,))
        await env.cog.upkeep()
        assert warned.removed == [env.guild.crew] and clean.removed == []
        log = env.guild.mod_log.sent
        assert len(log) == 1 and "<@21>" in log[0]["embed"].description
        assert log[0]["allowed_mentions"].users is False
        env.t += 60
        await env.cog.upkeep()  # at most hourly
        assert len(log) == 1
    with_env(body, monkeypatch)


def test_upkeep_deletes_rows_of_members_who_opted_out(monkeypatch):
    async def body(env):
        crew = env.member(20, crew=True)
        for uid in (101, 102):
            new = env.newcomer(uid)
            await env.cog.arrived(new)
            await env.exchange(crew, new)
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (102, 1)")
        await env.cog.upkeep()
        assert set(await env.greetings()) == {101} and set(await env.arrivals()) == {101}
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (20, 1)")
        env.t += C.HOUR
        await env.cog.upkeep()
        assert await env.greetings() == {}
        assert await env.balance(20) == 2 * C.COINS  # coins already paid are not clawed back
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- staff numbers
def test_stats_and_digest_line(monkeypatch):
    async def body(env):
        crew = env.member(20, crew=True)
        env.member(21, crew=True)
        start = env.t
        a, b, c = env.newcomer(101), env.newcomer(102), env.newcomer(103)
        await env.cog.arrived(a)
        await env.cog.arrived(b)
        await env.cog.flush()
        env.t += 3600
        await env.cog.arrived(c)
        await env.cog.flush()
        await env.exchange(crew, a)
        stats = await env.cog.stats(start, env.t + 1)
        assert stats == C.Stats(arrivals=3, pinged=3, pings=2, greeted=1, coins=C.COINS, top=((20, 1),))
        assert await env.cog.digest_line(start, env.t + 1) == "3 newcomers, 1 greeted by the crew (33%) · crew of 2"
        assert (await env.cog.stats(start - 100, start)).arrivals == 0
    with_env(body, monkeypatch)


def test_digest_line_is_none_without_the_role(monkeypatch):
    async def body(env):
        assert await env.cog.digest_line(0, env.t) is None
    with_env(body, monkeypatch, with_role=False)


def test_crew_stats_is_staff_only_and_hidden_by_default(monkeypatch):
    perms = Crew.crew_stats.default_permissions
    assert perms is not None and perms.value == discord.Permissions(moderate_members=True).value
    assert Crew.crew.default_permissions is None

    async def body(env):
        member = env.member(30)
        i = env.click(member)
        await Crew.crew_stats.callback(env.cog, i)
        assert i.response.sent == [dict(content=cogmod.STAFF_ONLY, ephemeral=True)]
        mod = env.member(31, roles=[env.guild.mod])
        j = env.click(mod)
        await Crew.crew_stats.callback(env.cog, j)
        sent = j.response.sent[0]
        assert sent["ephemeral"] is True and [f.name for f in sent["embed"].fields] == ["Last 7 days", "Last 30 days"]
    with_env(body, monkeypatch)
