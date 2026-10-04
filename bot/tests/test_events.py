"""Pure logic for Module 7: the /gamenight time parser, voice channel choice,
reminder window and free-games filtering."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from logic import events as E

TZ = ZoneInfo("America/Los_Angeles")


def local(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


NOW = local(2026, 10, 7, 12, 0)  # a Wednesday, local noon


def parse(text, now=NOW):
    return E.parse_when(text, now, TZ)


# ---------------------------------------------------------------- parse_when
@pytest.mark.parametrize("text, expected", [
    ("9pm", local(2026, 10, 7, 21)),
    ("9 PM", local(2026, 10, 7, 21)),
    ("9:30pm", local(2026, 10, 7, 21, 30)),
    ("21:00", local(2026, 10, 7, 21)),
    ("tonight 9pm", local(2026, 10, 7, 21)),
    ("tomorrow 8pm", local(2026, 10, 8, 20)),
    ("tmrw 8pm", local(2026, 10, 8, 20)),
    ("fri 9pm", local(2026, 10, 9, 21)),
    ("friday 9pm", local(2026, 10, 9, 21)),
    ("Friday 21:30", local(2026, 10, 9, 21, 30)),
    ("2026-10-10 20:00", local(2026, 10, 10, 20)),
    ("2026-10-10 8pm", local(2026, 10, 10, 20)),
    ("  tomorrow   8pm  ", local(2026, 10, 8, 20)),
    ("12am", local(2026, 10, 8, 0)),  # midnight already passed today -> tomorrow
    ("12pm", local(2026, 10, 8, 12)),  # noon is "now", not the future -> tomorrow
])
def test_parse_formats(text, expected):
    got = parse(text)
    assert got == expected
    assert got.tzinfo is not None


def test_bare_time_already_passed_rolls_to_tomorrow():
    assert parse("9am") == local(2026, 10, 8, 9)


def test_weekday_today_later_is_today_earlier_is_next_week():
    assert parse("wed 9pm") == local(2026, 10, 7, 21)
    assert parse("wed 9am") == local(2026, 10, 14, 9)


def test_tonight_in_the_past_is_not_rolled():
    assert parse("tonight 9am") == local(2026, 10, 7, 9)  # past: check_when rejects it


@pytest.mark.parametrize("text", [
    "", "soon", "25:00", "13pm", "9:75pm", "0pm", "someday 9pm", "fri", "2026-13-01 20:00",
    "2026-02-30 20:00", "9pm <@&123>", "tomorrow tomorrow 9pm", "9", "-9pm",
])
def test_parse_garbage(text):
    assert parse(text) is None


def test_check_when():
    assert E.check_when(local(2026, 10, 7, 21), NOW) is None
    assert E.check_when(local(2026, 10, 7, 9), NOW) == "past"
    assert E.check_when(NOW, NOW) == "past"
    assert E.check_when(NOW + timedelta(days=30), NOW) is None
    assert E.check_when(NOW + timedelta(days=30, minutes=1), NOW) == "too_far"
    assert E.check_when(parse("2026-12-25 20:00"), NOW) == "too_far"
    assert E.check_when(parse("2026-10-01 20:00"), NOW) == "past"


def test_dst_fall_back_day():
    # 2026-11-01 is the end of PDT in Los Angeles: 9pm that day is PST (UTC-8).
    now = local(2026, 10, 31, 22)
    got = parse("tomorrow 9pm", now)
    assert got.astimezone(timezone.utc) == datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    # And the day before, still PDT (UTC-7).
    assert parse("9pm", local(2026, 10, 31, 12)).astimezone(timezone.utc) == \
        datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)


def test_dst_spring_forward_day():
    # 2027-03-14: PST -> PDT. 8pm is PDT (UTC-7).
    got = parse("2027-03-14 20:00", local(2027, 3, 13, 12))
    assert got.astimezone(timezone.utc) == datetime(2027, 3, 15, 3, 0, tzinfo=timezone.utc)


def test_to_utc_is_aware_utc():
    u = E.to_utc(local(2026, 10, 7, 21))
    assert u.tzinfo == timezone.utc
    assert u == datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- channel, names
def test_voice_for_size():
    assert E.voice_for(None) == config.SQUAD_VOICE
    assert E.voice_for(2) == config.SQUAD_VOICE
    assert E.voice_for(5) == config.SQUAD_VOICE
    assert E.voice_for(6) == config.LOBBY_VOICE
    assert E.voice_for(20) == config.LOBBY_VOICE


def test_event_name_and_description():
    assert E.event_name("Valorant") == "Valorant game night"
    assert E.event_name(None) == "Game night"
    d = E.event_description("user1", "bring snacks", 8)
    assert "Hosted by user1" in d and "bring snacks" in d and "8" in d
    assert E.event_description("user1", None, None) == "Hosted by user1."
    assert len(E.event_description("u", "x" * 5000, 4)) <= 1000


# ---------------------------------------------------------------- reminders
START = 1_800_000_000


@pytest.mark.parametrize("now, state", [
    (START - 16 * 60, "wait"),
    (START - 15 * 60, "send"),
    (START - 60, "send"),
    (START, "send"),
    (START + 10 * 60, "send"),
    (START + 10 * 60 + 1, "expired"),
])
def test_reminder_state(now, state):
    assert E.reminder_state(START, now) == state


# ---------------------------------------------------------------- free games
def item(i, **kw):
    base = dict(id=i, title=f"Game {i}", worth="$19.99", platforms="PC, Steam",
                end_date="2026-10-15 23:59:00", status="Active", type="Game",
                open_giveaway_url=f"https://www.gamerpower.com/open/game-{i}")
    base.update(kw)
    return base


def test_select_filters_inactive_seen_and_bad_urls():
    data = [
        item(1),
        item(2, status="Expired"),
        item(3),
        item(4, open_giveaway_url="javascript:alert(1)"),
        item(5, open_giveaway_url="ftp://x/y"),
        item(6, open_giveaway_url=None),
        item(7, worth="N/A", end_date="N/A"),
        {"id": "eight", "title": "bad id"},
        "not a dict",
        item(9, title=""),
    ]
    got = E.select_giveaways(data, seen={3})
    assert [g.id for g in got] == [1, 7]
    assert got[1].worth is None and got[1].end_date is None
    assert got[0].worth == "$19.99" and got[0].platforms == "PC, Steam"


def test_select_dedupes_within_response():
    assert [g.id for g in E.select_giveaways([item(1), item(1)], seen=set())] == [1]


def test_select_no_giveaways_object_is_empty():
    assert E.select_giveaways({"status": 0, "status_message": "No active giveaways"}, set()) == []


@pytest.mark.parametrize("data", [None, "oops", 42, {"unexpected": True}])
def test_select_malformed_raises(data):
    with pytest.raises(E.MalformedFeed):
        E.select_giveaways(data, set())


def test_safe_url():
    assert E.safe_url("https://a.b/c") == "https://a.b/c"
    assert E.safe_url("http://a.b/c") == "http://a.b/c"
    for bad in ("javascript:x", "HTTPS://", "data:text/html,x", "//a.b", "", None, 5, "https://a b/c",
                "https://a.b/c)[x](javascript:y", "https://a.b/" + "x" * 600):
        assert E.safe_url(bad) is None


def test_free_games_lines_escape_and_limit():
    games = E.select_giveaways([item(i) for i in range(1, 12)] +
                               [item(50, title="[evil](javascript:x) **bold** <@&1>")], set())
    lines = E.giveaway_lines(games)
    assert len(lines) == E.MAX_FREE_GAMES == 8
    assert lines[0].startswith("**[Game 1](https://www.gamerpower.com/open/game-1)**")
    assert "$19.99" in lines[0] and "PC, Steam" in lines[0] and "2026-10-15" in lines[0]
    evil = E.giveaway_lines(E.select_giveaways([item(50, title="[evil](javascript:x) **bold**")], set()))[0]
    assert "](javascript" not in evil and "**bold**" not in evil
