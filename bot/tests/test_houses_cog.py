"""Offline tests for cogs.houses: a real in-memory SQLite database plus small fakes for the
bot, guild, roles, channels, members and interactions. No network, no real time."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
import main
from cogs import houses as cogmod
from cogs.houses import Houses
from logic import houses as H
from logic import recap as R

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, MOD, BOTUSER = 1, 2, 50
U1, U2, U3, U4, U5, U6 = 11, 12, 13, 14, 15, 16
HOUR = 3600
DAY = 24 * HOUR
FERN, KESTREL, OTTER = "fern", "kestrel", "otter"


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


T = ts(2026, 10, 7, 15, 0)  # Wednesday of 2026-W41
WEEK = H.week_of(T, TZ)
CLOSE = ts(2026, 10, 12, 10, 11)  # the Monday after: W41 is judged


def run(coro):
    return asyncio.run(coro)


def young_id(at: int, n: int = 0) -> int:
    """A Discord id for an account created 5 days before `at` (under MIN_ACCOUNT_DAYS)."""
    return discord.utils.time_snowflake(datetime.fromtimestamp(at - 5 * DAY, timezone.utc)) + n


class FakeRole:
    def __init__(self, name, position, **perms):
        self.id = 5000 + position
        self.name = name
        self.position = position
        self.managed = False
        self.permissions = SimpleNamespace(**perms)


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
    def __init__(self, uid, guild, bot=False, roles=(), admin=False):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.mention = f"<@{uid}>"
        self.roles = [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.role_calls = []

    async def add_roles(self, *roles, reason=None):
        self.role_calls.append(("add", [r.name for r in roles]))
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.role_calls.append(("remove", [r.name for r in roles]))
        self.roles = [r for r in self.roles if r not in roles]

    def houses(self):
        return sorted(r.name for r in self.roles if r.name.startswith("House "))


class FakeGuild:
    def __init__(self, house_roles=True):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.roles = [FakeRole("Moderator", 8), FakeRole("Front Desk", 6)]
        if house_roles:
            self.roles += [FakeRole(h.role, i + 1) for i, h in enumerate(H.HOUSES)]
        self.me = SimpleNamespace(top_role=self.role("Front Desk"))
        self.announcements = FakeText(config.ANNOUNCEMENTS_CHANNEL)
        self.text_channels = [FakeText(config.GENERAL_CHANNEL), self.announcements]
        self.afk_channel = None
        self.members = []

    def role(self, name):
        return next(r for r in self.roles if r.name == name)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls

    async def send_message(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


class FakeInteraction:
    def __init__(self, bot, user):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.response = FakeResponse(self.calls)

    @property
    def reply(self):
        assert len(self.calls) == 1
        return self.calls[0]


def embed_text(embed) -> str:
    return "\n".join([embed.title or "", embed.description or "", *(f"{f.name}\n{f.value}" for f in embed.fields)])


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T
        self.pauses = []

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, **kw)
            self.guild.members.append(m)
        return m

    def inter(self, uid, **kw):
        return FakeInteraction(self.bot, self.member(uid, **kw))

    async def house(self, uid, house, left_at=None):
        self.member(uid)
        await self.db.execute("INSERT OR REPLACE INTO house_members (user_id, house, joined_at, left_at)"
                              " VALUES (?, ?, ?, ?)", (uid, house, T - DAY, left_at))

    async def chat(self, uid, days, week=WEEK):
        """`days` active days in `week`: 3 house points each."""
        for i in range(days):
            day = date.fromordinal(week.first_day.toordinal() + i).isoformat()
            await self.db.execute("INSERT OR REPLACE INTO message_counts (user_id, day, count) VALUES (?, ?, ?)",
                                  (uid, day, H.ACTIVE_DAY_MESSAGES))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def rows(self):
        return {r["user_id"]: (r["house"], r["left_at"], r["source"])
                for r in await self.db.fetchall("SELECT * FROM house_members")}

    async def result(self, kind, period):
        return await self.db.fetchone("SELECT * FROM house_results WHERE kind = ? AND period = ?", (kind, period))

    async def jobs(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def start_then(self, t):
        """The first tick only records what's already past; then move the clock."""
        await self.cog.run_jobs()
        self.t = t


