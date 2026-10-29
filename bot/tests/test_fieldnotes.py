"""logic/fieldnotes.py: the weekly field notes. Every source is faked (no network, no files)."""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from logic import fieldnotes as F
from logic import quiet
from logic import schedule

TZ = ZoneInfo("America/Los_Angeles")
USER = "owner-name"


def ts(y, mo, d, h=12, mi=0, tz=TZ):
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())


# Sunday 1 Nov 2026 18:00 PST; the window opens Sunday 25 Oct 18:00 PDT (DST ends in between)
PERIOD = schedule.occurrence(F.JOB, date(2026, 11, 1), TZ)
WEEK = F.week_of(PERIOD, TZ)
INSIDE = ts(2026, 10, 28, 15)


# ---------------------------------------------------------------- the job and the week
def test_job_is_sunday_evening_and_its_key_format_is_fixed():
    assert F.JOB == schedule.Weekly("fieldnotes", 6, 18, 0)
    assert PERIOD.key == "fieldnotes:2026-W44"
    assert F.QUIET is quiet.FIELD_NOTES and quiet.key(F.QUIET) == "quiet:fieldnotes"


def test_week_labels_follow_the_brief():
    assert WEEK.title == "Field notes, week of 26 Oct"
    assert WEEK.iso_week == 44
    assert (WEEK.start, WEEK.end) == (PERIOD.window_start, PERIOD.window_end)
    assert WEEK.end - WEEK.start == 169 * 3600  # the DST week is an hour longer
    assert F.thread_name(WEEK) == "Field notes, week 44"


def test_week_title_across_a_month_boundary():
    week = F.week_of(schedule.occurrence(F.JOB, date(2026, 12, 6), TZ), TZ)
    assert week.title == "Field notes, week of 30 Nov"


def test_preview_week_runs_from_the_last_post_to_now_and_names_the_next_one():
    now = ts(2026, 10, 29, 9)
    week = F.preview_week(F.JOB, now, TZ)
    assert week.start == ts(2026, 10, 25, 18) and week.end == now
    assert week.title == "Field notes, week of 26 Oct" and week.iso_week == 44


def test_preview_week_on_sunday_after_the_post_starts_the_new_week():
    now = ts(2026, 11, 1, 19)
    week = F.preview_week(F.JOB, now, TZ)
    assert week.start == PERIOD.scheduled_at
    assert week.title == "Field notes, week of 2 Nov"


def test_question_rotates_and_invites_a_thread_reply():
    asks = {F.question(F.Week("t", n, 0, 1, TZ)) for n in range(1, 11)}
    assert len(asks) == len(F.QUESTIONS)
    for ask in asks:
        assert ask.endswith(F.REPLY_HINT) and "!" not in ask and "@" not in ask
    assert F.QUESTIONS[0] == "What did you watch this week?"


# ---------------------------------------------------------------- settings
def test_no_settings_means_nothing_runs():
    s = F.read_settings({})
    assert not s.enabled and not s.played
    assert s.sources() == [("Built", False), ("Heard", False), ("Watched", False), ("Played", False)]


def test_settings_read_each_source():
    s = F.read_settings({F.ENV_GITHUB: USER, F.ENV_SPOTIFY: '"C:\\data\\listening_history.jsonl"',
                         F.ENV_LETTERBOXD: "film_fan_9"})
    assert s.github_user == USER and s.letterboxd_user == "film_fan_9"
    assert s.spotify_history == Path("C:\\data\\listening_history.jsonl")
    assert s.played  # on by default once another source is set
    assert s.enabled


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_played_can_be_turned_off(value):
    assert not F.read_settings({F.ENV_GITHUB: USER, F.ENV_PLAYED: value}).played


def test_played_alone_is_enough():
    s = F.read_settings({F.ENV_PLAYED: "on"})
    assert s.played and s.enabled


@pytest.mark.parametrize("bad", ["-dash", "has space", "a/b", "x" * 40, "evil.com/../", "name?q=1"])
def test_bad_github_user_is_unset(bad):
    assert F.read_settings({F.ENV_GITHUB: bad}).github_user is None


