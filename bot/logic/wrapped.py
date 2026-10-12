"""Wrapped: a member's calendar year in numbers, and the server's. Pure: no Discord, no
database, no drawing (logic/wrappedcard.py draws what this module works out).

The cog hands in raw rows (`MemberRows`, `ServerRows`); `summarize` and `summarize_server`
cut them to one local calendar year (midnight Jan 1 to midnight Jan 1, in the server's
timezone) and add them up. Only numbers the tables can support are promised:

- Voice, game time and messages come from the stats tables, which are never pruned by age
  but are deleted by /privacy off, so a tracking-off member has no card at all.
- Levels have no history (the `xp` table holds the current level only). "Levels gained" is
  shown only when the cog has a level snapshot from the start of the year; otherwise the
  card says which level the member reached.
- Coins come from the ledger: "earned" leaves out gifts and anything that pays a stake back
  (coinflip, slots, blackjack, predictions, raffle), and "biggest payout" is the largest
  single prize or game payout (it includes the stake for the staked games, hence "payout").
- The longest /daily streak is rebuilt from the dated ledger rows (wallets only keep the
  current streak).

Teammates are ranked by shared time: seconds in the same voice channel plus half an hour
per /lfg squad or matchmaking match both were in. Only members in `allowed` (in the server
and not opted out of tracking) can appear, and the section is dropped unless two qualify."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from logic import achievements as A
from logic import econstats
from logic import stats as S
from logic.schedule import Plan

HOUR = 60 * 60
SQUAD_WEIGHT = 30 * 60  # one shared squad or match counts like half an hour of shared voice
TEAMMATES = 3
MIN_TEAMMATES = 2  # fewer than this qualify: no teammates section
TOP_GAMES = 5
GAME_PLAYED = HOUR  # a game counts as "played" for the Sampler at an hour or more
FIRST_YEAR = 2026  # the bot's first year: nothing older can exist

# Ledger reasons whose positive rows hand a stake back, so they aren't "earned".
STAKED = frozenset({"coinflip", "slots", "blackjack", "predict", "raffle"})
NOT_EARNED = STAKED | {"give", "shop"}
BET_REASONS = frozenset({"coinflip", "slots", "blackjack"})
# Reasons that can be a "biggest payout": games and prizes, never routine income.
PAYOUT_REASONS = frozenset({"coinflip", "slots", "blackjack", "trivia", "predict", "raffle", "tournament",
                            "mvp", "clipweek", "season", "invitecontest", "motm", "event"})
DAILY_REASON = "daily"
WRAPPED_BADGE_PREFIX = "wrapped_"  # the share badge (logic/achievements.py shows it)

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {many or word + 's'}"


# ---------------------------------------------------------------- the calendar
def _midnight(day: date, tz: tzinfo) -> int:
    return int(datetime.combine(day, time(0), tz).timestamp())


def year_bounds(year: int, tz: tzinfo) -> tuple[int, int]:
    """[start, end) of the local calendar year in unix seconds."""
    return _midnight(date(year, 1, 1), tz), _midnight(date(year + 1, 1, 1), tz)


def month_bounds(year: int, month: int, tz: tzinfo) -> tuple[int, int]:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return _midnight(date(year, month, 1), tz), _midnight(nxt, tz)


def local_date(ts: int, tz: tzinfo) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def day_range(year: int) -> tuple[str, str]:
    """First and last ISO day of the year, as message_counts stores days."""
    return f"{year:04}-01-01", f"{year:04}-12-31"


def longest_streak(days: Iterable[date]) -> int:
    """The longest run of consecutive dates."""
    best = run = 0
    prev = None
    for d in sorted(set(days)):
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = d
    return best


# ---------------------------------------------------------------- when it's open
def open_year(now: int, tz: tzinfo) -> int | None:
    """The year whose Wrapped is open right now: all of December for that year, and
    January 1-15 for the year before. None the rest of the time."""
    today = local_date(now, tz)
    if today.month == 12:
        return today.year
    if today.month == 1 and today.day <= 15:
        return today.year - 1
    return None


def opens_at(year: int, tz: tzinfo) -> int:
    return _midnight(date(year, 12, 1), tz)


def closes_at(year: int, tz: tzinfo) -> int:
    """The first second Wrapped `year` is closed (midnight starting January 16)."""
    return _midnight(date(year + 1, 1, 16), tz)


CLOSED_TEXT = ("Wrapped opens on December 1 and stays open until January 15. "
               "Come back then for your year in review.")


def resolve_year(asked: int | None, now: int, tz: tzinfo, staff: bool = False) -> tuple[int | None, str | None]:
    """(year to show, None) or (None, why not). Members get the open year only; staff may
    preview any year from FIRST_YEAR up to the current one, at any time."""
    this_year = local_date(now, tz).year
    opened = open_year(now, tz)
    if staff:
        year = asked if asked is not None else (opened or this_year)
        if year < FIRST_YEAR or year > this_year:
            return None, f"There's nothing to wrap for {year}: pick a year from {FIRST_YEAR} to {this_year}."
        return year, None
    if opened is None:
        return None, CLOSED_TEXT
    if asked is not None and asked != opened:
        return None, f"Only Wrapped {opened} is open right now."
    return opened, None


# ---------------------------------------------------------------- rows in
@dataclass(frozen=True)
class Squad:
    """An /lfg post or a matchmaking match: when it was made and who was in it."""

    created_at: int
    members: tuple[int, ...]
    host_id: int | None = None  # None for a match


@dataclass(frozen=True)
class Coin:
    delta: int
    reason: str
    ref: str | None
    at: int
    user_id: int = 0  # only read by the server summary


@dataclass(frozen=True)
class MemberRows:
    """Everything the cog read about one member. Times are unix seconds; nothing here has
    to be cut to the year already (summarize does that)."""

    voice: Sequence[S.Session] = ()  # everyone's sessions in the channels this member used
    games: Sequence[S.Session] = ()  # the member's own
    message_days: Mapping[str, int] = field(default_factory=dict)  # ISO local day -> messages
    ledger: Sequence[Coin] = ()
    badges: Sequence[tuple[str, int]] = ()  # (key, earned at)
    badge_holders: Mapping[str, int] = field(default_factory=dict)  # key -> holders, whole server
    squads: Sequence[Squad] = ()
    matches: Sequence[Squad] = ()
    gamenights: Sequence[int] = ()  # starts_at of the ones they hosted
    tournaments_entered: Sequence[int] = ()
    tournaments_won: Sequence[int] = ()
    clips: Sequence[int] = ()
    clip_week_wins: Sequence[int] = ()
    hall_of_fame: Sequence[int] = ()
    challenges: Sequence[int] = ()  # weekly challenge claims
    event_quests: Sequence[int] = ()  # seasonal daily-quest claims
    event_drops: Sequence[int] = ()  # seasonal drops grabbed
    words_solved: Sequence[int] = ()
    level: int | None = None  # now
    level_at_start: int | None = None  # snapshot from the start of the year, if one exists
    level_at_end: int | None = None  # snapshot from the start of the next year, if one exists
    joined_at: int | None = None
    house: str | None = None


# ---------------------------------------------------------------- the member's year
@dataclass(frozen=True)
class Teammate:
    user_id: int
    voice_seconds: int
    squads: int  # squads and matches both were in

    @property
    def score(self) -> int:
        return self.voice_seconds + self.squads * SQUAD_WEIGHT


@dataclass(frozen=True)
class Summary:
    user_id: int
    year: int
    voice_seconds: int = 0
    game_seconds: int = 0
    top_games: tuple[tuple[str, int], ...] = ()
    games_played: int = 0  # games with GAME_PLAYED or more
    messages: int = 0
    active_days: int = 0  # days with a message
    busiest_day: tuple[str, int] | None = None  # (ISO day, messages)
    busiest_month: int | None = None  # 1-12
    busiest_month_messages: int = 0
    busiest_month_voice: int = 0
    chat_streak: int = 0  # longest run of days with a message
    daily_streak: int = 0  # longest run of /daily claims
    teammates: tuple[Teammate, ...] = ()
    level: int | None = None
    levels_gained: int | None = None  # None: no start-of-year snapshot to compare with
    coins_earned: int = 0
    biggest_payout: tuple[int, str] | None = None  # (coins, ledger reason)
    bets: int = 0
    badges: tuple[str, ...] = ()  # keys earned this year
    rarest_badge: tuple[str, int] | None = None  # (key, holders)
    squads_hosted: int = 0
    squads_joined: int = 0
    matches: int = 0
    gamenights: int = 0
    tournaments_entered: int = 0
    tournaments_won: int = 0
    clips: int = 0
    clip_week_wins: int = 0
    hall_of_fame: int = 0
    challenges: int = 0
    event_quests: int = 0
    event_drops: int = 0
    words_solved: int = 0
    joined_month: int | None = None  # set when they joined during this year
    house: str | None = None

    @property
    def squads(self) -> int:
        return self.squads_hosted + self.squads_joined + self.matches

    @property
    def quests(self) -> int:
        return self.challenges + self.event_quests

    @property
    def streak(self) -> int:
        return max(self.daily_streak, self.chat_streak)

    @property
    def sparse(self) -> bool:
        """Almost nothing to show: this member gets the short card instead of a card of zeros."""
        return (self.voice_seconds < HOUR and self.game_seconds < HOUR and self.messages < 20
                and self.squads == 0 and self.tournaments_entered == 0 and self.clips == 0
                and len(self.badges) < 3 and self.quests + self.words_solved < 5)


def _count(times: Iterable[int], start: int, end: int) -> int:
    return sum(1 for t in times if start <= t < end)


def _merged(sessions: Iterable[S.Session], start: int, end: int, now: int,
            excluded: Collection[int | str]) -> dict[tuple[int, int | str], list[tuple[int, int]]]:
    """(user, channel) -> merged, sorted spans inside the window."""
    raw: dict[tuple[int, int | str], list[tuple[int, int]]] = defaultdict(list)
    for s in sessions:
        if s.key in excluded:
            continue
        span = S.clip(s, start, end, now)
        if span:
            raw[(s.user_id, s.key)].append(span)
    out = {}
    for key, spans in raw.items():
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(spans):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        out[key] = merged
    return out


def _overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """Seconds two sorted, merged span lists have in common."""
    total = i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            total += hi - lo
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return total


def shared_voice_seconds(sessions: Iterable[S.Session], user_id: int, start: int, end: int, now: int,
                         excluded: Collection[int | str] = ()) -> dict[int, int]:
    """Per other member: seconds spent in the same voice channel as `user_id` in the window."""
    spans = _merged(sessions, start, end, now, excluded)
    mine = {channel: s for (user, channel), s in spans.items() if user == user_id}
    shared: dict[int, int] = defaultdict(int)
    for (user, channel), theirs in spans.items():
        if user == user_id or channel not in mine:
            continue
        secs = _overlap(mine[channel], theirs)
        if secs:
            shared[user] += secs
    return dict(shared)


def teammates(rows: MemberRows, user_id: int, start: int, end: int, now: int, allowed: Collection[int],
              excluded: Collection[int | str] = ()) -> tuple[Teammate, ...]:
    """The member's top teammates by shared time, from `allowed` only; empty unless at
    least MIN_TEAMMATES qualify."""
    voice = shared_voice_seconds(rows.voice, user_id, start, end, now, excluded)
    together: dict[int, int] = defaultdict(int)
    for squad in (*rows.squads, *rows.matches):
        if start <= squad.created_at < end and user_id in squad.members:
            for other in set(squad.members) - {user_id}:
                together[other] += 1
    found = [Teammate(uid, voice.get(uid, 0), together.get(uid, 0))
             for uid in set(voice) | set(together) if uid in allowed and uid != user_id]
    found = sorted((t for t in found if t.score > 0), key=lambda t: (-t.score, t.user_id))
    return tuple(found[:TEAMMATES]) if len(found) >= MIN_TEAMMATES else ()


def is_refund(coin: Coin) -> bool:
    ref = coin.ref or ""
    return ref.startswith("bjrefund:") or ":refund:" in ref


def coins_earned(ledger: Iterable[Coin]) -> int:
    return sum(c.delta for c in ledger if c.delta > 0 and c.reason not in NOT_EARNED and not is_refund(c))


def biggest_payout(ledger: Iterable[Coin]) -> tuple[int, str] | None:
    wins = [(c.delta, c.reason) for c in ledger
            if c.delta > 0 and c.reason in PAYOUT_REASONS and not is_refund(c)]
    return max(wins, key=lambda w: (w[0], w[1])) if wins else None


def badge_name(key: str) -> str | None:
    """A badge's name without its emoji (the card font has none), or None if unknown."""
    found = A.held_badges([key])
    return found[0].name if found else None


