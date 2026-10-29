"""Field notes: once a week, what the owner built, heard, watched and played. Pure: no Discord
I/O, no database, no network, no clock (the cog passes `now`, rows and fetched bodies in).

The post is the owner's own notebook page, so it holds the owner's activity only and never a
member's. Four sections, each left out when its source is not set up, failed or has nothing:

- Built: pull requests merged that week in the owner's PUBLIC GitHub repos. A pull request
  counts only when the repo listing says `visibility == "public"` and `private` is false; a
  repo the listing does not show is left out too.
- Heard: the week's plays from SpotifyForge's listening log (`listening_history.jsonl`, one
  play per line, written by its own `history sync`). Front Desk never sees a Spotify token.
- Watched: Letterboxd diary entries from the profile's public RSS feed, with stars.
- Played: the owner's own game time from `game_sessions` (cogs/stats.py records it).

Settings (server/.env, all optional; with none of them set the feature posts nothing):
FIELD_NOTES_GITHUB_USER, FIELD_NOTES_SPOTIFY_HISTORY, FIELD_NOTES_LETTERBOXD_USER and
FIELD_NOTES_PLAYED (on/off; unset: on whenever another source is set).
"""

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path

import discord

from logic import quiet
from logic.schedule import Period, Weekly, latest_due

JOB = Weekly("fieldnotes", 6, 18, 0)  # Sundays 18:00 local
QUIET = quiet.FIELD_NOTES  # its meta key, quiet:fieldnotes, is live data

MAX_LINES = 4  # lines per section, the "and N more" line included
MAX_BODY = 2 * 1024 * 1024  # bytes read from any one response or file page
MAX_TITLE = 90  # characters of a pull request, track or film title
MIN_PLAYS = 5  # fewer plays than this in a week: no Heard section
MIN_GAME_SECONDS = 20 * 60  # a game under 20 minutes that week is not named
BACKFILL_DAYS = 14  # a diary entry logged this week for a film watched longer ago is skipped

QUESTIONS = (
    "What did you watch this week?",
    "What are you listening to right now?",
    "What did you play this week?",
    "Made or fixed anything this week?",
    "Anything good you'd recommend?",
)
REPLY_HINT = "Tell me in the thread."

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

ENV_GITHUB = "FIELD_NOTES_GITHUB_USER"
ENV_SPOTIFY = "FIELD_NOTES_SPOTIFY_HISTORY"
ENV_LETTERBOXD = "FIELD_NOTES_LETTERBOXD_USER"
ENV_PLAYED = "FIELD_NOTES_PLAYED"

GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
LETTERBOXD_USER_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
OFF = {"off", "0", "false", "no"}


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    github_user: str | None = None
    spotify_history: Path | None = None
    letterboxd_user: str | None = None
    played: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.github_user or self.spotify_history or self.letterboxd_user or self.played)

    def sources(self) -> list[tuple[str, bool]]:
        """(section, set up) in post order, for the staff preview."""
        return [("Built", bool(self.github_user)), ("Heard", bool(self.spotify_history)),
                ("Watched", bool(self.letterboxd_user)), ("Played", self.played)]


def read_settings(environ: Mapping[str, str]) -> Settings:
    """Invalid values count as unset, so a typo turns a section off instead of sending a
    request somewhere unexpected."""
    def get(key):
        return str(environ.get(key) or "").strip()

    github = get(ENV_GITHUB)
    letterboxd = get(ENV_LETTERBOXD)
    history = get(ENV_SPOTIFY).strip('"')
    settings = Settings(
        github_user=github if GITHUB_USER_RE.match(github) else None,
        spotify_history=Path(history).expanduser() if history else None,
        letterboxd_user=letterboxd if LETTERBOXD_USER_RE.match(letterboxd) else None,
    )
    played = get(ENV_PLAYED).lower()
    on = played not in OFF if played else bool(settings.github_user or settings.spotify_history
                                                or settings.letterboxd_user)
    return Settings(settings.github_user, settings.spotify_history, settings.letterboxd_user, on)


