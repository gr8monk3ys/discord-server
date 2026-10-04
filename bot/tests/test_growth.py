from logic import growth as G
from logic.growth import Join

A, B, C, D, E = 1, 2, 3, 4, 5
DISBOARD = 302050872383242240
OTHER_BOT = 77
DAY = 24 * 60 * 60
NOW = 100 * DAY


# --- attribute_join: which invite was used ---


def test_single_increment_is_attributed():
    old = {"aaa": (A, 3), "bbb": (B, 1)}
    new = {"aaa": (A, 4), "bbb": (B, 1)}
    assert G.attribute_join(old, new) == "aaa"


def test_no_change_is_none():
    old = {"aaa": (A, 3)}
    assert G.attribute_join(old, dict(old)) is None


def test_empty_snapshots_are_none():
    assert G.attribute_join({}, {}) is None


def test_two_codes_up_is_ambiguous():
    old = {"aaa": (A, 3), "bbb": (B, 1)}
    new = {"aaa": (A, 4), "bbb": (B, 2)}
    assert G.attribute_join(old, new) is None


def test_one_code_up_by_two_is_ambiguous():
    # Two people joined between snapshots; we can't tell which join is which.
    old = {"aaa": (A, 3)}
    new = {"aaa": (A, 5)}
    assert G.attribute_join(old, new) is None


def test_one_use_invite_disappearing_is_attributed():
    old = {"aaa": (A, 3), "once": (B, 0)}
    new = {"aaa": (A, 3)}
    assert G.attribute_join(old, new) == "once"


def test_two_disappearing_is_ambiguous():
    old = {"x": (A, 0), "y": (B, 0)}
    assert G.attribute_join(old, {}) is None


def test_increment_wins_over_a_disappearance():
    # An expired/revoked invite vanishing at the same time doesn't hide a clear +1.
    old = {"aaa": (A, 3), "old": (B, 0)}
    new = {"aaa": (A, 4)}
    assert G.attribute_join(old, new) == "aaa"


def test_invite_created_between_snapshots_and_used_once():
    old = {"aaa": (A, 3)}
    new = {"aaa": (A, 3), "fresh": (C, 1)}
    assert G.attribute_join(old, new) == "fresh"


def test_invite_created_between_snapshots_unused_is_ignored():
    old = {"aaa": (A, 3)}
    new = {"aaa": (A, 4), "fresh": (C, 0)}
    assert G.attribute_join(old, new) == "aaa"


def test_vanity_increment_is_attributed_without_inviter():
    old = {"aaa": (A, 3), "myserver": (None, 40)}
    new = {"aaa": (A, 3), "myserver": (None, 41)}
    assert G.attribute_join(old, new) == "myserver"
    assert G.inviter_of("myserver", old, new) is None


def test_uses_going_down_is_not_a_join():
    old = {"aaa": (A, 3)}
    new = {"aaa": (A, 2)}
    assert G.attribute_join(old, new) is None


def test_inviter_of_prefers_new_then_old():
    old = {"once": (B, 0)}
    assert G.inviter_of("once", old, {}) == B
    assert G.inviter_of("aaa", {"aaa": (A, 1)}, {"aaa": (A, 2)}) == A
    assert G.inviter_of(None, old, {}) is None
    assert G.inviter_of("missing", old, {}) is None


# --- stayed / summaries ---


def j(user, inviter, joined, left=None):
    return Join(user, inviter, joined, left)


def test_stayed_needs_three_days():
    assert G.STAY_SECONDS == 3 * DAY
    assert not G.stayed(j(C, A, NOW - 3 * DAY + 1), NOW)
    assert G.stayed(j(C, A, NOW - 3 * DAY), NOW)


def test_left_before_three_days_did_not_stay():
    assert not G.stayed(j(C, A, NOW - 10 * DAY, left=NOW - 10 * DAY + 2 * DAY), NOW)


def test_left_after_three_days_still_stayed():
    assert G.stayed(j(C, A, NOW - 10 * DAY, left=NOW - 10 * DAY + 4 * DAY), NOW)


