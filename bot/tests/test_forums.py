"""Tests for logic.forums: which forum, the minimum-content bar, the payout decision, the recap line."""

import config
from logic import forums as F
from logic import quests as Q

DAY = 24 * 60 * 60
T0 = 1_791_400_000  # October 2026
OLD = 1  # a 2015 snowflake: long established


def young(at: int, days: int = 3) -> int:
    """A snowflake id for an account created `days` before `at`."""
    return ((at - days * DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22


GUIDE = ("Start every round by buying light armor and a ghost, then hold the angle from "
         "heaven instead of pushing main. Lower your sensitivity until a full swipe turns you "
         "exactly half a circle, and practise ten minutes of deathmatch before ranked games. "
         "It took me from silver to platinum in about a month.")


def test_kind_of_matches_the_two_forums_only():
    assert F.kind_of(config.GUIDES_FORUM) == F.GUIDES
    assert F.kind_of(config.SETUPS_FORUM) == F.SETUPS
    assert F.kind_of("guides") == F.GUIDES and F.kind_of("🖥️・Setups") == F.SETUPS
    assert F.kind_of(config.LFG_FORUM) is None and F.kind_of(config.SUGGESTIONS_FORUM) is None
    assert F.kind_of(None) is None and F.kind_of("") is None


def test_reactions_match_the_layout_defaults():
    import layout
    specs = {config.slug(c["name"]): c for cat in layout.CATEGORIES for c in cat["channels"]}
    for kind, name in F.FORUMS.items():
        spec = specs[config.slug(name)]
        assert spec["type"] == "forum" and spec["reaction"] == F.REACTIONS[kind]


def test_is_image():
    assert F.is_image("desk.png", "image/png") and F.is_image("x", "IMAGE/webp")
    assert F.is_image("desk.JPG", None) and F.is_image("a.jpeg", "")
    assert not F.is_image("notes.txt", "text/plain") and not F.is_image("clip.mp4", "video/mp4")
    assert not F.is_image("fake.png", "application/zip")  # the content type wins over the name
    assert not F.is_image(None, None)


def test_guides_need_real_text():
    assert F.qualifies(F.GUIDES, GUIDE, 0)
    assert not F.qualifies(F.GUIDES, "", 0) and not F.qualifies(F.GUIDES, None, 0)
    assert not F.qualifies(F.GUIDES, "use the ghost", 0)
    assert not F.qualifies(F.GUIDES, "a" * 500, 0)  # long, but one "word"
    assert not F.qualifies(F.GUIDES, "buy armor then push. " * 30, 0)  # one sentence on repeat
    assert not F.qualifies(F.GUIDES, "great guide here " + "https://example.com/" + "x" * 400, 0)  # link-only
    assert not F.qualifies(F.GUIDES, "short", 3)  # pictures don't make a guide
    assert not F.qualifies(F.GUIDES, GUIDE.replace(" ", "     ")[:150], 0)


def test_setups_need_a_picture_or_a_description():
    assert F.qualifies(F.SETUPS, "", 1)
    assert F.qualifies(F.SETUPS, "my desk", 2)
    assert not F.qualifies(F.SETUPS, "my desk", 0) and not F.qualifies(F.SETUPS, "", 0)
    assert F.qualifies(F.SETUPS, GUIDE, 0)  # a base tour in words is fine
    assert not F.qualifies("lfg", GUIDE, 5)


def test_measure_ignores_links_and_whitespace():
    assert F.measure("  hello   world  ") == (11, 2)
    assert F.measure("see https://example.com/a?b=c and www.example.org now") == (11, 3)
    assert F.measure("Hello hello HELLO") == (17, 1)
    assert F.measure(None) == (0, 0)


def decide(content=GUIDE, **kw):
    args = dict(author_id=OLD, created_at=T0, locked=False, paid_this_week=0)
    args.update(kw)
    return F.decide(F.GUIDES, content, 0, **args)


def test_decide_pays_a_good_post_from_an_established_account():
    assert decide() == F.PAID
    assert decide(paid_this_week=F.WEEKLY_CAP - 1) == F.PAID


def test_decide_refuses_thin_locked_capped_and_young():
    assert decide(content="nice") == F.SKIPPED
    assert decide(locked=True) == F.SKIPPED  # a mod locked it
    assert decide(paid_this_week=F.WEEKLY_CAP) == F.UNPAID
    assert decide(author_id=young(T0)) == F.UNPAID
    # Age counts at the time of posting: waiting out the survival window doesn't help an alt.
    almost = young(T0, Q.MIN_ACCOUNT_DAYS - 1)
    assert decide(author_id=almost, created_at=T0) == F.UNPAID
    assert decide(author_id=young(T0, Q.MIN_ACCOUNT_DAYS + 1)) == F.PAID
    assert decide(content="nice", author_id=young(T0)) == F.SKIPPED  # thin comes first


def test_reward_is_small_next_to_ordinary_earning():
    from logic import coins
    assert F.REWARD * F.WEEKLY_CAP <= coins.MESSAGE_DAILY_CAP * 2  # a week of posts < two days of chat
    assert F.SURVIVE >= DAY
    assert F.ref(123) == "forum:123" and F.ref(123) != F.ref(124)


def test_show_title_filters_and_escapes():
    assert F.show_title("Crosshair settings") == "Crosshair settings"
    assert F.show_title("  spaced   out  ") == "spaced out"
    assert F.show_title("**bold** _x_") == r"\*\*bold\*\* \_x\_"
    assert "@everyone" not in F.show_title("hey @everyone look")
    assert F.show_title("join discord.gg/abcdef") is None
    assert F.show_title("cheap skins at example.com") is None  # no links in what the bot repeats
    assert F.show_title("") is None and F.show_title(None) is None and F.show_title("   ") is None
    long = F.show_title("word " * 40)
    assert len(long) <= F.MAX_TITLE and long.endswith("…")


def post(kind, tid, title, at=T0):
    return F.Post(kind, tid, title, at)


def test_fresh_line_counts_and_links_the_newest():
    assert F.fresh_line([]) is None
    one = F.fresh_line([post(F.GUIDES, 10, "Crosshairs")])
    assert one == "Fresh in the forums: 1 new guide. Latest: **Crosshairs** (<#10>)"
    line = F.fresh_line([post(F.GUIDES, 10, "Old guide", T0), post(F.SETUPS, 11, "My desk", T0 + 50),
                         post(F.GUIDES, 12, "Mid guide", T0 + 20)])
    assert line == "Fresh in the forums: 2 new guides and 1 new setup. Latest: **My desk** (<#11>)"
    assert F.fresh_line([post(F.SETUPS, 1, "a"), post(F.SETUPS, 2, "b", T0 + 1)]).startswith(
        "Fresh in the forums: 2 new setups. Latest: **b**")


def test_fresh_line_never_repeats_or_links_a_refused_title():
    bad = post(F.GUIDES, 20, "free nitro discord.gg/scam123", T0 + 100)
    good = post(F.GUIDES, 21, "Rotations", T0)
    line = F.fresh_line([bad, good])
    assert line == "Fresh in the forums: 2 new guides. Latest: **Rotations** (<#21>)"
    only_bad = F.fresh_line([bad])
    assert only_bad == "Fresh in the forums: 1 new guide." and "20" not in only_bad
    assert F.fresh_line([post("other", 1, "x")]) is None