# ---------------------------------------------------------------- text helpers
def esc(text: str) -> str:
    """Bot-authored text from outside sources: no markdown, no mentions, one line."""
    flat = " ".join(str(text or "").split())
    return discord.utils.escape_mentions(discord.utils.escape_markdown(flat))


def fit(text: str, limit: int = MAX_TITLE) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def capped(lines: list[str], extra: int, what: str) -> list[str]:
    """At most MAX_LINES lines; anything cut is counted on the last one."""
    total = len(lines) + extra
    if total <= MAX_LINES:
        return lines
    keep = lines[: MAX_LINES - 1]
    return [*keep, f"And {plural(total - len(keep), what)} more."]


def and_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


# ---------------------------------------------------------------- the week
@dataclass(frozen=True)
class Week:
    title: str  # "Field notes, week of 26 Oct"
    iso_week: int  # the footer is style.label("field notes", f"week {iso_week}")
    start: int  # the window, unix seconds, [start, end)
    end: int
    tz: tzinfo


def week_of(period: Period, tz: tzinfo) -> Week:
    """Labels for a period: named after the Monday of the ISO week the post goes out in."""
    local = datetime.fromtimestamp(period.scheduled_at, tz).date()
    return _week(local, period.window_start, period.window_end, tz)


def preview_week(job: Weekly, now: int, tz: tzinfo) -> Week:
    """The week so far: from the last scheduled post to `now`, labelled as the next post."""
    last = latest_due(job, now, tz)
    upcoming = datetime.fromtimestamp(last.scheduled_at, tz).date() + timedelta(days=7)
    return _week(upcoming, last.scheduled_at, now, tz)


def _week(local: date, start: int, end: int, tz: tzinfo) -> Week:
    iso = local.isocalendar()
    monday = local - timedelta(days=local.weekday())
    return Week(f"Field notes, week of {monday.day} {MONTHS[monday.month - 1]}", iso.week, start, end, tz)


def question(week: Week) -> str:
    """The ending line, rotating with the week number."""
    return f"{QUESTIONS[week.iso_week % len(QUESTIONS)]} {REPLY_HINT}"


# ---------------------------------------------------------------- Built (GitHub)
@dataclass(frozen=True)
class Merged:
    repo: str
    number: int
    title: str
    merged_at: int


def search_url(user: str, week: Week) -> str:
    """Merged pull requests in repos `user` owns, by day (widened a day each side; merged_at
    filters exactly). Unauthenticated search only ever returns public repos, and
    public_repos() is checked as well."""
    lo = datetime.fromtimestamp(week.start, timezone.utc).date() - timedelta(days=1)
    hi = datetime.fromtimestamp(week.end, timezone.utc).date() + timedelta(days=1)
    q = f"is:pr+is:merged+user:{user}+merged:{lo.isoformat()}..{hi.isoformat()}"
    return f"https://api.github.com/search/issues?q={q}&sort=updated&order=desc&per_page=50"


def repos_url(user: str, page: int = 1) -> str:
    return f"https://api.github.com/users/{user}/repos?type=owner&per_page=100&page={page}"


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("not JSON") from None


def public_repos(body: bytes, user: str) -> tuple[set[str], int]:
    """(names of the user's repos that are public, how many repos the page listed). Anything
    that is not plainly public (private, internal, no visibility field) is left out."""
    data = _json(body)
    if not isinstance(data, list):
        raise ValueError("repo listing is not a list")
    out = set()
    for repo in data:
        if not isinstance(repo, dict):
            continue
        owner = (repo.get("owner") or {}).get("login", "") if isinstance(repo.get("owner"), dict) else ""
        name = repo.get("name")
        if (isinstance(name, str) and GITHUB_REPO_RE.match(name) and owner.lower() == user.lower()
                and repo.get("visibility") == "public" and repo.get("private") is False):
            out.add(name.lower())
    return out, len(data)


