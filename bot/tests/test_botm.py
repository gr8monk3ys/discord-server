"""logic/botm.py: months, the two-months-running rule, state and the announcement."""

from datetime import datetime

from logic import botm as B


def test_month_key_and_label():
    d = datetime(2027, 1, 2, 18)
    assert B.month_key(d) == "2027-01" and B.month_label(d) == "January 2027"


def test_state_round_trip_and_bad_values():
    s = B.State(month="2027-01", winners=(1, 2), before=(3,))
    assert B.load(B.dump(s)) == s
    for bad in (None, "", "nope", "[]", '{"winners": ["x"]}'):
        assert B.load(bad) == B.State()


def test_two_months_running_is_refused():
    jan = B.award(B.State(), "2027-01", [1, 2])
    assert jan == B.State(month="2027-01", winners=(1, 2), before=())
    assert B.repeat_winners(jan, "2027-02", [3, 2]) == [2]
    assert B.repeat_winners(jan, "2027-02", [3, 4]) == []
    feb = B.award(jan, "2027-02", [3, 3, 4])
    assert feb == B.State(month="2027-02", winners=(3, 4), before=(1, 2))
    # re-running in February corrects February: checked against January, not against itself
    assert B.repeat_winners(feb, "2027-02", [3, 5]) == []
    assert B.repeat_winners(feb, "2027-02", [1]) == [1]
    assert B.award(feb, "2027-02", [5]) == B.State(month="2027-02", winners=(5,), before=(1, 2))


def test_announcement():
    assert B.announcement(["<@1>"], "January 2027") == (
        "🏰 **Base of the Month, January 2027:** <@1>! The Base of the Month role is yours for the month.")
    text = B.announcement(["<@1>", "<@2>", "<@3>"], "March 2027", "We dug the whole hill out.")
    assert "<@1>, <@2> and <@3>!" in text and text.endswith("\n> We dug the whole hill out.")
