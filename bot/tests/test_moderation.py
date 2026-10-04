"""Unit tests for logic.moderation: target checks, escalation, anti-spam and
anti-raid trackers, the raid restore plan and the text the mods and members see."""

from types import SimpleNamespace

import config
from logic import moderation as M
from logic.moderation import Problem

T0 = 1_790_000_000
HOUR = 60 * 60
DAY = 24 * HOUR


def roles(*names):
    return [SimpleNamespace(name=n) for n in names]


# ---------------------------------------------------------------- staff
def test_is_staff_owner_admin_keeper_moderator():
    assert M.is_staff(True, False, roles())
    assert M.is_staff(False, True, roles())
    assert M.is_staff(False, False, roles("@everyone", config.KEEPER_ROLE))
    assert M.is_staff(False, False, roles("🛡️ moderator"))
    assert not M.is_staff(False, False, roles("@everyone", "LFG", "Squad"))


# ---------------------------------------------------------------- target checks
def check(**kw):
    base = dict(actor_id=1, target_id=2, bot_id=99, target_is_staff=False,
                actor_is_owner=False, actor_top=10, target_top=5, bot_top=20)
    base.update(kw)
    return M.check_target(**base)


def test_check_target_ok():
    assert check() is None


def test_check_target_refuses_self_bot_and_staff():
    assert check(target_id=1) is Problem.SELF
    assert check(target_id=99) is Problem.BOT_SELF
    assert check(target_is_staff=True) is Problem.STAFF


def test_check_target_hierarchy_actor():
    assert check(target_top=10) is Problem.ABOVE_YOU  # equal is not below
    assert check(target_top=11) is Problem.ABOVE_YOU
    assert check(target_top=11, actor_is_owner=True) is None  # owner outranks everyone


def test_check_target_hierarchy_bot():
    assert check(target_top=20, actor_top=30) is Problem.ABOVE_BOT
    assert check(target_top=25, actor_is_owner=True) is Problem.ABOVE_BOT


def test_every_problem_has_a_reply():
    assert set(M.TARGET_REPLIES) == set(Problem)


# ---------------------------------------------------------------- escalation
def test_escalation_thresholds():
    assert [M.escalation(n) for n in range(0, 7)] == [None, None, None, HOUR, HOUR, DAY, DAY]


# ---------------------------------------------------------------- anti-spam
def msg(t, mid, content="hi", mentions=0, channel=7):
    return M.SpamMessage(at=t, message_id=mid, channel_id=channel,
                         content_key=M.content_key(content), mentions=mentions)


def test_content_key_normalises_and_empty_is_none():
    assert M.content_key("  Hello   THERE ") == M.content_key("hello there")
    assert M.content_key("") is None and M.content_key("   ") is None
    assert M.content_key("a") != M.content_key("b")


def test_rate_six_messages_in_five_seconds():
    tr = M.SpamTracker()
    for i in range(5):
        assert tr.add(1, msg(T0 + i * 0.9, i, content=f"m{i}")) is None
    hit = tr.add(1, msg(T0 + 4.9, 5, content="m5"))
    assert hit.reason is M.SpamReason.RATE
    assert sorted(m.message_id for m in hit.burst) == [0, 1, 2, 3, 4, 5]


def test_rate_spread_out_is_fine():
    tr = M.SpamTracker()
    for i in range(20):
        assert tr.add(1, msg(T0 + i * 1.1, i, content=f"m{i}")) is None


def test_rate_is_per_user():
    tr = M.SpamTracker()
    for i in range(10):
        assert tr.add(i % 2, msg(T0 + i * 0.4, i, content=f"m{i}")) is None


def test_three_identical_in_thirty_seconds():
    tr = M.SpamTracker()
    assert tr.add(1, msg(T0, 1, "buy coins")) is None
    assert tr.add(1, msg(T0 + 10, 2, "other")) is None
    assert tr.add(1, msg(T0 + 20, 3, "BUY coins")) is None
    hit = tr.add(1, msg(T0 + 29, 4, "buy  coins"))
    assert hit.reason is M.SpamReason.DUPLICATE
    assert [m.message_id for m in hit.burst] == [1, 3, 4]


def test_identical_spread_beyond_thirty_seconds_is_fine():
    tr = M.SpamTracker()
    for i in range(5):
        assert tr.add(1, msg(T0 + i * 16, i, "gg")) is None


def test_empty_messages_never_count_as_identical():
    tr = M.SpamTracker()
    for i in range(4):
        assert tr.add(1, msg(T0 + i * 6, i, "")) is None


def test_mass_mentions_in_one_message():
    tr = M.SpamTracker()
    assert tr.add(1, msg(T0, 1, "hey", mentions=4)) is None
    hit = tr.add(1, msg(T0 + 1, 2, "hey all", mentions=5))
    assert hit.reason is M.SpamReason.MENTIONS
    assert [m.message_id for m in hit.burst] == [2]


def test_hit_clears_history_and_cools_down():
    tr = M.SpamTracker()
    for i in range(6):
        hit = tr.add(1, msg(T0 + i * 0.1, i, content=f"m{i}"))
    assert hit is not None
    # In-flight messages right after the hit don't open a second case.
    for i in range(6, 12):
        assert tr.add(1, msg(T0 + 1 + i * 0.1, i, content=f"m{i}")) is None
    # After the cooldown, a fresh burst is caught again.
    later = T0 + M.SPAM_COOLDOWN + 5
    hits = [tr.add(1, msg(later + i * 0.1, 100 + i, content=f"x{i}")) for i in range(6)]
    assert hits[-1] is not None