def with_env(fn, monkeypatch, house_roles=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(house_roles)
        bot = FakeBot(db, guild)
        cog = Houses(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)

        async def pause(seconds):  # no real waiting: just note what the sweep asked for
            env.pauses.append(seconds)
        monkeypatch.setattr(cogmod, "pause", pause)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- wiring
def test_module_is_registered_with_its_commands():
    assert ("cogs.houses", {"members"}) in main.MODULES
    assert {c.name for c in Houses.house.commands} == {"me", "standings", "join", "leave", "history"}
    assert [c.name for c in Houses.admin.commands] == ["move"]
    assert Houses.admin.default_permissions.value == discord.Permissions(moderate_members=True).value
    assert Houses.house.default_permissions is None
    assert sorted(c.value for c in cogmod.HOUSE_CHOICES) == sorted(H.BY_KEY)


# ---------------------------------------------------------------- the placement sweep
def test_sweep_places_established_humans_into_balanced_houses_with_roles(monkeypatch):
    async def go(env):
        for uid in (U1, U2, U3, U4, U5, U6):
            env.member(uid)
        env.member(BOTUSER, bot=True)
        young = env.member(young_id(T))
        assert await env.cog.run_sweep() == 6
        rows = await env.rows()
        assert set(rows) == {U1, U2, U3, U4, U5, U6}  # bots and fresh accounts never get a house
        assert sorted(H.sizes({u: h for u, (h, _, _) in rows.items()}).values()) == [2, 2, 2]
        assert all(source == "auto" and left is None for _, left, source in rows.values())
        for uid, (house, _, _) in rows.items():
            assert env.member(uid).houses() == [H.BY_KEY[house].role]
        assert young.houses() == [] and env.member(BOTUSER).houses() == []
        # Nothing changes on the next sweep, and no role call is repeated.
        calls = sum(len(m.role_calls) for m in env.guild.members)
        assert await env.cog.run_sweep() == 0
        assert await env.rows() == rows and sum(len(m.role_calls) for m in env.guild.members) == calls
    with_env(go, monkeypatch)


def test_sweep_skips_opted_out_members_and_those_who_left(monkeypatch):
    async def go(env):
        env.member(U1)
        env.member(U2)
        await env.optout(U1)
        await env.house(U3, FERN, left_at=T - DAY)
        env.member(U3).roles.append(env.guild.role("House Fern"))  # a stale role from before they left
        assert await env.cog.run_sweep() == 1
        rows = await env.rows()
        assert set(rows) == {U2, U3} and rows[U3][1] == T - DAY  # still left, not re-placed
        assert env.member(U3).houses() == []  # and the stale role is taken away
    with_env(go, monkeypatch)


def test_sweep_fixes_a_wrong_house_role(monkeypatch):
    async def go(env):
        await env.house(U1, KESTREL)
        env.member(U1).roles += [env.guild.role("House Fern"), env.guild.role("Moderator")]
        await env.cog.run_sweep()
        assert env.member(U1).houses() == ["House Kestrel"]
        assert env.guild.role("Moderator") in env.member(U1).roles  # other roles are never touched
    with_env(go, monkeypatch)


def test_everything_works_before_the_roles_exist(monkeypatch):
    async def go(env):
        for uid in (U1, U2, U3):
            env.member(uid)
        assert await env.cog.run_sweep() == 3
        assert all(m.role_calls == [] for m in env.guild.members)
        i = env.inter(U4)
        await Houses.join.callback(env.cog, i)
        assert "Welcome to" in i.reply["content"] and U4 in await env.rows()
    with_env(go, monkeypatch, house_roles=False)


def test_roles_above_the_bot_or_with_mod_powers_are_never_given(monkeypatch):
    async def go(env):
        env.guild.role("House Fern").position = 7  # above Front Desk
        env.guild.role("House Kestrel").permissions = SimpleNamespace(manage_messages=True)
        await env.house(U1, FERN)
        await env.house(U2, KESTREL)
        await env.house(U3, OTTER)
        await env.cog.run_sweep()
        assert env.member(U1).houses() == [] and env.member(U2).houses() == []
        assert env.member(U3).houses() == ["House Otter"]
    with_env(go, monkeypatch)


def test_first_sweep_spaces_out_the_role_changes(monkeypatch):
    async def go(env):
        people = [env.member(100 + n) for n in range(15)]
        assert await env.cog.run_sweep() == 15
        assert all(len(m.role_calls) == 1 for m in people)  # one role change each, never a retry
        assert env.pauses == [cogmod.ROLE_PAUSE] * 15 and cogmod.ROLE_PAUSE >= 1
        assert await env.cog.run_sweep() == 0 and len(env.pauses) == 15  # nothing to change: no waiting
    with_env(go, monkeypatch)


def intro_posts(env):
    return [p for p in env.guild.announcements.sent if p["embed"].title == "Houses are here"]


def test_first_placement_posts_the_introduction_once_without_pings(monkeypatch):
    async def go(env):
        for uid in (U1, U2, U3):
            env.member(uid)
        assert await env.cog.run_sweep() == 3
        post, = intro_posts(env)
        text = post["embed"].description
        assert post["content"] is None and post["allowed_mentions"].to_dict() == {"parse": []}
        assert all(h.title in text for h in H.HOUSES)
        assert "`/house me`" in text and "`/house leave`" in text and "points per member" in text
        assert "<@" not in text and "@everyone" not in text and "@here" not in text
        # later sweeps, later arrivals and a restart never repeat it
        env.member(U4)
        assert await env.cog.run_sweep() == 1 and await env.cog.run_sweep() == 0
        assert await Houses(env.bot).run_sweep() == 0
        assert len(intro_posts(env)) == 1
        row = await env.db.fetchone("SELECT value FROM meta WHERE key = ?", (cogmod.INTRO_KEY,))
        assert row["value"] == str(T)
    with_env(go, monkeypatch)


def test_no_introduction_when_someone_already_had_a_house(monkeypatch):
    async def go(env):
        await env.house(U1, FERN, left_at=T - DAY)  # even a former member: houses are not new here
        env.member(U2)
        assert await env.cog.run_sweep() == 1
        assert intro_posts(env) == []
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cogmod.INTRO_KEY,)) is None
    with_env(go, monkeypatch)


