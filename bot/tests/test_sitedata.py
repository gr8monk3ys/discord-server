"""logic.sitedata: the public site document is aggregates only, and provably so."""

import json
import re

import pytest

import config
import site_schema as schema
from logic import sitedata as SD

T = 1_800_000_000
HOUR = 3600
DAY = 24 * HOUR
SNOWFLAKE = 123456789012345678  # shaped like a Discord ID; not a real one


def full_inputs(**over) -> SD.Inputs:
    base = dict(
        now=T, member_count=15, online=6,
        messages=1243, message_people=9, voice_seconds=34 * HOUR + 200, voice_people=5,
        events=[SD.EventIn("Valorant game night", T + 2 * DAY), SD.EventIn("October Cup", T + 5 * DAY),
                SD.EventIn("Watch party: Movie night", T + 3 * DAY), SD.EventIn("Town Hall", T + 6 * DAY),
                SD.EventIn("Movie night", T + 4 * DAY)],  # made by hand: never published
        active_tournaments={"october cup": "Valorant"},
        games=[("Valorant", 12 * HOUR, 4), ("Minecraft", 30 * HOUR, 3), ("Roblox", 50 * HOUR, 2)],
        tournaments={"signup": 1, "done": 3, "cancelled": 2},
        matches=4, match_people=6, queued=3,
        seasonal=("Spooky Season", "🎃", T + 10 * DAY),
        houses=[("Tide", 34, 5), ("Moss", 12, 3), ("Ember", 12, 4)],
        starred=12, starred_people=5, clips=30, clip_people=4, badges=87, badge_people=11,
    )
    base.update(over)
    return SD.Inputs(**base)


# ---------------------------------------------------------------- small pieces
def test_due_runs_at_startup_then_every_interval():
    assert SD.due(None, T)
    assert not SD.due(T, T + SD.INTERVAL - 1)
    assert SD.due(T, T + SD.INTERVAL)
    assert SD.due(T, T - 10)  # the clock went backwards: don't wait forever


def test_rounding():
    assert SD.round_to(1243, 10) == 1240 and SD.round_to(1245, 10) == 1250 and SD.round_to(4, 10) == 0
    assert SD.hours(34 * HOUR + 200) == 34 and SD.hours(1799) == 0 and SD.hours(1800) == 1


def test_session_total_clips_to_the_window_and_counts_people():
    lo, hi = T - SD.WINDOW, T
    sessions = [(1, lo - 100, lo + 100), (1, T - 50, None), (2, lo - 500, lo - 10), (3, T - 10, T + 99)]
    assert SD.session_total(sessions, lo, hi, T) == (100 + 50 + 10, 2)


def test_match_game_only_knows_layout_games():
    assert SD.match_game("VALORANT").role == "Valorant"
    assert SD.match_game("Counter-Strike 2").role == "Counter-Strike 2"
    assert SD.match_game("Minecraft Launcher").role == "Minecraft"
    assert SD.match_game("Some Obscure Game") is None
    assert SD.match_game("") is None and SD.match_game(None) is None


def test_game_table_drops_unknown_games_and_counts_players():
    lo = T - SD.WINDOW
    rows = SD.game_table([(1, "VALORANT", T - 7200, T - 3600), (2, "Valorant", T - 3600, None),
                          (1, "valorant", T - 100, T), (9, "Visual Studio Code", T - 9000, T)], lo, T, T)
    assert rows == [("Valorant", 3600 + 3600 + 100, 2)]


# ---------------------------------------------------------------- event titles
def test_bot_templated_game_nights_are_rebuilt_from_the_game_name():
    assert SD.classify_event("  valorant GAME night ", {}) == {
        "title": "Valorant game night", "kind": "gamenight", "game": "Valorant"}
    assert SD.classify_event("Game night", {}) == {"title": "Game night", "kind": "gamenight"}


def test_a_tournament_is_named_by_its_game_never_by_its_own_name():
    assert SD.classify_event("October Cup", {"october cup": "Valorant"}) == {
        "title": "Valorant tournament", "kind": "tournament", "game": "Valorant"}
    assert SD.classify_event("October Cup", {"october cup": None}) == {"title": "Tournament", "kind": "tournament"}
    assert SD.classify_event("October Cup", {"october cup": "Some Hobby Game"}) == {
        "title": "Tournament", "kind": "tournament"}
    assert SD.classify_event("October Cup", {}) is None  # not a tournament the bot knows: a hand-made event


