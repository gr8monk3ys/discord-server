"""The weekly "is the games list still honest?" check (logic/gamelist.py): pure rules."""

import config
from logic import gamelist as GL
from logic.stats import Session

HOUR, DAY = 3600, 86400
END = 1_792_000_000
START = END - GL.WINDOW
A, B, C, D = 700_001, 700_002, 700_003, 700_004


def played(user, name, hours, ago_days=1.0):
    """One finished session of `hours`, ending `ago_days` before the window ends."""
    stop = END - int(ago_days * DAY)
    return Session(user, name, stop - int(hours * HOUR), stop)


def review(sessions, **kw):
    return GL.review(sessions, START, END, END, **kw)


def listed_all():
    """Somebody played every listed game, so nothing is worth retiring."""
    return [played(A, g.role, 1, ago_days=2 + i) for i, g in enumerate(config.GAMES)]


def test_the_thresholds_are_the_ones_staff_were_told():
    assert GL.WINDOW == 28 * DAY and GL.MIN_PLAYERS == 3 and GL.MIN_SECONDS == 5 * HOUR


def test_an_unlisted_game_needs_three_players_and_five_hours_combined():
    base = listed_all()
    three = [played(A, "Helldivers 2", 2), played(B, "Helldivers 2", 2), played(C, "Helldivers 2", 1)]
    r = review(base + three)
    assert r.add == (GL.Candidate("Helldivers 2", 3, 5 * HOUR),) and r.retire == ()
    # one minute short of five hours
    short = [played(A, "Helldivers 2", 2), played(B, "Helldivers 2", 2), played(C, "Helldivers 2", 59 / 60)]
    assert review(base + short).add == ()
    # two people, however long they played: one member's (or a pair's) game is never named
    assert review(base + [played(A, "Helldivers 2", 40), played(B, "Helldivers 2", 40)]).add == ()
    # the same member three times is still one player
    assert review(base + [played(A, "Helldivers 2", 3, ago_days=d) for d in (1, 2, 3)]).add == ()


def test_spellings_of_one_game_fold_together_and_the_most_played_one_is_shown():
    sessions = listed_all() + [played(A, "HELLDIVERS™ 2", 1), played(B, "Helldivers 2", 3),
                               played(C, "helldivers 2", 2)]
    (c,) = review(sessions).add
    assert (c.name, c.players, c.seconds) == ("Helldivers 2", 3, 6 * HOUR)


def test_listed_games_fold_presence_names_and_never_count_as_unlisted():
    sessions = [played(A, "Gears of War: E-Day", 3), played(B, "Gears of War: E-Day", 3),
                played(C, "Gears of War: Reloaded", 3), played(A, "Apex Legends with Medal", 2),
                played(B, "WARDOGS", 2), played(C, "Overwatch® 2", 2)]
    r = review(sessions)
    assert r.add == ()  # 9 h by three people in a listed game is not a suggestion
    assert r.retire == ("Risk of Rain 2", "Marvel Rivals", "Minecraft", "Tabletop Simulator")  # list order


def test_only_play_inside_the_window_counts():
    old = [played(u, "Helldivers 2", 9, ago_days=29) for u in (A, B, C)]  # ended before the window
    r = review(listed_all() + old)
    assert r.add == ()
    edge = [Session(u, "Helldivers 2", START - 10 * HOUR, START + 2 * HOUR) for u in (A, B, C)]  # 2 h each inside
    (c,) = review(listed_all() + edge).add
    assert c.seconds == 6 * HOUR
    stale = [played(A, config.GAMES[0].role, 5, ago_days=30)]
    assert config.GAMES[0].role in review(listed_all()[1:] + stale).retire


def test_open_sessions_count_up_to_now_and_blips_are_ignored():
    still_playing = [Session(u, "Helldivers 2", END - 2 * HOUR, None) for u in (A, B, C)]
    (c,) = review(listed_all() + still_playing).add
    assert c.seconds == 6 * HOUR
    blips = [Session(u, "Helldivers 2", END - 200, END - 100) for u in (A, B, C)]  # under five minutes each
    assert review(listed_all() + blips).add == ()


def test_excluded_members_never_count():
    sessions = listed_all() + [played(u, "Helldivers 2", 3) for u in (A, B, C)]
    assert len(review(sessions).add) == 1
    assert review(sessions, exclude={C}).add == ()  # C opted out of tracking: two players left
    # ...and an excluded member's play does not keep a listed game off the "worth retiring" list
    only_bot = [played(D, "Wardogs", 9)]
    assert "Wardogs" in review([s for s in listed_all() if s.key != "Wardogs"] + only_bot, exclude={D}).retire