@pytest.mark.parametrize("bad", ["has space", "a/b", "../x", "x" * 41, "user?x"])
def test_bad_letterboxd_user_is_unset(bad):
    assert F.read_settings({F.ENV_LETTERBOXD: bad}).letterboxd_user is None


# ---------------------------------------------------------------- Built (GitHub)
def repo(name, visibility="public", private=None, owner=USER):
    private = (visibility != "public") if private is None else private
    return {"name": name, "visibility": visibility, "private": private, "owner": {"login": owner}}


def pr(repo_name, number, title, merged_at, owner=USER):
    stamp = None if merged_at is None else datetime.fromtimestamp(merged_at, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"repository_url": f"https://api.github.com/repos/{owner}/{repo_name}", "number": number,
            "title": title, "pull_request": {"merged_at": stamp}}


def body(obj):
    return json.dumps(obj).encode()


LISTING = body([
    repo("discord-server"), repo("Spotify"),
    repo("fleet", "private"),  # a fake private repo
    repo("streaming", "internal"),
    repo("sneaky", "public", private=True),  # contradicting fields: not plainly public
    {"name": "novis", "private": False, "owner": {"login": USER}},  # no visibility field
    repo("forked-elsewhere", owner="someone-else"),
    "not a dict",
])


def test_public_repos_keeps_only_plainly_public_repos_the_user_owns():
    names, listed = F.public_repos(LISTING, USER)
    assert names == {"discord-server", "spotify"}
    assert listed == 8


def test_public_repos_rejects_non_lists():
    with pytest.raises(ValueError):
        F.public_repos(b'{"message": "API rate limit exceeded"}', USER)
    with pytest.raises(ValueError):
        F.public_repos(b"<html>", USER)


def test_private_repo_prs_never_reach_the_post():
    public, _ = F.public_repos(LISTING, USER)
    search = body({"items": [
        pr("discord-server", 70, "feat(bot): Season I channels (#70)", INSIDE),
        pr("fleet", 12, "secret machine notes", INSIDE),  # private
        pr("streaming", 3, "internal thing", INSIDE),  # internal
        pr("unlisted", 4, "repo missing from the listing", INSIDE),
        pr("sneaky", 5, "private flag set", INSIDE),
    ]})
    merged = F.merged_prs(search, USER, public, WEEK)
    assert [(m.repo, m.number) for m in merged] == [("discord-server", 70)]
    text = "\n".join(F.built_lines(USER, merged))
    for leaked in ("fleet", "streaming", "unlisted", "sneaky", "secret"):
        assert leaked not in text


def test_merged_prs_filters_the_window_exactly_and_dedupes():
    public = {"discord-server"}
    search = body({"items": [
        pr("discord-server", 1, "before", WEEK.start - 1),
        pr("discord-server", 2, "first second", WEEK.start),
        pr("discord-server", 3, "at the end", WEEK.end),
        pr("discord-server", 4, "not merged", None),
        pr("discord-server", 2, "first second", WEEK.start),  # a duplicate
        pr("discord-server", 5, "newest", WEEK.end - 1),
        pr("discord-server", 6, "someone else's repo", INSIDE, owner="other"),
        {"repository_url": f"https://api.github.com/repos/{USER}/discord-server", "number": "7",
         "pull_request": {"merged_at": "2026-10-28T00:00:00Z"}},  # number not an int
    ]})
    assert [m.number for m in F.merged_prs(search, USER, public, WEEK)] == [5, 2]


def test_merged_prs_rejects_a_non_search_body():
    for bad in (b"[]", b'{"items": 3}', b"nope"):
        with pytest.raises(ValueError):
            F.merged_prs(bad, USER, set(), WEEK)


def test_search_url_widens_the_days_and_asks_for_merged_prs_only():
    url = F.search_url(USER, WEEK)
    assert url.startswith("https://api.github.com/search/issues?q=is:pr+is:merged+user:owner-name+merged:")
    assert "merged:2026-10-25..2026-11-03" in url
    assert F.repos_url(USER, 2).endswith(f"/users/{USER}/repos?type=owner&per_page=100&page=2")