def test_watch_parties_and_the_town_hall_get_the_bot_s_own_words():
    from logic import events as E

    assert SD.classify_event(E.watchparty_name("Alex's  secret movie"), {}) == {"title": "Watch party", "kind": "event"}
    assert SD.classify_event(" WATCH PARTY: anything at all", {}) == {"title": "Watch party", "kind": "event"}
    assert SD.classify_event(E.watchparty_name("x" * 200), {}) == {"title": "Watch party", "kind": "event"}
    assert SD.classify_event(E.TOWNHALL_NAME, {}) == {"title": "Town Hall", "kind": "event"}
    assert SD.classify_event("town  hall", {}) == {"title": "Town Hall", "kind": "event"}
    assert SD.classify_event("Town Hall with Alex", {}) is None
    assert SD.classify_event("Watch party", {}) is None  # not the template: someone typed it


@pytest.mark.parametrize("title", ["Movie night", "Alex's birthday", "Game night at mine", "Valorant", "Karaoke"])
def test_a_hand_made_title_is_never_published_however_harmless(title):
    assert SD.classify_event(title, {}) is None
    assert SD.clean_title(title) == title  # it would pass the text checks; it is still not ours


@pytest.mark.parametrize("title", [
    "join discord.gg/abcdef now",  # invite
    "watch at twitch.tv/someone",  # link
    "see https://example.com",  # link
    f"shoutout <@{SNOWFLAKE}>",  # mention
    "hey @everyone",  # mention
    f"room {SNOWFLAKE}",  # ID
    "a" * 81,  # too long
    "   ",  # empty
    "line\nbreak\x00",  # control characters survive whitespace folding as NUL
    "`code`",  # markup
])
def test_free_text_titles_are_dropped_when_in_doubt(title):
    assert SD.classify_event(title, {}) is None
    assert SD.clean_title(title) is None


def test_blocked_words_drop_the_title(monkeypatch):
    monkeypatch.setattr(SD.textfilter, "check", lambda *a, **k: (False, "word"))
    assert SD.clean_title("anything") is None


def test_free_text_title_is_checked_with_links_refused(monkeypatch):
    seen = {}

    def check(text, **kwargs):
        seen.update(kwargs, text=text)
        return True, None

    monkeypatch.setattr(SD.textfilter, "check", check)
    assert SD.clean_title("  Movie   night ") == "Movie night"
    assert seen == {"text": "Movie night", "allow_links": False, "invite_allowlist": ()}


# ---------------------------------------------------------------- the document
def test_build_full_document():
    doc = SD.build(full_inputs())
    assert doc["v"] == 1 and doc["generated_at"] == schema.iso(T)
    assert doc["members"] == {"total": 15, "online": 6}
    assert doc["week"] == {"messages": 1240, "voice_hours": 34}
    assert [e["title"] for e in doc["events"]] == ["Valorant game night", "Watch party", "Valorant tournament",
                                                   "Town Hall"]
    assert doc["events"][0] == {"title": "Valorant game night", "kind": "gamenight", "game": "Valorant",
                                "start": schema.iso(T + 2 * DAY)}
    assert doc["events"][1] == {"title": "Watch party", "kind": "event", "start": schema.iso(T + 3 * DAY)}
    assert doc["events"][2]["kind"] == "tournament" and doc["events"][3]["kind"] == "event"
    assert "Movie" not in SD.render(doc) and "October" not in SD.render(doc)
    assert doc["games"] == [{"name": "Minecraft", "hours": 30}, {"name": "Valorant", "hours": 12}]  # Roblox: 2 players
    assert doc["tournaments"] == {"open": 1, "running": 0, "finished": 3}
    assert doc["matchmaking"] == {"matches_week": 4, "queued": 3}
    assert doc["seasonal"] == {"name": "Spooky Season", "emoji": "🎃", "ends": schema.iso(T + 10 * DAY)}
    assert doc["houses"] == [{"name": "Tide", "points": 34}, {"name": "Moss", "points": 12},
                             {"name": "Ember", "points": 12}]  # level houses keep the order they came in
    assert doc["hall"] == {"starred": 12, "clips": 30, "badges": 87}
    assert schema.validate(doc) == []
    assert SD.summary(doc) == ("members, week, events, games, tournaments, matchmaking, seasonal, houses, hall")