def test_a_retired_game_people_play_again_is_suggested_by_its_own_name():
    sessions = listed_all() + [played(A, "VALORANT", 4), played(B, "Valorant", 4), played(C, "VALORANT", 4)]
    assert review(sessions).add == (GL.Candidate("Valorant", 3, 12 * HOUR, retired=True),)


def test_candidates_are_ordered_by_hours():
    sessions = listed_all()
    for name, each in (("Small", 2), ("Big", 9), ("Middle", 4)):
        sessions += [played(u, name, each) for u in (A, B, C)]
    assert [c.name for c in review(sessions).add] == ["Big", "Middle", "Small"]


def test_nothing_played_means_nothing_to_compare():
    r = review([])
    assert r.played is False and GL.digest_text(r) is None
    assert GL.digest_text(review([played(A, "Solo Game", 9)], exclude={A})) is None


def test_un_retiring_a_game_is_just_passing_the_lists():
    """What moving one line back into GAMES does to the report."""
    valorant = config.known_game_by_key("valorant")
    sessions = [played(u, "VALORANT", 4) for u in (A, B, C)]
    r = review(sessions, games=[*config.GAMES, valorant], retired=[g for g in config.RETIRED_GAMES if g != valorant])
    assert r.add == () and "Valorant" not in r.retire


# ---------------------------------------------------------------- what staff read
def test_digest_text_lists_both_directions_without_any_member():
    sessions = [played(u, "Helldivers 2", 10.4) for u in (A, B, C, D)]
    sessions += [played(u, "Deep Rock Galactic", 3) for u in (A, B, C)]
    sessions += [played(A, g.role, 1) for g in config.GAMES if g.role not in ("Wardogs", "Marvel Rivals")]
    text = GL.digest_text(review(sessions))
    assert text.splitlines() == [
        "Worth adding: **Helldivers 2** (4 players, 42 h) · **Deep Rock Galactic** (3 players, 9 h)",
        "Worth retiring: Marvel Rivals, Wardogs (nobody played in 28 days)",
        "Nothing changes by itself: edit GAMES in server/layout.py.",
    ]
    assert "<@" not in text and not any(str(u) in text for u in (A, B, C, D))
    assert len(text) <= 1024


def test_digest_text_when_the_list_is_right():
    assert GL.digest_text(review(listed_all())) == "No changes suggested. The list matches the last 28 days of play."


def test_presence_names_are_filtered_cut_and_escaped():
    assert GL.safe_name("Helldivers 2") == "Helldivers 2"
    assert GL.safe_name("**Bold** [x](https://evil.example)") is None  # a masked link is refused outright
    assert GL.safe_name("Star*Craft_II") == "Star\\*Craft\\_II"
    assert GL.safe_name("@everyone game") is not None and "@everyone" not in GL.safe_name("@everyone game")
    assert GL.safe_name("join discord.gg/raid") is None
    assert GL.safe_name("play at phish.com") is None
    assert GL.safe_name("n1gger simulator") is None
    assert GL.safe_name("  two\\nlines\\t ") == "two\\\\nlines\\\\t"  # literal backslashes are escaped
    assert GL.safe_name("line one\nline two") == "line one line two"
    assert GL.safe_name("") is None and GL.safe_name("\n\t ") is None
    long = GL.safe_name("x" * 200)
    assert len(long) == GL.MAX_NAME and long.endswith("…")


def test_a_refused_name_is_counted_not_shown_and_the_rest_still_show():
    sessions = listed_all() + [played(u, "free nitro discord.gg/raid", 9) for u in (A, B, C)]
    sessions += [played(u, "Fine *Game*", 3) for u in (A, B, C)]
    text = GL.digest_text(review(sessions))
    assert "discord.gg" not in text and "nitro" not in text
    assert text.splitlines()[0] == "Worth adding: **Fine \\*Game\\*** (3 players, 9 h) · 1 more not shown"
    only_bad = GL.digest_text(review(listed_all() + [played(u, "discord.gg/raid", 9) for u in (A, B, C)]))
    assert only_bad.splitlines()[0] == "Worth adding: 1 more not shown"


def test_long_lists_are_capped():
    sessions = listed_all()
    for i in range(GL.MAX_SHOWN + 3):
        sessions += [played(u, f"Game {chr(65 + i)}", 2 + i) for u in (A, B, C)]
    first = GL.digest_text(review(sessions)).splitlines()[0]
    assert first.count("**") == 2 * GL.MAX_SHOWN and first.endswith("· 3 more not shown")


def test_a_retired_candidate_is_marked():
    sessions = listed_all() + [played(u, "Counter-Strike 2", 5) for u in (A, B, C)]
    assert GL.digest_text(review(sessions)).splitlines()[0] == (
        "Worth adding: **Counter-Strike 2** (retired, 3 players, 15 h)")
