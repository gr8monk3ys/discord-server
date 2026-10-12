"""Houses: three teams, a weekly House Cup and a season (calendar quarter) trophy.

Every member belongs to one of the three houses in server/layout.py. Nothing is counted
here: a house's points for a week are derived from the tables the other modules already
keep, so this module never hooks into another cog.

The formula (tested in tests/test_houses.py)
--------------------------------------------
1. Units. For one member and one local Monday-Sunday week, each source below yields a
   number of units, clipped to that source's weekly cap:

       source               points  cap/week  read from
       days chatted            3       7      message_counts (a day counts at ACTIVE_DAY_MESSAGES)
       voice hours             2      10      voice_sessions, only time with established company
       squads joined           5       3      lfg_members, someone else's post, established host
       daily words solved      2       7      word_games
       challenges claimed      5       4      challenge_claims
       tournaments entered     5       2      tournament_entries of a started tournament
       tournaments won        15       1      ledger refs tourney:<id>:first
       hall of fame posts      8       2      starboard (posted, not pending)
       badges earned           3       3      achievements
       starter quest steps     2       5      quest_steps

   These are the sources that are already alt-resistant where they are recorded: a chat day
   is presence, not volume (spam earns nothing extra), voice needs a second established
   account, squads need an established host, the rest are one-per-day or one-per-week by
   construction. XP is left out on purpose: the xp table only keeps a running total, so a
   week's XP cannot be derived from it (and it is the same chat and voice anyway).
2. Member points = min(MEMBER_CAP, sum(units * points)). The cap is below the sum of the
   per-source maximums, so one hyperactive member cannot carry a house.
3. Who counts. Only members with an active house row who allow tracking (/privacy), whose
   Discord account is established (quests.established) and, when the caller knows who is in
   the server, who are still there. Everyone else neither scores nor is counted in a roster.
4. House score = total member points * SCALE // divisor, shown as "points per member".
   divisor = max(roster size, largest roster - 1, 1): per-capita, so a bigger house does not
   win by headcount, and a house that shrank is never divided by fewer than the balanced size,
   so a tiny house cannot win on one member's average either.
5. Rank: score, then members who contributed, then total points, then a rotation seeded by
   the period key (so an exact tie does not always favour the same house). A week with no
   points has no winner.

Switching cannot be gamed: a week is scored once, when it closes, with the membership at
that moment, and the result is frozen in `house_results`. Members are placed in the smallest
house and never pick one; leaving starts a REJOIN_COOLDOWN before /house join works again.

Season: each closed week gives the houses 3/2/1 cup points by rank (nothing for a house
with no points). The season tally is the sum over the closed weeks whose Sunday falls in
the quarter, so a new quarter starts at zero without anything being reset by hand.

Read API for other modules (no cog internals needed): `standings(db, week, now)`,
`season_standings(db, season)`, `house_of(db, user_id)` plus the pure helpers here.
Pure apart from those async helpers, which only read the database they are given.
"""

import json
import zlib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, tzinfo

import config
from logic import challenges as CH
from logic import quests as Q
from logic import recap as R
from logic import stats as S
from logic.schedule import Weekly, latest_due
from logic.starboard import PENDING

DAY = 24 * 60 * 60
HOUR = 60 * 60
MEMBER_CAP = 100  # points one member can bring their house in a week
SCALE = 10  # scores are tenths of a point per member
ACTIVE_DAY_MESSAGES = 5  # messages that make a local day a "day chatted"
CUP_POINTS = (3, 2, 1)  # season points for a week's 1st/2nd/3rd
REJOIN_COOLDOWN = 30 * DAY  # after /house leave
WIN_REWARD = 100  # coins to each paid member of the week's winning house
MIN_PAID_POINTS = 10  # a member needs this many points that week to be paid
MIN_PAYEES = 2  # nobody is paid unless this many housemates qualify (no solo farming)
REASON = "housecup"
HISTORY_WEEKS = 8  # weekly winners shown by /house history
WEEKLY_JOB = Weekly("housecup", weekday=0, hour=10, minute=10)  # Mondays 10:10, after the recap
SEASON_JOB = R.Monthly("houseseason", day=1, hour=10, minute=20)  # the 1st; acts on quarter ends
MEDALS = ("🥇", "🥈", "🥉")