def test_empty_server_gives_only_the_envelope():
    doc = SD.build(SD.Inputs(now=T))
    assert doc == {"v": 1, "generated_at": schema.iso(T)}
    assert schema.validate(doc) == []
    assert "nothing yet" in SD.summary(doc)


def test_small_groups_are_suppressed():
    """Nothing that describes fewer than MIN_GROUP people is published."""
    doc = SD.build(full_inputs(
        online=2, message_people=2, voice_people=1, games=[("Valorant", 90 * HOUR, 2)],
        match_people=2, queued=2, starred_people=2, clip_people=1, badge_people=2))
    assert doc["members"] == {"total": 15}
    for key in ("week", "games", "matchmaking", "hall"):
        assert key not in doc, key
    assert "members" not in SD.build(full_inputs(member_count=2))
    assert SD.MIN_GROUP >= 3


def test_each_week_number_stands_on_its_own_group():
    doc = SD.build(full_inputs(message_people=3, voice_people=2))
    assert doc["week"] == {"messages": 1240}
    doc = SD.build(full_inputs(starred_people=3, clip_people=0, badge_people=1))
    assert doc["hall"] == {"starred": 12}


def test_events_window_privacy_and_cap():
    events = [SD.EventIn("Game night", T - 60),  # already over (not active)
              SD.EventIn("Game night", T - 600, active=True),  # running now
              SD.EventIn("Game night", T + SD.EVENT_HORIZON + 1),  # too far ahead
              SD.EventIn("Staff sync", T + HOUR, public=False),  # hidden channel
              SD.EventIn("see https://example.com", T + HOUR)]  # unsafe title
    doc = SD.build(full_inputs(events=events))
    assert doc["events"] == [{"title": "Game night", "kind": "gamenight", "start": schema.iso(T - 600)}]
    many = [SD.EventIn("Game night", T + i * HOUR) for i in range(1, 30)]
    assert len(SD.build(full_inputs(events=many))["events"]) == schema.MAX_EVENTS


def test_houses_are_all_or_nothing():
    assert "houses" not in SD.build(full_inputs(houses=None))
    assert "houses" not in SD.build(full_inputs(houses=[("Ember", 5, 3)]))  # one house is not a standing
    assert "houses" not in SD.build(full_inputs(houses=[("Ember", 5, 3), (f"<@{SNOWFLAKE}>", 9, 3)]))
    assert "houses" not in SD.build(full_inputs(houses=[("Ember", 5, 3), ("Tide", SNOWFLAKE, 3)]))
    assert "houses" not in SD.build(full_inputs(houses=[("Ember", 5, 3), ("Tide", "9", 3)]))
    assert "houses" not in SD.build(full_inputs(houses=[("Ember", 5, 3), (None, 9, 3)]))
    assert "houses" in SD.build(full_inputs(houses=[("Ember", 5, 3), ("Tide", 9, 3)]))


@pytest.mark.parametrize("people", [0, 1, 2, None, True, "3"])
def test_houses_need_a_group_behind_every_score(people):
    """One house scored by fewer than MIN_GROUP members takes the whole table with it: its
    number would be one or two members' week, and a gap would say the same."""
    rows = [("Fern", 34, 5), ("Kestrel", 12, people), ("Otter", 9, 4)]
    for order in (rows, rows[::-1], [rows[1], rows[0], rows[2]]):
        assert "houses" not in SD.build(full_inputs(houses=order))
    assert "houses" not in SD.build(full_inputs(houses=[("Fern", 0, 0), ("Kestrel", 0, 0), ("Otter", 0, 0)]))
    assert len(SD.build(full_inputs(houses=[("Fern", 34, 5), ("Kestrel", 12, 3), ("Otter", 9, 4)]))["houses"]) == 3


def test_seasonal_event_that_is_over_is_left_out():
    assert "seasonal" not in SD.build(full_inputs(seasonal=("Spooky Season", "🎃", T - 1)))
    assert "emoji" not in SD.build(full_inputs(seasonal=("Spooky Season", "", T + DAY)))["seasonal"]


# ---------------------------------------------------------------- schema / leaks
def walk(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)
    else:
        yield node