def test_nothing_to_introduce_when_nobody_was_placed(monkeypatch):
    async def go(env):
        env.member(young_id(T))
        assert await env.cog.run_sweep() == 0 and intro_posts(env) == []
        env.member(U1)
        assert await env.cog.run_sweep() == 1 and len(intro_posts(env)) == 1  # the first real placement
    with_env(go, monkeypatch)


def test_introduction_is_skipped_quietly_without_an_announcements_channel(monkeypatch, caplog):
    async def go(env):
        env.guild.text_channels = [c for c in env.guild.text_channels if c is not env.guild.announcements]
        env.member(U1)
        assert await env.cog.run_sweep() == 1
        env.guild.text_channels.append(env.guild.announcements)  # made later: still not posted
        env.member(U2)
        assert await env.cog.run_sweep() == 1
        assert intro_posts(env) == [] and "introduction" not in caplog.text
    with_env(go, monkeypatch)


def test_introduction_retries_after_a_refused_post_and_still_goes_out_once(monkeypatch):
    async def go(env):
        env.member(U1)
        env.guild.announcements.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        assert await env.cog.run_sweep() == 1 and intro_posts(env) == []
        assert env.member(U1).houses() != []  # the roles don't wait for the post
        env.guild.announcements.fail = None
        assert await env.cog.run_sweep() == 0 and len(intro_posts(env)) == 1
        assert await env.cog.run_sweep() == 0 and len(intro_posts(env)) == 1
    with_env(go, monkeypatch)