def rarest(keys: Sequence[str], holders: Mapping[str, int]) -> tuple[str, int] | None:
    """Of these badges, the one the fewest members hold (first wins a tie)."""
    known = [k for k in keys if badge_name(k) is not None]
    if not known:
        return None
    key = min(known, key=lambda k: (max(holders.get(k, 1), 1), known.index(k)))
    return key, max(holders.get(key, 1), 1)


def _year_days(message_days: Mapping[str, int], year: int) -> dict[date, int]:
    out = {}
    for day, count in message_days.items():
        try:
            d = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if d.year == year and count and count > 0:
            out[d] = int(count)
    return out


def _busiest_month(messages: Mapping[int, int], voice: Mapping[int, int]) -> int | None:
    """The month with the most messages plus voice minutes (the earlier month wins a tie)."""
    scores = {m: messages.get(m, 0) + voice.get(m, 0) // 60 for m in range(1, 13)}
    best = max(scores, key=lambda m: (scores[m], -m))
    return best if scores[best] > 0 else None


def _levels_gained(rows: MemberRows) -> int | None:
    if rows.level_at_start is None:
        return None
    end = rows.level_at_end if rows.level_at_end is not None else rows.level
    return max((end or 0) - rows.level_at_start, 0)


def summarize(user_id: int, year: int, tz: tzinfo, rows: MemberRows, now: int,
              allowed: Collection[int] = (), excluded_channels: Collection[int] = ()) -> Summary:
    """One member's year. `allowed`: members who may be named as teammates (in the server,
    tracking on). `excluded_channels`: voice channels that never count (AFK)."""
    start, end = year_bounds(year, tz)
    excluded = set(excluded_channels)

    voice = S.counted_voice_seconds(rows.voice, start, end, now, excluded_channels=excluded).get(user_id, 0)
    voice_by_month = {}
    if voice:
        for month in range(1, 13):
            lo, hi = month_bounds(year, month, tz)
            voice_by_month[month] = S.counted_voice_seconds(
                rows.voice, lo, hi, now, excluded_channels=excluded).get(user_id, 0)

    own_games = [s for s in rows.games if s.user_id == user_id]
    per_game = S.game_seconds(own_games, start, end, now).get(user_id, {})

    days = _year_days(rows.message_days, year)
    by_month: dict[int, int] = defaultdict(int)
    for d, count in days.items():
        by_month[d.month] += count
    busiest_day = None
    if days:
        top_day = max(days, key=lambda d: (days[d], -d.toordinal()))
        busiest_day = (top_day.isoformat(), days[top_day])
    month = _busiest_month(by_month, voice_by_month)

    ledger = [c for c in rows.ledger if start <= c.at < end]
    daily_days = [local_date(c.at, tz) for c in ledger if c.reason == DAILY_REASON and c.delta > 0]

    earned = [(key, at) for key, at in rows.badges
              if start <= at < end and not key.startswith(WRAPPED_BADGE_PREFIX)]
    order = {b.key: i for i, b in enumerate(A.BADGES)}
    keys = tuple(k for k, _ in sorted(earned, key=lambda ka: (order.get(ka[0], len(order)), ka[0])))

    lfg = [s for s in rows.squads if start <= s.created_at < end and user_id in (*s.members, s.host_id)]
    hosted = sum(1 for s in lfg if s.host_id == user_id)
    joined_month = None
    if rows.joined_at is not None and start <= rows.joined_at < end:
        joined_month = local_date(rows.joined_at, tz).month

    return Summary(
        user_id=user_id, year=year,
        voice_seconds=voice,
        game_seconds=sum(per_game.values()),
        top_games=tuple(S.top_games(per_game, n=TOP_GAMES)),
        games_played=sum(1 for secs in per_game.values() if secs >= GAME_PLAYED),
        messages=sum(days.values()),
        active_days=len(days),
        busiest_day=busiest_day,
        busiest_month=month,
        busiest_month_messages=by_month.get(month, 0) if month else 0,
        busiest_month_voice=voice_by_month.get(month, 0) if month else 0,
        chat_streak=longest_streak(days),
        daily_streak=longest_streak(daily_days),
        teammates=teammates(rows, user_id, start, end, now, allowed, excluded),
        level=rows.level,
        levels_gained=_levels_gained(rows),
        coins_earned=coins_earned(ledger),
        biggest_payout=biggest_payout(ledger),
        bets=sum(1 for c in ledger if c.delta < 0 and c.reason in BET_REASONS),
        badges=keys,
        rarest_badge=rarest(keys, rows.badge_holders),
        squads_hosted=hosted,
        squads_joined=len(lfg) - hosted,
        matches=sum(1 for m in rows.matches if start <= m.created_at < end and user_id in m.members),
        gamenights=_count(rows.gamenights, start, end),
        tournaments_entered=_count(rows.tournaments_entered, start, end),
        tournaments_won=_count(rows.tournaments_won, start, end),
        clips=_count(rows.clips, start, end),
        clip_week_wins=_count(rows.clip_week_wins, start, end),
        hall_of_fame=_count(rows.hall_of_fame, start, end),
        challenges=_count(rows.challenges, start, end),
        event_quests=_count(rows.event_quests, start, end),
        event_drops=_count(rows.event_drops, start, end),
        words_solved=_count(rows.words_solved, start, end),
        joined_month=joined_month,
        house=rows.house or None,
    )


# ---------------------------------------------------------------- personas
@dataclass(frozen=True)
class Persona:
    key: str
    title: str
    line: str  # the playful sentence under the title
    rule: str  # how it's picked, in words (tests and the docs keep it honest)
    check: Callable[[Summary], bool]


def _top_share(s: Summary) -> float:
    return s.top_games[0][1] / s.game_seconds if s.top_games and s.game_seconds else 0.0


# In priority order: the first persona whose rule holds is the member's title.
PERSONAS: tuple[Persona, ...] = (
    Persona("champion", "The Champion", "Took a bracket all the way. The trophy shelf says thanks.",
            "won a tournament", lambda s: s.tournaments_won >= 1),
    Persona("captain", "Squad Captain", "If there was a lobby, you probably opened it.",
            "hosted 5+ squads and game nights", lambda s: s.squads_hosted + s.gamenights >= 5),
    Persona("director", "The Director", "Clip it or it didn't happen.",
            "posted 10+ clips or won Clip of the Week", lambda s: s.clips >= 10 or s.clip_week_wins >= 1),
    Persona("voice", "Voice of the Lobby", "Mic on, door open, always room for one more.",
            "50+ hours in voice and more voice minutes than messages",
            lambda s: s.voice_seconds >= 50 * HOUR and s.voice_seconds // 60 >= s.messages),
    Persona("chat", "Group Chat Engine", "The chat scrolls because you type.",
            "1,000+ messages", lambda s: s.messages >= 1000),
    Persona("loyalist", "One-Game Loyalist", "You found the one and stayed.",
            "20+ hours in games, 70% or more of it in one game",
            lambda s: s.game_seconds >= 20 * HOUR and _top_share(s) >= 0.7),
    Persona("sampler", "The Sampler", "A library bigger than the backlog admits.",
            "an hour or more in 5+ different games", lambda s: s.games_played >= 5),
    Persona("teamplayer", "Team Player", "Always a yes when someone asks for one more.",
            "joined 8+ squads and matches", lambda s: s.squads_joined + s.matches >= 8),
    Persona("collector", "Badge Hunter", "Achievement unlocked. Repeatedly.",
            "earned 6+ badges", lambda s: len(s.badges) >= 6),
    Persona("streak", "Streak Keeper", "Showed up every day. The daily never stood a chance.",
            "a 14+ day /daily or chat streak", lambda s: s.streak >= 14),
    Persona("wordsmith", "Wordsmith", "Five letters, no fear.",
            "solved 20+ Daily Words", lambda s: s.words_solved >= 20),
    Persona("quester", "Quest Runner", "If it had a Claim button, you pressed it.",
            "10+ challenges, event quests and drops", lambda s: s.quests + s.event_drops >= 10),
    Persona("regular", "The Regular", "Part of the furniture, in the best way.",
            "chatted on 30+ days or 10+ hours in voice",
            lambda s: s.active_days >= 30 or s.voice_seconds >= 10 * HOUR),
    Persona("fresh", "Fresh Ink", "New page, good start.",
            "joined this year", lambda s: s.joined_month is not None),
    Persona("quiet", "Quiet Corner", "Around when it counted.",
            "everyone else", lambda s: True),
)
PERSONA_BY_KEY = {p.key: p for p in PERSONAS}


def persona(summary: Summary) -> Persona:
    return next(p for p in PERSONAS if p.check(summary))


# ---------------------------------------------------------------- the server's year
@dataclass(frozen=True)
class ServerRows:
    voice: Sequence[S.Session] = ()
    games: Sequence[S.Session] = ()
    messages: Sequence[tuple[int, str, int]] = ()  # (user, ISO local day, count)
    ledger: Sequence[Coin] = ()
    badges: Sequence[tuple[int, str, int]] = ()  # (user, key, earned at)
    squads: Sequence[Squad] = ()
    matches: Sequence[Squad] = ()
    gamenights: Sequence[int] = ()
    tournaments: Sequence[int] = ()
    clips: Sequence[tuple[int, int]] = ()  # (user, posted at)
    joins: Sequence[tuple[int, int]] = ()  # (user, joined at)


@dataclass(frozen=True)
class Superlative:
    key: str
    title: str
    user_ids: tuple[int, ...]  # one winner, or two who tied
    detail: str


@dataclass(frozen=True)
class ServerSummary:
    year: int
    messages: int = 0
    voice_seconds: int = 0  # everyone's counted voice, added up
    game_seconds: int = 0
    top_games: tuple[tuple[str, int], ...] = ()
    squads: int = 0  # /lfg squads and matches
    gamenights: int = 0
    tournaments: int = 0
    clips: int = 0
    badges: int = 0
    coins_earned: int = 0
    new_members: int = 0
    active_members: int = 0  # members with any voice, game time or messages
    busiest_month: int | None = None
    superlatives: tuple[Superlative, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.messages or self.voice_seconds or self.game_seconds or self.squads
                    or self.clips or self.badges or self.coins_earned)


def _superlative(key: str, title: str, scores: Mapping[int, int], allowed: Collection[int], least: int,
                 show: Callable[[int], str]) -> Superlative | None:
    """The top scorer among `allowed` (two share it on a tie; a wider tie names nobody).
    Always a "most", never a "least"."""
    scores = {u: n for u, n in scores.items() if u in allowed and n >= least}
    if not scores:
        return None
    best = max(scores.values())
    winners = tuple(sorted(u for u, n in scores.items() if n == best))
    if len(winners) > 2:
        return None
    return Superlative(key, title, winners, show(best))


def fmt_hours(seconds: int) -> str:
    """Hours for a card: one decimal under ten ("7.5"), whole above ("1,204")."""
    hours = max(seconds, 0) / HOUR
    if hours < 10:
        text = f"{hours:.1f}"
        return text[:-2] if text.endswith(".0") else text
    return f"{round(hours):,}"


def summarize_server(year: int, tz: tzinfo, rows: ServerRows, now: int, allowed: Collection[int] = (),
                     excluded_channels: Collection[int] = ()) -> ServerSummary:
    """The whole server's year: totals from everything recorded, and shout-outs that only
    ever name members in `allowed` (in the server, tracking on)."""
    start, end = year_bounds(year, tz)
    excluded = set(excluded_channels)
    voice = S.counted_voice_seconds(rows.voice, start, end, now, excluded_channels=excluded)
    per_user_games = S.game_seconds(rows.games, start, end, now)
    per_game: dict[str, int] = defaultdict(int)
    for games in per_user_games.values():
        for game, secs in games.items():
            per_game[game] += secs
    gaming = S.gaming_totals(per_user_games)

    messages: dict[int, int] = defaultdict(int)
    by_month: dict[int, int] = defaultdict(int)
    for user, day, count in rows.messages:
        for d, n in _year_days({day: count}, year).items():
            messages[user] += n
            by_month[d.month] += n
    voice_by_month = {}
    if voice:
        for month in range(1, 13):
            lo, hi = month_bounds(year, month, tz)
            voice_by_month[month] = sum(S.counted_voice_seconds(
                rows.voice, lo, hi, now, excluded_channels=excluded).values())

    ledger = [c for c in rows.ledger if start <= c.at < end]
    daily: dict[int, list[date]] = defaultdict(list)
    for c in ledger:
        if c.reason == DAILY_REASON and c.delta > 0:
            daily[c.user_id].append(local_date(c.at, tz))
    streaks = {u: longest_streak(days) for u, days in daily.items()}

    badges: dict[int, int] = defaultdict(int)
    for user, key, at in rows.badges:
        if start <= at < end and not key.startswith(WRAPPED_BADGE_PREFIX):
            badges[user] += 1
    lfg = [s for s in rows.squads if start <= s.created_at < end]
    hosted: dict[int, int] = defaultdict(int)
    for s in lfg:
        if s.host_id is not None and len(set(s.members) - {s.host_id}) >= 1:  # someone came
            hosted[s.host_id] += 1
    clips: dict[int, int] = defaultdict(int)
    for user, at in rows.clips:
        if start <= at < end:
            clips[user] += 1

    candidates = (
        _superlative("voice", "Most hours in voice", voice, allowed, HOUR, lambda n: f"{fmt_hours(n)} h"),
        _superlative("chat", "Kept the chat moving", messages, allowed, 50, lambda n: plural(n, "message")),
        _superlative("gaming", "Most hours in game", gaming, allowed, HOUR, lambda n: f"{fmt_hours(n)} h"),
        _superlative("host", "Squad captain", hosted, allowed, 2, lambda n: f"{plural(n, 'squad')} hosted"),
        _superlative("clips", "Director's chair", clips, allowed, 2, lambda n: plural(n, "clip")),
        _superlative("badges", "Badge hunter", badges, allowed, 2, lambda n: plural(n, "badge")),
        _superlative("streak", "Longest /daily streak", streaks, allowed, 3, lambda n: plural(n, "day")),
    )
    return ServerSummary(
        year=year,
        messages=sum(messages.values()),
        voice_seconds=sum(voice.values()),
        game_seconds=sum(per_game.values()),
        top_games=tuple(S.top_games(per_game, n=TOP_GAMES)),
        squads=len(lfg) + _count((m.created_at for m in rows.matches), start, end),
        gamenights=_count(rows.gamenights, start, end),
        tournaments=_count(rows.tournaments, start, end),
        clips=sum(clips.values()),
        badges=sum(badges.values()),
        coins_earned=coins_earned(ledger),
        new_members=len({u for u, at in rows.joins if start <= at < end}),
        active_members=len(set(voice) | set(gaming) | set(messages)),
        busiest_month=_busiest_month(by_month, voice_by_month),
        superlatives=tuple(s for s in candidates if s is not None),
    )


# ---------------------------------------------------------------- cache, cooldown, sharing
COOLDOWN = 30  # seconds between /wrapped runs per member
SHARE_COOLDOWN = 6 * HOUR  # and between public shares of the same card
CACHE_SIZE = 64  # rendered cards kept in memory


def digest(*parts) -> str:
    """A stable fingerprint of what a card shows: the same parts give the same card, so
    it is drawn once until something changes."""
    return hashlib.sha256(repr(parts).encode("utf-8", "surrogatepass")).hexdigest()


def cooldown_left(last: int | None, now: int, cooldown: int = COOLDOWN) -> int:
    """Seconds until the member may go again (0: now)."""
    return 0 if last is None else max(last + cooldown - now, 0)


def share_key(year: int, user_id: int) -> str:
    """meta key holding when this member last shared their card for `year`."""
    return f"wrapped:shared:{year}:{user_id}"


def share_keys_like(user_id: int) -> str:
    """SQL LIKE pattern for this member's share_key of every year."""
    return f"wrapped:shared:%:{user_id}"


def badge_key(year: int) -> str:
    return f"{WRAPPED_BADGE_PREFIX}{year}"


LEVELS_PREFIX = "wrapped:levels:"


def levels_key(year: int) -> str:
    """meta key for the level snapshot taken at the start of `year`."""
    return f"{LEVELS_PREFIX}{year}"


def snapshot_due(now: int, tz: tzinfo) -> int | None:
    """The year whose start-of-year level snapshot may be taken now: only in January, so a
    snapshot is never a mid-year number passed off as the start."""
    today = local_date(now, tz)
    return today.year if today.month == 1 else None


OPTOUT_SELF = ("You've turned stat tracking off with `/privacy`, so Front Desk kept nothing to wrap up, "
               "and that's a fine way to spend a year. Turn it on with `/privacy tracking:on` and there'll "
               "be a card for you next December.")


def optout_other(name: str) -> str:
    return f"{name} has stat tracking turned off, so there's no card to show."


# ---------------------------------------------------------------- the two posts
@dataclass(frozen=True)
class Annual:
    """A job that runs once a year at a local wall-clock time, and is dropped rather than
    run if the bot only gets to it `late_days` or more afterwards."""

    name: str
    month: int
    day: int
    hour: int
    minute: int
    late_days: int


@dataclass(frozen=True)
class YearPeriod:
    key: str  # f"{name}:{year}"
    year: int
    scheduled_at: int
    expires_at: int


ANNOUNCE_JOB = Annual("wrapped", 12, 1, 10, 0, late_days=45)  # still worth saying until it closes
REMINDER_JOB = Annual("wrappedreminder", 12, 20, 10, 0, late_days=14)
REMINDER_GAP = 7 * 24 * HOUR  # no reminder this soon after a late announcement


def _occurrence(job: Annual, year: int, tz: tzinfo) -> YearPeriod:
    at = datetime.combine(date(year, job.month, job.day), time(job.hour, job.minute), tz)
    return YearPeriod(f"{job.name}:{year}", year, int(at.timestamp()),
                      int((at + timedelta(days=job.late_days)).timestamp()))


def latest_due_annual(job: Annual, now: int, tz: tzinfo) -> YearPeriod:
    period = _occurrence(job, local_date(now, tz).year, tz)
    return period if period.scheduled_at <= now else _occurrence(job, period.year - 1, tz)


def plan_annual(job: Annual, now: int, tz: tzinfo, done_keys: Collection[str], first_seen: int | None) -> Plan:
    """schedule.plan for a yearly job: run only the latest due period, never one from
    before the bot first saw the job, never twice, and not at all once it is too late."""
    latest = latest_due_annual(job, now, tz)
    if latest.key in done_keys:
        return Plan(None, [])
    if first_seen is None or latest.scheduled_at < first_seen or now >= latest.expires_at:
        return Plan(None, [latest])
    return Plan(latest, [])


def announce_text(year: int, with_card: bool = True) -> str:
    text = (f"📓 **Wrapped {year} is open.** Your year here on one card: top games, voice hours, "
            f"favourite teammates and a title you didn't ask for. Run `/wrapped` (only you see it, "
            f"until you press Share). Open until January 15.")
    return text + ("\nHere's how the whole server's year went:" if with_card else "")


def reminder_text(year: int) -> str:
    return (f"📓 **Wrapped {year}** is still open: run `/wrapped` for your year in review, "
            f"and share it if you like the title you got. It closes after January 15.")


def share_text(mention: str, year: int, title: str) -> str:
    return f"📓 {mention}'s Wrapped {year}: **{title}**"


def label_for(reason: str) -> str:
    """A ledger reason as members know it ("blackjack" -> "Blackjack")."""
    return econstats.label(reason)
