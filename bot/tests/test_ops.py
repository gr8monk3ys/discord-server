from datetime import date, datetime
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


# --- permission self-check ------------------------------------------------------
Need, Problem = ops.Need, ops.Problem
TABLE = [
    Need("cards", "welcome", ops.POST + ("attach_files",)),
    Need("recap", "news", ops.POST),
    Need("vibes", "news", ops.POST + ("read_message_history",)),
    Need("lfg", "lfg", ops.POST + ("manage_threads",), "forum"),
]
FULL = {"view_channel", "send_messages", "embed_links", "attach_files", "read_message_history", "manage_threads"}


def test_channel_problems_none_when_everything_is_granted():
    perms = {"welcome": FULL, "news": FULL, "lfg": FULL}
    assert ops.channel_problems(perms, TABLE) == []


def test_channel_problems_names_the_permission_and_the_cogs():
    perms = {"welcome": FULL - {"send_messages", "attach_files"}, "news": FULL - {"send_messages"}, "lfg": FULL}
    assert ops.channel_problems(perms, TABLE) == [
        Problem("welcome", "missing Send Messages (cards)"),
        Problem("welcome", "missing Attach Files (cards)"),
        Problem("news", "missing Send Messages (recap, vibes)"),
    ]


def test_channel_problems_missing_channel_and_unloaded_cogs():
    perms = {"welcome": None, "lfg": FULL}
    assert ops.channel_problems(perms, TABLE) == [
        Problem("welcome", "channel not found (needed by cards)"),
        Problem("news", "channel not found (needed by recap, vibes)"),
    ]
    # Only what a loaded cog needs counts.
    assert ops.channel_problems(perms, TABLE, loaded={"lfg"}) == []
    assert ops.channel_problems({"news": FULL - {"read_message_history"}}, TABLE, loaded={"recap"}) == []
    assert ops.channel_problems({"news": FULL - {"read_message_history"}}, TABLE, loaded={"recap", "vibes"}) == [
        Problem("news", "missing Read Message History (vibes)")]


def test_guild_problems_and_the_create_events_hint():
    everything = {n.perm for n in ops.GUILD_NEEDS}
    assert ops.guild_problems(everything) == []
    assert {n.perm for n in ops.GUILD_NEEDS} >= {"manage_roles", "manage_channels", "manage_events",
                                                 "create_events", "moderate_members"}
    (p,) = ops.guild_problems(everything - {"create_events"})
    assert p.where == ops.SERVER and p.text.startswith("missing Create Events (engagement, events, tournaments)")
    assert "Server Settings -> Roles -> Front Desk -> turn on Create Events" in p.text
    assert ops.guild_problems(everything - {"create_events"}, loaded={"moderation", "lfg"}) == []
    assert ops.guild_problems(set(), loaded={"moderation"}) == [
        Problem(ops.SERVER, "missing Moderate Members (moderation)"),
        Problem(ops.SERVER, "missing Manage Messages (moderation)"),
        Problem(ops.SERVER, "missing Ban Members (moderation): Server Settings -> Roles -> Front Desk -> "
                            "turn on Ban Members")]


def test_role_problems_missing_and_not_below_the_bot():
    needs = [("Regular", "levels"), ("PC", "selfroles"), ("Bumper", "selfroles"), ("Bumper", "growth")]
    assert ops.role_problems({"Regular": 3, "PC": 2, "Bumper": 1}, 10, needs) == []
    assert ops.role_problems({"Regular": 10, "PC": None}, 10, needs) == [
        Problem(ops.ROLES, "Regular: has to sit below Front Desk's role (levels)"),
        Problem(ops.ROLES, "PC: role not found (selfroles)"),
        Problem(ops.ROLES, "Bumper: role not found (growth, selfroles)"),
    ]
    assert ops.role_problems({}, 10, needs, loaded={"lfg"}) == []