def test_sweep_does_nothing_without_a_member_cache(monkeypatch):
    async def go(env):
        assert await env.cog.run_sweep() == 0  # no members cached
        env.member(U1)
        env.guild.chunked = False  # a partial cache can't tell who is here
        assert await env.cog.run_sweep() == 0 and await env.rows() == {}
        env.bot.guild = None
        assert await env.cog.run_sweep() == 0
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /house join and leave
def test_join_places_in_the_smallest_house_and_cannot_pick(monkeypatch):
    async def go(env):
        await env.house(U1, FERN)
        await env.house(U2, FERN)
        await env.house(U3, OTTER)
        i = env.inter(U4)
        await Houses.join.callback(env.cog, i)
        assert i.reply["ephemeral"] and "Welcome to 🪶 House Kestrel" in i.reply["content"]
        assert (await env.rows())[U4] == (KESTREL, None, "join")
        assert env.member(U4).houses() == ["House Kestrel"]
        again = env.inter(U4)
        await Houses.join.callback(env.cog, again)
        assert "already in 🪶 House Kestrel" in again.reply["content"]
        assert not [p for p in Houses.join.parameters]  # no option to choose a house
    with_env(go, monkeypatch)


def test_join_ignores_members_who_are_gone_when_sizing_houses(monkeypatch):
    async def go(env):
        # Fern looks full on paper, but both of its members have left the server.
        for uid in (U1, U2):
            await env.house(uid, FERN)
        await env.house(U3, KESTREL)
        await env.house(U4, OTTER)
        env.guild.members = [m for m in env.guild.members if m.id not in (U1, U2)]
        i = env.inter(U5)
        await Houses.join.callback(env.cog, i)
        assert (await env.rows())[U5][0] == FERN
    with_env(go, monkeypatch)


def test_fresh_accounts_cannot_join(monkeypatch):
    async def go(env):
        i = env.inter(young_id(T))
        await Houses.join.callback(env.cog, i)
        assert i.reply["content"] == cogmod.TOO_NEW and await env.rows() == {}
    with_env(go, monkeypatch)


def test_opted_out_member_may_join_but_is_told_they_do_not_score(monkeypatch):
    async def go(env):
        await env.optout(U1)
        i = env.inter(U1)
        await Houses.join.callback(env.cog, i)
        assert "Your stats are off" in i.reply["content"] and U1 in await env.rows()
    with_env(go, monkeypatch)


def test_leaving_starts_a_cooldown_so_nobody_hops_to_the_winning_house(monkeypatch):
    async def go(env):
        await env.house(U1, FERN)
        env.member(U1).roles.append(env.guild.role("House Fern"))
        i = env.inter(U1)
        await Houses.leave.callback(env.cog, i)
        assert f"<t:{T + H.REJOIN_COOLDOWN}:R>" in i.reply["content"] and i.reply["ephemeral"]
        assert (await env.rows())[U1][:2] == (FERN, T) and env.member(U1).houses() == []
        assert await H.house_of(env.db, U1) is None
        twice = env.inter(U1)
        await Houses.leave.callback(env.cog, twice)
        assert twice.reply["content"] == "You're not in a house."

        env.t = T + H.REJOIN_COOLDOWN - 60
        early = env.inter(U1)
        await Houses.join.callback(env.cog, early)
        assert "You can join again" in early.reply["content"] and (await env.rows())[U1][1] == T
        assert await env.cog.run_sweep() == 0 and (await env.rows())[U1][1] == T  # the sweep doesn't undo it

        env.t = T + H.REJOIN_COOLDOWN
        back = env.inter(U1)
        await Houses.join.callback(env.cog, back)
        assert "Welcome to" in back.reply["content"]
        house, left, source = (await env.rows())[U1]
        assert left is None and source == "join" and env.member(U1).houses() == [H.BY_KEY[house].role]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /house-admin move