def test_tracker_forgets_idle_users():
    tr = M.SpamTracker()
    tr.add(1, msg(T0, 1))
    tr.add(2, msg(T0, 2))
    tr.add(3, msg(T0 + 120, 3))
    assert set(tr.users) == {3}


# ---------------------------------------------------------------- anti-raid
def test_raid_eight_joins_in_a_minute():
    tr = M.RaidTracker()
    for i in range(7):
        assert not tr.add(T0 + i * 5, young=False)
    assert tr.add(T0 + 40, young=False)


def test_raid_young_accounts_count_double():
    tr = M.RaidTracker()
    for i in range(3):
        assert not tr.add(T0 + i, young=True)  # 6
    assert tr.add(T0 + 10, young=True)  # 8


def test_raid_slow_joins_never_trigger():
    tr = M.RaidTracker()
    for i in range(50):
        assert not tr.add(T0 + i * 10, young=False)  # at most 6 per 60 s


def test_raid_trigger_resets_window():
    tr = M.RaidTracker()
    for i in range(8):
        tr.add(T0 + i, young=False)
    assert not tr.add(T0 + 9, young=False)


def test_is_young():
    assert M.is_young(T0 - 6 * DAY, T0)
    assert not M.is_young(T0 - 7 * DAY, T0)


# ---------------------------------------------------------------- lockdown / restore
def test_lockdown_raises_level_and_pauses_invites():
    state = M.lockdown(T0, prev_level=1, prev_invites_until=None)
    assert state == M.RaidState(until=T0 + M.RAID_LOCK, prev_level=1, prev_invites_until=None,
                                raised=True, started=T0)
    assert M.RaidState.loads(state.dumps()) == state


def test_lockdown_keeps_stricter_level():
    assert not M.lockdown(T0, prev_level=3, prev_invites_until=None).raised
    assert not M.lockdown(T0, prev_level=4, prev_invites_until=None).raised


def test_lockdown_extend_keeps_original_previous_values():
    first = M.lockdown(T0, prev_level=1, prev_invites_until=None)
    again = M.extend(first, T0 + 600)
    assert again.until == T0 + 600 + M.RAID_LOCK
    assert (again.prev_level, again.prev_invites_until, again.raised, again.started) == (1, None, True, T0)


def test_restore_plan_not_due():
    state = M.lockdown(T0, 1, None)
    assert M.restore_plan(state, T0 + M.RAID_LOCK - 1, current_level=3) is None


def test_restore_plan_resets_level_and_invites():
    state = M.lockdown(T0, 1, None)
    plan = M.restore_plan(state, T0 + M.RAID_LOCK, current_level=3)
    assert plan == M.Restore(level=1, invites_until=None)


def test_restore_plan_leaves_level_a_mod_changed():
    state = M.lockdown(T0, 1, None)
    assert M.restore_plan(state, T0 + M.RAID_LOCK, current_level=4) == M.Restore(level=None, invites_until=None)


def test_restore_plan_level_not_raised():
    state = M.lockdown(T0, 3, None)
    assert M.restore_plan(state, T0 + M.RAID_LOCK, current_level=3).level is None


def test_restore_plan_restores_a_longer_earlier_invite_pause():
    later = T0 + 5 * HOUR
    state = M.lockdown(T0, 1, later)
    assert M.restore_plan(state, T0 + M.RAID_LOCK, 3).invites_until == later
    # ...but not one that has run out meanwhile.
    state = M.lockdown(T0, 1, T0 + 60)
    assert M.restore_plan(state, T0 + M.RAID_LOCK, 3).invites_until is None


def test_raid_state_loads_bad_json_is_none():
    assert M.RaidState.loads("not json") is None
    assert M.RaidState.loads(None) is None


# ---------------------------------------------------------------- text
def test_fmt_duration():
    assert M.fmt_duration(600) == "10 min"
    assert M.fmt_duration(3600) == "1 h"
    assert M.fmt_duration(5400) == "1 h 30 min"
    assert M.fmt_duration(DAY) == "24 h"
    assert M.fmt_duration(3 * DAY) == "3 days"


def test_dm_text():
    warn = M.dm_text("warn", "Cool Server", "spamming memes", None, warns=2)
    assert "warning" in warn and "Cool Server" in warn and "spamming memes" in warn and "2" in warn
    timeout = M.dm_text("timeout", "Cool Server", "flaming", 3600)
    assert "timed out" in timeout and "1 h" in timeout and "flaming" in timeout
    auto = M.dm_text("auto_timeout", "S", "Automatic: 3 warnings in 30 days", 3600)
    assert "timed out" in auto
    spam = M.dm_text("spam", "S", "Sending messages too fast", 600)
    assert "10 min" in spam


def test_case_line_and_list():
    line = M.case_line(12, "timeout", "<@2>", "<@1>", "flaming", 3600)
    assert "#12" in line and "<@2>" in line and "<@1>" in line and "flaming" in line and "1 h" in line
    auto = M.case_line(13, "auto_timeout", "<@2>", None, "Automatic", 3600)
    assert "Front Desk" in auto or "automatic" in auto.lower()
    rows = [dict(id=3, kind="warn", reason="r1", at=T0, duration=None, mod_id=1),
            dict(id=4, kind="spam", reason="r2", at=T0 + 5, duration=600, mod_id=None)]
    text = M.cases_text(rows)
    assert text.index("#4") < text.index("#3")  # newest first
    assert f"<t:{T0}:R>" in text and "10 min" in text
    assert M.cases_text([]) == M.NO_CASES


def test_reason_is_trimmed_to_limit():
    assert M.clip("x" * 1000, 50) == "x" * 49 + "…"
    assert M.clip("short", 50) == "short"
