"""The quiet rule (logic/quiet.py): pure, so plain values in and out."""

from datetime import datetime
from zoneinfo import ZoneInfo

from logic import quiet as Q
from logic.quiet import Evidence, Post, State

TZ = ZoneInfo("America/Los_Angeles")
DAY = Q.DAY
T0 = int(datetime(2026, 10, 5, 12, 0, tzinfo=TZ).timestamp())
IGNORED = Evidence(answered=False, member_since=False)


def posts(*answered, start=T0):
    """One prompt a day from `start`, oldest first."""
    return tuple(Post(start + i * DAY, 100 + i, a) for i, a in enumerate(answered))


def after(state, days=1):
    return state.posts[-1].at + days * DAY


# ---------------------------------------------------------------- posting while the room answers
def test_nothing_on_record_posts():
    d = Q.decide(State(), T0)
    assert d.post and d.reason == Q.ACTIVE and d.change is None and d.state == State()


def test_fewer_than_n_prompts_always_post_even_when_ignored():
    for n in (1, 2):
        state = State(posts(*[False] * n))
        d = Q.decide(state, after(state), IGNORED)
        assert d.post and d.reason == Q.ACTIVE and d.state == state


def test_one_answer_in_the_last_three_keeps_it_posting():
    for answered in ((True, False, False), (False, True, False)):
        state = State(posts(*answered))
        d = Q.decide(state, after(state), IGNORED)
        assert d.post and d.reason == Q.ACTIVE and d.change is None


def test_an_answer_to_the_newest_prompt_is_recorded():
    state = State(posts(False, False, False))
    d = Q.decide(state, after(state), Evidence(answered=True))
    assert d.post and d.reason == Q.ACTIVE
    assert [p.answered for p in d.state.posts] == [False, False, True]


def test_an_answer_once_seen_is_kept():
    state = State(posts(False, False, True))
    d = Q.decide(state, after(state), IGNORED)  # e.g. the reaction was removed since
    assert d.post and d.state.posts[-1].answered


# ---------------------------------------------------------------- going to sleep
def test_three_ignored_in_a_row_skips_and_says_so_once():
    state = State(posts(False, False, False))
    d = Q.decide(state, after(state), IGNORED)
    assert not d.post and d.reason == Q.ASLEEP and d.change == Q.PAUSED and d.state.asleep
    again = Q.decide(d.state, after(state, 2), IGNORED)
    assert not again.post and again.change is None and again.state == d.state


def test_only_the_last_three_count():
    state = Q.posted(State(posts(True, False, False)), T0 + 3 * DAY, 103)  # the answered one drops off
    assert len(state.posts) == Q.N
    assert not Q.decide(state, after(state), IGNORED).post


# ---------------------------------------------------------------- waking
def test_a_member_message_wakes_it_and_the_next_ignored_prompt_sleeps_again():
    asleep = State(posts(False, False, False), asleep=True)
    d = Q.decide(asleep, after(asleep, 3), Evidence(answered=False, member_since=True))
    assert d.post and d.reason == Q.WOKEN and d.change == Q.RESUMED and not d.state.asleep
    state = Q.posted(d.state, after(asleep, 3), 200)
    nxt = Q.decide(state, after(state), IGNORED)
    assert not nxt.post and nxt.change == Q.PAUSED


def test_probe_after_seven_quiet_days_and_not_before():
    asleep = State(posts(False, False, False), asleep=True)
    for days in range(1, 7):
        assert not Q.decide(asleep, after(asleep, days), IGNORED).post
    d = Q.decide(asleep, after(asleep, 7), IGNORED)
    assert d.post and d.reason == Q.PROBE
    assert d.change is None and d.state.asleep  # a probe is not a wake-up: staff hear nothing


def test_an_ignored_probe_sleeps_another_seven_days_without_a_second_note():
    asleep = State(posts(False, False, False), asleep=True)
    probe_time = after(asleep, 7)
    state = Q.posted(Q.decide(asleep, probe_time, IGNORED).state, probe_time, 300)
    for days in range(1, 7):
        d = Q.decide(state, probe_time + days * DAY, IGNORED)
        assert not d.post and d.change is None
    assert Q.decide(state, probe_time + 7 * DAY, IGNORED).reason == Q.PROBE


def test_an_answered_probe_resumes():
    asleep = State(posts(False, False, False), asleep=True)
    probe_time = after(asleep, 7)
    state = Q.posted(Q.decide(asleep, probe_time, IGNORED).state, probe_time, 300)
    d = Q.decide(state, probe_time + DAY, Evidence(answered=True))
    assert d.post and d.reason == Q.ACTIVE and d.change == Q.RESUMED and not d.state.asleep


