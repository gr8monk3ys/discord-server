import config
from logic import matchmaking as M
from logic.matchmaking import Entry

NOW = 1_800_000_000


def entry(uid, at=NOW, game="overwatch", mode="Ranked", size=2):
    return Entry(uid, game, mode, size, at)


# ---------------------------------------------------------------- sizes
def test_default_sizes_by_game():
    assert M.default_size("overwatch") == 5
    assert M.default_size("riskofrain2") == 4
    assert M.default_size("marvelrivals") == 5
    assert M.default_size("apexlegends") == 4
    assert M.default_size("valorant") == 5  # a retired game keeps its size for the day it comes back
    assert M.default_size("minecraft") == 2
    assert M.default_size("unknown") == 2


def test_default_size_keys_are_real_games():
    keys = {g.key for g in (*config.GAMES, *config.RETIRED_GAMES)}
    assert set(M.DEFAULT_SIZES) <= keys
    assert all(M.MIN_SIZE <= n <= M.MAX_SIZE for n in M.DEFAULT_SIZES.values())


def test_resolve_size_member_choice_wins_and_is_clamped():
    assert M.resolve_size("overwatch", None) == 5
    assert M.resolve_size("overwatch", 3) == 3
    assert M.resolve_size("minecraft", 9) == 5
    assert M.resolve_size("minecraft", 1) == 2


def test_resolve_game_by_key_or_name():
    assert M.resolve_game("overwatch").role == "Overwatch"
    assert M.resolve_game("Risk of Rain 2").key == "riskofrain2"
    assert M.resolve_game("risk-of-rain-2").key == "riskofrain2"
    assert M.resolve_game("Chess") is None
    assert M.resolve_game("valorant") is None and M.resolve_game("Counter-Strike 2") is None  # retired: no queue
    assert M.resolve_game("") is None
    assert M.resolve_game(None) is None


def test_resolve_mode():
    assert M.resolve_mode("ranked") == "Ranked"
    assert M.resolve_mode("Casual") == "Casual"
    assert M.resolve_mode("hardcore") is None
    assert M.resolve_mode(None) is None


# ---------------------------------------------------------------- expiry
def test_expiry_at_one_hour():
    assert not M.expired(NOW - M.QUEUE_TTL + 1, NOW)
    assert M.expired(NOW - M.QUEUE_TTL, NOW)
    assert M.expires_in(NOW - 600, NOW) == M.QUEUE_TTL - 600
    assert M.expires_in(NOW - 2 * M.QUEUE_TTL, NOW) == 0


# ---------------------------------------------------------------- popping
def test_pick_needs_a_full_bucket():
    assert M.pick([entry(1)], 2, NOW) is None
    picked = M.pick([entry(1), entry(2)], 2, NOW)
    assert [e.user_id for e in picked] == [1, 2]


def test_pick_takes_longest_waiting_first():
    es = [entry(3, NOW - 10), entry(1, NOW - 30), entry(2, NOW - 20)]
    assert [e.user_id for e in M.pick(es, 2, NOW)] == [1, 2]


def test_pick_ties_break_by_user_id():
    es = [entry(9), entry(4), entry(6)]
    assert [e.user_id for e in M.pick(es, 2, NOW)] == [4, 6]


def test_pick_skips_expired_entries():
    es = [entry(1, NOW - M.QUEUE_TTL), entry(2), entry(3)]
    assert [e.user_id for e in M.pick(es, 2, NOW)] == [2, 3]
    assert M.pick([entry(1, NOW - M.QUEUE_TTL), entry(2)], 2, NOW) is None


def test_pick_skips_members_who_left():
    es = [entry(1, NOW - 50), entry(2), entry(3)]
    assert [e.user_id for e in M.pick(es, 2, NOW, present=lambda u: u != 1)] == [2, 3]


def test_bucket_is_game_mode_size():
    assert entry(1).bucket == ("overwatch", "Ranked", 2)


def test_members_round_trip():
    assert M.decode_members(M.encode_members([3, 1, 2])) == [3, 1, 2]
    assert M.decode_members("") == []
    assert M.decode_members(None) == []


# ---------------------------------------------------------------- status
def test_summary_counts_live_entries_and_marks_mine():
    es = [
        entry(1, game="apexlegends", mode="Casual", size=4),
        entry(2, game="overwatch", size=5),
        entry(3, game="overwatch", size=5),
        entry(4, NOW - M.QUEUE_TTL, game="overwatch", size=5),  # expired
        entry(5, game="overwatch", mode="Casual", size=5),
    ]
    rows = M.summary(es, me=2, now=NOW)
    # config.GAMES order: Overwatch before Apex Legends; Ranked before Casual.
    assert [(r.game, r.mode, r.size, r.waiting, r.mine) for r in rows] == [
        ("overwatch", "Ranked", 5, 2, True),
        ("overwatch", "Casual", 5, 1, False),
        ("apexlegends", "Casual", 4, 1, False),
    ]


def test_summary_expired_me_is_not_mine():
    rows = M.summary([entry(1, NOW - M.QUEUE_TTL)], me=1, now=NOW)
    assert rows == []


# ---------------------------------------------------------------- names
def test_channel_name():
    val = M.resolve_game("overwatch")
    assert M.channel_name(val, "overwatch") == f"{val.emoji} Overwatch match"
    assert M.channel_name(None, "chess") == "🎮 chess match"
    assert M.channel_name(None, "") == "🎮 Game match"
    assert len(M.channel_name(None, "x" * 300)) <= 100


def test_empty_for():
    assert M.empty_for({}, 1, NOW) == 0
    assert M.empty_for({1: NOW - 30}, 1, NOW) == 30
