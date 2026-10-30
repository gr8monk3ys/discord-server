"""logic/smpnudge.py: who is due a "we miss you" DM, once per lapse, and what it says."""

from datetime import datetime
from zoneinfo import ZoneInfo

from logic import smpnudge as N

TZ = ZoneInfo("America/Los_Angeles")
T = int(datetime(2026, 11, 20, 12, 0, tzinfo=TZ).timestamp())


def cand(uid=1, seen=T - N.AWAY, nudged=None, closed=None, linked=True, tracking=True):
    return N.Candidate(uid, seen, nudged, closed, linked, tracking)


# ---------------------------------------------------------------- the switch
def test_off_unless_switched_on():
    assert not N.enabled({})
    assert not N.enabled({"SMP_NUDGES": ""})
    assert not N.enabled({"SMP_NUDGES": "off"})
    assert not N.enabled({"SMP_NUDGES": "maybe"})
    for word in ("on", "ON", " true ", "1", "yes"):
        assert N.enabled({"SMP_NUDGES": word})


# ---------------------------------------------------------------- who is due
def test_due_after_a_week_away_not_before():
    assert N.due(cand(seen=T - N.AWAY), T)
    assert N.due(cand(seen=T - 30 * N.DAY), T)
    assert not N.due(cand(seen=T - N.AWAY + 1), T)


def test_never_seen_is_never_due():
    assert not N.due(cand(seen=None), T)


def test_once_per_lapse():
    seen = T - 10 * N.DAY
    assert N.due(cand(seen=seen, nudged=None), T)
    assert not N.due(cand(seen=seen, nudged=seen), T)  # already nudged for this lapse
    assert not N.due(cand(seen=seen, nudged=seen), T + 365 * N.DAY)  # however long they stay away
    # they played again (last seen moved on), then lapsed again: due once more
    again = seen + 3 * N.DAY
    assert not N.due(cand(seen=again, nudged=seen), again + N.DAY)
    assert N.due(cand(seen=again, nudged=seen), again + N.AWAY)


def test_opted_out_or_unlinked_or_dms_closed_is_never_due():
    assert not N.due(cand(tracking=False), T)
    assert not N.due(cand(linked=False), T)
    assert not N.due(cand(closed=T - N.DAY), T)


def test_pick_orders_longest_away_first_and_caps():
    rows = [cand(uid=u, seen=T - (8 + u) * N.DAY) for u in range(1, 9)]
    rows.append(cand(uid=99, seen=T - N.DAY))  # not due
    rows.append(cand(uid=98, seen=T - 40 * N.DAY, nudged=T - 40 * N.DAY))  # done
    picked = N.pick(rows, T)
    assert [c.user_id for c in picked] == [8, 7, 6, 5, 4]
    assert len(N.pick(rows, T, limit=2)) == 2
    assert N.pick(rows, T, limit=0) == []
    # same lapse length: by ID
    assert [c.user_id for c in N.pick([cand(uid=5), cand(uid=3)], T)] == [3, 5]


# ---------------------------------------------------------------- opting in
def test_opt_in_without_playtime_starts_unseen():
    assert N.opt_in_state(None, T, TZ) == (None, None)


def test_opt_in_seeds_from_the_end_of_the_last_day_played():
    seen, nudged = N.opt_in_state("2026-11-18", T, TZ)
    assert seen == int(datetime(2026, 11, 19, 0, 0, tzinfo=TZ).timestamp())
    assert nudged is None  # two days away: the nudge comes when the week is up
    assert N.due(cand(seen=seen, nudged=nudged), seen + N.AWAY)


def test_opt_in_today_is_capped_at_now():
    seen, _ = N.opt_in_state("2026-11-20", T, TZ)
    assert seen == T


def test_opt_in_already_lapsed_counts_this_lapse_as_handled():
    seen, nudged = N.opt_in_state("2026-10-01", T, TZ)
    assert nudged == seen
    assert not N.due(cand(seen=seen, nudged=nudged), T + 30 * N.DAY)


def test_day_end_across_daylight_saving():
    # 2026-11-01 is 25 hours long in Los Angeles
    assert N.day_end("2026-11-01", TZ) - N.day_end("2026-10-31", TZ) == 25 * N.HOUR


# ---------------------------------------------------------------- the message
def test_smp_events_filters_by_name_window_and_order():
    events = [("Monkey SMP build night", T + 2 * N.DAY), ("Valorant 5-stack", T + N.HOUR),
              ("Minecraft PvP", T + N.HOUR), ("SMP finale", T + 30 * N.DAY), ("smp past", T - N.HOUR),
              ("SMP a", T + 3 * N.DAY), ("SMP b", T + 4 * N.DAY)]
    assert N.smp_events(events, T) == [("Minecraft PvP", T + N.HOUR), ("Monkey SMP build night", T + 2 * N.DAY),
                                       ("SMP a", T + 3 * N.DAY)]


def test_month_label():
    assert N.month_label("2026-11") == "November 2026"
    assert N.month_label("") is None and N.month_label(None) is None and N.month_label("nope") is None


def test_message_lists_what_happened_and_how_back():
    text = N.message(N.Digest(away_days=9, players=4, hours=31, factions=("Iron Pact", "Moss"), faction_count=5,
                              botm_month="November 2026", events=(("SMP build night", T + N.DAY),),
                              online_now=2, address="play.example.org", channel="<#1>"))
    assert "9 days since you were on Monkey SMP" in text
    assert "- 4 other players put in 31 hours" in text
    assert "- New factions: Iron Pact, Moss and 3 more" in text
    assert "- Base of the Month for November 2026 was picked" in text
    assert f"- Coming up: SMP build night, <t:{T + N.DAY}:R>" in text
    assert "`/trigger standings`" in text
    assert "Hop back in at `play.example.org`. 2 are on right now. Chat and the status panel are in <#1>." in text
    assert "`/smp nudges off`" in text and "only nudge until you've played again" in text
    assert len(text) < 2000


def test_message_when_nothing_happened():
    text = N.message(N.Digest(away_days=1, address="a.example"))
    assert "1 day since" in text and "Since then" not in text and "quiet stretch" in text
    assert "right now" not in text and "- " not in text


def test_message_singulars():
    text = N.message(N.Digest(away_days=7, players=1, hours=1, factions=("Moss",), faction_count=1, online_now=1))
    assert "- 1 other player put in 1 hour\n" in text
    assert "- New faction: Moss\n" in text
    assert "1 is on right now" in text and "Hop back in any time." in text
    assert "dropped in" in N.message(N.Digest(away_days=7, players=2, hours=0))
