"""Pure logic for module 3 (clip of the week): link detection, poll selection, winner."""

import pytest

from logic import clips as C


# ---------------------------------------------------------------- links
@pytest.mark.parametrize("text, url", [
    ("look https://www.youtube.com/watch?v=abc123", "https://www.youtube.com/watch?v=abc123"),
    ("https://youtube.com/shorts/xyz", "https://youtube.com/shorts/xyz"),
    ("https://m.youtube.com/watch?v=q", "https://m.youtube.com/watch?v=q"),
    ("https://youtu.be/abc", "https://youtu.be/abc"),
    ("https://clips.twitch.tv/FunnyClipName-abc", "https://clips.twitch.tv/FunnyClipName-abc"),
    ("https://www.twitch.tv/someone/clip/FunnyClip-xyz?filter=clips", "https://www.twitch.tv/someone/clip/FunnyClip-xyz?filter=clips"),
    ("https://medal.tv/games/valorant/clips/abc", "https://medal.tv/games/valorant/clips/abc"),
    ("https://streamable.com/abc12", "https://streamable.com/abc12"),
    ("https://outplayed.tv/valorant/abc", "https://outplayed.tv/valorant/abc"),
    ("https://kick.com/someone/clips/clip_01", "https://kick.com/someone/clips/clip_01"),
    ("HTTPS://YOUTU.BE/abc", "HTTPS://YOUTU.BE/abc"),
    ("no embed please <https://youtu.be/abc>", "https://youtu.be/abc"),
    ("(https://streamable.com/abc).", "https://streamable.com/abc"),
    ("first https://example.com/x then https://youtu.be/a and https://youtu.be/b", "https://youtu.be/a"),
])
def test_clip_link_positive(text, url):
    assert C.clip_link(text) == url


@pytest.mark.parametrize("text", [
    "",
    None,
    "just talking about youtube.com",
    "youtu.be/abc without scheme",
    "https://example.com/video.mp4",
    "https://notyoutube.com/watch?v=1",
    "https://youtube.com.evil.io/watch?v=1",
    "https://www.youtube.com/",
    "https://www.twitch.tv/someone",  # a channel, not a clip
    "https://www.twitch.tv/videos/12345",  # a VOD, not a clip
    "ftp://youtu.be/abc",
])
def test_clip_link_negative(text):
    assert C.clip_link(text) is None


def test_is_video():
    assert C.is_video("video/mp4")
    assert C.is_video("video/quicktime")
    assert not C.is_video("image/png")
    assert not C.is_video(None)
    assert not C.is_video("")


def test_clip_url_attachment_first_then_link():
    att = [("https://cdn/x.png", "image/png"), ("https://cdn/clip.mp4", "video/mp4")]
    assert C.clip_url("https://youtu.be/a", att) == "https://cdn/clip.mp4"
    assert C.clip_url("https://youtu.be/a", [("https://cdn/x.png", "image/png")]) == "https://youtu.be/a"
    assert C.clip_url("nice", [("https://cdn/x.png", "image/png")]) is None
    assert C.clip_url(None, []) is None


# ---------------------------------------------------------------- poll
def clip(mid, posted, reactions=0, uid=None):
    return C.Clip(message_id=mid, user_id=uid or mid, posted_at=posted, reactions=reactions)


def test_poll_entries_chronological_when_ten_or_fewer():
    clips = [clip(3, 300), clip(1, 100), clip(2, 200)]
    assert [c.message_id for c in C.poll_entries(clips)] == [1, 2, 3]


def test_poll_entries_top_ten_by_reactions_ties_earlier_then_chronological():
    # 12 clips; reactions: two of them at 0, ties at 5 broken by earlier post
    clips = [clip(i, 1000 + i, reactions=r) for i, r in enumerate([5, 9, 0, 5, 7, 5, 1, 3, 2, 8, 0, 5])]
    picked = C.poll_entries(clips)
    assert len(picked) == 10
    ids = [c.message_id for c in picked]
    # dropped: the two 0-reaction clips (2 and 10)
    assert 2 not in ids and 10 not in ids
    assert ids == sorted(ids)  # numbered in posting order

    # 11 clips where only a tie decides the last slot: earlier post wins
    clips = [clip(i, 1000 + i, reactions=10) for i in range(9)] + [clip(20, 5000, 1), clip(21, 4000, 1)]
    ids = [c.message_id for c in C.poll_entries(clips)]
    assert 21 in ids and 20 not in ids
    assert ids == list(range(9)) + [21]


def test_answer_text_truncated_to_55():
    assert C.answer_text(1, "Lorenzo") == "#1 · Lorenzo"
    long = C.answer_text(10, "x" * 100)
    assert len(long) == 55 and long.startswith("#10 · xxx")


def test_week_of_key():
    assert C.week_of("clips:2026-W40") == "2026-W40"


# ---------------------------------------------------------------- winner
def test_winner_most_votes():
    assert C.winner_index(3, {1: 2, 2: 5, 3: 1}) == 1


def test_winner_tie_goes_to_earliest():
    assert C.winner_index(3, {1: 0, 2: 4, 3: 4}) == 1
    assert C.winner_index(3, {3: 4, 1: 4}) == 0


def test_winner_zero_votes_none():
    assert C.winner_index(3, {}) is None
    assert C.winner_index(3, {1: 0, 2: 0}) is None


def test_winner_ignores_unknown_answers():
    assert C.winner_index(2, {7: 10, 2: 1}) == 1


def test_give_up_after_48h():
    ends = 1_000_000
    assert not C.give_up(ends + 47 * 3600, ends)
    assert not C.give_up(ends + 48 * 3600, ends)
    assert C.give_up(ends + 48 * 3600 + 1, ends)