def _iso(value) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def merged_prs(body: bytes, user: str, public: set[str], week: Week) -> list[Merged]:
    """Pull requests from a search page that were merged inside the week in a public repo of
    `user`, newest first. Raises ValueError on a body that is not a search result."""
    data = _json(body)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("not a search result")
    prefix = f"https://api.github.com/repos/{user.lower()}/"
    out, seen = [], set()
    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        repo_url = str(item.get("repository_url") or "")
        if not repo_url.lower().startswith(prefix):
            continue
        repo = repo_url[len(prefix):]
        pr = item.get("pull_request") if isinstance(item.get("pull_request"), dict) else {}
        merged_at = _iso(pr.get("merged_at"))
        number = item.get("number")
        if (not GITHUB_REPO_RE.match(repo) or repo.lower() not in public or merged_at is None
                or not isinstance(number, int) or not week.start <= merged_at < week.end):
            continue
        if (repo.lower(), number) in seen:
            continue
        seen.add((repo.lower(), number))
        out.append(Merged(repo, number, str(item.get("title") or ""), merged_at))
    return sorted(out, key=lambda m: -m.merged_at)


PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?[a-z]+(?:\([^)]*\))?!?:\s+", re.I)
SUFFIX_RE = re.compile(r"\s*\(#\d+\)\s*$")


def clean_title(title: str) -> str:
    """'feat(bot): tempo estimates from previews (#70)' -> 'Tempo estimates from previews'."""
    text = SUFFIX_RE.sub("", PREFIX_RE.sub("", " ".join(str(title).split()))).rstrip(" .")
    return text[:1].upper() + text[1:]


def pr_url(user: str, pr: Merged) -> str:
    """Rebuilt from validated parts, never copied from the response."""
    return f"https://github.com/{user}/{pr.repo}/pull/{pr.number}"


def built_lines(user: str, prs: list[Merged]) -> list[str]:
    """A count, then the newest pull requests, one line each (MAX_LINES in all)."""
    if not prs:
        return []
    repos = {p.repo.lower() for p in prs}
    head = f"Merged {plural(len(prs), 'pull request')} in {plural(len(repos), 'public repo')}."
    lines = [f"`{p.repo}` {esc(fit(clean_title(p.title)))} ([#{p.number}](<{pr_url(user, p)}>))"
             for p in prs]
    room = MAX_LINES - 1
    if len(lines) > room:
        lines = [*lines[: room - 1], f"And {len(lines) - (room - 1)} more."]
    return [head, *lines]


# ---------------------------------------------------------------- Heard (Spotify log)
@dataclass(frozen=True)
class Play:
    at: int
    track_id: str
    name: str
    artist: str
    duration_ms: int


def parse_history(lines: Iterable[str], week: Week) -> tuple[list[Play], int | None]:
    """(plays inside the week, the newest play time in the whole log). Bad lines are skipped."""
    plays, newest = [], None
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            at = _iso(raw["played_at"])
            artists = raw.get("artists") or []
            play = Play(at, str(raw["track_id"]), str(raw.get("name") or ""),
                        str(artists[0]) if artists else "", int(raw.get("duration_ms") or 0))
        except (ValueError, KeyError, TypeError, IndexError):
            continue
        if at is None:
            continue
        newest = at if newest is None else max(newest, at)
        if week.start <= at < week.end:
            plays.append(play)
    return plays, newest


def _hours(seconds: int) -> str:
    hours = seconds / 3600
    if hours < 0.75:
        return f"{max(5, round(seconds / 300) * 5)} minutes"
    if hours < 1.5:
        return "about an hour"
    return f"about {round(hours)} hours"


