"""Rules of the Tonight line (logic/tonight.py): game selection, wording and the session
state machine, driven with an injected clock."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
from logic import tonight as L
from logic.tonight import Edit, Post, Room, State

TZ = ZoneInfo("America/Los_Angeles")
MIN = 60
LOBBY, SQUAD = 501, 502
LISTED = config.GAMES[0].role  # whichever game layout.py lists first


def ts(h, mi=0, d=7):
    return int(datetime(2026, 10, d, h, mi, tzinfo=TZ).timestamp())


class Sim:
    """Ticks the state machine once a minute and plays Discord: a Post gets a message id, an
    Edit succeeds unless the message was deleted."""

    def __init__(self, t=None, state=None):
        self.t = ts(19) if t is None else t
        self.s = state or State()
        self.rooms: list[Room] = []
        self.human_since = False
        self.quiet_until = 0
        self.messages: dict[int, str] = {}
        self.log: list[tuple[str, int, int]] = []  # (kind, message id, time)
        self.next_id = 9000
        self.failing = False

    def people(self, n, channel=LOBBY, games=(), tick=True):
        """Who is in voice from now on (0 empties every room), seen by a tick at this moment."""
        self.rooms = [r for r in self.rooms if r.channel_id != channel and n]
        if n:
            self.rooms.append(Room(channel, n, tuple(games)))
        return self.tick() if tick else None

    def tick(self):
        self.s, action = L.step(self.s, self.rooms, self.t, TZ, human_since=self.human_since,
                                quiet_until=self.quiet_until)
        if action is None:
            return None
        if self.failing:
            self.s = L.failed(self.s, self.t)
        elif isinstance(action, Post):
            self.next_id += 1
            self.messages[self.next_id] = action.text
            self.log.append(("post", self.next_id, self.t))
            self.s = L.posted(self.s, self.next_id, action.text, self.t, TZ)
            self.human_since = False
        elif action.message_id not in self.messages:
            self.s = L.gone(self.s, self.t)
        else:
            self.messages[action.message_id] = action.text
            self.log.append(("edit", action.message_id, self.t))
            self.s = L.edited(self.s, action.text, self.t)
        return action

    def run(self, minutes):
        """Advance minute by minute; the actions taken."""
        out = []
        for _ in range(minutes):
            self.t += MIN
            action = self.tick()
            if action is not None:
                out.append(action)
        return out

    def posts(self):
        return [m for kind, m, _ in self.log if kind == "post"]

    def text(self):
        return self.messages[self.s.message_id]


def live(sim, n=3, minutes=10, **kw):
    """People arrive and stay for `minutes`: the line is up."""
    sim.people(n, **kw)
    sim.run(minutes)
    assert sim.s.live


# ---------------------------------------------------------------- game selection
def test_shared_game_is_named():
    assert L.pick_game(["Risk of Rain 2", "Risk of Rain 2", None]) == L.Pick(L.ONE, "Risk of Rain 2")
    assert L.pick_game(["Risk of Rain 2", "risk of  rain 2", "Deep Rock Galactic"]) == L.Pick(L.ONE, "Risk of Rain 2")


def test_only_game_seen_is_named_even_with_one_player():
    assert L.pick_game([None, "Deep Rock Galactic", None]) == L.Pick(L.ONE, "Deep Rock Galactic")


def test_listed_game_uses_the_servers_own_name():
    # The stats mapping (logic/sitedata.match_game): case, punctuation and launcher suffixes.
    assert L.pick_game([LISTED.upper()]) == L.Pick(L.ONE, LISTED)
    assert L.pick_game(["Minecraft Launcher", "minecraft"]) == L.Pick(L.ONE, "Minecraft")


def test_different_games_are_mixed():
    assert L.pick_game(["Risk of Rain 2", "Overwatch 2"]) == L.Pick(L.MIXED)
    assert L.pick_game(["A Game", "A Game", "B Game", "B Game", "C Game"]) == L.Pick(L.MIXED)


def test_no_game():
    assert L.pick_game([]) == L.Pick(L.NONE)
    assert L.pick_game([None, "", "   "]) == L.Pick(L.NONE)


@pytest.mark.parametrize("bad", [
    "join discord.gg/abcdef",  # another server's invite
    "free-nitro.com",  # a link
    "Hentai Game",  # blocklist
    "<@123456789012345678>",  # a mention
    "@everyone",
    "x" * 41,  # too long to be a game
])
def test_filtered_presence_is_never_printed(bad):
    assert L.printable(bad) is None
    assert L.pick_game([bad]) == L.Pick(L.NONE)
    assert L.pick_game([bad, bad, "Risk of Rain 2"]) == L.Pick(L.MIXED)  # most share it, but it can't be shown
    assert L.pick_game([bad, "Risk of Rain 2", "Risk of Rain 2"]) == L.Pick(L.ONE, "Risk of Rain 2")
    text = L.live_text([Room(LOBBY, 2, (bad, bad))])
    assert text == f"2 people are in <#{LOBBY}> right now. Click the channel to join."


def test_markdown_in_a_game_name_is_escaped():
    assert L.pick_game(["**Bold** _Game_"]).name == r"\*\*Bold\*\* \_Game\_"


# ---------------------------------------------------------------- wording
def test_live_text():
    assert L.live_text([Room(LOBBY, 3, ("Risk of Rain 2", "Risk of Rain 2"))]) == (
        f"3 people are in <#{LOBBY}> right now, playing Risk of Rain 2. Click the channel to join.")
    assert L.live_text([Room(LOBBY, 2, ("Risk of Rain 2", "Overwatch 2"))]) == (
        f"2 people are in <#{LOBBY}> right now, playing a few different things. Click the channel to join.")
    assert L.live_text([Room(LOBBY, 2)]) == f"2 people are in <#{LOBBY}> right now. Click the channel to join."


def test_live_text_with_two_rooms_leads_with_the_busiest():
    rooms = L.open_rooms([Room(SQUAD, 2), Room(LOBBY, 4, ("Risk of Rain 2",)), Room(503, 1)])
    assert [r.channel_id for r in rooms] == [LOBBY, SQUAD]  # one person alone is not a room
    assert L.live_text(rooms) == (f"4 people are in <#{LOBBY}> right now, playing Risk of Rain 2. "
                                  f"Also 2 in <#{SQUAD}>. Click a channel to join.")


def test_copy_has_no_names_pings_or_decoration():
    for text in (L.live_text([Room(LOBBY, 3, ("Risk of Rain 2",))]), L.ended_text(7800, 4)):
        assert "@" not in text and "—" not in text and text.isascii()


@pytest.mark.parametrize("seconds,text", [(0, "1 min"), (59, "1 min"), (45 * MIN, "45 min"), (3600, "1 h"),
                                          (7800, "2 h 10 min"), (7859, "2 h 10 min")])
def test_fmt_length(seconds, text):
    assert L.fmt_length(seconds) == text


def test_ended_text():
    assert L.ended_text(7800, 4) == "That voice session ran 2 h 10 min with up to 4 people."


# ---------------------------------------------------------------- the 3-minute hold
def test_line_appears_after_three_minutes_together():
    sim = Sim()
    assert sim.people(3, games=("Risk of Rain 2", "Risk of Rain 2")) is None
    assert sim.run(2) == []  # two minutes in: nothing yet
    assert sim.run(1) == [Post(f"3 people are in <#{LOBBY}> right now, playing Risk of Rain 2. "
                               "Click the channel to join.")]
    assert sim.s.live and sim.s.start == ts(19) and sim.s.own


def test_quick_join_and_leave_posts_nothing():
    sim = Sim()
    sim.people(2)
    sim.run(2)
    sim.people(0)
    assert sim.run(30) == [] and sim.s == State()
    sim.people(2)  # the hold starts again from zero, it is not added up
    assert sim.run(2) == []
    assert len(sim.run(1)) == 1


def test_one_person_alone_is_not_a_session():
    sim = Sim()
    sim.people(1)
    assert sim.run(60) == [] and sim.s == State()


# ---------------------------------------------------------------- edits
def test_edit_only_when_text_changes_and_at_most_every_two_minutes():
    sim = Sim()
    live(sim, 2, minutes=3)
    posted_at = sim.t
    assert sim.log == [("post", sim.s.message_id, posted_at)]
    for n in (3, 4, 5, 4, 3, 4):  # someone joins or leaves every minute
        sim.t += MIN
        sim.people(n)
    # +1 too soon; +2 edit (4); +3 too soon; +4 already says 4; +5 edit (3); +6 too soon
    assert [t - posted_at for kind, _, t in sim.log if kind == "edit"] == [2 * MIN, 5 * MIN]
    assert sim.text().startswith("3 people")
    sim.run(2)
    assert sim.text().startswith("4 people")
    count = len(sim.log)
    assert sim.run(30) == [] and len(sim.log) == count  # nothing changes: no edit, ever
    assert len(sim.posts()) == 1


def test_failed_edit_is_retried_after_the_gap_not_every_tick():
    sim = Sim()
    live(sim, 2)
    sim.failing = True
    tried = [bool(a) for a in (sim.people(3), sim.run(1), sim.run(1))]
    assert tried == [True, False, True]
    sim.failing = False
    sim.run(2)
    assert sim.text().startswith("3 people")


# ---------------------------------------------------------------- end of a session
def test_room_empties_and_the_line_turns_past_tense():
    sim = Sim()
    sim.people(2)
    sim.run(3)
    sim.people(4)
    sim.run(127)  # 2 h 10 min after the room opened
    assert sim.people(0) is None
    assert sim.run(2) == []  # could be a channel hop: the line is left alone
    assert sim.run(1) == [Edit(sim.s.message_id, "That voice session ran 2 h 10 min with up to 4 people.")]
    assert not sim.s.live and sim.s.ended_at == ts(19) + 130 * MIN
    assert sim.run(600) == []  # left in place, never deleted or touched again
    assert sim.posts() == [sim.s.message_id] and sim.text().startswith("That voice session ran")


def test_a_short_gap_does_not_end_the_session():
    sim = Sim()
    live(sim, 3)
    sim.people(0)
    sim.run(2)  # everyone moves to another channel
    action = sim.people(3, channel=SQUAD)
    assert isinstance(action, Edit) and f"<#{SQUAD}>" in action.text
    assert sim.run(5) == [] and sim.s.live and sim.s.active == 0  # one unbroken stretch


def test_dropping_to_one_person_ends_it():
    sim = Sim()
    live(sim, 2)
    sim.people(1)
    sim.run(3)
    assert not sim.s.live and sim.text().startswith("That voice session ran")


# ---------------------------------------------------------------- reuse within 45 minutes
def test_back_within_45_minutes_reuses_the_message():
    sim = Sim()
    live(sim, 3, minutes=30)
    sim.people(0)
    sim.run(40)  # ended 40 minutes ago
    first = sim.s.message_id
    sim.human_since = True  # even with chat in between, it is still the same session
    sim.people(2)
    actions = sim.run(4)
    assert actions == [Edit(first, f"2 people are in <#{LOBBY}> right now. Click the channel to join.")]
    assert sim.posts() == [first]
    sim.run(26)  # half an hour after they came back
    sim.people(0)
    sim.run(3)
    # Both stretches count, the gap between them does not; the peak is the session's.
    assert sim.text() == "That voice session ran 1 h with up to 3 people."


def test_back_after_45_minutes_is_a_new_session():
    sim = Sim()
    live(sim, 3)
    sim.people(0)
    sim.run(50)
    first = sim.s.message_id
    sim.human_since = True
    sim.people(2)
    sim.run(4)
    assert len(sim.posts()) == 2 and sim.s.message_id != first
    assert sim.messages[first].startswith("That voice session ran")  # the old line keeps its record
    assert sim.s.peak == 2  # counted afresh


# ---------------------------------------------------------------- stacking
def test_no_member_message_since_the_last_line_edits_it_instead_of_posting():
    sim = Sim()
    live(sim, 3)
    sim.people(0)
    sim.run(120)
    first = sim.s.message_id
    sim.people(2)
    actions = sim.run(4)
    assert actions == [Edit(first, f"2 people are in <#{LOBBY}> right now. Click the channel to join.")]
    assert sim.posts() == [first]
    sim.people(0)
    sim.run(3)
    assert sim.text().startswith("That voice session ran")


def test_member_message_since_the_last_line_allows_a_new_one():
    sim = Sim()
    live(sim, 3)
    sim.people(0)
    sim.run(120)
    sim.human_since = True
    sim.people(2)
    assert [type(a) for a in sim.run(4)] == [Post]


def test_a_new_session_waits_out_the_chat_revival_hour():
    sim = Sim()
    sim.quiet_until = sim.t + 30 * MIN  # the revival posted half an hour ago
    sim.people(2)
    assert sim.run(29) == []
    assert [type(a) for a in sim.run(1)] == [Post]
    sim.quiet_until = sim.t + 60 * MIN  # a later revival does not stop the running line's edits
    sim.people(3)
    assert [type(a) for a in sim.run(2)] == [Edit]


# ---------------------------------------------------------------- daily cap
def test_never_more_than_three_new_messages_a_day():
    sim = Sim(t=ts(9))
    for _ in range(5):
        sim.human_since = True
        sim.people(2)
        sim.run(10)
        sim.people(0)
        sim.run(60)
    assert len(sim.posts()) == L.MAX_PER_DAY == 3
    assert all(text.startswith("That voice session ran") for text in sim.messages.values())
    assert datetime.fromtimestamp(sim.t, TZ).day == 7
    sim.t = ts(9, d=8)  # the next local day
    sim.human_since = True
    sim.people(2)
    sim.run(10)
    assert len(sim.posts()) == 4 and L.posts_today(sim.s, sim.t, TZ) == 1


def test_capped_session_with_quiet_chat_still_reuses_the_last_line():
    sim = Sim(t=ts(9))
    for _ in range(3):
        sim.human_since = True
        sim.people(2)
        sim.run(10)
        sim.people(0)
        sim.run(60)
    last = sim.s.message_id
    sim.people(2)  # nobody wrote since: an edit is not a new message
    assert sim.run(4) == [Edit(last, f"2 people are in <#{LOBBY}> right now. Click the channel to join.")]


# ---------------------------------------------------------------- deleted message
def test_deleted_message_is_not_recreated_for_that_session():
    sim = Sim()
    live(sim, 2)
    del sim.messages[sim.s.message_id]
    sim.people(3)
    sim.run(2)  # the edit finds it gone
    assert sim.s.gone and sim.s.message_id is None
    sim.people(4)
    sim.run(30)
    sim.people(0)
    sim.run(20)
    sim.human_since = True
    sim.people(2)  # back within 45 minutes: still that session
    assert sim.run(20) == [] and len(sim.posts()) == 1
    sim.people(0)
    sim.run(60)
    sim.people(2)  # a new session may post again
    assert [type(a) for a in sim.run(4)] == [Post]
    assert not sim.s.gone


def test_deleted_previous_line_does_not_silence_the_next_session():
    sim = Sim()
    live(sim, 2)
    sim.people(0)
    sim.run(120)
    sim.messages.clear()  # a mod tidied up yesterday's line
    sim.people(2)
    actions = sim.run(5)  # the reuse fails, then a fresh line is posted
    assert [type(a) for a in actions] == [Edit, Post] and sim.s.own and not sim.s.gone


# ---------------------------------------------------------------- restart
def test_state_round_trips_through_json():
    sim = Sim()
    live(sim, 3, games=("Risk of Rain 2",))
    raw = L.dump(sim.s)
    assert L.load(raw) == sim.s
    assert "Risk of Rain 2" in raw and set(json.loads(raw)) == set(State.__dataclass_fields__)


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"live": true}', '{"message_id": "12"}',
                                 '{"peak": true, "posts": "many"}'])
def test_unreadable_state_starts_fresh(raw):
    assert L.load(raw) == State()


def test_load_keeps_the_previous_line_when_the_rest_is_broken():
    assert L.load('{"live": true, "message_id": 77, "text": "x", "posts": 2, "day": "2026-10-07"}') == State(
        message_id=77, text="x", posts=2, day="2026-10-07")


def test_short_restart_carries_on_without_touching_the_line():
    sim = Sim()
    live(sim, 3)
    sim.s = L.load(L.dump(sim.s))
    sim.t += 2 * MIN  # down for two minutes, same people
    assert sim.tick() is None and sim.s.live and sim.s.active == 0


def test_restart_after_everyone_left_closes_the_session_at_the_last_tick():
    sim = Sim()
    live(sim, 3, minutes=30)
    seen = sim.t
    sim.s = L.load(L.dump(sim.s))
    sim.people(0, tick=False)
    sim.t += 20 * MIN  # down for twenty minutes; nobody is there now
    assert sim.tick() == Edit(sim.s.message_id, "That voice session ran 30 min with up to 3 people.")
    assert sim.s.ended_at == seen  # the downtime is not counted


def test_restart_with_people_still_there_resumes_the_same_message():
    sim = Sim()
    live(sim, 3, minutes=30)
    first = sim.s.message_id
    sim.s = L.load(L.dump(sim.s))
    sim.people(2, tick=False)
    sim.t += 20 * MIN
    assert sim.tick() == Edit(first, f"2 people are in <#{LOBBY}> right now. Click the channel to join.")
    assert sim.s.live and sim.s.active == 30 * MIN and sim.s.stretch == sim.t
    assert sim.posts() == [first]


def test_restart_after_a_long_outage_starts_a_new_session():
    sim = Sim()
    live(sim, 3, minutes=30)
    first = sim.s.message_id
    sim.s = L.load(L.dump(sim.s))
    sim.human_since = True
    sim.people(2, tick=False)
    sim.t += 3 * 3600
    assert sim.tick() == Edit(first, "That voice session ran 30 min with up to 3 people.")
    assert not sim.s.live and sim.s.start is None
    actions = sim.run(4)  # the people there now go through the hold like anyone else
    assert [type(a) for a in actions] == [Post] and sim.s.message_id != first


def test_restart_during_the_hold_starts_the_hold_again():
    sim = Sim()
    sim.people(2)
    sim.run(2)
    sim.t += 30 * MIN
    assert sim.tick() is None and sim.s.pending_since == sim.t
    assert sim.run(2) == [] and len(sim.run(1)) == 1


# ---------------------------------------------------------------- off switch
def test_stop_closes_a_live_line_once():
    sim = Sim()
    live(sim, 3, minutes=10)
    seen = sim.t
    state, action = L.stop(sim.s, seen + 8 * 3600)
    assert action == Edit(sim.s.message_id, "That voice session ran 10 min with up to 3 people.")
    assert not state.live and state.ended_at == seen and state.seen_at == 0
    state = L.edited(state, action.text, seen + 8 * 3600)
    assert L.stop(state, seen + 9 * 3600) == (state, None)


def test_stop_with_nothing_running_does_nothing():
    assert L.stop(State(), ts(20)) == (State(), None)
    held = L.step(State(), [Room(LOBBY, 2)], ts(20), TZ)[0]
    assert held.pending_since == ts(20) and L.stop(held, ts(20, 1)) == (State(), None)


@pytest.mark.parametrize("value,on", [(None, True), ("", True), ("on", True), ("ON", True), ("off", False),
                                      (" Off ", False), ("0", False), ("false", False), ("no", False)])
def test_off_switch_setting(value, on):
    assert config.tonight_enabled({} if value is None else {"TONIGHT": value}) is on
