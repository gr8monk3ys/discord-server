from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from logic import ops
from logic.ops import Daily, DailyPeriod, DailyPlan, ErrorMonitor

LA = ZoneInfo("America/Los_Angeles")
BACKUP = Daily("backup", 4, 0)
MIN = 60
HOUR = 60 * MIN
DAY = 24 * HOUR


def local(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=LA).timestamp())


# --- daily schedule -------------------------------------------------------------


def test_daily_occurrence_key_and_time():
    p = ops.daily_occurrence(BACKUP, date(2026, 10, 4), LA)
    assert p == DailyPeriod("backup:2026-10-04", local(2026, 10, 4, 4), date(2026, 10, 4))


def test_latest_due_before_and_after_the_hour():
    assert ops.latest_due_daily(BACKUP, local(2026, 10, 4, 3, 59), LA).key == "backup:2026-10-03"
    assert ops.latest_due_daily(BACKUP, local(2026, 10, 4, 4, 0), LA).key == "backup:2026-10-04"
    assert ops.latest_due_daily(BACKUP, local(2026, 10, 4, 23, 0), LA).key == "backup:2026-10-04"


def test_latest_due_across_dst_change():
    # 2026-11-01 is the fall-back day in Los Angeles; 04:00 exists once.
    p = ops.latest_due_daily(BACKUP, local(2026, 11, 1, 5), LA)
    assert p.key == "backup:2026-11-01" and p.scheduled_at == local(2026, 11, 1, 4)


def test_plan_first_run_marks_done_without_running():
    now = local(2026, 10, 4, 12)
    plan = ops.plan_daily(BACKUP, now, LA, set(), None)
    assert plan == DailyPlan(None, [ops.latest_due_daily(BACKUP, now, LA)])


def test_plan_latest_predates_first_seen():
    now = local(2026, 10, 4, 12)
    plan = ops.plan_daily(BACKUP, now, LA, set(), first_seen=local(2026, 10, 4, 5))
    assert plan.run is None and [p.key for p in plan.mark_done] == ["backup:2026-10-04"]


def test_plan_runs_latest_and_marks_missed_since_first_seen():
    first = local(2026, 10, 1, 12)
    now = local(2026, 10, 4, 12)
    plan = ops.plan_daily(BACKUP, now, LA, {"backup:2026-10-02"}, first)
    assert plan.run.key == "backup:2026-10-04"
    assert [p.key for p in plan.mark_done] == ["backup:2026-10-03"]


def test_plan_nothing_when_latest_done():
    now = local(2026, 10, 4, 12)
    assert ops.plan_daily(BACKUP, now, LA, {"backup:2026-10-04"}, local(2026, 9, 1)) == DailyPlan(None, [])


def test_plan_runs_exactly_at_scheduled_time():
    now = local(2026, 10, 4, 4)
    plan = ops.plan_daily(BACKUP, now, LA, {"backup:2026-10-03"}, local(2026, 10, 3))
    assert plan.run.key == "backup:2026-10-04" and plan.mark_done == []


# --- backups ---------------------------------------------------------------------


def test_backup_name():
    assert ops.backup_name(date(2026, 10, 4)) == "front_desk-2026-10-04.db"


def test_backups_to_prune_keeps_newest_and_ignores_other_files():
    names = [ops.backup_name(date(2026, 9, d)) for d in range(1, 21)]
    names += ["notes.txt", "front_desk-bad.db", "front_desk-2026-10-04.db.tmp"]
    prune = ops.backups_to_prune(names, keep=14)
    assert prune == [ops.backup_name(date(2026, 9, d)) for d in range(6, 0, -1)]


def test_backups_to_prune_under_limit():
    assert ops.backups_to_prune(["front_desk-2026-09-01.db"], keep=14) == []


# --- redaction ---------------------------------------------------------------------

FAKE_TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4OQ" + ".GhAbCd." + "abcdefghijklmnopqrstuvwxyz0123456789AB"


def test_redact_strips_token_like_strings():
    text = f"login failed with {FAKE_TOKEN} ok"
    out = ops.redact(text)
    assert FAKE_TOKEN not in out and "[redacted]" in out and out.startswith("login failed")


def test_redact_strips_mfa_and_bot_prefix():
    mfa = "mfa." + "x" * 84
    assert mfa not in ops.redact(f"Bot {mfa}")


def test_redact_leaves_ordinary_text():
    assert ops.redact("channel 123456789012345678 failed: 403 Forbidden") == \
        "channel 123456789012345678 failed: 403 Forbidden"


def test_first_line_trims_and_redacts():
    msg = f"boom {FAKE_TOKEN}\nTraceback line 2"
    assert ops.first_line(msg) == "boom [redacted]"
    assert len(ops.first_line("x" * 1000)) <= ops.MAX_LINE
    assert ops.first_line("") == "(no message)"


# --- error monitor -------------------------------------------------------------------


def test_monitor_alerts_once_when_more_than_threshold_in_window():
    m = ErrorMonitor()
    t = 1000
    assert [m.record("cogs.lfg", t + i) for i in range(5)] == [False] * 5
    assert m.record("cogs.lfg", t + 5) is True  # the 6th: more than 5
    assert m.record("cogs.lfg", t + 6) is False  # within the hour
    assert m.count("cogs.lfg", t + 6) == 7