@pytest.mark.parametrize("raw,clean", [
    ("feat(bot): tempo estimates from previews (#70)", "Tempo estimates from previews"),
    ("fix: the SMP border.", "The SMP border"),
    ("[WIP] chore(deps)!: bump aiohttp", "Bump aiohttp"),
    ("Season I channels", "Season I channels"),
])
def test_clean_title(raw, clean):
    assert F.clean_title(raw) == clean


def test_built_lines_format_and_cap():
    prs = [F.Merged("discord-server", n, f"feat: change {n}", INSIDE - n) for n in range(1, 7)]
    prs.append(F.Merged("spotify", 9, "fix: a thing", INSIDE - 10))
    lines = F.built_lines(USER, prs)
    assert lines[0] == "Merged 7 pull requests in 2 public repos."
    assert lines[1] == f"`discord-server` Change 1 ([#1](<https://github.com/{USER}/discord-server/pull/1>))"
    assert len(lines) == F.MAX_LINES
    assert lines[-1] == "And 5 more."


def test_built_lines_singular_and_none():
    assert F.built_lines(USER, []) == []
    lines = F.built_lines(USER, [F.Merged("discord-server", 3, "docs: notes", INSIDE)])
    assert lines[0] == "Merged 1 pull request in 1 public repo."
    assert len(lines) == 2


def test_built_lines_escape_titles():
    lines = F.built_lines(USER, [F.Merged("r", 1, "fix: @everyone [click](https://evil) **bold**", INSIDE)])
    line = lines[1]
    assert "@everyone" not in line and r"\[click]" in line and r"\*\*bold" in line


# ---------------------------------------------------------------- Heard (Spotify log)
def play_line(at, track, name, artist, ms=200_000):
    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return json.dumps({"played_at": stamp, "track_id": track, "name": name, "artists": [artist],
                       "album": "x", "duration_ms": ms, "context_type": None, "context_uri": None,
                       "source": "recently_played", "synced_at": stamp})


def test_parse_history_keeps_the_week_and_skips_bad_lines():
    lines = [play_line(WEEK.start - 1, "t0", "Old", "A"), play_line(INSIDE, "t1", "In", "A"),
             "", "{not json", json.dumps({"played_at": "nope", "track_id": "x"}),
             json.dumps({"track_id": "no time"}), play_line(WEEK.end + 5, "t2", "Later", "B")]
    plays, newest = F.parse_history(lines, WEEK)
    assert [p.track_id for p in plays] == ["t1"]
    assert newest == WEEK.end + 5


def test_heard_lines():
    lines = []
    for i in range(6):
        lines.append(play_line(INSIDE + i, "loop", "Loop Song", "Artist A", 180_000))
    for i in range(4):
        lines.append(play_line(INSIDE + 100 + i, f"b{i}", f"B{i}", "Artist B", 240_000))
    lines.append(play_line(INSIDE + 200, "c", "C", "Artist C", 240_000))
    plays, _ = F.parse_history(lines, WEEK)
    assert F.heard_lines(plays) == [
        "Mostly Artist A, then Artist B and Artist C.",
        "On repeat: Loop Song (Artist A).",
        "11 plays, 40 minutes of music.",
    ]


def test_heard_needs_enough_plays():
    plays, _ = F.parse_history([play_line(INSIDE + i, f"t{i}", "N", "A") for i in range(F.MIN_PLAYS - 1)], WEEK)
    assert F.heard_lines(plays) == []


@pytest.mark.parametrize("seconds,text", [(60, "5 minutes"), (1800, "30 minutes"), (3600, "about an hour"),
                                          (6 * 3600 + 200, "about 6 hours")])
def test_hours(seconds, text):
    assert F._hours(seconds) == text


# ---------------------------------------------------------------- Watched (Letterboxd)
def rss(*items, extra=""):
    inner = "".join(items)
    return (f'<?xml version="1.0" encoding="utf-8"?>{extra}<rss version="2.0" '
            'xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org">'
            f"<channel><title>Letterboxd</title>{inner}</channel></rss>").encode()


