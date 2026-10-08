"""Tournaments: staff run single-elimination brackets in 🏆・tournaments.

/tournament create posts a signup card with Join / Leave buttons; /tournament start seeds
the entrants at random, posts the bracket (edited as results come in) and one message per
playable match with "P1 won" / "P2 won" buttons. A player's report is final once the other
player presses the same button, or when staff press one; a disagreement flags staff in
📋・mod-log. The champion gets the Tournament Champ role and 1,000 coins, the runner-up 400.

Everything lives in SQLite (tournaments, tournament_entries, tournament_matches, plus meta
keys for the Discord message ids), buttons are DynamicItems, and `sync` re-posts whatever a
restart interrupted, so a tournament survives the bot going down mid-bracket.

The bot also runs tournaments by itself (a one-minute tick, logic/schedule.py semantics):
- Monthly auto tournament: the first Monday of each month at 12:00 local, if no tournament is
  in signups or running, it opens a 16-player one for the most played server game of the last
  30 days (else a by-month rotation), starting the following Saturday 19:00, with a Discord
  scheduled event. The meta key tourney:<id>:auto marks it (value: the event id, or "").
- At the start time an auto tournament starts itself with at least 4 entrants; otherwise it is
  cancelled with a note and its event is cancelled.
- Every tournament with a start time pings its entrants once in the hour before it.
- A match with no report 24 h after it opened pings both players once; after 48 h a match
  still undecided (nobody reported, or one player's report the other never confirmed) is
  flagged to staff in mod-log (once). A lone report never decides a match by itself.
- Discord accounts younger than logic/quests.py MIN_ACCOUNT_DAYS can't sign up (alt farming).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from errors import reply_error
from logic import engagement as EG
from logic import events as E
from logic import stats as S
from logic import tournaments as T
from logic.quests import MIN_ACCOUNT_DAYS
from logic.tournaments import Bracket, Match, Report, Stale

log = logging.getLogger(__name__)

SIGNUP, RUNNING, FINISHED, CANCELLED = "signup", "running", "done", "cancelled"
ACTIVE = (SIGNUP, RUNNING)

SIZE_CHOICES = [app_commands.Choice(name=f"{n} players", value=n) for n in T.SIZES]

WHEN_PROBLEMS = {
    "unparsed": "I couldn't read that start time. Try `sat 8pm`, `tomorrow 7:30pm` or `2026-10-10 20:00`.",
    "past": "That start time has already passed.",
    "too_far": "That's too far ahead. Pick a time in the next few weeks.",
}

REPORT_REPLIES = {
    Report.NOT_PLAYER: "Only the two players (or a Keeper or Moderator) can report this match.",
    Report.NOT_OPEN: "This match isn't ready yet: both players have to be known first.",
    Report.DONE: "This match is already decided.",
    Report.DISPUTED: "You two disagree on this one, so a Keeper or Moderator will decide it.",
}


def now() -> int:
    return int(time.time())


def ping_only(users=()) -> discord.AllowedMentions:
    """Allow exactly these user pings (names and the tournament name are user text)."""
    return discord.AllowedMentions(everyone=False, roles=False, users=list(users) or False)


def esc(text) -> str:
    return discord.utils.escape_markdown(str(text or ""))


def is_staff(member) -> bool:
    """Owner, administrators, Keepers and Moderators."""
    guild = getattr(member, "guild", None)
    if guild is not None and getattr(guild, "owner_id", None) == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True
    roles = list(getattr(member, "roles", []))
    return any(config.match_by_name(roles, n) is not None for n in (config.KEEPER_ROLE, config.MOD_ROLE))


# meta keys for the Discord side (the migration has no columns for these)
def bracket_key(tid: int) -> str:
    return f"tourney:{tid}:bracket"


def match_key(match_id: int) -> str:
    return f"tourney:match:{match_id}"


def announce_key(tid: int) -> str:
    return f"tourney:{tid}:announce"


def auto_key(tid: int) -> str:
    """Present for auto tournaments; the value is the scheduled event id ("" if none)."""
    return f"tourney:{tid}:auto"


def reminded_key(tid: int) -> str:
    return f"tourney:{tid}:reminded"


def autocancel_key(tid: int) -> str:
    """Owed: the "not enough players" note and the event cancel of an auto tournament."""
    return f"tourney:{tid}:autocancel"


def opened_key(match_id: int) -> str:
    return f"tourney:match:{match_id}:opened"


def nudged_key(match_id: int) -> str:
    return f"tourney:match:{match_id}:nudged"


def flagged_key(match_id: int) -> str:
    return f"tourney:match:{match_id}:flagged"


EVENT_HOURS = 4  # how long the scheduled event says it runs
EVENT_LOCATION = f"#{config.TOURNAMENTS_CHANNEL}"


def created_ts(user) -> int | None:
    created = getattr(user, "created_at", None)
    return int(created.timestamp()) if created is not None else None


def pack(channel_id: int, message_id: int) -> str:
    return f"{channel_id}:{message_id}"


def unpack(value: str) -> tuple[int, int]:
    channel_id, message_id = value.split(":")
    return int(channel_id), int(message_id)


# ---------------------------------------------------------------- buttons
class SignupButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"tourney:(?P<action>join|leave):(?P<tid>\d+)"):
    def __init__(self, action: str, tid: int, disabled: bool = False):
        label, button_style = ("Join", discord.ButtonStyle.success) if action == "join" else \
            ("Leave", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=button_style, disabled=disabled,
                                           custom_id=f"tourney:{action}:{tid}"))
        self.action = action
        self.tid = tid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Tournaments = interaction.client.get_cog("Tournaments")
        try:
            await cog.handle_signup(interaction, self.action, self.tid)
        except Exception:
            log.exception("tournament %s button on %s failed", self.action, self.tid)
            await reply_error(interaction)


class MatchButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"tourney:report:(?P<match>\d+):(?P<pick>[12])"):
    def __init__(self, match_id: int, pick: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label=f"P{pick} won", style=discord.ButtonStyle.primary,
                                           disabled=disabled, custom_id=f"tourney:report:{match_id}:{pick}"))
        self.match_id = match_id
        self.pick = pick

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["match"]), int(match["pick"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Tournaments = interaction.client.get_cog("Tournaments")
        try:
            await cog.handle_report(interaction, self.match_id, self.pick)
        except Exception:
            log.exception("tournament report on match %s failed", self.match_id)
            await reply_error(interaction)


def signup_view(tid: int, full: bool, closed: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SignupButton("join", tid, disabled=closed or full))
    view.add_item(SignupButton("leave", tid, disabled=closed))
    return view


def match_view(match_id: int, closed: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MatchButton(match_id, 1, disabled=closed))
    view.add_item(MatchButton(match_id, 2, disabled=closed))
    return view


# ---------------------------------------------------------------- text
def card_embed(t, entrants: list[int]) -> discord.Embed:
    lines = []
    if t["game"]:
        lines.append(f"**Game** {esc(t['game'])}")
    if t["starts_at"]:
        lines.append(f"**Starts** <t:{t['starts_at']}:F> (<t:{t['starts_at']}:R>)")
    lines.append(f"**Format** single elimination, up to {t['size']} players")
    lines.append(f"**Prizes** {T.PRIZE_FIRST:,} coins and the {esc(config.TOURNEY_ROLE)} role for the winner, "
                 f"{T.PRIZE_SECOND:,} coins for the runner-up")
    lines += ["", f"**Entrants {len(entrants)}/{t['size']}**"]
    lines += [f"`{i:02}`  <@{uid}>" for i, uid in enumerate(entrants, start=1)] or ["Nobody yet. Press **Join**."]
    status = {SIGNUP: "full" if len(entrants) >= t["size"] else "signups open",
              RUNNING: "started", FINISHED: "finished", CANCELLED: "cancelled"}[t["status"]]
    if t["status"] != SIGNUP:
        lines += ["", {RUNNING: "Signups are closed: the bracket is below.",
                       FINISHED: "This tournament is over.",
                       CANCELLED: "This tournament was cancelled."}[t["status"]]]
    return style.embed(title=f"🏆 {esc(t['name'])}", description="\n".join(lines),
                       footer=style.label("tournament", f"#{t['id']}", status),
                       color=style.FOREST if t["status"] in ACTIVE else style.MUTED)


def bracket_embed(t, bracket: Bracket, name_of) -> discord.Embed:
    status = {RUNNING: "live", FINISHED: "finished", CANCELLED: "cancelled"}.get(t["status"], t["status"])
    return style.embed(title=f"🏆 {esc(t['name'])} · bracket", description=T.render_bracket(bracket, name_of),
                       footer=style.label("tournament", f"#{t['id']}", status),
                       color=style.FOREST if t["status"] == RUNNING else style.MUTED)


def match_text(t, m: Match, total_rounds: int) -> str:
    head = f"**{esc(t['name'])}** · {T.round_name(m.round, total_rounds)} · match {m.slot + 1}"
    players = f"P1 <@{m.p1}>  vs  P2 <@{m.p2}>"
    other = m.p2 if m.reported_by == m.p1 else m.p1
    state = {
        T.OPEN: "Play your match, then press who won. The other player confirms with the same button; "
                "a Keeper or Moderator can decide any match.",
        T.REPORTED: f"<@{m.reported_by}> says <@{m.winner}> won. <@{other}>, press the same button to confirm.",
        T.CONFLICT: "The players disagree, so a Keeper or Moderator will decide this one.",
        T.DONE: f"✅ <@{m.winner}> won.",
    }.get(m.status, "")
    return f"{head}\n{players}\n{state}"


def match_from_row(r) -> Match:
    return Match(round=r["round"], slot=r["slot"], p1=r["p1"], p2=r["p2"], winner=r["winner"],
                 reported_by=r["reported_by"], status=r["status"], id=r["id"])


# ---------------------------------------------------------------- the cog
class Tournaments(commands.Cog):
    tournament = app_commands.Group(name="tournament", description="Brackets with prizes: run by staff",
                                    guild_only=True)

    def __init__(self, bot, rng: random.Random | None = None):
        self.bot = bot
        self.rng = rng or random.Random()
        self.lock = asyncio.Lock()  # one sync at a time, so a match message is never posted twice

    @property
    def db(self):
        return self.bot.db

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(SignupButton, MatchButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(SignupButton, MatchButton)
        self.tick.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Finish anything a restart interrupted: missing match messages, the champion post."""
        try:
            running = await self.db.fetchall("SELECT id FROM tournaments WHERE status = ?", (RUNNING,))
            owed = await self.db.fetchall("SELECT key FROM meta WHERE key LIKE 'tourney:%:announce'")
        except Exception:
            log.exception("tournament resume failed")
            return
        ids = {r["id"] for r in running} | {int(r["key"].split(":")[1]) for r in owed}
        for tid in sorted(ids):
            try:
                await self.sync(tid)
            except Exception:
                log.exception("couldn't resume tournament %s", tid)

    # ------------------------------------------------------------ helpers
    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    def channel(self, guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild is not None else None

    def name_of(self, guild):
        def name(uid):
            member = guild.get_member(uid) if guild is not None else None
            return getattr(member, "display_name", None)
        return name

    @property
    def tz(self):
        return self.bot.settings.tz

    async def get(self, tid: int, tx=None):
        return await (tx or self.db).fetchone("SELECT * FROM tournaments WHERE id = ?", (tid,))

    async def entrants(self, tid: int, tx=None) -> list[int]:
        rows = await (tx or self.db).fetchall(
            "SELECT user_id FROM tournament_entries WHERE tournament_id = ? ORDER BY joined_at, rowid", (tid,))
        return [r["user_id"] for r in rows]

    async def bracket(self, tid: int, tx=None) -> Bracket:
        rows = await (tx or self.db).fetchall(
            "SELECT * FROM tournament_matches WHERE tournament_id = ? ORDER BY round, slot", (tid,))
        return Bracket(match_from_row(r) for r in rows)

    async def meta(self, key: str, tx=None) -> str | None:
        row = await (tx or self.db).fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    async def partial(self, guild, value: str | None):
        """The PartialMessage stored in a meta value, or None."""
        if value is None or guild is None:
            return None
        channel_id, message_id = unpack(value)
        channel = guild.get_channel(channel_id)
        return channel.get_partial_message(message_id) if channel is not None else None

    async def edit_card(self, t) -> None:
        if t["channel_id"] is None or t["message_id"] is None:
            return
        try:
            message = await self.partial(self.guild(), pack(t["channel_id"], t["message_id"]))
            if message is None:
                return
            entrants = await self.entrants(t["id"])
            await message.edit(embed=card_embed(t, entrants),
                               view=signup_view(t["id"], len(entrants) >= t["size"], t["status"] != SIGNUP))
        except discord.HTTPException:
            log.warning("couldn't update the signup card of tournament %s", t["id"], exc_info=True)

    async def resolve(self, interaction, tournament: int | None, statuses) -> object | None:
        """The tournament a staff command is about, or None after telling the user why."""
        if tournament is not None:
            t = await self.get(tournament)
            if t is None:
                await interaction.response.send_message(f"There's no tournament #{tournament}.", ephemeral=True)
                return None
        else:
            marks = ",".join("?" * len(statuses))
            rows = await self.db.fetchall(f"SELECT * FROM tournaments WHERE status IN ({marks}) ORDER BY id",
                                          tuple(statuses))
            if len(rows) != 1:
                text = ("There's no tournament in that state." if not rows else
                        "More than one tournament fits: pick one with the `tournament` option.")
                await interaction.response.send_message(text, ephemeral=True)
                return None
            t = rows[0]
        if t["status"] not in statuses:
            await interaction.response.send_message(
                f"Tournament #{t['id']} is {t['status']}, so that doesn't apply.", ephemeral=True)
            return None
        return t

    async def tournament_choices(self, interaction, current: str):
        rows = await self.db.fetchall("SELECT id, name, status FROM tournaments ORDER BY id DESC LIMIT 50")
        text = current.lower().strip()
        out = []
        for r in rows:
            label = f"#{r['id']} {r['name']} ({r['status']})"[:100]
            if not text or text in label.lower():
                out.append(app_commands.Choice(name=label, value=r["id"]))
        return out[:25]

    # ------------------------------------------------------------ /tournament create
    @tournament.command(name="create", description="Staff: open signups for a bracket tournament")
    @app_commands.describe(name="Tournament name", game="Which game", size="Most players who can sign up",
                           starts_at="When it starts, e.g. sat 8pm (optional)")
    @app_commands.choices(size=SIZE_CHOICES)
    async def create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 60],
                     game: app_commands.Range[str, 1, 40], size: app_commands.Choice[int],
                     starts_at: app_commands.Range[str, 1, 40] | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only Keepers and Moderators can run tournaments.",
                                                    ephemeral=True)
            return
        size_value = size.value if isinstance(size, app_commands.Choice) else int(size)
        start_ts = None
        if starts_at:
            local_now = datetime.fromtimestamp(now(), self.tz)
            when = E.parse_when(starts_at, local_now, self.tz)
            problem = "unparsed" if when is None else E.check_when(when, local_now)
            if problem is not None:
                await interaction.response.send_message(WHEN_PROBLEMS.get(problem, WHEN_PROBLEMS["unparsed"]),
                                                        ephemeral=True)
                return
            start_ts = int(when.timestamp())
        channel = self.channel(self.guild(), config.TOURNAMENTS_CHANNEL)
        if channel is None:
            await interaction.response.send_message(
                f"I couldn't find the {config.TOURNAMENTS_CHANNEL} channel.", ephemeral=True)
            return
        known = next((g for g in config.GAMES if config.slug(g.role) == config.slug(game)), None)
        game_text = known.role if known else " ".join(game.split())
        name_text = " ".join(name.split())
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.db.transaction() as tx:
            cur = await tx.execute(
                "INSERT INTO tournaments (name, game, size, status, created_by, created_at, starts_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name_text, game_text, size_value, SIGNUP, interaction.user.id, now(), start_ts))
            tid = cur.lastrowid
            t = await self.get(tid, tx)
        try:
            message = await channel.send(embed=card_embed(t, []), view=signup_view(tid, False, False),
                                         allowed_mentions=discord.AllowedMentions.none())
        except BaseException:
            await self.db.execute("DELETE FROM tournaments WHERE id = ?", (tid,))
            raise
        await self.db.execute("UPDATE tournaments SET channel_id = ?, message_id = ? WHERE id = ?",
                              (channel.id, message.id, tid))
        await interaction.followup.send(f"Tournament #{tid} is open for signups in {channel.mention}.",
                                        ephemeral=True)

    @create.autocomplete("game")
    async def game_choices(self, interaction, current: str):
        text = current.lower().strip()
        return [app_commands.Choice(name=g.role, value=g.role) for g in config.GAMES
                if text in g.role.lower()][:25]

    # ------------------------------------------------------------ signups
    async def handle_signup(self, interaction: discord.Interaction, action: str, tid: int) -> None:
        user = interaction.user
        reply = None
        async with self.db.transaction() as tx:
            t = await self.get(tid, tx)
            if t is None or t["status"] != SIGNUP:
                reply = "Signups for this tournament are closed."
            else:
                entrants = await self.entrants(tid, tx)
                if action == "join":
                    if user.id in entrants:
                        reply = "You're already signed up."
                    elif not T.can_join(created_ts(user), now()):
                        reply = (f"Your Discord account has to be at least {MIN_ACCOUNT_DAYS} days old "
                                 "to enter tournaments.")
                    elif len(entrants) >= t["size"]:
                        reply = "This tournament is full."
                    else:
                        await tx.execute("INSERT INTO tournament_entries (tournament_id, user_id, joined_at)"
                                         " VALUES (?, ?, ?)", (tid, user.id, now()))
                        entrants.append(user.id)
                else:
                    if user.id not in entrants:
                        reply = "You're not signed up."
                    else:
                        await tx.execute("DELETE FROM tournament_entries WHERE tournament_id = ? AND user_id = ?",
                                         (tid, user.id))
                        entrants.remove(user.id)
        if reply:
            await interaction.response.send_message(reply, ephemeral=True)
            return
        await interaction.response.edit_message(embed=card_embed(t, entrants),
                                                view=signup_view(tid, len(entrants) >= t["size"], False))

    # ------------------------------------------------------------ /tournament start
    @tournament.command(name="start", description="Staff: close signups, seed and post the bracket")
    @app_commands.describe(tournament="Which tournament (only needed if more than one is open)")
    async def start(self, interaction: discord.Interaction, tournament: int | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only Keepers and Moderators can start tournaments.",
                                                    ephemeral=True)
            return
        t = await self.resolve(interaction, tournament, (SIGNUP,))
        if t is None:
            return
        tid = t["id"]
        problem, t, entrants = await self.begin(tid)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.edit_card(t)
        await self.sync(tid)
        await self.event_action(tid, "start")
        await interaction.followup.send(
            f"Tournament #{tid} started with {len(entrants)} players. Round 1 is up.", ephemeral=True)

    async def begin(self, tid: int):
        """Close signups, seed and store the bracket in one transaction.
        Returns (problem or None, the tournament row, the entrants)."""
        problem = None
        async with self.db.transaction() as tx:
            t = await self.get(tid, tx)
            entrants = await self.entrants(tid, tx)
            if t is None or t["status"] != SIGNUP:
                problem = "That tournament already started."
            elif len(entrants) < 2:
                problem = "A tournament needs at least two entrants."
            else:
                seeded = T.seed(entrants, self.rng)
                bracket = Bracket.build(seeded)
                for i, uid in enumerate(seeded, start=1):
                    await tx.execute("UPDATE tournament_entries SET seed = ? WHERE tournament_id = ? AND user_id = ?",
                                     (i, tid, uid))
                for m in bracket.matches:
                    await tx.execute(
                        "INSERT INTO tournament_matches (tournament_id, round, slot, p1, p2, winner, reported_by,"
                        " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (tid, m.round, m.slot, m.p1, m.p2, m.winner, m.reported_by, m.status))
                await tx.execute("UPDATE tournaments SET status = ? WHERE id = ?", (RUNNING, tid))
                t = await self.get(tid, tx)
        return problem, t, entrants

    # ------------------------------------------------------------ sync: Discord catches up with the DB
    async def sync(self, tid: int) -> None:
        """Post or edit the bracket, post any playable match without a message, and announce
        the champion if that's still owed. Safe to call any number of times."""
        async with self.lock:
            t = await self.get(tid)
            if t is None:
                return
            guild = self.guild()
            if guild is None:
                return
            bracket = await self.bracket(tid)
            if t["status"] in (RUNNING, FINISHED, CANCELLED) and bracket.matches:
                await self.update_bracket(t, guild, bracket)
            if t["status"] == RUNNING:
                for m in bracket.open_matches():
                    if await self.meta(match_key(m.id)) is None:
                        await self.post_match(t, guild, bracket, m)
            if await self.meta(announce_key(tid)) is not None:
                await self.finish(t, guild, bracket)

    async def update_bracket(self, t, guild, bracket: Bracket) -> None:
        embed = bracket_embed(t, bracket, self.name_of(guild))
        stored = await self.meta(bracket_key(t["id"]))
        try:
            message = await self.partial(guild, stored)
            if message is not None:
                try:
                    await message.edit(embed=embed)
                    return
                except discord.NotFound:
                    pass  # deleted: post a new one
            if t["status"] != RUNNING:
                return
            channel = self.channel(guild, config.TOURNAMENTS_CHANNEL)
            if channel is None:
                log.warning("tournament %s: no %s channel for the bracket", t["id"], config.TOURNAMENTS_CHANNEL)
                return
            sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await self.set_meta(bracket_key(t["id"]), pack(channel.id, sent.id))
        except discord.HTTPException:
            log.warning("couldn't post or edit the bracket of tournament %s", t["id"], exc_info=True)

    async def post_match(self, t, guild, bracket: Bracket, m: Match) -> None:
        channel = self.channel(guild, config.TOURNAMENTS_CHANNEL)
        if channel is None:
            return
        try:
            sent = await channel.send(match_text(t, m, bracket.final_round),
                                      view=match_view(m.id, closed=False),
                                      allowed_mentions=ping_only([discord.Object(m.p1), discord.Object(m.p2)]))
        except discord.HTTPException:
            log.warning("couldn't post match %s of tournament %s", m.id, t["id"], exc_info=True)
            return
        await self.set_meta(match_key(m.id), pack(channel.id, sent.id))
        await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (opened_key(m.id), str(now())))

    # ------------------------------------------------------------ match reports
    async def handle_report(self, interaction: discord.Interaction, match_id: int, pick: int) -> None:
        user = interaction.user
        staff = is_staff(user)
        reply = None
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM tournament_matches WHERE id = ?", (match_id,))
            t = await self.get(row["tournament_id"], tx) if row else None
            if row is None or t is None or t["status"] != RUNNING:
                reply = "This tournament is over."
            else:
                bracket = await self.bracket(t["id"], tx)
                current = next(m for m in bracket.matches if m.id == match_id)
                outcome, updated = T.report(current, user.id, pick, staff)
                reply = REPORT_REPLIES.get(outcome)
                if outcome.final:
                    await self.decide_tx(tx, t, bracket, current, updated.winner, updated.reported_by)
                elif outcome in (Report.RECORDED, Report.CONFLICT):
                    current = updated
                    await self.save_match(tx, updated)
        if reply:
            await interaction.response.send_message(reply, ephemeral=True)
            return
        await interaction.response.edit_message(content=match_text(t, current, bracket.final_round),
                                                view=match_view(match_id, closed=current.status == T.DONE),
                                                allowed_mentions=discord.AllowedMentions.none())
        if outcome is Report.CONFLICT:
            await self.flag_conflict(t, current, bracket, interaction.message)
        if outcome.final or outcome is Report.RECORDED or outcome is Report.CONFLICT:
            await self.sync(t["id"])

    async def decide_tx(self, tx, t, bracket: Bracket, current: Match, winner: int, reported_by) -> None:
        """Make `winner` the winner of `current`, move them on and close out after the final."""
        changed = bracket.decide(current.round, current.slot, winner)
        current.reported_by = reported_by
        for m in changed:
            await self.save_match(tx, m)
        if bracket.champion() is not None:
            await self.close_out_tx(tx, t, bracket)

    async def save_match(self, tx, m: Match) -> None:
        await tx.execute("UPDATE tournament_matches SET p1 = ?, p2 = ?, winner = ?, reported_by = ?, status = ?"
                         " WHERE id = ?", (m.p1, m.p2, m.winner, m.reported_by, m.status, m.id))

    async def close_out_tx(self, tx, t, bracket: Bracket) -> None:
        """The final is decided: record the champion, pay the prizes, owe the announcement.
        All in the deciding transaction, so the payout refs make a repeat impossible."""
        champion, runner_up = bracket.champion(), bracket.runner_up()
        await tx.execute("UPDATE tournaments SET status = ?, winner_id = ? WHERE id = ?",
                         (FINISHED, champion, t["id"]))
        for uid, coins, ref in T.payouts(t["id"], champion, runner_up):
            await economy.apply_tx(tx, uid, coins, "tournament", now(), ref=ref)
        await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (announce_key(t["id"]), str(champion)))

    async def flag_conflict(self, t, m: Match, bracket: Bracket, message) -> None:
        guild = self.guild()
        channel = self.channel(guild, config.MOD_LOG_CHANNEL)
        if channel is None:
            log.warning("tournament %s match %s is disputed (no %s channel)", t["id"], m.id, config.MOD_LOG_CHANNEL)
            return
        where = f" {message.jump_url}" if getattr(message, "jump_url", None) else ""
        text = (f"🏆 **{esc(t['name'])}** · {T.round_name(m.round, bracket.final_round)} match {m.slot + 1}: "
                f"<@{m.p1}> and <@{m.p2}> disagree on who won. A Keeper or Moderator, press "
                f"**P1 won** or **P2 won** on the match message to decide.{where}")
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("couldn't flag disputed match %s", m.id, exc_info=True)

    # ------------------------------------------------------------ the champion
    async def finish(self, t, guild, bracket: Bracket) -> None:
        """Role, announcement and event, once: the marker is claimed before anything is sent."""
        cur = await self.db.execute("DELETE FROM meta WHERE key = ?", (announce_key(t["id"]),))
        if not cur:
            return
        t = await self.get(t["id"])
        champion, runner_up = bracket.champion(), bracket.runner_up()
        if champion is None:
            return
        try:
            await self.swap_role(t, guild, champion)
        except Exception:
            log.exception("tournament %s: champion role swap failed", t["id"])
        channel = self.channel(guild, config.TOURNAMENTS_CHANNEL)
        if channel is not None:
            second = f" Runner-up <@{runner_up}> gets {T.PRIZE_SECOND:,} coins." if runner_up else ""
            text = (f"🏆 <@{champion}> won **{esc(t['name'])}** and takes {T.PRIZE_FIRST:,} coins and "
                    f"the {esc(config.TOURNEY_ROLE)} role!{second} GG everyone.")
            try:
                await channel.send(text, allowed_mentions=ping_only([discord.Object(champion)]))
            except discord.HTTPException:
                log.warning("couldn't announce the champion of tournament %s", t["id"], exc_info=True)
        self.bot.dispatch("tournament_won", t["id"], champion)
        await self.event_action(t["id"], "end")

    async def swap_role(self, t, guild, champion: int) -> None:
        role = config.match_by_name(guild.roles, config.TOURNEY_ROLE)
        me = getattr(guild, "me", None)
        if role is None or me is None or me.top_role.position <= role.position:
            problem = (f"There's no `{config.TOURNEY_ROLE}` role." if role is None else
                       f"Front Desk's role has to be above `{config.TOURNEY_ROLE}` to hand it out.")
            log.warning("tournament %s: %s", t["id"], problem)
            mod_log = self.channel(guild, config.MOD_LOG_CHANNEL)
            if mod_log is not None:
                try:
                    await mod_log.send(f"Tournament #{t['id']}: {problem} The champion was still announced and paid.",
                                       allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            return
        # Previous holders: whoever the cache shows with the role, plus past champions from the DB
        # (the cache is incomplete without the members intent).
        holders = {m.id for m in getattr(role, "members", [])}
        rows = await self.db.fetchall(
            "SELECT DISTINCT winner_id FROM tournaments WHERE status = ? AND id != ? AND winner_id IS NOT NULL",
            (FINISHED, t["id"]))
        holders |= {r["winner_id"] for r in rows}
        holders.discard(champion)
        reason = f"Tournament #{t['id']} champion"
        for uid in holders:
            member = await self.member(guild, uid)
            if member is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason=reason)
                except discord.HTTPException:
                    log.warning("couldn't remove %s from a previous champion", config.TOURNEY_ROLE, exc_info=True)
        member = await self.member(guild, champion)
        if member is not None and role not in member.roles:
            await member.add_roles(role, reason=reason)

    async def member(self, guild, uid: int):
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                return None
        return member

    # ------------------------------------------------------------ /tournament cancel
    @tournament.command(name="cancel", description="Staff: call off a tournament (no prizes are paid)")
    @app_commands.describe(tournament="Which tournament (only needed if more than one is open)")
    async def cancel(self, interaction: discord.Interaction, tournament: int | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only Keepers and Moderators can cancel tournaments.",
                                                    ephemeral=True)
            return
        t = await self.resolve(interaction, tournament, ACTIVE)
        if t is None:
            return
        tid = t["id"]
        async with self.db.transaction() as tx:
            cur = await tx.execute("UPDATE tournaments SET status = ? WHERE id = ? AND status IN (?, ?)",
                                   (CANCELLED, tid, *ACTIVE))
            changed = cur.rowcount
        if not changed:
            await interaction.response.send_message("That tournament already ended.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = await self.get(tid)
        await self.edit_card(t)
        guild = self.guild()
        bracket = await self.bracket(tid)
        for m in bracket.open_matches():
            try:
                message = await self.partial(guild, await self.meta(match_key(m.id)))
                if message is not None:
                    await message.edit(content=f"{match_text(t, m, bracket.final_round)}\nThis tournament was cancelled.",
                                       view=match_view(m.id, closed=True))
            except discord.HTTPException:
                log.warning("couldn't close match %s of cancelled tournament %s", m.id, tid, exc_info=True)
        await self.sync(tid)
        await self.event_action(tid, "cancel")
        await interaction.followup.send(f"Tournament #{tid} is cancelled. No prizes were paid.", ephemeral=True)

    # ------------------------------------------------------------ /tournament bracket
    @tournament.command(name="bracket", description="Show a tournament's bracket")
    @app_commands.describe(tournament="Which tournament (default: the one running now)")
    async def show_bracket(self, interaction: discord.Interaction, tournament: int | None = None) -> None:
        if tournament is not None:
            t = await self.get(tournament)
        else:
            t = await self.db.fetchone(
                "SELECT * FROM tournaments ORDER BY status = ? DESC, status = ? DESC, id DESC LIMIT 1",
                (RUNNING, SIGNUP))
        if t is None:
            await interaction.response.send_message("There's no tournament yet.", ephemeral=True)
            return
        if t["status"] == SIGNUP or not await self.db.fetchone(
                "SELECT 1 FROM tournament_matches WHERE tournament_id = ?", (t["id"],)):
            embed = card_embed(t, await self.entrants(t["id"]))
        else:
            embed = bracket_embed(t, await self.bracket(t["id"]), self.name_of(self.guild()))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ automation: the tick
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        jobs = (
            ("monthly auto tournament", self.run_auto_create),
            ("start reminders", self.send_reminders),
            ("auto start", self.auto_start_due),
            ("stale matches", self.sweep_matches),
        )
        for name, job in jobs:
            try:
                await job()
            except Exception:
                log.exception("tournaments: %s failed", name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def due(self, name: str, make_plan):
        """Record first_seen and skipped periods; return the period to run now, if any."""
        seen_key = f"first_seen:{name}"
        first = await self.meta(seen_key)
        first_seen = int(first) if first is not None else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{name}:%",))}
        t = now()
        todo = make_plan(t, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    # ------------------------------------------------------------ automation: the monthly tournament
    async def run_auto_create(self) -> None:
        job = T.AUTO_JOB
        period = await self.due(job.name, lambda t, done, seen: T.plan_monthly(job, t, self.tz, done, seen))
        # Marked done only after it worked: an error retries on the next tick.
        if period is not None and await self.auto_create(period):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))

    async def auto_create(self, period: T.MonthlyPeriod) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        active = await self.db.fetchone("SELECT id FROM tournaments WHERE status IN (?, ?) LIMIT 1", ACTIVE)
        if active is not None:
            log.info("auto tournament %s: #%s is already on", period.key, active["id"])
            return True
        monday = T.first_monday(period.year, period.month)
        starts = T.auto_starts_at(monday, self.tz)
        if T.too_late_to_create(now(), starts):
            log.info("auto tournament %s: too late to open signups", period.key)
            return True
        channel = self.channel(guild, config.TOURNAMENTS_CHANNEL)
        if channel is None:
            log.warning("auto tournament %s: no %s channel", period.key, config.TOURNAMENTS_CHANNEL)
            return True
        top = await self.top_game(period.window_start, period.window_end)
        game = T.auto_game(top, period.year, period.month, config.GAMES)
        name = T.auto_name(game, monday)
        async with self.db.transaction() as tx:
            cur = await tx.execute(
                "INSERT INTO tournaments (name, game, size, status, created_by, created_at, starts_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, game.role if game else None, T.AUTO_SIZE, SIGNUP, self.bot.user.id, now(), starts))
            tid = cur.lastrowid
            await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (auto_key(tid), ""))
            t = await self.get(tid, tx)
        try:
            message = await channel.send(embed=card_embed(t, []), view=signup_view(tid, False, False),
                                         allowed_mentions=discord.AllowedMentions.none())
        except BaseException as exc:
            async with self.db.transaction() as tx:
                await tx.execute("DELETE FROM tournaments WHERE id = ?", (tid,))
                await tx.execute("DELETE FROM meta WHERE key = ?", (auto_key(tid),))
            if not isinstance(exc, Exception):
                raise  # cancelled: don't swallow it
            log.warning("auto tournament %s: couldn't post the signup card; retrying", period.key, exc_info=True)
            return False
        await self.db.execute("UPDATE tournaments SET channel_id = ?, message_id = ? WHERE id = ?",
                              (channel.id, message.id, tid))
        await self.create_event(guild, t, channel)
        log.info("auto tournament %s: opened #%s (%s)", period.key, tid, game.key if game else "anything")
        return True

    async def create_event(self, guild, t, channel) -> None:
        when = datetime.fromtimestamp(t["starts_at"], timezone.utc)
        description = (f"The monthly community tournament: single elimination, up to {t['size']} players. "
                       f"Sign up with the Join button in #{config.TOURNAMENTS_CHANNEL}. "
                       f"The winner gets {T.PRIZE_FIRST:,} coins and the {config.TOURNEY_ROLE} role, "
                       f"the runner-up {T.PRIZE_SECOND:,} coins. It needs at least {T.AUTO_MIN_ENTRANTS} players.")
        try:
            event = await guild.create_scheduled_event(
                name=t["name"][:100],
                description=description,
                start_time=when,
                end_time=when + timedelta(hours=EVENT_HOURS),
                entity_type=discord.EntityType.external,
                location=EVENT_LOCATION,
                privacy_level=discord.PrivacyLevel.guild_only,
                reason="Monthly auto tournament",
            )
        except discord.HTTPException:
            # The signups are up either way; the event is a nice-to-have (needs Manage Events).
            log.warning("auto tournament #%s: Discord wouldn't create the event", t["id"], exc_info=True)
            return
        await self.set_meta(auto_key(t["id"]), str(event.id))

    async def top_game(self, start: int, end: int):
        rows = await self.db.fetchall(
            'SELECT user_id, game AS k, start, "end" FROM game_sessions WHERE start < ? AND ("end" IS NULL OR "end" > ?)',
            (end, start))
        per_user = S.game_seconds([S.Session(r["user_id"], r["k"], r["start"], r["end"]) for r in rows],
                                  start, end, now())
        totals: dict[str, int] = {}
        for games in per_user.values():
            for game, secs in games.items():
                totals[game] = totals.get(game, 0) + secs
        return EG.top_game(totals)

    async def event_action(self, tid: int, action: str) -> None:
        """Start, end or cancel an auto tournament's scheduled event. Best effort: never raises."""
        try:
            value = await self.meta(auto_key(tid))
            guild = self.guild()
            if not value or guild is None:
                return
            event = guild.get_scheduled_event(int(value))
            if event is None:
                try:
                    event = await guild.fetch_scheduled_event(int(value))
                except discord.NotFound:
                    return
            status = event.status
            if action == "start" and status == discord.EventStatus.scheduled:
                await event.start(reason="The tournament started")
            elif action == "end" and status == discord.EventStatus.active:
                await event.end(reason="The tournament is over")
            elif action == "cancel" and status == discord.EventStatus.scheduled:
                await event.cancel(reason="The tournament was called off")
            elif action == "cancel" and status == discord.EventStatus.active:
                await event.end(reason="The tournament was called off")
        except Exception:
            log.warning("tournament %s: couldn't %s its scheduled event", tid, action, exc_info=True)

    # ------------------------------------------------------------ automation: start time
    async def send_reminders(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM tournaments WHERE status = ? AND starts_at IS NOT NULL",
                                      (SIGNUP,))
        for t in rows:
            reminded = await self.meta(reminded_key(t["id"])) is not None
            if not T.reminder_due(t["starts_at"], t["created_at"], now(), reminded):
                continue
            try:
                await self.remind(t)
            except Exception:
                log.exception("tournament %s: reminder failed", t["id"])

    async def remind(self, t) -> None:
        tid = t["id"]
        entrants = await self.entrants(tid)
        channel = self.channel(self.guild(), config.TOURNAMENTS_CHANNEL)
        if entrants and channel is not None:
            auto = await self.meta(auto_key(tid)) is not None
            start = t["starts_at"]
            if auto and len(entrants) < T.AUTO_MIN_ENTRANTS:
                how = (f"It needs {T.AUTO_MIN_ENTRANTS} players to go ahead and has {len(entrants)}, "
                       "so bring a friend.")
            elif auto:
                how = "The bracket goes up by itself at the start time, so be around."
            else:
                how = "Staff post the bracket at the start time, so be around."
            pings = " ".join(f"<@{uid}>" for uid in entrants)
            text = f"⏰ **{esc(t['name'])}** starts <t:{start}:R> (<t:{start}:t>). {how}\n{pings}"
            try:
                await channel.send(text, allowed_mentions=ping_only([discord.Object(u) for u in entrants]))
            except discord.HTTPException:
                log.warning("tournament %s: couldn't post the reminder; retrying", tid, exc_info=True)
                return
        await self.set_meta(reminded_key(tid), str(now()))

    async def auto_start_due(self) -> None:
        rows = await self.db.fetchall(
            "SELECT * FROM tournaments WHERE status = ? AND starts_at IS NOT NULL AND starts_at <= ?", (SIGNUP, now()))
        for t in rows:
            tid = t["id"]
            if await self.meta(auto_key(tid)) is None:
                continue  # staff tournaments are started by staff
            try:
                if T.start_outcome(len(await self.entrants(tid))) == "start":
                    problem, t, entrants = await self.begin(tid)
                    if problem is None:
                        await self.edit_card(t)
                        await self.sync(tid)
                        await self.event_action(tid, "start")
                        log.info("auto tournament #%s started with %d players", tid, len(entrants))
                else:
                    async with self.db.transaction() as tx:
                        cur = await tx.execute("UPDATE tournaments SET status = ? WHERE id = ? AND status = ?",
                                               (CANCELLED, tid, SIGNUP))
                        if cur.rowcount:
                            await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                             (autocancel_key(tid), str(now())))
            except Exception:
                log.exception("auto tournament #%s: start failed", tid)
        owed = await self.db.fetchall("SELECT key FROM meta WHERE key LIKE 'tourney:%:autocancel'")
        for r in owed:
            tid = int(r["key"].split(":")[1])
            try:
                await self.settle_autocancel(tid)
            except Exception:
                log.exception("auto tournament #%s: cancel note failed", tid)

    async def settle_autocancel(self, tid: int) -> None:
        """The card, the friendly note and the event, then the marker goes (retried until then)."""
        t = await self.get(tid)
        if t is None:
            await self.db.execute("DELETE FROM meta WHERE key = ?", (autocancel_key(tid),))
            return
        await self.edit_card(t)
        channel = self.channel(self.guild(), config.TOURNAMENTS_CHANNEL)
        if channel is not None:
            n = len(await self.entrants(tid))
            signed = "nobody signed up" if n == 0 else f"{n} signed up"
            text = (f"🏆 **{esc(t['name'])}** needed {T.AUTO_MIN_ENTRANTS} players and {signed}, so it's off this "
                    "time. Thanks to everyone who joined! There's a new one next month, and staff can run one "
                    "any time.")
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("auto tournament #%s: couldn't post the cancel note; retrying", tid, exc_info=True)
                return
        await self.event_action(tid, "cancel")
        await self.db.execute("DELETE FROM meta WHERE key = ?", (autocancel_key(tid),))

    # ------------------------------------------------------------ automation: stale matches
    async def sweep_matches(self) -> None:
        running = await self.db.fetchall("SELECT * FROM tournaments WHERE status = ?", (RUNNING,))
        for t in running:
            bracket = await self.bracket(t["id"])
            for m in bracket.open_matches():
                opened = await self.meta(opened_key(m.id))
                if opened is None:
                    # Opened before this was tracked, or its message isn't up yet: start the clock now.
                    await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                                          (opened_key(m.id), str(now())))
                    continue
                nudged = await self.meta(nudged_key(m.id)) is not None
                flagged = await self.meta(flagged_key(m.id)) is not None
                action = T.stale_action(m.status, int(opened), now(), nudged, flagged)
                try:
                    if action is Stale.NUDGE:
                        await self.nudge(t, bracket, m)
                    elif action is Stale.FLAG:
                        await self.flag_stale(t, bracket, m)
                except Exception:
                    log.exception("tournament %s match %s: %s failed", t["id"], m.id, action)

    async def match_link(self, guild, m: Match) -> str:
        value = await self.meta(match_key(m.id))
        if value is None or guild is None:
            return ""
        channel_id, message_id = unpack(value)
        return f" https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"

    async def nudge(self, t, bracket: Bracket, m: Match) -> None:
        guild = self.guild()
        channel = self.channel(guild, config.TOURNAMENTS_CHANNEL)
        if channel is not None:
            text = (f"⏳ <@{m.p1}> <@{m.p2}>, your {T.round_name(m.round, bracket.final_round)} match in "
                    f"**{esc(t['name'])}** has been open for a day with no result. Play it, then press who won "
                    f"on the match message. After 48 hours staff step in.{await self.match_link(guild, m)}")
            try:
                await channel.send(text, allowed_mentions=ping_only([discord.Object(m.p1), discord.Object(m.p2)]))
            except discord.HTTPException:
                log.warning("couldn't nudge match %s; retrying", m.id, exc_info=True)
                return
        await self.set_meta(nudged_key(m.id), str(now()))

    async def flag_stale(self, t, bracket: Bracket, m: Match) -> None:
        guild = self.guild()
        channel = self.channel(guild, config.MOD_LOG_CHANNEL)
        if channel is not None:
            if m.status == T.REPORTED and m.reported_by in m.players:
                other = m.p2 if m.reported_by == m.p1 else m.p1
                what = (f"<@{m.reported_by}> reported <@{m.winner}> as the winner and <@{other}> hasn't "
                        "confirmed it in 48 hours.")
            else:
                what = "has had no report for 48 hours."
            text = (f"🏆 **{esc(t['name'])}** · {T.round_name(m.round, bracket.final_round)} match {m.slot + 1}: "
                    f"<@{m.p1}> vs <@{m.p2}> {what} A Keeper or Moderator, press "
                    f"**P1 won** or **P2 won** on the match message to decide it.{await self.match_link(guild, m)}")
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.warning("couldn't flag stale match %s; retrying", m.id, exc_info=True)
                return
        else:
            log.warning("tournament %s match %s is stale (no %s channel)", t["id"], m.id, config.MOD_LOG_CHANNEL)
        await self.set_meta(flagged_key(m.id), str(now()))

    start.autocomplete("tournament")(tournament_choices)
    cancel.autocomplete("tournament")(tournament_choices)
    show_bracket.autocomplete("tournament")(tournament_choices)


async def setup(bot) -> None:
    await bot.add_cog(Tournaments(bot))
