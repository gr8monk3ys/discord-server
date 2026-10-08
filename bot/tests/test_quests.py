"""Pure rules for the starter quest (logic/quests.py)."""

import config
from logic import quests as Q

DAY = 24 * 3600
T0 = 1_790_000_000
OLD = T0 - 365 * DAY  # an established account
ALL = {s.key for s in Q.STEPS}


def test_five_steps_in_order_two_need_tracking():
    assert [s.key for s in Q.STEPS] == ["pick_roles", "say_hi", "join_squad", "claim_daily", "join_voice"]
    assert {s.key for s in Q.STEPS if s.tracking} == {"say_hi", "join_voice"}
    assert set(Q.BY_KEY) == ALL


def test_required_steps_follow_privacy():
    assert Q.required(True) == [s.key for s in Q.STEPS]
    assert Q.required(False) == ["pick_roles", "join_squad", "claim_daily"]


def test_complete_and_progress():
    assert not Q.is_complete(set(), True)
    assert not Q.is_complete(ALL - {"join_voice"}, True)
    assert Q.is_complete(ALL, True)
    # opted out: the tracking steps aren't needed
    assert Q.is_complete({"pick_roles", "join_squad", "claim_daily"}, False)
    assert Q.progress({"pick_roles", "say_hi"}, True) == (2, 5)
    assert Q.progress({"pick_roles", "say_hi"}, False) == (1, 3)
    assert Q.progress({"bogus"}, True) == (0, 5)


def test_has_picked_roles_platform_region_or_game():
    assert not Q.has_picked_roles([])
    assert not Q.has_picked_roles(["@everyone", "LFG", "Squad"])
    assert Q.has_picked_roles([config.PLATFORM_ROLES[0]])
    assert Q.has_picked_roles([config.REGION_ROLES[-1]])
    assert Q.has_picked_roles([config.GAMES[0].role])
    assert Q.has_picked_roles([config.PLATFORM_ROLES[1].upper()])  # names compared as slugs


def test_reward_only_for_members_who_joined_after_start_and_recently():
    start = T0
    assert Q.eligible_for_reward(start + 10, start, start + 20, created_at=OLD)
    assert Q.eligible_for_reward(start, start, start + Q.NEW_MEMBER_DAYS * DAY, created_at=OLD)
    assert not Q.eligible_for_reward(start - 1, start, start + 10, created_at=OLD)  # joined before the module
    assert not Q.eligible_for_reward(start + 10, start, start + 10 + Q.NEW_MEMBER_DAYS * DAY + 1, created_at=OLD)  # too late
    assert not Q.eligible_for_reward(None, start, start + 10, created_at=OLD)
    assert not Q.eligible_for_reward(start + 10, None, start + 20, created_at=OLD)
    # Young accounts (likely alts) are never paid; unknown age isn't either.
    young = start + 20 - (Q.MIN_ACCOUNT_DAYS * DAY - 1)
    assert not Q.eligible_for_reward(start + 10, start, start + 20, created_at=young)
    assert Q.eligible_for_reward(start + 10, start, start + 20, created_at=young - 1)
    assert not Q.eligible_for_reward(start + 10, start, start + 20, created_at=None)


def test_nudge_window():
    start = T0
    joined = start + 100
    assert not Q.should_nudge(joined, start, joined + DAY - 1, 0)  # too early
    assert Q.should_nudge(joined, start, joined + DAY, 0)
    assert Q.should_nudge(joined, start, joined + DAY, Q.NUDGE_BELOW - 1)
    assert not Q.should_nudge(joined, start, joined + DAY, Q.NUDGE_BELOW)  # doing fine
    assert not Q.should_nudge(joined, start, joined + Q.NUDGE_UNTIL + 1, 0)  # long gone quiet
    assert not Q.should_nudge(start - 1, start, start + 2 * DAY, 0)  # joined before the module
    assert not Q.should_nudge(None, start, joined + DAY, 0)


def test_checklist_marks_done_and_skipped():
    text = Q.checklist({"pick_roles"}, True)
    lines = text.splitlines()
    assert len(lines) == 5
    assert lines[0].startswith("✅") and "Pick your roles" in lines[0]
    assert all(line.startswith("▫️") for line in lines[1:])
    off = Q.checklist(set(), False).splitlines()
    skipped = [line for line in off if line.startswith("➖")]
    assert len(skipped) == 2 and any("/privacy" in line for line in skipped)


def test_texts():
    assert Q.ref(42) == "quest:42"
    msg = Q.congrats("<@42>", Q.REWARD, badge=False)
    assert "<@42>" in msg and f"{Q.REWARD:,}" in msg
    assert "badge" in Q.congrats("<@42>", Q.REWARD, badge=True).lower()
    nudge = Q.nudge_text({"pick_roles"}, True)
    assert "/quest" in nudge and "✅" in nudge and f"{Q.REWARD:,}" in nudge
    assert "/quest" in Q.nudge_text(set(), True, rewarded=False)
    assert f"{Q.REWARD:,}" not in Q.nudge_text(set(), True, rewarded=False)