def item(title, year, rating, watched, logged, rewatch="No"):
    rating_xml = f"<letterboxd:memberRating>{rating}</letterboxd:memberRating>" if rating is not None else ""
    pub = datetime.fromtimestamp(logged, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (f"<item><title>{title}</title><pubDate>{pub}</pubDate>"
            f"<letterboxd:watchedDate>{watched}</letterboxd:watchedDate>"
            f"<letterboxd:rewatch>{rewatch}</letterboxd:rewatch>"
            f"<letterboxd:filmTitle>{title}</letterboxd:filmTitle>"
            f"<letterboxd:filmYear>{year}</letterboxd:filmYear>{rating_xml}</item>")


FEED = rss(
    item("Paris, Texas", 1984, 4.5, "2026-10-27", INSIDE, rewatch="Yes"),
    item("Perfect Days", 2023, 3.5, "2026-10-26", INSIDE + 60),
    item("No Rating", 2001, None, "2026-10-28", INSIDE + 120),
    item("Last Week", 2020, 3.0, "2026-10-20", WEEK.start - 60),
    item("Backfilled", 1999, 4.0, "2026-09-01", INSIDE),  # logged now, watched long ago
    "<item><title>A list, not a diary entry</title><pubDate>Wed, 28 Oct 2026 10:00:00 +0000</pubDate></item>",
)


def test_watched_lines_from_the_feed():
    films = F.watched_this_week(F.parse_rss(FEED), WEEK)
    assert F.watched_lines(films) == [
        "Perfect Days (2023) ★★★½",
        "Paris, Texas (1984) ★★★★½, a rewatch",
        "No Rating (2001)",
    ]


@pytest.mark.parametrize("rating,text", [(0.5, "½"), (1.0, "★"), (3.5, "★★★½"), (5.0, "★★★★★"), (None, "")])
def test_stars(rating, text):
    assert F.stars(rating) == text


def test_parse_rss_rejects_dtds_and_junk():
    with pytest.raises(ValueError):
        F.parse_rss(rss(extra='<!DOCTYPE rss [<!ENTITY x "boom">]>'))
    for bad in (b"<html><body>not rss</body></html>", b"\xff\xfe", b"<rss><channel>"):
        with pytest.raises(ValueError):
            F.parse_rss(bad)


def test_watched_lines_cap():
    films = [F.Film(f"Film {i}", 2000 + i, 3.0, False, date(2026, 10, 27), INSIDE) for i in range(7)]
    lines = F.watched_lines(films)
    assert len(lines) == F.MAX_LINES and lines[-1] == "And 4 films more."


# ---------------------------------------------------------------- Played
def test_played_lines_skip_short_games_and_sort():
    lines = F.played_lines({"Minecraft": 6 * 3600 + 100, "Hades II": 2 * 3600, "Quick Look": 5 * 60, "": 9999})
    assert lines == ["Minecraft, about 6 hours.", "Hades II, about 2 hours."]


# ---------------------------------------------------------------- the post
def test_body_orders_sections_and_ends_with_the_question():
    sections = F.Sections(built=("b",), heard=(), watched=("w",), played=("p",))
    assert F.body(sections, "Q?") == "**Built**\nb\n\n**Watched**\nw\n\n**Played**\np\n\nQ?"
    assert F.body(sections, None).endswith("**Played**\np")
    assert not sections.empty and F.Sections().empty


def test_body_fits_an_embed():
    sections = F.Sections(built=tuple("x" * 2000 for _ in range(4)))
    assert len(F.body(sections, "Q")) <= 4096


def test_the_voice_has_no_exclamations_or_emoji():
    texts = [*F.QUESTIONS, F.REPLY_HINT, *F.built_lines(USER, [F.Merged("r", 1, "x", INSIDE)]),
             *F.played_lines({"G": 3600})]
    for text in texts:
        assert "!" not in text
        assert all(ord(ch) < 0x2000 or ch in "…★½" for ch in text), text
