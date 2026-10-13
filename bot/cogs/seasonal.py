"""Seasonal events (Halloween, Winter Fest): drops in chat, a daily quest, /event, prizes.

Events come from assets/events.json and recur every year (logic/seasonal.py). While one runs:

- Drops: a few times a day (seeded times, 10:00 to 23:30 local) a drop is posted in the general
  channel, but only when a member wrote there in the last 20 minutes. The Grab button is a
  restart-safe DynamicItem; the first account at least MIN_ACCOUNT_DAYS old to press it takes
  the drop in one UPDATE ... WHERE claimed_by IS NULL, is paid through bot/economy.py (ref
  event:KEY:YEAR:drop:ID) in the same transaction and earns one point. Unclaimed drops are
  deleted after ten minutes. One member can take twelve a day.
- Daily quest: one small task a day, measured from the other modules' tables for that local
  day (never counted here), granted by an hourly sweep (and when the member opens /event) with
  an event_claims row and its payout in one transaction.
- Points are read from the tables every time: one per claimed drop, three per quest claim.
- The start is announced once; after the end the top three are paid (refs event:KEY:YEAR:rank:N),
  the winner gets the Event Champ role and everyone with ten points gets the badge
  (achievements row event_KEY_YEAR). Every step is idempotent, so a restart can't pay twice.

Privacy: the chat-activity check keeps one timestamp in memory (no author, no text). Quests
measured from tracked activity are skipped for members who ran /privacy off."""

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
from logic import quests as Q
from logic import seasonal as S
from logic import stats as ST
from logic.selfroles import has_dangerous_permissions

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
MAX_ANNOUNCE = 8  # quest lines per sweep; the rest are paid quietly
FINALE_DELAY = 15 * 60  # after the end: the last drops have expired and can't change the board
GIVE_UP_DELETING = 60 * 60  # stop retrying to delete an expired drop after this
TOO_NEW = (f"Your Discord account is under {S.MIN_ACCOUNT_DAYS} days old, so event rewards unlock "
           "later. Thanks for hanging out!")
TAKEN = "Too slow, someone already grabbed that one."
GONE = "That one's gone. Keep an eye on chat for the next."
CAPPED = f"You've grabbed {S.DROP_DAILY_MAX} today. Leave some for the others and come back tomorrow."


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Loops and listeners log and carry on."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        try:
            return await fn(self, *args, **kwargs)
        except Exception:
            log.exception("seasonal: %s failed", fn.__name__)
    return wrapper


def can_earn(member, t: int) -> bool:
    """Not a bot, and an established account."""
    return member is not None and not getattr(member, "bot", False) and Q.established(member.id, t)


def _in(col: str, ids) -> tuple[str, tuple]:
    if ids is None:
        return "", ()
    ids = tuple(ids)
    return f" AND {col} IN ({','.join('?' * len(ids))})", ids


class GrabButton(discord.ui.DynamicItem[discord.ui.Button], template=r"seasonal:grab:(?P<id>\d+)"):
    def __init__(self, drop_id: int, emoji: str | None = None):
        super().__init__(discord.ui.Button(label="Grab", emoji=emoji, style=discord.ButtonStyle.success,
                                           custom_id=f"seasonal:grab:{drop_id}"))
        self.drop_id = drop_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.client.get_cog("Seasonal").handle_grab(interaction, self.drop_id)
        except Exception:
            log.exception("seasonal: grab button for drop %s failed", self.drop_id)
            await reply_error(interaction)