def test_probe_lands_on_the_same_slot_across_a_clock_change():
    last = int(datetime(2026, 3, 5, 12, 0, tzinfo=TZ).timestamp())  # clocks go forward on March 8
    slot = int(datetime(2026, 3, 12, 12, 0, tzinfo=TZ).timestamp())
    assert slot - last == 7 * DAY - Q.HOUR
    state = State(posts(False, False, False, start=last - 2 * DAY), asleep=True)
    assert state.posts[-1].at == last
    assert Q.decide(state, slot, IGNORED).reason == Q.PROBE
    assert not Q.decide(state, slot - DAY, IGNORED).post


def test_probe_after_long_downtime_does_not_claim_to_be_asleep():
    state = State(posts(False, False, False))  # never evaluated as asleep: the bot was off
    d = Q.decide(state, after(state, 9), IGNORED)
    assert d.post and d.reason == Q.PROBE and d.change is None and not d.state.asleep


# ---------------------------------------------------------------- could not tell
def test_could_not_tell_counts_as_answered():
    state = State(posts(False, False, False), asleep=True)
    d = Q.decide(state, after(state), Evidence())
    assert d.post and d.reason == Q.ACTIVE and d.state.posts[-1].answered and d.change == Q.RESUMED


def test_unknown_member_activity_wakes():
    state = State(posts(False, False, False))
    d = Q.decide(state, after(state), Evidence(answered=False, member_since=None))
    assert d.post and d.reason == Q.WOKEN and not d.state.posts[-1].answered


# ---------------------------------------------------------------- evidence from a known time
def test_heard():
    state = State(posts(False, False, False))
    last = state.posts[-1].at
    assert Q.heard(state, last + 1) == Evidence(True, True)
    assert Q.heard(state, last) == Evidence(False, False)
    assert Q.heard(state, None) == Evidence()
    assert Q.heard(State(), last) == Evidence()


# ---------------------------------------------------------------- off switch, state, wording
def test_off_switch():
    assert Q.enabled({}) and Q.enabled({"QUIET": ""}) and Q.enabled({"QUIET": "on"})
    for value in ("off", "OFF", " off ", "0", "false", "no"):
        assert not Q.enabled({"QUIET": value})


def test_state_round_trip_and_garbage():
    state = State((Post(T0, 1, True), Post(T0 + DAY, None, False)), asleep=True)
    assert Q.load(Q.dump(state)) == state
    assert "user" not in Q.dump(state) and Q.dump(State()) == '{"posts":[],"asleep":0}'
    for raw in (None, "", "nope", "[]", '{"posts": 3}', '{"posts": [[1]]}', '{"posts": [["x", 2, 0]]}'):
        assert Q.load(raw) == State()
    long = Q.dump(State(posts(False, True, False, False, False)))
    assert len(Q.load(long).posts) == Q.N


def test_posted_keeps_the_last_n():
    state = State()
    for i in range(5):
        state = Q.posted(state, T0 + i * DAY, i)
    assert [p.message_id for p in state.posts] == [2, 3, 4]
    assert not any(p.answered for p in state.posts)


def test_keys_are_distinct_and_stable():
    assert [Q.key(k) for k in Q.KINDS] == ["quiet:qotd", "quiet:thisorthat", "quiet:revival",
                                          "quiet:fieldnotes"]


def test_what_staff_read():
    state = State(posts(False, False, False), asleep=True)
    now = after(state)
    assert Q.paused_line(Q.QOTD, state, now) == (
        "Question of the day is paused: the last 3 went unanswered. It resumes when someone posts "
        "in the channel, and tries again in 6 days.")
    assert Q.paused_line(Q.POLL, state, after(state, 6)).endswith("tries again in 1 day.")
    assert Q.resumed_line(Q.QOTD, Q.WOKEN) == "Question of the day is back: someone posted in the channel."
    assert Q.resumed_line(Q.POLL, Q.ACTIVE) == "This or that poll is back: the last one got a response."
    assert Q.status_line(Q.QOTD, state) == f"Question of the day: paused, tries again <t:{now + 6 * DAY}:R>"
    for line in (Q.paused_line(Q.REVIVAL, state, now), Q.resumed_line(Q.REVIVAL, Q.WOKEN)):
        assert "—" not in line and "@" not in line