Week = CH.Week
week_of = CH.week_of
previous_week = CH.previous_week


# ---------------------------------------------------------------- houses
@dataclass(frozen=True)
class House:
    key: str  # stored in the database
    name: str
    emoji: str
    color: int
    role: str  # the Discord role name

    @property
    def title(self) -> str:
        return f"{self.emoji} House {self.name}"


HOUSES: tuple[House, ...] = tuple(House(k, n, e, c, config.HOUSE_ROLES[k]) for k, n, e, c in config.HOUSES)
BY_KEY = {h.key: h for h in HOUSES}


def _order(period: str) -> dict[str, int]:
    """House key -> tie-break position for `period`: the fixed order, rotated by the key."""
    offset = zlib.crc32(period.encode("utf-8")) % len(HOUSES)
    return {h.key: (i - offset) % len(HOUSES) for i, h in enumerate(HOUSES)}


# ---------------------------------------------------------------- membership
def sizes(membership: Mapping[int, str]) -> dict[str, int]:
    out = {h.key: 0 for h in HOUSES}
    for house in membership.values():
        if house in out:
            out[house] += 1
    return out


def place(user_id: int, house_sizes: Mapping[str, int]) -> str:
    """The house a new member goes to: the smallest one. Ties are broken by the account's
    creation time (the id's timestamp bits), so the answer is the same on every run."""
    smallest = min(house_sizes.get(h.key, 0) for h in HOUSES)
    options = [h.key for h in HOUSES if house_sizes.get(h.key, 0) == smallest]
    return options[(user_id >> 22) % len(options)]


def rejoin_at(left_at: int) -> int:
    return left_at + REJOIN_COOLDOWN


def can_rejoin(left_at: int, now: int) -> bool:
    return now >= rejoin_at(left_at)


# ---------------------------------------------------------------- points
@dataclass(frozen=True)
class Source:
    key: str
    label: str  # shown in /house me
    points: int  # per unit
    cap: int  # units counted per week


SOURCES: tuple[Source, ...] = (
    Source("active_days", "days chatted", 3, 7),
    Source("voice_hours", "hours in voice with others", 2, 10),
    Source("squads", "squads joined", 5, 3),
    Source("word_games", "daily words solved", 2, 7),
    Source("challenges", "challenges claimed", 5, 4),
    Source("tournament_entries", "tournaments entered", 5, 2),
    Source("tournament_wins", "tournaments won", 15, 1),
    Source("hall_of_fame", "hall of fame posts", 8, 2),
    Source("badges", "badges earned", 3, 3),
    Source("quest_steps", "starter quest steps", 2, 5),
)


def breakdown(units: Mapping[str, int]) -> list[tuple[Source, int, int]]:
    """(source, units counted, points) for every source that scored, in table order."""
    out = []
    for s in SOURCES:
        n = max(0, min(int(units.get(s.key, 0) or 0), s.cap))
        if n:
            out.append((s, n, n * s.points))
    return out


def member_points(units: Mapping[str, int]) -> int:
    return min(MEMBER_CAP, sum(points for _, _, points in breakdown(units)))


@dataclass(frozen=True)
class Standing:
    house: str
    score: int  # total * SCALE // divisor
    total: int  # sum of the members' capped points
    members: int  # roster size
    contributors: int  # members with at least one point
    rank: int = 0  # 1 = first


def score_houses(membership: Mapping[int, str], points: Mapping[int, int], period: str) -> list[Standing]:
    """Standings for one period, best first. `membership` is the eligible roster (user ->
    house key); points of anyone not in it are ignored."""
    roster = sizes(membership)
    divisor_floor = max(max(roster.values()) - 1, 1)
    total = {h.key: 0 for h in HOUSES}
    scorers = {h.key: 0 for h in HOUSES}
    for uid, house in membership.items():
        mine = min(MEMBER_CAP, max(0, int(points.get(uid, 0))))
        if house in total and mine > 0:
            total[house] += mine
            scorers[house] += 1
    order = _order(period)
    rows = [Standing(h.key, total[h.key] * SCALE // max(roster[h.key], divisor_floor), total[h.key],
                     roster[h.key], scorers[h.key]) for h in HOUSES]
    rows.sort(key=lambda s: (-s.score, -s.contributors, -s.total, order[s.house]))
    return [Standing(s.house, s.score, s.total, s.members, s.contributors, i + 1) for i, s in enumerate(rows)]