def test_no_value_looks_like_a_snowflake_or_a_mention():
    """The schema test: nothing in a built document is ID-shaped or mention-shaped, even when
    the inputs are hostile."""
    hostile = full_inputs(
        events=[SD.EventIn(f"<@{SNOWFLAKE}> birthday", T + DAY), SD.EventIn(f"{SNOWFLAKE}", T + DAY),
                SD.EventIn("@here game night", T + DAY), SD.EventIn("Valorant game night", T + DAY)],
        active_tournaments={f"<@{SNOWFLAKE}> birthday": "Valorant"},
        games=[("Valorant", 12 * HOUR, 4), (f"{SNOWFLAKE}", 99 * HOUR, 9), ("Not A Layout Game", 99 * HOUR, 9)],
        houses=[(f"{SNOWFLAKE}", 1, 9), ("Tide", 2, 9)],
    )
    for inputs in (full_inputs(), hostile):
        doc = SD.build(inputs)
        text = SD.render(doc)
        assert not re.search(r"\d{15,20}", text)
        for value in walk(doc):
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                assert len(str(abs(value))) < 15
            else:
                assert isinstance(value, str)
                assert not re.search(r"\d{15,20}", value) and "@" not in value and "<" not in value
    # the tournament with the hostile name is still a tournament: it shows under the bot's words
    assert [e["title"] for e in SD.build(hostile)["events"]] == ["Valorant tournament", "Valorant game night"]
    assert SD.build(hostile)["games"] == [{"name": "Valorant", "hours": 12}]


def test_game_names_in_the_document_come_from_layout_only():
    doc = SD.build(full_inputs())
    names = {g["name"] for g in doc["games"]} | {e["game"] for e in doc["events"] if "game" in e}
    assert names and names <= {g.role for g in config.GAMES}
    doc["games"][0]["name"] = "Some Obscure Game"
    assert any("$.games[0].name" in p for p in schema.validate(doc))


@pytest.mark.parametrize("path, value", [
    (("members", "total"), SNOWFLAKE),
    (("members", "total"), 10 ** 14),  # 15 digits
    (("members", "total"), -1),
    (("members", "total"), 1.5),
    (("members", "total"), True),
    (("members", "user_id"), 5),  # unknown key
    (("events", 0, "title"), f"hi <@{SNOWFLAKE}>"),
    (("events", 0, "title"), "hi <@&123>"),
    (("events", 0, "title"), "mail me@example.test"),
    (("events", 0, "title"), "1" * 15),
    (("events", 0, "title"), "discord.gg/abcdef"),
    (("events", 0, "title"), "x" * 81),
    (("events", 0, "kind"), "birthday"),
    (("events", 0, "start"), "tomorrow"),
    (("events", 0, "host"), "someone"),
    (("games", 0, "name"), "Not A Game"),
    (("houses", 0, "members"), 4),
    (("seasonal", "name"), "https://example.com"),
    (("top_chatters",), [1, 2, 3]),  # unknown section
    (("v",), 2),
    (("generated_at",), 1_800_000_000),
])
def test_validate_refuses(path, value):
    doc = json.loads(json.dumps(SD.build(full_inputs())))
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    problems = schema.validate(doc)
    assert problems, path
    assert str(SNOWFLAKE) not in " ".join(problems)  # problems name the place, never the value
    with pytest.raises(SD.Unsafe):
        SD.render(doc)


def test_render_is_stable_and_small():
    doc = SD.build(full_inputs())
    text = SD.render(doc)
    assert text == SD.render(json.loads(text)) and text.endswith("\n")
    assert len(text.encode("utf-8")) < 2000 < schema.MAX_BYTES


def test_size_cap():
    doc = SD.build(full_inputs())
    doc["events"] = [{"title": "x" * 80, "kind": "event", "start": schema.iso(T)}] * 8
    assert schema.validate(doc) == []
    big = schema.dumps(doc) + " " * schema.MAX_BYTES
    assert schema.check_text(big) == (None, [f"$: larger than {schema.MAX_BYTES} bytes"])
    # the worst document the schema allows still fits under the cap
    doc["houses"] = [{"name": "h" * 40, "points": schema.MAX_COUNT}] * schema.MAX_HOUSES
    assert len(SD.render(doc).encode("utf-8")) < schema.MAX_BYTES


def test_content_key_ignores_the_volatile_parts():
    a = SD.build(full_inputs())
    b = SD.build(full_inputs(now=T + 1800, online=9, seasonal=("Spooky Season", "🎃", T + 10 * DAY)))
    assert a != b and schema.content_key(a) == schema.content_key(b)
    c = SD.build(full_inputs(member_count=16))
    assert schema.content_key(a) != schema.content_key(c)
    assert "generated_at" in a and "online" in a["members"]  # content_key works on a copy