def test_requirements_tables_cover_config():
    import config
    by_channel = {}
    for n in ops.CHANNEL_NEEDS:
        by_channel.setdefault(n.channel, set()).update(n.perms)
        assert n.kind in ("text", "forum", "voice", "category", "stage")
        assert "view_channel" in n.perms
    # every channel a cog posts to
    for name in (config.GENERAL_CHANNEL, config.MOD_LOG_CHANNEL, config.MOD_CHANNEL, config.BOT_COMMANDS_CHANNEL,
                 config.HALL_OF_FAME_CHANNEL, config.CLIPS_CHANNEL, config.GAMING_CHANNEL,
                 config.ANNOUNCEMENTS_CHANNEL, config.COUNTING_CHANNEL, config.HELP_CHANNEL,
                 config.WELCOME_CHANNEL, config.TOURNAMENTS_CHANNEL, config.CREATORS_CHANNEL,
                 config.ROLES_CHANNEL, config.PARTNERS_CHANNEL, config.GAMES_CHANNEL, config.LFG_FORUM,
                 *(g.channel_name for g in config.GAMES)):
        assert {"send_messages", "embed_links"} <= by_channel[name], name
    assert "attach_files" in by_channel[config.WELCOME_CHANNEL]  # welcome cards are images
    assert "pin_messages" in by_channel[config.GENERAL_CHANNEL]  # the Spotlight pin
    assert "manage_messages" not in by_channel[config.GENERAL_CHANNEL]  # it no longer pins (Discord, 2026-02-23)
    assert "create_public_threads" in by_channel[config.GENERAL_CHANNEL]  # QOTD thread
    assert "create_private_threads" in by_channel[config.HELP_CHANNEL]  # tickets
    assert "manage_threads" in by_channel[config.LFG_FORUM]
    assert "manage_threads" in by_channel[config.SUGGESTIONS_FORUM]
    assert "manage_channels" in by_channel[config.VOICE_CATEGORY]
    # the announcements gap that started this: recap, milestones, member of the month
    assert {n.cog for n in ops.CHANNEL_NEEDS if n.channel == config.ANNOUNCEMENTS_CHANNEL} >= {"recap", "vibes"}
    roles = {name for name, _ in ops.ROLE_NEEDS}
    assert roles >= {name for _, name in config.LEVEL_ROLES}
    assert roles >= set(config.PLATFORM_ROLES) | set(config.REGION_ROLES) | set(config.PLAYTIME_ROLES)
    assert roles >= {config.HYPE_ROLE, config.SEASON_ROLE, config.BIRTHDAY_ROLE, config.COUNTING_ROLE,
                     config.CLIP_ROLE, config.TOURNEY_ROLE, config.RECRUITER_ROLE, config.BUMPER_ROLE,
                     config.GAMENIGHT_ROLE, config.LFG_ROLE}
    assert config.MOD_ROLE not in roles and config.KEEPER_ROLE not in roles  # above the bot by design
    # wave 9: the Wrapped card is an image in announcements; houses post there and hand out three roles
    announce = {n.cog: set(n.perms) for n in ops.CHANNEL_NEEDS if n.channel == config.ANNOUNCEMENTS_CHANNEL}
    assert {"send_messages", "embed_links", "attach_files"} <= announce["wrapped"]
    assert {"send_messages", "embed_links"} <= announce["houses"]
    assert roles >= set(config.HOUSE_ROLES.values()) | {config.CREW_ROLE} and len(config.HOUSE_ROLES) == 3
    assert "houses" in ops.ROLE_HOLDERS and config.SUPPORTER_ROLE not in roles
    # cogs that posted or handed out a role without a row until the coverage test below existed
    channels_of = {}
    for n in ops.CHANNEL_NEEDS:
        channels_of.setdefault(n.cog, set()).add(n.channel)
    assert channels_of["seasonal"] == {config.GENERAL_CHANNEL, config.GAMES_CHANNEL, config.ANNOUNCEMENTS_CHANNEL}
    assert channels_of["votehub"] == {config.GAMES_CHANNEL} and channels_of["gamelink"] == {config.MOD_LOG_CHANNEL}
    assert {"send_messages", "embed_links"} <= announce["events"] and {"send_messages", "embed_links"} <= announce[
        "seasonal"]
    assert (config.EVENT_ROLE, "seasonal") in ops.ROLE_NEEDS and "seasonal" in ops.ROLE_HOLDERS
    assert "pin_messages" in by_channel[config.REALMS_CHANNEL]  # the status panel is pinned
    assert ops.role_problems({}, 5, loaded={"seasonal"}) == [
        Problem(ops.ROLES, f"{config.EVENT_ROLE}: role not found (seasonal)")]