def test_monitor_window_slides():
    m = ErrorMonitor()
    for i in range(5):
        m.record("x", 1000 + i)
    # the first five fall out of the 10-minute window
    assert m.record("x", 1000 + 10 * MIN + 10) is False
    assert m.count("x", 1000 + 10 * MIN + 10) == 1


def test_monitor_per_logger_and_hourly_cooldown():
    m = ErrorMonitor()
    for i in range(6):
        m.record("a", 1000 + i)
    assert [m.record("b", 1000 + i) for i in range(6)][-1] is True  # b is separate
    later = 1000 + HOUR + 1
    assert [m.record("a", later + i) for i in range(6)][-1] is True


def test_monitor_cooldown_restored_from_storage():
    m = ErrorMonitor(last_alert={"a": 1000})
    assert [m.record("a", 1100 + i) for i in range(6)] == [False] * 6


def test_monitor_counts_and_totals():
    m = ErrorMonitor()
    m.record("a", 1000)
    m.record("a", 1001)
    m.record("b", 1002)
    assert m.counts(1002) == {"a": 2, "b": 1}
    assert m.totals == {"a": 2, "b": 1}
    assert m.counts(1002 + 11 * MIN) == {}
    assert m.totals == {"a": 2, "b": 1}


# --- back online ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "heartbeat,last_note,expected",
    [
        (None, None, False),  # first ever start: never "back"
        (10_000 - 29 * MIN, None, False),  # quick restart
        (10_000 - 31 * MIN, None, True),
        (10_000 - 31 * MIN, 10_000 - 5 * HOUR, False),  # posted one recently
        (10_000 - 31 * MIN, 10_000 - 6 * HOUR, True),
    ],
)
def test_should_note_online(heartbeat, last_note, expected):
    assert ops.should_note_online(heartbeat, 10_000, last_note) is expected


# --- drift -------------------------------------------------------------------------


def role(name, perms=(), color=None):
    return {"name": name, "color": color, "hoist": False, "mentionable": False, "managed": False,
            "permissions": list(perms)}


def chan(name, kind="text", **kw):
    return {"name": name, "type": kind, "overwrites": {}, **kw}


def snap(*cats, loose=()):
    return {"categories": [{**chan(n, "category"), "channels": list(chs)} for n, chs in cats],
            "uncategorized": list(loose)}


def test_role_drift():
    old = [role("Keeper", ["administrator"]), role("Moderator"), role("Gone")]
    new = [role("Keeper", ["administrator"]), role("Moderator", ["kick_members"]), role("Fresh")]
    d = ops.diff_named(old, new)
    assert d.added == ["Fresh"] and d.removed == ["Gone"]
    assert d.changed == [("Moderator", ["permissions"])]


def test_channel_drift_including_moves_and_categories():
    old = snap(("info", [chan("rules"), chan("old")]), ("chat", [chan("general", topic="hi")]))
    new = snap(("info", [chan("rules")]), ("chat", [chan("general", topic="hey"), chan("new")]),
               ("voice", []), loose=[])
    old2 = ops.flatten_channels(old)
    assert old2["general"]["category"] == "chat"
    d = ops.diff_named(list(ops.flatten_channels(old).values()), list(ops.flatten_channels(new).values()))
    assert d.added == ["new", "voice"] and d.removed == ["old"]
    assert d.changed == [("general", ["topic"])]


def test_channel_move_counts_as_change():
    old = snap(("a", [chan("x")]), ("b", []))
    new = snap(("a", []), ("b", [chan("x")]))
    d = ops.diff_named(list(ops.flatten_channels(old).values()), list(ops.flatten_channels(new).values()))
    assert d.changed == [("x", ["category"])]


def test_duplicate_names_kept_apart():
    new = snap(("a", [chan("x"), chan("x", kind="voice")]))
    flat = ops.flatten_channels(new)
    assert set(flat) == {"a", "x", "x (2)"}


def test_drift_summary_text():
    roles = ops.Drift(added=["Fresh"], removed=[], changed=[("Moderator", ["permissions", "color"])])
    chans = ops.Drift(added=[], removed=["old"], changed=[])
    text = ops.drift_summary(roles, chans)
    assert "**Roles**" in text and "+ Fresh" in text and "~ Moderator (permissions, color)" in text
    assert "**Channels**" in text and "- old" in text


def test_drift_summary_no_changes():
    empty = ops.Drift([], [], [])
    assert ops.drift_summary(empty, empty) == ""


def test_drift_summary_is_capped():
    many = ops.Drift(added=[f"role-{i}" * 5 for i in range(500)], removed=[], changed=[])
    text = ops.drift_summary(many, ops.Drift([], [], []), limit=1000)
    assert len(text) <= 1000 and "more" in text.splitlines()[-1]


def test_drift_summary_escapes_markdown_and_mentions():
    d = ops.Drift(added=["**bold** @everyone"], removed=[], changed=[])
    text = ops.drift_summary(d, ops.Drift([], [], []))
    assert "**bold**" not in text and "@everyone" not in text


# --- formatting ------------------------------------------------------------------------


@pytest.mark.parametrize("n,text", [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")])
def test_fmt_bytes(n, text):
    assert ops.fmt_bytes(n) == text


@pytest.mark.parametrize("s,text", [(5, "5s"), (65, "1m"), (3 * HOUR + 120, "3h 2m"), (2 * DAY + HOUR, "2d 1h")])
def test_fmt_uptime(s, text):
    assert ops.fmt_uptime(s) == text