def winner(standings: Iterable[Standing]) -> Standing | None:
    """The first-placed house, or None when nobody scored."""
    first = next(iter(sorted(standings, key=lambda s: s.rank)), None)
    return first if first is not None and first.total > 0 else None


def cup_points(standings: Iterable[Standing]) -> dict[str, int]:
    """Season points this week's standings are worth, per house."""
    out = {h.key: 0 for h in HOUSES}
    for s in standings:
        if s.total > 0 and 1 <= s.rank <= len(CUP_POINTS) and s.house in out:
            out[s.house] = CUP_POINTS[s.rank - 1]
    return out


def payees(membership: Mapping[int, str], points: Mapping[int, int], house: str | None) -> list[int]:
    """Members of the winning house who are paid: at least MIN_PAID_POINTS that week, and
    only when MIN_PAYEES of them qualify (one person alone earns the house a win, not coins)."""
    if house is None:
        return []
    out = sorted(uid for uid, h in membership.items() if h == house and points.get(uid, 0) >= MIN_PAID_POINTS)
    return out if len(out) >= MIN_PAYEES else []


def reward_ref(week_key: str, user_id: int) -> str:
    return f"{REASON}:{week_key}:{user_id}"


# ---------------------------------------------------------------- seasons
def season_of(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def week_season(week: Week) -> str:
    """A week belongs to the quarter its Sunday falls in."""
    return season_of(week.last_day)


def season_closing(month: str) -> str | None:
    """The season that ends with month "YYYY-MM", or None when a quarter doesn't end there."""
    year, m = (int(x) for x in month.split("-"))
    return f"{year}-Q{m // 3}" if m % 3 == 0 else None


def season_label(season: str) -> str:
    year, q = season.split("-Q")
    return f"Q{q} {year}"


@dataclass(frozen=True)
class Tally:
    house: str
    cup: int  # season points
    wins: int  # weeks won
    score: int  # sum of weekly scores (tie-break)
    rank: int = 0


def season_tally(weeks: Iterable[list[Standing]], season: str) -> list[Tally]:
    """The season table from its closed weeks' standings, best first."""
    cup = {h.key: 0 for h in HOUSES}
    wins = dict(cup)
    score = dict(cup)
    for standings in weeks:
        for house, n in cup_points(standings).items():
            cup[house] += n
        first = winner(standings)
        if first is not None and first.house in wins:
            wins[first.house] += 1
        for s in standings:
            if s.house in score:
                score[s.house] += s.score
    order = _order(season)
    rows = sorted(HOUSES, key=lambda h: (-cup[h.key], -wins[h.key], -score[h.key], order[h.key]))
    return [Tally(h.key, cup[h.key], wins[h.key], score[h.key], i + 1) for i, h in enumerate(rows)]


def champion(tally: Iterable[Tally]) -> Tally | None:
    first = next(iter(sorted(tally, key=lambda t: t.rank)), None)
    return first if first is not None and first.cup > 0 else None


# ---------------------------------------------------------------- frozen results
def dump_week(standings: Iterable[Standing], paid: Iterable[int]) -> str:
    return json.dumps({"standings": [[s.house, s.score, s.total, s.members, s.contributors, s.rank]
                                     for s in standings], "paid": list(paid)})


def load_week(data: str) -> tuple[list[Standing], list[int]]:
    """(standings, paid member ids) from a house_results row; unknown houses are dropped."""
    try:
        raw = json.loads(data)
        rows = [Standing(str(h), int(score), int(total), int(members), int(scorers), int(rank))
                for h, score, total, members, scorers, rank in raw.get("standings", [])]
        paid = [int(u) for u in raw.get("paid", [])]
    except (ValueError, TypeError, AttributeError):
        return [], []
    return [s for s in rows if s.house in BY_KEY], paid


def dump_season(tally: Iterable[Tally]) -> str:
    return json.dumps({"tally": [[t.house, t.cup, t.wins, t.score, t.rank] for t in tally]})


def load_season(data: str) -> list[Tally]:
    try:
        rows = [Tally(str(h), int(cup), int(wins), int(score), int(rank))
                for h, cup, wins, score, rank in json.loads(data).get("tally", [])]
    except (ValueError, TypeError, AttributeError):
        return []
    return [t for t in rows if t.house in BY_KEY]


# ---------------------------------------------------------------- text
def fmt_score(score: int) -> str:
    return f"{score / SCALE:.1f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def standings_text(standings: Iterable[Standing]) -> str:
    """One line per house. No member names: a standings post can't ping or single anyone out."""
    lines = []
    for s in sorted(standings, key=lambda s: s.rank):
        house = BY_KEY[s.house]
        medal = MEDALS[s.rank - 1] if s.total > 0 and s.rank <= len(MEDALS) else "▫️"
        lines.append(f"{medal} {house.emoji} **{house.name}** · **{fmt_score(s.score)}** per member"
                     f" · {s.contributors} of {_plural(s.members, 'member')} chipped in")
    return "\n".join(lines)


def season_text(tally: Iterable[Tally]) -> str:
    lines = []
    for t in sorted(tally, key=lambda t: t.rank):
        house = BY_KEY[t.house]
        medal = MEDALS[t.rank - 1] if t.cup > 0 and t.rank <= len(MEDALS) else "▫️"
        lines.append(f"{medal} {house.emoji} **{house.name}** · **{t.cup}** cup "
                     f"{'point' if t.cup == 1 else 'points'} · {_plural(t.wins, 'week')} won")
    return "\n".join(lines)


def contribution_text(units: Mapping[str, int]) -> str:
    rows = breakdown(units)
    if not rows:
        return "Nothing yet this week. Chat, hop in voice, join a squad or solve the daily word."
    raw = sum(points for _, _, points in rows)
    lines = [f"**{points}** · {n} {s.label}" for s, n, points in rows]
    total = member_points(units)
    capped = f" (capped from {raw})" if raw > total else ""
    lines.append(f"Total: **{total}** of {MEMBER_CAP} points{capped}")
    return "\n".join(lines)


def how_text() -> str:
    return ("Points come from what you already do here: chatting on different days, voice time with "
            "others, squads, the daily word, challenges, tournaments, the hall of fame and badges. "
            f"Each member brings at most {MEMBER_CAP} a week and the score is per member, so every "
            "housemate matters.")


def intro_text() -> str:
    """The one-time post when the first members are sorted. Names houses only, never a member."""
    names = [h.title for h in HOUSES]
    return "\n".join([
        f"Everyone here now belongs to one of {len(HOUSES)} houses: {', '.join(names[:-1])} and {names[-1]}. "
        "You were sorted so the houses stay the same size; nobody picks one.", "",
        "Your house scores from what you already do here: chatting on different days, voice time with "
        "others, squads, the daily word, challenges and tournaments. Each week the house with the most "
        f"points per member takes the House Cup, and its active members get {WIN_REWARD} coins each.", "",
        "`/house me` shows your house, this week's standings and what you've brought in.",
        f"`/house leave` takes you out of the houses (you can join again after {REJOIN_COOLDOWN // DAY} days).",
    ])


def winner_text(week: Week, standings: list[Standing], paid: Collection[int]) -> str:
    first = winner(standings)
    house = BY_KEY[first.house]
    lines = [f"{house.title} takes the House Cup for {R.week_label(week.first_day)}.", "",
             standings_text(standings), ""]
    if paid:
        lines.append(f"**{WIN_REWARD} coins** each to " + " ".join(f"<@{u}>" for u in paid) + ".")
    else:
        lines.append(f"No coins this week: a win pays once {MIN_PAYEES} housemates each bring "
                     f"{MIN_PAID_POINTS}+ points.")
    lines.append("See where your house stands with `/house me`.")
    return "\n".join(lines)


def trophy_text(season: str, tally: list[Tally]) -> str:
    first = champion(tally)
    house = BY_KEY[first.house]
    return "\n".join([f"🏆 {house.title} wins the {season_label(season)} season trophy.", "",
                      season_text(tally), "", "A new season starts now, and every house is back to zero."])


def history_text(seasons: Iterable[tuple[str, str | None]], weeks: Iterable[tuple[str, str | None]]) -> str:
    """seasons / weeks: (period, winning house key or None), newest first."""
    lines = []
    for period, key in seasons:
        who = BY_KEY[key].title if key in BY_KEY else "no winner"
        lines.append(f"🏆 **House Cup · {season_label(period)}**: {who}")
    week_lines = []
    for period, key in weeks:
        who = f"{BY_KEY[key].emoji} {BY_KEY[key].name}" if key in BY_KEY else "no winner"
        week_lines.append(f"`{period}` {who}")
    if week_lines:
        lines += ([""] if lines else []) + ["**Recent weeks**", *week_lines]
    return "\n".join(lines) or "No House Cup has been decided yet. The first one closes next Monday."


# ---------------------------------------------------------------- reading the database
async def house_of(db, user_id: int) -> str | None:
    """The member's current house key, or None."""
    row = await db.fetchone("SELECT house FROM house_members WHERE user_id = ? AND left_at IS NULL", (user_id,))
    return row["house"] if row and row["house"] in BY_KEY else None


async def roster(db, now: int, present: Collection[int] | None = None) -> dict[int, str]:
    """user -> house for everyone who counts right now (rule 3 in the module docstring).
    `present`: ids currently in the server, or None when that isn't known."""
    rows = await db.fetchall("SELECT user_id, house FROM house_members WHERE left_at IS NULL"
                             " AND user_id NOT IN (SELECT user_id FROM privacy_optout)")
    return {r["user_id"]: r["house"] for r in rows
            if r["house"] in BY_KEY and Q.established(r["user_id"], now)
            and (present is None or r["user_id"] in present)}


async def _counts(db, sql: str, params: tuple) -> dict[int, int]:
    return {r["u"]: r["n"] for r in await db.fetchall(sql, params) if r["u"] is not None}


async def units(db, week: Week, now: int, excluded_channels: Collection[int] = (),
                bot_ids: Collection[int] = ()) -> dict[int, dict[str, int]]:
    """Per user, raw units per source for `week` (uncapped; nobody is filtered out here)."""
    w = (week.start, week.end)
    days = (week.first_day.isoformat(), week.last_day.isoformat())
    found: dict[str, dict[int, int]] = {}
    found["active_days"] = await _counts(
        db, "SELECT user_id AS u, COUNT(*) AS n FROM message_counts WHERE day >= ? AND day <= ? AND count >= ?"
        " GROUP BY user_id", (*days, ACTIVE_DAY_MESSAGES))
    rows = await db.fetchall('SELECT user_id, channel_id, start, "end" FROM voice_sessions'
                             ' WHERE start < ? AND ("end" IS NULL OR "end" > ?)', (week.end, week.start))
    sessions = [S.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in rows]
    # Fresh accounts (likely alts) are neither scored nor company: time with only them is alone.
    fresh = {s.user_id for s in sessions if not Q.established(s.user_id, now)}
    seconds = S.counted_voice_seconds(sessions, week.start, week.end, now,
                                      excluded_channels=set(excluded_channels), bot_ids=set(bot_ids) | fresh)
    found["voice_hours"] = {u: s // HOUR for u, s in seconds.items()}
    found["squads"] = await _counts(
        db, "SELECT m.user_id AS u, COUNT(DISTINCT m.post_id) AS n FROM lfg_members m"
        " JOIN lfg_posts p ON p.id = m.post_id WHERE m.user_id != p.host_id AND m.joined_at >= ? AND m.joined_at < ?"
        f" AND {Q.established_sql('p.host_id', 'p.created_at')} GROUP BY m.user_id", w)
    found["word_games"] = await _counts(
        db, "SELECT user_id AS u, COUNT(*) AS n FROM word_games WHERE solved = 1 AND day >= ? AND day <= ?"
        " GROUP BY user_id", days)
    # By claim time, not week key: a claim made in Monday's grace period counts for the new week.
    found["challenges"] = await _counts(
        db, "SELECT user_id AS u, COUNT(*) AS n FROM challenge_claims WHERE at >= ? AND at < ? GROUP BY user_id", w)
    found["tournament_entries"] = await _counts(
        db, "SELECT e.user_id AS u, COUNT(DISTINCT e.tournament_id) AS n FROM tournament_entries e"
        " JOIN tournaments t ON t.id = e.tournament_id WHERE e.joined_at >= ? AND e.joined_at < ?"
        " AND t.status IN ('running', 'done') GROUP BY e.user_id", w)
    found["tournament_wins"] = await _counts(
        db, "SELECT user_id AS u, COUNT(*) AS n FROM ledger WHERE ref LIKE 'tourney:%:first' AND delta > 0"
        " AND at >= ? AND at < ? GROUP BY user_id", w)
    found["hall_of_fame"] = await _counts(
        db, "SELECT author_id AS u, COUNT(*) AS n FROM starboard WHERE at >= ? AND at < ?"
        " AND board_message_id IS NOT NULL AND board_message_id != ? GROUP BY author_id", (*w, PENDING))
    found["badges"] = await _counts(
        db, "SELECT user_id AS u, COUNT(*) AS n FROM achievements WHERE at >= ? AND at < ? GROUP BY user_id", w)
    found["quest_steps"] = await _counts(
        db, "SELECT user_id AS u, COUNT(*) AS n FROM quest_steps WHERE at >= ? AND at < ? GROUP BY user_id", w)
    out: dict[int, dict[str, int]] = {}
    for key, per_user in found.items():
        for uid, n in per_user.items():
            if n:
                out.setdefault(uid, {})[key] = n
    return out


@dataclass(frozen=True)
class Live:
    standings: list[Standing]
    membership: dict[int, str]  # the eligible roster
    units: dict[int, dict[str, int]]  # roster members only
    points: dict[int, int]


async def live(db, week: Week, now: int, present: Collection[int] | None = None,
               excluded_channels: Collection[int] = (), bot_ids: Collection[int] = ()) -> Live:
    """Standings for `week` computed from the tables right now, with today's membership."""
    membership = await roster(db, now, present)
    raw = await units(db, week, now, excluded_channels, bot_ids)
    mine = {uid: raw[uid] for uid in membership if uid in raw}
    points = {uid: member_points(u) for uid, u in mine.items()}
    return Live(score_houses(membership, points, week.key), membership, mine, points)


async def frozen_week(db, week_key: str) -> tuple[list[Standing], list[int]] | None:
    row = await db.fetchone("SELECT data FROM house_results WHERE kind = 'week' AND period = ?", (week_key,))
    return load_week(row["data"]) if row else None


async def standings(db, week: Week, now: int, present: Collection[int] | None = None,
                    excluded_channels: Collection[int] = (), bot_ids: Collection[int] = ()) -> list[Standing]:
    """The read API: a closed week's frozen standings, else the live ones. Best first."""
    done = await frozen_week(db, week.key)
    if done is not None:
        return done[0]
    return (await live(db, week, now, present, excluded_channels, bot_ids)).standings


async def season_standings(db, season: str) -> list[Tally]:
    """The season table: the frozen trophy result once the season closed, else the running tally."""
    row = await db.fetchone("SELECT data FROM house_results WHERE kind = 'season' AND period = ?", (season,))
    if row is not None:
        return load_season(row["data"])
    rows = await db.fetchall("SELECT data FROM house_results WHERE kind = 'week' AND season = ? ORDER BY period",
                             (season,))
    return season_tally([load_week(r["data"])[0] for r in rows], season)


def season_job_week(scheduled_at: int, tz: tzinfo) -> str:
    """The jobs key of the weekly close that has to be settled before a season closing at
    `scheduled_at` can be judged (the latest Monday 10:10 at or before it)."""
    return latest_due(WEEKLY_JOB, scheduled_at, tz).key