def test_wave9_needs_are_reported_like_the_rest():
    import config
    have = {config.ANNOUNCEMENTS_CHANNEL: {"view_channel", "send_messages", "embed_links"}}
    assert ops.channel_problems(have, loaded={"wrapped", "houses"}) == [
        Problem(config.ANNOUNCEMENTS_CHANNEL, "missing Attach Files (wrapped)")]
    assert ops.channel_problems(have, loaded={"houses"}) == []
    assert ops.channel_problems({}, loaded={"houses"}) == [
        Problem(config.ANNOUNCEMENTS_CHANNEL, "channel not found (needed by houses)")]
    # house roles behave like every other role the bot hands out: missing or too high is a problem
    made = {name: 1 for name in config.HOUSE_ROLES.values()}
    assert ops.role_problems(made, 5, loaded={"houses"}) == []
    assert ops.role_problems({}, 5, loaded={"houses"}) == [
        Problem(ops.ROLES, f"{name}: role not found (houses)") for name in config.HOUSE_ROLES.values()]
    assert ops.role_problems(made, 5, loaded={"wrapped", "lfg"}) == []  # not loaded: not asked for
    assert ops.guild_problems(set(), loaded={"houses"}) == [Problem(ops.SERVER, "missing Manage Roles (houses)")]


def test_requirements_only_name_real_cogs_and_permissions():
    import discord
    import main
    cogs = {ext.removeprefix("cogs.") for ext, _ in main.MODULES}
    used = ({n.cog for n in ops.CHANNEL_NEEDS} | {c for n in ops.GUILD_NEEDS for c in n.cogs}
            | {cog for _, cog in ops.ROLE_NEEDS})
    assert used <= cogs
    perms = {p for n in ops.CHANNEL_NEEDS for p in n.perms} | {n.perm for n in ops.GUILD_NEEDS}
    assert perms <= set(discord.Permissions.VALID_FLAGS)
    # The invite link asks for everything the self-check expects.
    assert all(getattr(main.PERMISSIONS, p) for p in perms)


def test_every_module_is_covered_by_the_tables_or_exempt_with_a_reason():
    """A new cog must get its rows or say why it needs none: this fails for a module that is in
    neither CHANNEL_NEEDS nor NO_CHANNEL, and for a cog that changes members' roles without
    being in ROLE_HOLDERS (the Manage Roles check) or NO_ROLE_HOLDER."""
    import re
    from pathlib import Path

    import main
    cogs = [ext.removeprefix("cogs.") for ext, _ in main.MODULES]
    with_rows = {n.cog for n in ops.CHANNEL_NEEDS}
    missing = [c for c in cogs if c not in with_rows and c not in ops.NO_CHANNEL]
    assert missing == [], f"add CHANNEL_NEEDS rows in logic/ops.py, or a NO_CHANNEL reason, for: {missing}"
    # an exemption is for a real module that has no row, and it says why
    assert set(ops.NO_CHANNEL) <= set(cogs) and not set(ops.NO_CHANNEL) & with_rows
    assert all(reason.strip() for reason in ops.NO_CHANNEL.values())

    cogs_dir = Path(main.__file__).parent / "cogs"
    changes_roles = {c for c in cogs
                     if re.search(r"\b(?:add|remove)_roles\(", (cogs_dir / f"{c}.py").read_text(encoding="utf-8"))}
    assert len(changes_roles) >= 10  # the search still finds them
    holders = set(ops.ROLE_HOLDERS)
    missing = sorted(changes_roles - holders - set(ops.NO_ROLE_HOLDER))
    assert missing == [], f"add to ROLE_HOLDERS in logic/ops.py, or a NO_ROLE_HOLDER reason, for: {missing}"
    assert set(ops.NO_ROLE_HOLDER) <= changes_roles and not set(ops.NO_ROLE_HOLDER) & holders
    assert all(reason.strip() for reason in ops.NO_ROLE_HOLDER.values())
    # a role in ROLE_NEEDS is handed out by a cog whose Manage Roles need is checked too
    assert {cog for _, cog in ops.ROLE_NEEDS} <= holders


