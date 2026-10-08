"""Weekly challenges: three goals each ISO week (Monday 00:00 to Sunday, local time), one
per tier from a pool of twelve, picked from the week key so every restart agrees. Rewards
are 150/250/400 coins plus a 300 bonus for all three.

Progress is computed from the other modules' tables for the week's window only (squads,
clips, ledger rows for /daily, /blackjack and /trivia, message counts, word games,
tournament entries, the hall of fame, game nights, counted voice time); nothing is
counted here. Mondays at 09:00 a board goes up in the games channel. /challenges shows
your progress with a Claim button (restart-safe DynamicItem); the hourly sweep also
claims for everyone and posts one short line per member (a few per sweep at most).
Each payout is ledger ref challenge:WEEK:USER:KEY with a challenge_claims row in the same
transaction. Last week stays claimable for a day, so the final hour isn't lost.

/privacy off: tracking-based challenges show as unavailable and their data is ignored;
the bonus then needs the others. Accounts younger than MIN_ACCOUNT_DAYS can't claim.

Anti-farming: where other people's actions earn someone progress, only accounts that were
MIN_ACCOUNT_DAYS old at the time count (squads posted or joined, voice company, and stars via
logic/starboard.py), so alts can't hand out progress. A game night counts once its start time
has passed and its Discord event wasn't cancelled or deleted; a tournament entry counts once the
tournament has started (no sign up, get paid, leave)."""

import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.lfg import ping_only
from errors import reply_error
from logic import challenges as CH
from logic import coins as C
from logic import quests as Q
from logic import stats as S
from logic.schedule import plan
from logic.starboard import PENDING

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
SEEN_KEY = f"first_seen:{CH.BOARD_JOB.name}"
MAX_ANNOUNCE = 5  # claim lines per sweep; the rest are paid quietly
NOT_YOURS = "These aren't your challenges. Run `/challenges` to see yours."
TOO_NEW = (f"Your Discord account is under {CH.MIN_ACCOUNT_DAYS} days old, so challenge rewards "
           "unlock later. Progress still shows here.")


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Loops and listeners log and carry on."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("challenges: %s failed", fn.__name__)
    return wrapper


def created_ts(member) -> int | None:
    created = getattr(member, "created_at", None)
    return int(created.timestamp()) if created is not None else None


def _in(col: str, ids) -> tuple[str, tuple]:
    if ids is None:
        return "", ()
    ids = tuple(ids)
    return f" AND {col} IN ({','.join('?' * len(ids))})", ids