def grab_view(drop_id: int, emoji: str | None = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(GrabButton(drop_id, emoji))
    return view


def load_events() -> tuple[S.Event, ...]:
    try:
        return S.load_events()
    except Exception:
        log.exception("seasonal: couldn't read %s; no events will run", S.EVENTS_FILE.name)
        return ()


class Seasonal(commands.Cog):
    def __init__(self, bot, events: tuple[S.Event, ...] | None = None):
        self.bot = bot
        self.events = load_events() if events is None else tuple(events)
        self.last_chat: int | None = None  # when a member last wrote in general (memory only)
        self.warned: set[str] = set()  # problems already logged once
        self.skipped: set[str] = set()  # ended runs this bot never saw: nothing to finish

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(GrabButton)
        self.tick.start()
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.sweep.cancel()
        self.bot.remove_dynamic_items(GrabButton)

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    @staticmethod
    def channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild else None

    def warn_once(self, key: str, message: str, *args) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(message, *args)

    def event_of(self, run_id: str) -> S.Event | None:
        parsed = S.parse_run_id(run_id)
        return next((e for e in self.events if parsed and e.key == parsed[0]), None)

    def slots_for(self, run: S.Run, day) -> list[S.Slot]:
        return S.drop_slots(run, day, self.tz)

    async def done(self, key: str) -> bool:
        return await self.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (key,)) is not None

    async def mark(self, key: str, t: int) -> None:
        await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (key, t))

    # ------------------------------------------------------------ chat activity
    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        try:
            guild = getattr(message, "guild", None)
            if guild is None or guild.id != self.bot.settings.guild_id or getattr(message.author, "bot", True):
                return
            if config.match_by_name([message.channel], config.GENERAL_CHANNEL) is not None:
                self.last_chat = now()
        except Exception:
            log.exception("seasonal: on_message failed")

    # ------------------------------------------------------------ the minute tick
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        await self.run_tick()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_tick(self) -> None:
        guild = self.guild()
        if guild is None:
            return
        t = now()
        run = S.current(self.events, t, self.tz)
        steps = [("expire_drops", self.expire_drops(guild, t))]
        if run is not None:
            steps += [("open_event", self.open_event(guild, run, t)), ("maybe_drop", self.maybe_drop(guild, run, t))]
        steps.append(("finish_events", self.finish_events(guild, t)))
        for name, step in steps:  # one failing step doesn't stop the others
            try:
                await step
            except Exception:
                log.exception("seasonal: %s failed", name)

    # ------------------------------------------------------------ opening
    async def open_event(self, guild, run: S.Run, t: int) -> bool:
        """Remember the run was seen (so its end is handled) and announce it once."""
        await self.mark(f"seasonal:seen:{run.id}", t)
        key = f"seasonal:open:{run.id}"
        if await self.done(key):
            return False
        channel = self.channel(guild, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None:
            self.warn_once(key, "seasonal: no %s channel to announce %s in", config.ANNOUNCEMENTS_CHANNEL, run.id)
            return False
        e = run.event
        embed = style.embed(title=f"{e.emoji} {e.name}", description=S.opening_text(run), color=e.color,
                            footer=style.label("event", e.name, str(run.year)))
        await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        await self.mark(key, t)
        log.info("seasonal: announced %s", run.id)
        return True

    # ------------------------------------------------------------ drops
    async def maybe_drop(self, guild, run: S.Run, t: int) -> bool:
        """Post the drop that is due, if chat is awake. True when one was posted."""
        channel = self.channel(guild, config.GENERAL_CHANNEL)
        if channel is None:
            self.warn_once("general", "seasonal: no %s channel for drops", config.GENERAL_CHANNEL)
            return False
        day = S.local_day(t, self.tz)
        prefix = f"seasonal:drop:{run.id}:{day.isoformat()}:"
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (prefix + "%",))}
        slot = S.due_slot(self.slots_for(run, day), t, done, run, day)
        if slot is None or not S.chat_active(self.last_chat, t):
            return False
        row = await self.db.fetchone("SELECT MAX(created_at) AS at FROM event_drops")
        if not S.gap_ok(row["at"] if row else None, t):
            return False
        # The slot is spent before the post goes out: a crash can lose a drop, never repeat one.
        async with self.db.transaction() as tx:
            cur = await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (slot.job_key(run, day), t))
            if not cur.rowcount:
                return False
            cur = await tx.execute("INSERT INTO event_drops (event, channel_id, kind, value, created_at)"
                                   " VALUES (?, ?, 'drop', ?, ?)", (run.id, channel.id, slot.coins, t))
            drop_id = cur.lastrowid
        e = run.event
        embed = style.embed(description=S.drop_text(e, slot.line), color=e.color, footer=style.label(e.name, "drop"))
        try:
            message = await channel.send(embed=embed, view=grab_view(drop_id, e.drop_emoji), allowed_mentions=NO_PINGS)
        except Exception:
            await self.db.execute("DELETE FROM event_drops WHERE id = ? AND message_id IS NULL AND claimed_by IS NULL",
                                  (drop_id,))
            raise
        await self.db.execute("UPDATE event_drops SET message_id = ? WHERE id = ?", (message.id, drop_id))
        log.info("seasonal: drop %s posted for %s", drop_id, run.id)
        return True

    async def expire_drops(self, guild, t: int) -> int:
        """Delete drops nobody grabbed in time. Returns how many were cleared."""
        rows = await self.db.fetchall(
            "SELECT id, channel_id, message_id, created_at FROM event_drops"
            " WHERE claimed_by IS NULL AND message_id IS NOT NULL AND created_at <= ?", (t - S.DROP_EXPIRY,))
        cleared = 0
        for r in rows:
            gone = t - r["created_at"] >= GIVE_UP_DELETING
            channel = guild.get_channel(r["channel_id"])
            if channel is None:
                gone = True
            else:
                try:
                    await channel.get_partial_message(r["message_id"]).delete()
                    gone = True
                except discord.NotFound:
                    gone = True
                except Exception:
                    log.warning("seasonal: couldn't delete expired drop %s yet", r["id"], exc_info=True)
            if gone:
                await self.db.execute("UPDATE event_drops SET message_id = NULL WHERE id = ? AND claimed_by IS NULL",
                                      (r["id"],))
                cleared += 1
        return cleared

    async def take(self, drop_id: int, user_id: int, t: int) -> tuple[str, object]:
        """Try to claim a drop for a member. Returns (outcome, row): outcome is "won", "taken",
        "gone" or "capped". The claim, the payout and the cap check share one transaction."""
        day_start, day_end = S.day_bounds(S.local_day(t, self.tz), self.tz)
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM event_drops WHERE id = ?", (drop_id,))
            if row is None:
                return "gone", None
            if row["claimed_by"] is not None:
                return "taken", row
            if S.drop_expired(row["created_at"], t):
                return "gone", row
            mine = await tx.fetchone("SELECT COUNT(*) AS n FROM event_drops WHERE claimed_by = ?"
                                     " AND claimed_at >= ? AND claimed_at < ?", (user_id, day_start, day_end))
            if mine["n"] >= S.DROP_DAILY_MAX:
                return "capped", row
            cur = await tx.execute("UPDATE event_drops SET claimed_by = ?, claimed_at = ? WHERE id = ?"
                                   " AND claimed_by IS NULL AND created_at > ?", (user_id, t, drop_id, t - S.DROP_EXPIRY))
            if cur.rowcount != 1:
                return "taken", row
            await economy.apply_tx(tx, user_id, row["value"], S.REASON, t, ref=S.drop_ref(row["event"], drop_id))
        log.info("seasonal: %s took drop %s (%s coins)", user_id, drop_id, row["value"])
        return "won", row

    async def handle_grab(self, interaction: discord.Interaction, drop_id: int) -> None:
        user = interaction.user
        t = now()
        if getattr(user, "bot", False):
            return
        if not can_earn(user, t):
            await interaction.response.send_message(TOO_NEW, ephemeral=True)
            return
        outcome, row = await self.take(drop_id, user.id, t)
        if outcome == "won":
            event = self.event_of(row["event"])
            text = (S.claimed_text(event, f"<@{user.id}>", row["value"]) if event is not None else
                    f"<@{user.id}> grabbed it: **+{row['value']:,} coins** and 1 event point.")
            embed = style.embed(description=text, color=style.MUTED,
                                footer=style.label(event.name if event else "event", "drop"))
            try:
                # An edit never pings, and the mention is locked down anyway.
                await interaction.response.edit_message(embed=embed, view=None, allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                log.warning("seasonal: couldn't edit drop %s after the claim", drop_id, exc_info=True)
                await reply_error(interaction, f"You got it: +{row['value']:,} coins and 1 event point.")
            return
        await interaction.response.send_message({"taken": TAKEN, "capped": CAPPED}.get(outcome, GONE), ephemeral=True)
        if outcome == "gone":  # a leftover message (the sweep was down, or its row is gone)
            try:
                await interaction.message.delete()
            except Exception:
                pass
            if row is not None:
                await self.db.execute("UPDATE event_drops SET message_id = NULL WHERE id = ? AND claimed_by IS NULL",
                                      (drop_id,))

    # ------------------------------------------------------------ points
    async def board(self, run_id: str) -> list[S.Standing]:
        drops = await self.db.fetchall(
            "SELECT claimed_by AS u, COUNT(*) AS n, MAX(claimed_at) AS at FROM event_drops"
            " WHERE event = ? AND claimed_by IS NOT NULL GROUP BY claimed_by", (run_id,))
        quests = await self.db.fetchall(
            "SELECT user_id AS u, COUNT(*) AS n, MAX(at) AS at FROM event_claims"
            " WHERE event = ? AND key LIKE 'quest:%' GROUP BY user_id", (run_id,))
        return S.standings([(r["u"], r["n"], r["at"]) for r in drops], [(r["u"], r["n"], r["at"]) for r in quests])

    # ------------------------------------------------------------ daily quest
    async def counts(self, sql: str, params: tuple, col: str, ids) -> dict[int, int]:
        extra, extra_params = _in(col, ids)
        rows = await self.db.fetchall(f"{sql}{extra} GROUP BY {col}", params + extra_params)
        return {r["u"]: r["n"] for r in rows if r["u"] is not None}

    async def measure(self, quest: S.Quest, day, ids, t: int) -> dict[int, int]:
        """Per member, progress on one quest within one local day, read from the tables."""
        w = S.day_bounds(day, self.tz)
        iso = day.isoformat()
        if quest.key == "squad":
            # In a squad today with at least one other established account (an alt is no company).
            return await self.counts(
                "SELECT m.user_id AS u, COUNT(DISTINCT m.post_id) AS n FROM lfg_members m"
                " WHERE m.joined_at >= ? AND m.joined_at < ? AND EXISTS (SELECT 1 FROM lfg_members o"
                f" WHERE o.post_id = m.post_id AND o.user_id != m.user_id AND {Q.established_sql('o.user_id', 'o.joined_at')})",
                w, "m.user_id", ids)
        if quest.key == "win_game":
            trivia = await self.counts(
                "SELECT user_id AS u, COUNT(*) AS n FROM ledger WHERE reason = 'trivia' AND delta > 0"
                " AND ref LIKE 'trivia:%' AND at >= ? AND at < ?", w, "user_id", ids)
            # A blackjack win pays more than the bet it settles (a push pays the bet back).
            blackjack = await self.counts(
                "SELECT p.user_id AS u, COUNT(*) AS n FROM ledger p WHERE p.reason = 'blackjack' AND p.delta > 0"
                " AND p.ref LIKE 'blackjack:%' AND p.at >= ? AND p.at < ?"
                " AND p.delta > (SELECT -b.delta FROM ledger b WHERE b.user_id = p.user_id"
                " AND b.reason = 'blackjack' AND b.delta < 0 AND b.id < p.id ORDER BY b.id DESC LIMIT 1)",
                w, "p.user_id", ids)
            return {u: trivia.get(u, 0) + blackjack.get(u, 0) for u in trivia.keys() | blackjack.keys()}
        if quest.key == "chat":
            return await self.counts("SELECT user_id AS u, SUM(count) AS n FROM message_counts WHERE day = ?",
                                     (iso,), "user_id", ids)
        if quest.key == "word":
            return await self.counts("SELECT user_id AS u, COUNT(*) AS n FROM word_games WHERE solved = 1 AND day = ?",
                                     (iso,), "user_id", ids)
        if quest.key == "voice":
            rows = await self.db.fetchall(
                'SELECT user_id, channel_id, start, "end" FROM voice_sessions WHERE start < ? AND ("end" IS NULL OR "end" > ?)',
                (w[1], w[0]))
            sessions = [ST.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in rows]
            guild = self.guild()
            afk = getattr(guild, "afk_channel", None) if guild else None
            bots = {m.id for m in getattr(guild, "members", ()) if m.bot} if guild else set()
            fresh = {s.user_id for s in sessions if not Q.established(s.user_id, t)}  # alts are no company
            scores = ST.counted_voice_seconds(sessions, w[0], w[1], t, excluded_channels={afk.id} if afk else set(),
                                              bot_ids=bots | fresh)
            return scores if ids is None else {u: s for u, s in scores.items() if u in set(ids)}
        raise KeyError(quest.key)

    async def grant(self, run: S.Run, day, user_id: int, t: int) -> bool:
        """Record the day's quest for a member and pay it, once. True when it was new."""
        async with self.db.transaction() as tx:
            cur = await tx.execute("INSERT OR IGNORE INTO event_claims (event, user_id, key, at) VALUES (?, ?, ?, ?)",
                                   (run.id, user_id, S.quest_claim_key(day), t))
            if not cur.rowcount:
                return False
            await economy.apply_tx(tx, user_id, S.QUEST_COINS, S.REASON, t, ref=S.quest_ref(run.id, day, user_id))
        log.info("seasonal: %s finished the %s quest of %s", user_id, run.id, day)
        return True

    async def settle_day(self, guild, run: S.Run, day, t: int, ids=None) -> list[int]:
        """Grant one day's quest to everyone (or `ids`) who did it. Returns who was paid now."""
        quest = S.quest_for(run, day, self.tz)
        values = await self.measure(quest, day, ids, t)
        if not values:
            return []
        optouts = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}
        rows = await self.db.fetchall("SELECT user_id FROM event_claims WHERE event = ? AND key = ?",
                                      (run.id, S.quest_claim_key(day)))
        claimed = {r["user_id"] for r in rows}
        paid = []
        for uid in sorted(values):
            if uid in claimed or not S.quest_done(quest, values[uid]) or (quest.tracking and uid in optouts):
                continue
            if not can_earn(guild.get_member(uid), t):
                continue
            try:
                if await self.grant(run, day, uid, t):
                    paid.append(uid)
            except Exception:
                log.exception("seasonal: quest grant for %s failed", uid)
        return paid

    @tasks.loop(hours=1)
    async def sweep(self) -> None:
        await self.run_sweep()

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_sweep(self) -> int:
        """Grant the daily quest to everyone who finished it. Returns how many were paid."""
        guild = self.guild()
        if guild is None:
            return 0
        t = now()
        run = S.current(self.events, t, self.tz)
        if run is None:
            return 0
        paid: list[tuple[int, S.Quest]] = []
        for day in S.quest_days(run, t, self.tz):
            quest = S.quest_for(run, day, self.tz)
            paid += [(uid, quest) for uid in await self.settle_day(guild, run, day, t)]
        if not paid:
            return 0
        channel = self.channel(guild, config.GAMES_CHANNEL)
        if channel is None:
            self.warn_once("games", "seasonal: no %s channel to announce quests in", config.GAMES_CHANNEL)
            return len(paid)
        for uid, quest in paid[:MAX_ANNOUNCE]:
            try:
                await channel.send(S.quest_text(run.event, f"<@{uid}>", quest),
                                   allowed_mentions=ping_only(users=[discord.Object(uid)]))
            except Exception:
                log.exception("seasonal: announcing the quest of %s failed", uid)
        return len(paid)

    # ------------------------------------------------------------ the end
    async def finish_events(self, guild, t: int) -> int:
        finished = 0
        for run in S.last_ended(self.events, t, self.tz):
            if t < run.end + FINALE_DELAY or run.id in self.skipped:
                continue
            key = f"seasonal:finale:{run.id}"
            if await self.done(key):
                self.skipped.add(run.id)
                continue
            if not await self.done(f"seasonal:seen:{run.id}"):
                self.skipped.add(run.id)  # over before this bot ever saw it
                continue
            try:
                if await self.finish(guild, run, t):
                    await self.mark(key, t)
                    self.skipped.add(run.id)
                    finished += 1
            except Exception:
                log.exception("seasonal: finishing %s failed; will retry", run.id)
        return finished

    async def finish(self, guild, run: S.Run, t: int) -> bool:
        """Pay, badge, crown and announce. Every step can be repeated safely; True once the
        announcement is out (or there was nothing to announce)."""
        await self.settle_day(guild, run, S.last_day(run, self.tz), t)  # the last day's quest, quietly
        board = await self.board(run.id)

        def here(uid: int) -> bool:
            member = guild.get_member(uid)
            return member is not None and not member.bot

        top = S.winners(board, here)
        for rank, s in enumerate(top, start=1):
            await economy.apply(self.db, s.user_id, S.RANK_COINS[rank - 1], S.REASON, t, ref=S.rank_ref(run.id, rank))
        holders = S.badge_holders(board)
        if holders:
            async with self.db.transaction() as tx:
                for uid in holders:
                    await tx.execute("INSERT OR IGNORE INTO achievements (user_id, key, at) VALUES (?, ?, ?)",
                                     (uid, run.badge_key, t))
        if not top:
            log.info("seasonal: %s ended with nobody on the board", run.id)
            return True
        role_given = await self.crown(guild, run, top[0].user_id)
        channel = self.channel(guild, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None:
            self.warn_once(f"finale:{run.id}", "seasonal: no %s channel to announce the end of %s in",
                           config.ANNOUNCEMENTS_CHANNEL, run.id)
            return False
        e = run.event
        embed = style.embed(title=f"{e.emoji} {e.name} {run.year}: results",
                            description=S.finale_text(run, top, len(holders), role_given), color=e.color,
                            footer=style.label("event", e.name, str(run.year)))
        mentions = " ".join(f"<@{s.user_id}>" for s in top)
        await channel.send(mentions, embed=embed,
                           allowed_mentions=ping_only(users=[discord.Object(s.user_id) for s in top]))
        log.info("seasonal: %s finished; winners %s", run.id, [s.user_id for s in top])
        return True

    async def crown(self, guild, run: S.Run, winner_id: int) -> bool:
        """Move the Event Champ role to the winner. True when they hold it afterwards."""
        role = config.match_by_name(guild.roles, config.EVENT_ROLE)
        me = getattr(guild, "me", None)
        if role is None or me is None or me.top_role.position <= role.position:
            self.warn_once(f"role:{run.id}", "seasonal: can't hand out %s (missing, or not below Front Desk)",
                           config.EVENT_ROLE)
            return False
        if has_dangerous_permissions(role):
            self.warn_once(f"role:{run.id}", "seasonal: won't give %s: it has moderator permissions", config.EVENT_ROLE)
            return False
        reason = f"{run.event.name} {run.year} winner"
        for member in list(getattr(role, "members", [])):
            if member.id != winner_id:
                try:
                    await member.remove_roles(role, reason=reason)
                except discord.HTTPException:
                    log.warning("seasonal: couldn't remove %s from a previous winner", config.EVENT_ROLE, exc_info=True)
        winner = guild.get_member(winner_id)
        if winner is None:
            return False
        if role in winner.roles:
            return True
        try:
            await winner.add_roles(role, reason=reason)
        except discord.HTTPException:
            log.warning("seasonal: couldn't give %s to the winner", config.EVENT_ROLE, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ /event
    async def event_embed(self, member) -> discord.Embed:
        t = now()
        run = S.current(self.events, t, self.tz)
        if run is None:
            return style.embed(title="Seasonal events", description=S.idle_text(S.upcoming(self.events, t, self.tz)),
                               color=style.MUTED, footer=style.label("event"))
        e = run.event
        guild = self.guild()
        day = S.local_day(t, self.tz)
        quest = S.quest_for(run, day, self.tz)
        if guild is not None:
            try:  # pay today's quest now if it's done, instead of making them wait for the sweep
                await self.settle_day(guild, run, day, t, ids={member.id})
            except Exception:
                log.exception("seasonal: settling the quest of %s failed", member.id)
        claimed = await self.db.fetchone("SELECT 1 FROM event_claims WHERE event = ? AND user_id = ? AND key = ?",
                                         (run.id, member.id, S.quest_claim_key(day))) is not None
        tracking = await self.db.tracking_allowed(member.id)
        board = await self.board(run.id)
        points, place = S.my_points(board, member.id)
        mine = f"**{S.plural(points, 'point')}**" + (f" (#{place})" if place else "")
        lines = [f"Ends <t:{run.end}:R>.", "", f"Your score: {mine}", "", "**Today's quest**",
                 S.quest_line(quest, tracking=tracking, claimed=claimed, old=can_earn(member, t)), "",
                 f"**Top {S.TOP_N}**", S.board_text(board)]
        embed = style.embed(title=f"{e.emoji} {e.name}", description="\n".join(lines), color=e.color,
                            footer=style.label("event", e.name, str(run.year)))
        embed.add_field(name="How it works", value=S.how_it_works(e), inline=False)
        return embed

    @app_commands.command(name="event", description="The current seasonal event: your points, the top 10 and time left")
    @app_commands.guild_only()
    async def event(self, interaction: discord.Interaction) -> None:
        embed = await self.event_embed(interaction.user)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)


async def setup(bot) -> None:
    await bot.add_cog(Seasonal(bot))