def heard_lines(plays: list[Play]) -> list[str]:
    if len(plays) < MIN_PLAYS:
        return []
    artists = Counter(p.artist for p in plays if p.artist)
    top_artists = [esc(fit(a, 60)) for a, _ in sorted(artists.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    lines = []
    if top_artists:
        rest = f", then {and_list(top_artists[1:])}" if len(top_artists) > 1 else ""
        lines.append(f"Mostly {top_artists[0]}{rest}.")
    tracks = Counter(p.track_id for p in plays)
    names = {p.track_id: p for p in plays}
    repeat = [names[t] for t, n in sorted(tracks.items(), key=lambda kv: (-kv[1], names[kv[0]].name)) if n >= 3][:2]
    if repeat:
        lines.append("On repeat: " + and_list([f"{esc(fit(p.name, 60))} ({esc(fit(p.artist, 40))})"
                                               for p in repeat]) + ".")
    seconds = sum(p.duration_ms for p in plays) // 1000
    lines.append(f"{plural(len(plays), 'play')}, {_hours(seconds)} of music.")
    return lines


# ---------------------------------------------------------------- Watched (Letterboxd RSS)
LBX = "{https://letterboxd.com}"


@dataclass(frozen=True)
class Film:
    title: str
    year: int | None
    rating: float | None
    rewatch: bool
    watched: date
    logged_at: int


def rss_url(user: str) -> str:
    return f"https://letterboxd.com/{user}/rss/"


def parse_rss(body: bytes) -> list[Film]:
    """Diary entries from a Letterboxd profile feed (lists and plain reviews have no
    watchedDate and are skipped). Raises ValueError on anything that is not that feed."""
    if len(body) > MAX_BODY:
        raise ValueError("feed too large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("feed is not UTF-8") from None
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("feed has a DTD")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"bad feed: {exc}") from None
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("not an RSS feed")
    films = []
    for item in channel.findall("item"):
        def get(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        watched, title = get(f"{LBX}watchedDate"), get(f"{LBX}filmTitle")
        if not watched or not title:
            continue
        try:
            when = date.fromisoformat(watched)
            logged = int(parsedate_to_datetime(get("pubDate")).timestamp())
        except (TypeError, ValueError, IndexError):
            continue
        try:
            year = int(get(f"{LBX}filmYear"))
        except ValueError:
            year = None
        try:
            rating = float(get(f"{LBX}memberRating"))
            rating = rating if 0.5 <= rating <= 5 else None
        except ValueError:
            rating = None
        films.append(Film(title, year, rating, get(f"{LBX}rewatch").lower() == "yes", when, logged))
    return films


def stars(rating: float | None) -> str:
    """Letterboxd's own: 3.5 -> '★★★½'."""
    if rating is None:
        return ""
    halves = round(rating * 2)
    return "★" * (halves // 2) + ("½" if halves % 2 else "")


def watched_this_week(films: list[Film], week: Week) -> list[Film]:
    """Logged inside the week (so each entry shows once), for a film watched recently,
    in the order they were watched."""
    oldest = datetime.fromtimestamp(week.end, week.tz).date() - timedelta(days=BACKFILL_DAYS)
    picked = [f for f in films if week.start <= f.logged_at < week.end and f.watched >= oldest]
    return sorted(picked, key=lambda f: (f.watched, f.logged_at))


def watched_lines(films: list[Film]) -> list[str]:
    lines = []
    for f in films:
        line = esc(fit(f.title)) + (f" ({f.year})" if f.year else "")
        if f.rating is not None:
            line += f" {stars(f.rating)}"
        if f.rewatch:
            line += ", a rewatch"
        lines.append(line)
    return capped(lines, 0, "film")


# ---------------------------------------------------------------- Played (game_sessions)
def played_lines(per_game: Mapping[str, int]) -> list[str]:
    """The owner's games that week, most played first. Short ones are not named."""
    top = sorted(((g, s) for g, s in per_game.items() if s >= MIN_GAME_SECONDS and g),
                 key=lambda kv: (-kv[1], kv[0]))
    return capped([f"{esc(fit(g, 60))}, {_hours(s)}." for g, s in top], 0, "game")


# ---------------------------------------------------------------- the post
@dataclass(frozen=True)
class Sections:
    built: tuple[str, ...] = ()
    heard: tuple[str, ...] = ()
    watched: tuple[str, ...] = ()
    played: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.built or self.heard or self.watched or self.played)


def body(sections: Sections, ask: str | None) -> str:
    """The embed description: a bold label per section with its lines, then the question."""
    parts = []
    for label, lines in (("Built", sections.built), ("Heard", sections.heard),
                         ("Watched", sections.watched), ("Played", sections.played)):
        if lines:
            parts.append("\n".join([f"**{label}**", *lines]))
    if ask:
        parts.append(ask)
    return "\n\n".join(parts)[:4096]


def thread_name(week: Week) -> str:
    return f"Field notes, week {week.iso_week}"