def test_summary_counts_distinct_people():
    joins = [
        j(C, A, NOW - 10 * DAY),  # stayed, here
        j(D, A, NOW - 10 * DAY, left=NOW - 9 * DAY),  # left after a day
        j(E, A, NOW - DAY),  # here, too new to count as stayed
        j(B, C, NOW - 10 * DAY),  # someone else's invite
    ]
    s = G.summary(joins, A, NOW)
    assert (s.total, s.still_here, s.stayed) == (3, 2, 1)


def test_summary_rejoin_counts_once_and_uses_latest_row_for_still_here():
    joins = [
        j(C, A, NOW - 20 * DAY, left=NOW - 19 * DAY),
        j(C, A, NOW - 10 * DAY),  # came back through A again and stayed
    ]
    s = G.summary(joins, A, NOW)
    assert (s.total, s.still_here, s.stayed) == (1, 1, 1)


def test_summary_rejoin_then_left_is_not_here():
    joins = [j(C, A, NOW - 20 * DAY), j(C, A, NOW - 5 * DAY, left=NOW - DAY)]
    s = G.summary(joins, A, NOW)
    # Left now, but the first stay was long enough.
    assert (s.total, s.still_here, s.stayed) == (1, 0, 1)


def test_self_invite_and_null_inviter_never_count():
    joins = [j(A, A, NOW - 10 * DAY), j(C, None, NOW - 10 * DAY)]
    assert G.summary(joins, A, NOW).total == 0
    assert G.stayed_counts(joins, NOW) == {}


def test_stayed_counts_per_inviter_with_since():
    joins = [
        j(C, A, NOW - 40 * DAY),  # outside a 30-day window
        j(D, A, NOW - 10 * DAY),
        j(E, A, NOW - 10 * DAY),
        j(C, B, NOW - 5 * DAY),
        j(D, B, NOW - DAY),  # too new
    ]
    assert G.stayed_counts(joins, NOW) == {A: 3, B: 1}
    assert G.stayed_counts(joins, NOW, since=NOW - 30 * DAY) == {A: 2, B: 1}


def test_stayed_counts_same_person_counts_once_per_inviter():
    joins = [j(C, A, NOW - 20 * DAY, left=NOW - 15 * DAY), j(C, A, NOW - 10 * DAY)]
    assert G.stayed_counts(joins, NOW) == {A: 1}


# --- recruiter threshold ---


def test_recruiter_threshold():
    assert G.RECRUITER_THRESHOLD == 3
    assert G.recruiters({A: 3, B: 2, C: 5}) == {A, C}
    assert G.recruiters({}) == set()


# --- bump detection ---


def test_disboard_bump_with_bump_done_embed_is_confirmed():
    assert G.bump_status(DISBOARD, "bump", ["Bump done! :thumbsup:"]) == G.BUMP_CONFIRMED


def test_bump_done_is_case_insensitive():
    assert G.bump_status(DISBOARD, "bump", ["", "please... BUMP DONE"]) == G.BUMP_CONFIRMED


def test_other_bot_is_not_a_bump():
    assert G.bump_status(OTHER_BOT, "bump", ["Bump done!"]) is None


def test_non_bump_command_is_not_a_bump():
    assert G.bump_status(DISBOARD, "help", ["Bump done!"]) is None


def test_readable_embed_without_bump_done_is_not_a_bump():
    # Disboard's cooldown reply: "Please wait another 1 hour..."
    assert G.bump_status(DISBOARD, "bump", ["Please wait another 87 minutes until the server can be bumped"]) is None


def test_missing_embed_still_counts_unverified():
    assert G.bump_status(DISBOARD, "bump", []) == G.BUMP_UNVERIFIED
    assert G.bump_status(DISBOARD, "bump", ["", None]) == G.BUMP_UNVERIFIED


def test_unknown_command_name_needs_the_embed():
    assert G.bump_status(DISBOARD, None, ["Bump done!"]) == G.BUMP_CONFIRMED
    assert G.bump_status(DISBOARD, None, []) is None


def test_reminder_due():
    assert G.REMINDER_DELAY == 2 * 60 * 60
    assert G.reminder_due_at(NOW) == NOW + 2 * 60 * 60
    assert not G.reminder_ready(None, NOW)
    assert not G.reminder_ready(NOW + 1, NOW)
    assert G.reminder_ready(NOW, NOW)
    assert G.reminder_ready(NOW - 5 * DAY, NOW)