class ProgressButton(discord.ui.DynamicItem[discord.ui.Button], template=r"challenges:progress"):
    """On the weekly board: shows the clicker their progress, privately."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="My progress", emoji="📋", style=discord.ButtonStyle.primary,
                                           custom_id="challenges:progress"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.client.get_cog("Challenges").show(interaction)
        except Exception:
            log.exception("challenges: progress button failed")
            await reply_error(interaction)


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"challenges:claim:(?P<user>\d+)"):
    def __init__(self, user_id: int, disabled: bool = False):
        super().__init__(discord.ui.Button(label="Claim", emoji="🎁", style=discord.ButtonStyle.success,
                                           custom_id=f"challenges:claim:{user_id}", disabled=disabled))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["user"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.client.get_cog("Challenges").handle_claim(interaction, self.user_id)
        except Exception:
            log.exception("challenges: claim button for %s failed", self.user_id)
            await reply_error(interaction)


def board_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ProgressButton())
    return view


def claim_view(user_id: int, can_claim: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ClaimButton(user_id, disabled=not can_claim))
    return view


class Challenges(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.warned: set[str] = set()  # board periods already logged as having no channel

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ProgressButton, ClaimButton)
        self.tick.start()
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.sweep.cancel()
        self.bot.remove_dynamic_items(ProgressButton, ClaimButton)

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def games_channel(self, guild):
        return config.match_by_name(guild.text_channels, config.GAMES_CHANNEL) if guild else None

    # ------------------------------------------------------------ progress from tables
    async def counts(self, sql: str, params: tuple, col: str, ids) -> dict[int, int]:
        extra, extra_params = _in(col, ids)
        group = f" GROUP BY {col}"
        rows = await self.db.fetchall(sql + extra + group, params + extra_params)
        return {r["u"]: r["n"] for r in rows if r["u"] is not None}

    async def voice(self, week: CH.Week, t: int) -> dict[int, int]:
        rows = await self.db.fetchall(
            'SELECT user_id, channel_id, start, "end" FROM voice_sessions WHERE start < ? AND ("end" IS NULL OR "end" > ?)',
            (week.end, week.start))
        sessions = [S.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in rows]
        guild = self.guild()
        afk = getattr(guild, "afk_channel", None) if guild else None
        bots = {m.id for m in getattr(guild, "members", ()) if m.bot} if guild else set()
        # Fresh accounts (likely alts) are neither paid nor company: time with only them is alone.
        fresh = {s.user_id for s in sessions if not Q.established(s.user_id, t)}
        return S.counted_voice_seconds(sessions, week.start, week.end, t,
                                       excluded_channels={afk.id} if afk else set(), bot_ids=bots | fresh)

    async def measure(self, key: str, week: CH.Week, ids, t: int) -> dict[int, int]:
        """Per member, progress on one challenge in this week's window."""
        w = (week.start, week.end)
        days = (week.first_day.isoformat(), week.last_day.isoformat())
        if key == "join_squads":
            return await self.counts(
                "SELECT m.user_id AS u, COUNT(DISTINCT m.post_id) AS n FROM lfg_members m"
                " JOIN lfg_posts p ON p.id = m.post_id"
                " WHERE m.user_id != p.host_id AND m.joined_at >= ? AND m.joined_at < ?"
                f" AND {Q.established_sql('p.host_id', 'p.created_at')}", w, "m.user_id", ids)
        if key == "host_squads":
            return await self.counts(
                "SELECT p.host_id AS u, COUNT(*) AS n FROM lfg_posts p WHERE p.created_at >= ? AND p.created_at < ?"
                " AND EXISTS (SELECT 1 FROM lfg_members m WHERE m.post_id = p.id AND m.user_id != p.host_id"
                f" AND {Q.established_sql('m.user_id', 'm.joined_at')})",
                w, "p.host_id", ids)
        if key == "post_clip":
            return await self.counts("SELECT user_id AS u, COUNT(*) AS n FROM clips WHERE posted_at >= ? AND posted_at < ?",
                                     w, "user_id", ids)
        if key == "win_game":
            trivia = await self.counts(
                "SELECT user_id AS u, COUNT(*) AS n FROM ledger WHERE reason = 'trivia' AND delta > 0"
                " AND ref LIKE 'trivia:%' AND at >= ? AND at < ?", w, "user_id", ids)
            # A blackjack win pays more than the bet it settles (a push pays the bet back). Hands
            # are one at a time per member, so the bet is the member's latest blackjack debit.
            blackjack = await self.counts(
                "SELECT p.user_id AS u, COUNT(*) AS n FROM ledger p WHERE p.reason = 'blackjack' AND p.delta > 0"
                " AND p.ref LIKE 'blackjack:%' AND p.at >= ? AND p.at < ?"
                " AND p.delta > (SELECT -b.delta FROM ledger b WHERE b.user_id = p.user_id"
                " AND b.reason = 'blackjack' AND b.delta < 0 AND b.id < p.id ORDER BY b.id DESC LIMIT 1)",
                w, "p.user_id", ids)
            return {u: trivia.get(u, 0) + blackjack.get(u, 0) for u in trivia.keys() | blackjack.keys()}
        if key == "messages":
            return await self.counts("SELECT user_id AS u, SUM(count) AS n FROM message_counts WHERE day >= ? AND day <= ?",
                                     days, "user_id", ids)
        if key == "daily":
            return await self.counts("SELECT user_id AS u, COUNT(*) AS n FROM ledger WHERE reason = ? AND delta > 0"
                                     " AND at >= ? AND at < ?", (C.DAILY, *w), "user_id", ids)
        if key == "word_games":
            return await self.counts("SELECT user_id AS u, COUNT(*) AS n FROM word_games WHERE solved = 1"
                                     " AND day >= ? AND day <= ?", days, "user_id", ids)
        if key == "tournament":
            return await self.counts("SELECT e.user_id AS u, COUNT(DISTINCT e.tournament_id) AS n"
                                     " FROM tournament_entries e JOIN tournaments t ON t.id = e.tournament_id"
                                     " WHERE e.joined_at >= ? AND e.joined_at < ? AND t.status IN ('running', 'done')",
                                     w, "e.user_id", ids)
        if key == "hall_of_fame":
            return await self.counts("SELECT author_id AS u, COUNT(*) AS n FROM starboard WHERE at >= ? AND at < ?"
                                     " AND board_message_id IS NOT NULL AND board_message_id != ?",
                                     (*w, PENDING), "author_id", ids)
        if key == "gamenight":
            return await self.gamenights(week, ids, t)
        if key in ("voice_3h", "voice_8h"):
            scores = await self.voice(week, t)
            return scores if ids is None else {u: s for u, s in scores.items() if u in set(ids)}
        raise KeyError(key)

    async def gamenights(self, week: CH.Week, ids, t: int) -> dict[int, int]:
        """Game nights hosted this week whose start time has passed and whose Discord event
        still exists and wasn't cancelled (scheduling one, then calling it off, earns nothing)."""
        extra, extra_params = _in("host_id", ids)
        rows = await self.db.fetchall("SELECT event_id, host_id FROM gamenights WHERE starts_at >= ? AND starts_at < ?"
                                      " AND starts_at <= ?" + extra, (week.start, week.end, t, *extra_params))
        out: dict[int, int] = {}
        guild = self.guild()
        for r in rows:
            if r["host_id"] is not None and await self.gamenight_held(guild, r["event_id"]):
                out[r["host_id"]] = out.get(r["host_id"], 0) + 1
        return out

    @staticmethod
    async def gamenight_held(guild, event_id: int) -> bool:
        if guild is None:
            return False
        event = guild.get_scheduled_event(event_id)
        if event is None:
            try:
                event = await guild.fetch_scheduled_event(event_id)
            except discord.NotFound:
                return False  # deleted
            except discord.HTTPException:
                log.info("challenges: couldn't check game night %s; not counted yet", event_id)
                return False
        return event.status != discord.EventStatus.cancelled

    async def optouts(self) -> set[int]:
        return {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}

    async def progress(self, week: CH.Week, ids=None, t: int | None = None) -> dict[int, dict[str, int]]:
        """Per member, progress on this week's three challenges (None: everyone). Tracking-based
        progress is dropped for opted-out members."""
        t = now() if t is None else t
        if ids is not None:
            ids = tuple(ids)
            if not ids:
                return {}
        optouts = await self.optouts()
        out: dict[int, dict[str, int]] = {}
        for c in CH.pick(week.key):
            for uid, n in (await self.measure(c.key, week, ids, t)).items():
                if c.tracking and uid in optouts:
                    continue
                out.setdefault(uid, {})[c.key] = n
        return out

    async def claims(self, week_key: str, ids=None) -> dict[int, set[str]]:
        extra, params = _in("user_id", ids)
        rows = await self.db.fetchall("SELECT user_id, key FROM challenge_claims WHERE week = ?" + extra,
                                      (week_key, *params))
        out: dict[int, set[str]] = {}
        for r in rows:
            out.setdefault(r["user_id"], set()).add(r["key"])
        return out

    # ------------------------------------------------------------ claiming
    async def settle(self, uid: int, week: CH.Week, progress: dict[str, int], tracking: bool,
                     t: int) -> list[tuple[str, int]]:
        """Pay what's finished and unclaimed for one member and week. Returns what was paid now."""
        picks = CH.pick(week.key)
        paid: list[tuple[str, int]] = []
        async with self.db.transaction() as tx:
            rows = await tx.fetchall("SELECT key FROM challenge_claims WHERE week = ? AND user_id = ?",
                                     (week.key, uid))
            claimed = {r["key"] for r in rows}
            for key, coins in CH.claimable(picks, progress, claimed, tracking):
                result = await economy.apply_tx(tx, uid, coins, CH.REASON, t, ref=CH.ref(week.key, uid, key))
                if result.ok or result.status is economy.Status.DUPLICATE:
                    await tx.execute("INSERT OR IGNORE INTO challenge_claims (week, user_id, key, at) VALUES (?, ?, ?, ?)",
                                     (week.key, uid, key, t))
                if result.ok:
                    paid.append((key, coins))
        if paid:
            log.info("challenges: paid %s for %s: %s", uid, week.key, ", ".join(k for k, _ in paid))
        return paid

    async def claim(self, member) -> list[tuple[str, int]]:
        """Claim everything finished for one member (this week, and last week during the grace)."""
        t = now()
        if getattr(member, "bot", False) or not CH.old_enough(created_ts(member), t):
            return []
        tracking = await self.db.tracking_allowed(member.id)
        paid = []
        for week in CH.weeks_to_settle(t, self.tz):
            progress = (await self.progress(week, {member.id}, t)).get(member.id, {})
            paid += await self.settle(member.id, week, progress, tracking, t)
        return paid

    # ------------------------------------------------------------ hourly sweep
    @tasks.loop(hours=1)
    async def sweep(self) -> None:
        await self.run_sweep()

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_sweep(self) -> int:
        """Auto-claim for every member. Returns how many members were paid."""
        guild = self.guild()
        if guild is None:
            return 0
        t = now()
        optouts = await self.optouts()
        members = {m.id: m for m in guild.members
                   if not m.bot and CH.old_enough(created_ts(m), t)}
        paid: dict[int, list[tuple[str, int]]] = {}
        for week in CH.weeks_to_settle(t, self.tz):
            picks = CH.pick(week.key)
            progress = await self.progress(week, None, t)
            claims = await self.claims(week.key)
            for uid, mine in progress.items():
                member = members.get(uid)
                if member is None:
                    continue
                tracking = uid not in optouts
                if not CH.claimable(picks, mine, claims.get(uid, set()), tracking):
                    continue
                try:
                    got = await self.settle(uid, week, mine, tracking, t)
                except Exception:
                    log.exception("challenges: sweep claim for %s failed", uid)
                    continue
                if got:
                    paid.setdefault(uid, []).extend(got)
        await self.announce(guild, paid)
        return len(paid)

    async def announce(self, guild, paid: dict[int, list[tuple[str, int]]]) -> int:
        if not paid:
            return 0
        channel = self.games_channel(guild)
        if channel is None:
            log.info("challenges: no %s channel to announce claims in", config.GAMES_CHANNEL)
            return 0
        sent = 0
        for uid, items in list(paid.items())[:MAX_ANNOUNCE]:
            try:
                await channel.send(CH.claim_text(f"<@{uid}>", items),
                                   allowed_mentions=ping_only(users=[discord.Object(uid)]))
                sent += 1
            except Exception:
                log.exception("challenges: announcing %s failed", uid)
        return sent

    # ------------------------------------------------------------ weekly board
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        await self.run_board()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_board(self) -> bool:
        """Post this week's board once (first run only marks it done). True when posted."""
        job = CH.BOARD_JOB
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (SEEN_KEY,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
        t = now()
        todo = plan(job, t, self.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (SEEN_KEY, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        if todo.run is None:
            return False
        # Marked done only after it worked: no channel yet or an error retries on the next tick.
        if await self.post_board(todo.run):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))
            return True
        return False

    def board_embed(self, week: CH.Week) -> discord.Embed:
        return style.embed(title=f"Weekly challenges · {week.key}", description=CH.board_text(CH.pick(week.key), week),
                           footer=style.label("challenges", week.key))

    async def post_board(self, period) -> bool:
        channel = self.games_channel(self.guild())
        if channel is None:
            if period.key not in self.warned:
                self.warned.add(period.key)
                log.warning("challenges board %s: no %s channel yet", period.key, config.GAMES_CHANNEL)
            return False
        week = CH.week_of(period.scheduled_at, self.tz)
        await channel.send(embed=self.board_embed(week), view=board_view(), allowed_mentions=NO_PINGS)
        log.info("challenges: posted board %s", week.key)
        return True

    # ------------------------------------------------------------ /challenges
    async def progress_embed(self, member, note: str | None = None) -> tuple[discord.Embed, bool]:
        """The member's progress this week, and whether the Claim button should be live."""
        t = now()
        week = CH.week_of(t, self.tz)
        picks = CH.pick(week.key)
        tracking = await self.db.tracking_allowed(member.id)
        mine = (await self.progress(week, {member.id}, t)).get(member.id, {})
        claimed = (await self.claims(week.key, {member.id})).get(member.id, set())
        old = CH.old_enough(created_ts(member), t)
        ready = CH.claimable(picks, mine, claimed, tracking)
        lines = [CH.progress_text(picks, mine, claimed, tracking), "", f"Resets <t:{week.end}:R>."]
        if not old:
            lines.append(TOO_NEW)
        if note:
            lines.append(note)
        embed = style.embed(title=f"Your challenges · {week.key}", description="\n".join(lines),
                            footer=style.label("challenges", f"{len(claimed - {CH.BONUS_KEY})}/3 claimed"))
        return embed, bool(ready) and old

    async def show(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        embed, live = await self.progress_embed(member)
        await interaction.response.send_message(embed=embed, view=claim_view(member.id, live), ephemeral=True,
                                                allowed_mentions=NO_PINGS)

    async def handle_claim(self, interaction: discord.Interaction, user_id: int) -> None:
        member = interaction.user
        if member.id != user_id:
            await interaction.response.send_message(NOT_YOURS, ephemeral=True)
            return
        paid = await self.claim(member)
        note = f"🏅 Claimed **{CH.total(paid):,} coins**." if paid else "Nothing new to claim yet."
        embed, live = await self.progress_embed(member, note)
        await interaction.response.edit_message(embed=embed, view=claim_view(member.id, live))

    @app_commands.command(name="challenges", description="This week's challenges, your progress and rewards")
    @app_commands.guild_only()
    async def challenges(self, interaction: discord.Interaction) -> None:
        await self.show(interaction)


async def setup(bot) -> None:
    await bot.add_cog(Challenges(bot))