def test_problems_digest_is_stable_and_order_free():
    a, b = Problem("x", "missing Send Messages (cards)"), Problem("y", "channel not found (needed by lfg)")
    assert ops.problems_digest([]) == ops.OK_DIGEST
    assert ops.problems_digest([a, b]) == ops.problems_digest([b, a]) != ops.OK_DIGEST
    assert ops.problems_digest([a]) != ops.problems_digest([a, b])


def test_problems_report_groups_by_channel_and_truncates():
    text = ops.problems_report([Problem("a_b", "missing Send Messages (cards)"), Problem(ops.SERVER, "missing X (y)"),
                                Problem("a_b", "missing Embed Links (cards)")])
    assert text == ("**a\\_b**\n- missing Send Messages (cards)\n- missing Embed Links (cards)\n"
                    "**Server**\n- missing X (y)")
    assert ops.problems_report([]) == ""
    many = [Problem(f"ch{i}", "missing Send Messages (cards)") for i in range(200)]
    short = ops.problems_report(many, limit=500)
    assert len(short) <= 500 and "more lines" in short.splitlines()[-1]


@pytest.mark.parametrize("n,text", [(0, "OK"), (1, "1 problem, see mod-log"), (4, "4 problems, see mod-log")])
def test_permissions_status(n, text):
    assert ops.permissions_status(n) == text


def test_modules_do_not_collide():
    """Things every module shares with the others, each of which must be unique across them:
    slash command names, cog names (get_cog), the first part of a persistent button's custom_id,
    and scheduled job names (the `jobs` table and `first_seen:<name>` in meta are keyed on them)."""
    import importlib
    import inspect
    import re
    from collections import defaultdict

    import discord
    from discord.ext import commands

    import main
    commands_by, cogs_by, prefixes, jobs = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    for ext, _ in main.MODULES:
        short = ext.removeprefix("cogs.")
        module = importlib.import_module(ext)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if issubclass(obj, commands.Cog):
                cogs_by[obj.__cog_name__].add(short)
                for cmd in obj.__cog_app_commands__:
                    commands_by[(type(cmd).__name__, cmd.name)].add(short)
            if issubclass(obj, discord.ui.DynamicItem):
                prefixes[re.split(r"[:(]", obj.__discord_ui_compiled_template__.pattern)[0]].add(short)
        for name in (ext, f"logic.{short}"):
            try:
                values = vars(importlib.import_module(name)).values()
            except ImportError:
                continue
            for value in values:
                if type(value).__name__ in ("Weekly", "Daily", "Monthly", "Annual") and isinstance(
                        getattr(value, "name", None), str):
                    jobs[value.name].add(short)
    assert len(cogs_by) == len(main.MODULES) and len(commands_by) > 60 and len(prefixes) > 10 and len(jobs) > 25
    for what, found in (("command", commands_by), ("cog", cogs_by), ("custom_id prefix", prefixes), ("job", jobs)):
        shared = {str(k): sorted(v) for k, v in found.items() if len(v) > 1}
        assert shared == {}, f"{what} names used by more than one module: {shared}"
    # a job key is "<name>:<period>" and is looked up with LIKE "<name>:%"
    assert not [n for n in jobs if ":" in n or "%" in n]