def test_only_staff_can_move_members(monkeypatch):
    async def go(env):
        await env.house(U1, FERN)
        choice = SimpleNamespace(value=OTTER)
        nope = env.inter(U2)
        await Houses.move.callback(env.cog, nope, env.member(U1), choice)
        assert nope.reply["content"] == cogmod.STAFF_ONLY and (await env.rows())[U1][0] == FERN

        mod = env.inter(MOD, roles=("Moderator",))
        await Houses.move.callback(env.cog, mod, env.member(U1), choice)
        assert "is now in 🦦 House Otter" in mod.reply["content"] and mod.reply["ephemeral"]
        assert mod.reply["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert (await env.rows())[U1] == (OTTER, None, "staff") and env.member(U1).houses() == ["House Otter"]

        # The owner can place someone who has no house (or left one), cooldown or not.
        await env.house(U3, FERN, left_at=T - 5)
        owner = env.inter(OWNER)
        await Houses.move.callback(env.cog, owner, env.member(U3), SimpleNamespace(value=KESTREL))
        assert (await env.rows())[U3] == (KESTREL, None, "staff")

        bot = env.inter(OWNER)
        await Houses.move.callback(env.cog, bot, env.member(BOTUSER, bot=True), choice)
        assert bot.reply["content"] == "Bots don't get a house." and BOTUSER not in await env.rows()
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /house me, standings, history
def test_me_shows_house_standings_and_own_contribution_privately(monkeypatch):
    async def go(env):
        await env.house(U1, KESTREL)
        await env.house(U2, FERN)
        await env.chat(U1, 3)
        await env.chat(U2, 1)
        i = env.inter(U1)
        await Houses.me.callback(env.cog, i)
        embed = i.reply["embed"]
        text = embed_text(embed)
        assert i.reply["ephemeral"] and embed.title == f"🪶 House Kestrel · {WEEK.key}"
        assert embed.color.value == H.BY_KEY[KESTREL].color
        assert "🥇 🪶 **Kestrel** · **9.0** per member" in text and "🥈 🌿 **Fern** · **3.0** per member" in text
        assert "**9** · 3 days chatted" in text and "Total: **9** of 100 points" in text
        assert "Season · Q4 2026" in text and f"<t:{WEEK.end}:R>" in text

        nobody = env.inter(U3)
        await Houses.me.callback(env.cog, nobody)
        assert nobody.reply["content"] == cogmod.NO_HOUSE and nobody.reply["ephemeral"]
    with_env(go, monkeypatch)


def test_opted_out_member_brings_nothing_and_is_told_why(monkeypatch):
    async def go(env):
        await env.house(U1, KESTREL)
        await env.chat(U1, 5)
        await env.optout(U1)
        i = env.inter(U1)
        await Houses.me.callback(env.cog, i)
        text = embed_text(i.reply["embed"])
        assert "Your stats are off" in text and "days chatted" not in text
        assert "**Kestrel** · **0.0** per member · 0 of 0 members" in text
    with_env(go, monkeypatch)


def test_standings_are_public_ping_free_and_name_no_member(monkeypatch):
    async def go(env):
        await env.house(U1, KESTREL)
        await env.house(U2, FERN)
        await env.chat(U1, 3)
        i = env.inter(U3)
        await Houses.standings.callback(env.cog, i)
        assert not i.reply.get("ephemeral") and i.reply["content"] is None
        assert i.reply["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        text = embed_text(i.reply["embed"])
        assert "<@" not in text and "@everyone" not in text and "@here" not in text
        assert "🥇 🪶 **Kestrel**" in text and "How it works" in text
    with_env(go, monkeypatch)


def test_members_who_left_the_server_are_not_in_a_roster(monkeypatch):
    async def go(env):
        await env.house(U1, FERN)
        await env.house(U2, FERN)
        await env.chat(U2, 7)
        env.guild.members = [m for m in env.guild.members if m.id != U2]
        _, current, _ = await env.cog.board(env.guild, env.t)
        fern = next(s for s in current.standings if s.house == FERN)
        assert (fern.members, fern.total) == (1, 0)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- weekly close
async def two_houses(env):
    """Fern: U1 and U2 with 12 points each. Kestrel: U3 with 9. Otter: U4, idle."""
    for uid, house in ((U1, FERN), (U2, FERN), (U3, KESTREL), (U4, OTTER)):
        await env.house(uid, house)
    await env.chat(U1, 4)
    await env.chat(U2, 4)
    await env.chat(U3, 3)


def test_first_tick_only_marks_past_periods_done(monkeypatch):
    async def go(env):
        await two_houses(env)
        await env.cog.run_jobs()
        assert await env.jobs() == {"housecup:2026-W41", "houseseason:2026-09"}
        assert env.guild.announcements.sent == [] and await env.result("week", "2026-W40") is None
    with_env(go, monkeypatch)


def test_monday_close_freezes_pays_the_winning_contributors_and_posts_once(monkeypatch):
    async def go(env):
        await two_houses(env)
        await env.start_then(ts(2026, 10, 12, 10, 9))
        await env.cog.run_jobs()
        assert env.guild.announcements.sent == []  # not 10:10 yet (the recap goes first, at 10:00)
        env.t = CLOSE
        await env.cog.run_jobs()
        row = await env.result("week", WEEK.key)
        standings, paid = H.load_week(row["data"])
        assert (row["winner"], row["season"], row["closed_at"]) == (FERN, "2026-Q4", CLOSE) and paid == [U1, U2]
        assert [(s.house, s.total, s.members) for s in standings] == [(FERN, 24, 2), (KESTREL, 9, 1), (OTTER, 0, 1)]
        assert [await env.balance(u) for u in (U1, U2, U3, U4)] == [H.WIN_REWARD, H.WIN_REWARD, 0, 0]
        refs = {r["ref"] for r in await env.db.fetchall("SELECT ref FROM ledger WHERE reason = ?", (H.REASON,))}
        assert refs == {f"housecup:{WEEK.key}:{U1}", f"housecup:{WEEK.key}:{U2}"}
        (post,) = env.guild.announcements.sent
        assert post["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict() and post["content"] is None
        text = embed_text(post["embed"])
        assert "🌿 House Fern takes the House Cup for Oct 5 – Oct 11" in text and f"<@{U1}> <@{U2}>" in text
        assert post["embed"].color.value == H.BY_KEY[FERN].color
        assert "housecup:2026-W42" in await env.jobs()
        # Later ticks, and a rerun of the same period, change nothing.
        await env.cog.run_jobs()
        await env.cog.close_week(SimpleNamespace(scheduled_at=CLOSE, key="housecup:2026-W42"))
        assert await env.balance(U1) == H.WIN_REWARD and len(env.guild.announcements.sent) == 2
        assert len(await env.db.fetchall("SELECT 1 FROM ledger WHERE reason = ?", (H.REASON,))) == 2
    with_env(go, monkeypatch)


def test_failed_post_retries_with_the_frozen_result_and_never_pays_twice(monkeypatch):
    async def go(env):
        await two_houses(env)
        await env.start_then(CLOSE)
        env.guild.announcements.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        await env.cog.run_jobs()
        assert "housecup:2026-W42" not in await env.jobs() and await env.balance(U1) == H.WIN_REWARD
        # Before the retry the table changes: U3 hops to Fern and U1 leaves. The frozen result stands.
        await env.house(U3, FERN)
        await env.house(U1, FERN, left_at=CLOSE)
        env.guild.announcements.fail = None
        env.t = CLOSE + 60
        await env.cog.run_jobs()
        assert "housecup:2026-W42" in await env.jobs() and len(env.guild.announcements.sent) == 1
        assert [await env.balance(u) for u in (U1, U2, U3)] == [H.WIN_REWARD, H.WIN_REWARD, 0]
        assert H.load_week((await env.result("week", WEEK.key))["data"])[1] == [U1, U2]
    with_env(go, monkeypatch)


def test_points_count_for_the_house_a_member_is_in_when_the_week_closes(monkeypatch):
    async def go(env):
        await two_houses(env)
        await env.start_then(CLOSE)
        # Staff moved U3 (9 points, earned while in Kestrel) to Otter before the close.
        mod = env.inter(MOD, roles=("Moderator",))
        await Houses.move.callback(env.cog, mod, env.member(U3), SimpleNamespace(value=OTTER))
        await env.cog.run_jobs()
        standings, _ = H.load_week((await env.result("week", WEEK.key))["data"])
        totals = {s.house: s.total for s in standings}
        assert totals == {FERN: 24, KESTREL: 0, OTTER: 9}
    with_env(go, monkeypatch)


def test_a_house_carried_by_one_member_wins_but_nobody_is_paid(monkeypatch):
    async def go(env):
        await env.house(U1, FERN)
        await env.house(U2, FERN)
        await env.house(U3, KESTREL)
        await env.chat(U1, 7)
        await env.chat(U2, 1)  # 3 points: under MIN_PAID_POINTS
        await env.start_then(CLOSE)
        await env.cog.run_jobs()
        assert (await env.result("week", WEEK.key))["winner"] == FERN
        assert await env.balance(U1) == 0 and await env.balance(U2) == 0
        assert "No coins this week" in embed_text(env.guild.announcements.sent[0]["embed"])
    with_env(go, monkeypatch)


def test_quiet_week_records_no_winner_and_posts_nothing(monkeypatch):
    async def go(env):
        await env.house(U1, FERN)
        await env.start_then(CLOSE)
        await env.cog.run_jobs()
        row = await env.result("week", WEEK.key)
        assert row is not None and row["winner"] is None and env.guild.announcements.sent == []
        assert "housecup:2026-W42" in await env.jobs()
    with_env(go, monkeypatch)


def test_close_without_an_announcements_channel_still_pays_and_finishes(monkeypatch):
    async def go(env):
        await two_houses(env)
        env.guild.text_channels = env.guild.text_channels[:1]
        await env.start_then(CLOSE)
        await env.cog.run_jobs()
        assert await env.balance(U1) == H.WIN_REWARD and "housecup:2026-W42" in await env.jobs()
    with_env(go, monkeypatch)


def test_close_waits_for_the_guild(monkeypatch):
    async def go(env):
        await two_houses(env)
        await env.start_then(CLOSE)
        env.bot.guild = None
        await env.cog.run_jobs()
        assert "housecup:2026-W42" not in await env.jobs() and await env.result("week", WEEK.key) is None
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- season close and history
def test_season_close_records_the_trophy_and_history_shows_the_house_cup(monkeypatch):
    async def go(env):
        await two_houses(env)
        await env.start_then(CLOSE)
        await env.cog.run_jobs()  # Fern wins W41
        env.t = ts(2026, 11, 1, 10, 21)
        await env.cog.run_jobs()  # a month that doesn't end a quarter: done, nothing judged
        assert "houseseason:2026-10" in await env.jobs() and await env.result("season", "2026-Q4") is None
        sent = len(env.guild.announcements.sent)

        env.t = ts(2027, 1, 1, 10, 21)  # Friday
        await env.cog.run_jobs()
        row = await env.result("season", "2026-Q4")
        tally = H.load_season(row["data"])
        assert row["winner"] == FERN and (tally[0].house, tally[0].cup, tally[0].wins) == (FERN, 3, 1)
        assert "houseseason:2026-12" in await env.jobs()
        trophy = env.guild.announcements.sent[sent:]
        assert len(trophy) == 1 and "House Fern wins the Q4 2026 season trophy" in embed_text(trophy[0]["embed"])
        assert trophy[0]["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        # The new season starts at zero.
        assert all(t.cup == 0 for t in await H.season_standings(env.db, "2027-Q1"))
        await env.cog.run_jobs()
        assert len(env.guild.announcements.sent) == sent + 1  # announced once

        i = env.inter(U3)
        await Houses.history.callback(env.cog, i)
        text = embed_text(i.reply["embed"])
        assert "🏆 **House Cup · Q4 2026**: 🌿 House Fern" in text and f"`{WEEK.key}` 🌿 Fern" in text
        assert "<@" not in text and i.reply["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    with_env(go, monkeypatch)


def test_season_close_waits_until_the_last_weekly_close_is_settled(monkeypatch):
    async def go(env):
        t = ts(2029, 1, 1, 10, 21)  # a quarter that starts on a Monday
        env.t = t
        period = R.latest_due_monthly(H.SEASON_JOB, t, TZ)
        assert period.month == "2028-12"
        assert await env.cog.close_season(period) is False  # this morning's weekly close hasn't run
        assert await env.result("season", "2028-Q4") is None
        await env.db.execute("INSERT INTO jobs (key, done_at) VALUES ('housecup:2029-W01', ?)", (t,))
        assert await env.cog.close_season(period) is True
        row = await env.result("season", "2028-Q4")
        assert row is not None and row["winner"] is None and env.guild.announcements.sent == []  # no weeks, no trophy
    with_env(go, monkeypatch)


def test_history_before_any_cup(monkeypatch):
    async def go(env):
        i = env.inter(U1)
        await Houses.history.callback(env.cog, i)
        assert "No House Cup has been decided yet" in i.reply["embed"].description
    with_env(go, monkeypatch)
