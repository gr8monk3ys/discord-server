import pytest

from logic import lfg
from logic.lfg import Join, Leave, Roster

HOST, A, B, C = 1, 2, 3, 4


def test_new_roster_has_host_first():
    r = Roster.new(HOST, 3)
    assert r.members == (HOST,)
    assert lfg.count_label(r) == "1 / 3"
    assert not r.full


def test_join_until_full():
    r = Roster.new(HOST, 3)
    r, res, became_full = r.join(A)
    assert (res, became_full) == (Join.JOINED, False)
    r, res, became_full = r.join(B)
    assert (res, became_full) == (Join.JOINED, True)
    assert r.full and r.members == (HOST, A, B)


def test_join_when_full_or_already_in():
    r = Roster(HOST, 2, (HOST, A))
    assert r.join(B)[1:] == (Join.FULL, False)
    assert r.join(A)[1:] == (Join.ALREADY_IN, False)
    assert r.join(HOST)[1:] == (Join.ALREADY_IN, False)
    assert r.join(B)[0] is r  # unchanged


def test_leave_reopens_a_spot():
    r = Roster(HOST, 2, (HOST, A))
    r, res = r.leave(A)
    assert res is Leave.LEFT and not r.full and r.members == (HOST,)
    # Rejoining after a drop fills it again.
    assert r.join(B)[1:] == (Join.JOINED, True)


def test_leave_rules():
    r = Roster(HOST, 3, (HOST, A))
    assert r.leave(B)[1] is Leave.NOT_IN
    assert r.leave(HOST)[1] is Leave.HOST  # host closes instead


def test_resize():
    r = Roster(HOST, 4, (HOST, A, B))
    assert r.resize(3).full
    assert r.resize(6).size == 6
    assert r.resize(2) is None  # below current headcount
    assert r.resize(lfg.MAX_PLAYERS + 1) is None
    assert Roster.new(HOST, 5).resize(lfg.MIN_PLAYERS - 1) is None


@pytest.mark.parametrize("user,keeper,ok", [(HOST, False, True), (A, True, True), (A, False, False)])
def test_can_close(user, keeper, ok):
    assert lfg.can_close(user, HOST, keeper) is ok


def test_expiry_at_three_hours():
    assert not lfg.is_expired(1_000, 1_000 + lfg.EXPIRY_SECONDS - 1)
    assert lfg.is_expired(1_000, 1_000 + lfg.EXPIRY_SECONDS)


def test_titles_respect_discord_limit():
    assert lfg.title("Valorant", None) == "Valorant"
    assert lfg.title("Valorant", "Ranked") == "Valorant · Ranked"
    long = lfg.title("x" * 150, "Ranked")
    assert len(lfg.closed_title(long)) <= 100
    assert lfg.closed_title("Valorant") == "✓ Valorant"
    assert lfg.closed_title("✓ Valorant") == "✓ Valorant"  # never double-prefixed


def test_voice_hint():
    assert lfg.voice_hint(5) == "squad"
    assert lfg.voice_hint(6) == "lobby"
