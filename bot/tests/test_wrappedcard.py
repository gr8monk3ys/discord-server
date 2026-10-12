"""Wrapped cards (logic.wrappedcard): what a card says, and render smoke tests in the style
of the welcome-card tests. Pure: no Discord, no network."""

import io
from dataclasses import replace

import pytest
from PIL import Image

from logic import cards
from logic import wrapped as W
from logic import wrappedcard as WC

HOUR = 3600
ME, PAL, BUDDY = 11, 12, 13
NAMES_BY_ID = {PAL: "Pal", BUDDY: "Buddy"}

FULL = W.Summary(
    user_id=ME, year=2026, voice_seconds=212 * HOUR, game_seconds=140 * HOUR,
    top_games=(("Alpha Strike", 90 * HOUR), ("Beta Kart", 45 * HOUR), ("Gamma", 5 * HOUR)), games_played=3,
    messages=4812, active_days=143, busiest_day=("2026-11-14", 212), busiest_month=11,
    busiest_month_messages=1904, busiest_month_voice=71 * HOUR, chat_streak=19, daily_streak=41,
    teammates=(W.Teammate(PAL, 88 * HOUR, 14), W.Teammate(BUDDY, 0, 6)),
    level=17, coins_earned=48210, biggest_payout=(2400, "blackjack"),
    badges=("first_squad", "chatty", "tourney_win"), rarest_badge=("tourney_win", 1),
    squads_hosted=9, squads_joined=22, matches=5, gamenights=3, tournaments_entered=4, tournaments_won=1,
    clips=14, clip_week_wins=2, hall_of_fame=3, challenges=11, event_quests=6, event_drops=9, words_solved=37,
    house="Emberfall")
SPARSE = W.Summary(user_id=ME, year=2026, messages=7, active_days=3, badges=("early_member",), coins_earned=150,
                   joined_month=11)
SERVER = W.ServerSummary(
    year=2026, messages=58214, voice_seconds=1904 * HOUR, game_seconds=3120 * HOUR,
    top_games=(("Alpha Strike", 900 * HOUR), ("Beta Kart", 610 * HOUR)), squads=143, gamenights=12, tournaments=4,
    clips=96, badges=118, coins_earned=1_284_000, new_members=15, active_members=14, busiest_month=11,
    superlatives=(W.Superlative("voice", "Most hours in voice", (PAL,), "212 h"),
                  W.Superlative("host", "Squad captain", (PAL, BUDDY), "9 squads hosted"),
                  W.Superlative("clips", "Director's chair", (99,), "14 clips")))


def decode(png: bytes) -> Image.Image:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    img.load()
    return img


def avatar_png(color=(200, 80, 60), size=256) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def spec(summary=FULL, name="Alex", names=NAMES_BY_ID) -> WC.CardSpec:
    return WC.member_spec(summary, name, "alex", "Chill Gaming", names)


def section(card: WC.CardSpec, title: str) -> WC.Section | None:
    return next((s for s in card.sections if s.title == title), None)


def fact(card: WC.CardSpec, left: str, title: str = "Highlights") -> str | None:
    found = section(card, title)
    return next((r.right for r in found.rows if r.left == left), None) if found else None


# ---------------------------------------------------------------- what a member's card says
def test_member_spec_header_and_tiles():
    card = spec()
    assert card.label == ("Wrapped 2026", "Chill Gaming") and card.name == "Alex" and card.alt_name == "alex"
    assert card.title == "The Champion" and card.line == W.PERSONA_BY_KEY["champion"].line
    assert [(t.value, t.label) for t in card.tiles] == [
        ("212", "hours in voice"), ("4,812", "messages"), ("140", "hours in game"), ("48,210", "coins earned"),
        ("17", "level reached"), ("3", "badges earned")]
    assert card.notes == ()


def test_tiles_leave_out_zeros_and_say_levels_gained_only_with_a_snapshot():
    some = replace(FULL, voice_seconds=0, game_seconds=0, coins_earned=0, level=9, levels_gained=4)
    labels = {t.label: t.value for t in spec(some).tiles}
    assert labels == {"messages": "4,812", "levels gained": "+4", "badges earned": "3",
                      "squads and matches": "36", "days in chat": "143"}
    assert all(t.value not in ("0", "+0") for t in spec(some).tiles)
    # A snapshot that shows no gain falls back to the level itself.
    assert ("9", "level reached") in [(t.value, t.label) for t in spec(replace(some, levels_gained=0)).tiles]
    assert "level reached" not in {t.label for t in spec(replace(some, level=None, levels_gained=None)).tiles}


def test_top_games_rows_carry_hours_and_bar_shares():
    rows = section(spec(), "Top games").rows
    assert [(r.left, r.right) for r in rows] == [("Alpha Strike", "90 h"), ("Beta Kart", "45 h"), ("Gamma", "5 h")]
    assert rows[0].share == 1.0 and rows[1].share == 0.5
    assert section(spec(replace(FULL, top_games=(), game_seconds=0)), "Top games") is None


def test_teammates_are_shown_by_display_name():
    rows = section(spec(), "Favourite teammates").rows
    assert [(r.left, r.right) for r in rows] == [("Pal", "88 h in voice · 14 squads"), ("Buddy", "6 squads")]
    assert all(str(uid) not in r.left + r.right for r in rows for uid in (PAL, BUDDY))


def test_teammates_section_is_dropped_when_fewer_than_two_can_be_named():
    assert section(spec(names={PAL: "Pal"}), "Favourite teammates") is None  # Buddy left the server
    assert section(spec(names={}), "Favourite teammates") is None
    assert section(spec(replace(FULL, teammates=())), "Favourite teammates") is None


def test_highlights():
    card = spec()
    assert fact(card, "Busiest month") == "November · 1,904 messages, 71 h in voice"
    assert fact(card, "Busiest day") == "Nov 14 · 212 messages"
    assert fact(card, "Longest /daily streak") == "41 days"
    assert fact(card, "Biggest payout") == "2,400 coins · Blackjack"
    assert fact(card, "Rarest badge") == "Champion · only you so far"
    assert fact(card, "Squads") == "9 hosted, 22 joined, 5 matches"
    assert fact(card, "Tournaments") == "4 entered, 1 won"
    assert fact(card, "Clips") == "14 posted, 2 Clip of the Week"
    assert fact(card, "Quests and challenges") == "17 claimed, 9 event drops"
    assert fact(card, "Daily Word") == "37 solved" and fact(card, "House") == "Emberfall"
    assert fact(card, "Joined") is None


def test_highlights_only_say_what_there_is():
    plain = W.Summary(ME, 2026, messages=300, active_days=40, busiest_day=("2026-03-02", 30), busiest_month=3,
                      busiest_month_messages=200, chat_streak=5, joined_month=2,
                      badges=("chatty",), rarest_badge=("chatty", 6))
    card = spec(plain)
    assert [r.left for r in section(card, "Highlights").rows] == [
        "Busiest month", "Busiest day", "Longest chat streak", "Rarest badge", "Joined"]
    assert fact(card, "Busiest month") == "March · 200 messages"
    assert fact(card, "Longest chat streak") == "5 days in a row"
    assert fact(card, "Rarest badge") == "Chatty · held by 6" and fact(card, "Joined") == "in February"
    assert [s.title for s in card.sections] == ["Highlights"]  # no games, no teammates: no empty sections


def test_a_member_with_almost_no_data_gets_the_short_kind_card():
    card = spec(SPARSE)
    assert card.tiles == () and card.sections == ()
    assert card.title == "Fresh Ink"
    assert "Glad you're here" in card.notes[0]
    assert card.notes[1] == "7 messages · 1 badge · 150 coins earned · joined in November"
    assert card.notes[-1] == "Here's to more in 2027."
    assert not any(ch.isdigit() and ch == "0" for ch in card.notes[1].replace("150", ""))  # never a row of zeros
    nothing = spec(W.Summary(ME, 2026))
    assert nothing.title == "Quiet Corner" and len(nothing.notes) == 2 and "quiet year" in nothing.notes[0]


def test_names_that_fail_the_text_filter_fall_back():
    invite = "join discord.gg/abcdef"
    assert WC.screened(invite) is None and WC.screened("Alex") == "Alex" and WC.screened(None) is None
    card = WC.member_spec(replace(FULL, house=invite, top_games=((invite, HOUR),)), invite, "alex", invite,
                          {PAL: invite, BUDDY: "Buddy"})
    assert card.name is None and card.alt_name == "alex" and card.label == ("Wrapped 2026", "")
    assert section(card, "Favourite teammates").rows[0].left == ""
    assert section(card, "Favourite teammates").rows[0].fallback == WC.MEMBER_FALLBACK
    assert section(card, "Top games").rows[0].left == "" and fact(card, "House") is None
    assert invite not in repr(card)


def test_compact():
    assert WC.compact(0) == "0" and WC.compact(99_999) == "99,999"
    assert WC.compact(123_456) == "123k" and WC.compact(1_284_000) == "1.3M"


# ---------------------------------------------------------------- what the server card says
def test_server_spec_totals_and_shout_outs():
    card = WC.server_spec(SERVER, "Chill Gaming", NAMES_BY_ID)
    assert card.label == ("Wrapped 2026", "The whole server") and card.name == "Chill Gaming"
    assert card.line == "14 members left a mark on the page this year."
    assert [(t.value, t.label) for t in card.tiles][:3] == [
        ("58,214", "messages"), ("1,904", "hours in voice"), ("3,120", "hours in game")]
    shouts = section(card, "Shout-outs").rows
    assert [(r.left, r.names, r.right) for r in shouts] == [
        ("Most hours in voice", ("Pal",), "212 h"), ("Squad captain", ("Pal", "Buddy"), "9 squads hosted")]
    assert fact(card, "Busiest month", "Also this year") == "November"
    assert fact(card, "Clips posted", "Also this year") == "96"


def test_server_spec_never_names_someone_without_a_name_to_show():
    # Member 99 isn't in `names` (left, or opted out since): that shout-out is dropped, not shown as an id.
    card = WC.server_spec(SERVER, "Chill Gaming", NAMES_BY_ID)
    assert "99" not in repr(section(card, "Shout-outs"))
    assert section(WC.server_spec(SERVER, "Chill Gaming", {}), "Shout-outs") is None
    half = WC.server_spec(SERVER, "Chill Gaming", {PAL: "Pal"})
    assert [r.left for r in section(half, "Shout-outs").rows] == ["Most hours in voice"]  # a shared one needs both


def test_empty_server_spec():
    card = WC.server_spec(W.ServerSummary(2026), "Chill Gaming")
    assert card.tiles == () and card.sections == () and "first pages" in card.line


# ---------------------------------------------------------------- rendering
NAMES = [
    "Alex",
    "Maximilian Alexander Featherstonehaugh-Worthington the Third of Somewhere Very Far",
    "W" * 32,
    "🎮💀🔥",
    "🔥 Nova 🔥",
    "שלום עולם",
    "مرحبا بالعالم",
    "Zoë Ångström",
    "李小龍",
    "‮\u0000weird‏",
    "",
]


@pytest.mark.parametrize("name", NAMES)
def test_render_is_a_valid_png_whatever_the_names(name):
    card = WC.member_spec(replace(FULL, top_games=((name, 9 * HOUR), ("Beta", HOUR)), house=name), name, name, name,
                          {PAL: name, BUDDY: name})
    img = decode(WC.render(card, avatar_png()))
    assert img.format == "PNG" and img.width == WC.WIDTH == 1080
    assert 1200 < img.height <= WC.MAX_HEIGHT


def test_render_is_deterministic():
    assert WC.render(spec(), avatar_png()) == WC.render(spec(), avatar_png())
    assert WC.render(spec(SPARSE)) == WC.render(spec(SPARSE))
    assert WC.render(spec()) != WC.render(spec(replace(FULL, messages=4813)))


def test_card_is_as_tall_as_its_content():
    full = decode(WC.render(spec()))
    short = decode(WC.render(spec(SPARSE)))
    no_mates = decode(WC.render(spec(names={})))
    assert short.height < 700 < full.height
    assert no_mates.height < full.height
    assert decode(WC.render(spec(W.Summary(ME, 2026)))).height < short.height  # one note fewer


def test_tile_rows_fill_the_width():
    # 4 tiles sit 2 + 2 and 5 sit 3 + 2, so there is never a lone tile on a row.
    assert WC.TILE_ROWS[4] == (2, 2) and WC.TILE_ROWS[5] == (3, 2) and WC.TILE_ROWS[6] == (3, 3)
    assert all(sum(rows) == n for n, rows in WC.TILE_ROWS.items())
    assert len(spec(replace(FULL, voice_seconds=0, game_seconds=0)).tiles) == WC.MAX_TILES  # others move up
    for n in range(0, 7):
        card = replace(spec(), tiles=tuple(WC.Tile(str(i), "label") for i in range(n)))
        assert decode(WC.render(card)).width == 1080


def test_render_without_avatar_uses_initial_circle():
    img = decode(WC.render(spec())).convert("RGB")
    r, g, b = img.getpixel((WC.LEFT + 20, WC.TOP + WC.AVATAR // 2))
    assert g > r and g > b  # the pen-coloured fallback circle


def test_render_uses_the_avatar_when_given():
    img = decode(WC.render(spec(), avatar_png((250, 0, 0)))).convert("RGB")
    r, g, b = img.getpixel((WC.LEFT + WC.AVATAR // 2, WC.TOP + WC.AVATAR // 2))
    assert r > 200 and g < 60 and b < 60


@pytest.mark.parametrize("bad", [b"", b"not an image", b"\x89PNG\r\n\x1a\n broken"])
def test_render_survives_a_broken_avatar(bad):
    assert decode(WC.render(spec(), bad)).size == decode(WC.render(spec())).size


def test_render_is_dark_with_the_pen_spine():
    img = decode(WC.render(spec())).convert("RGB")
    assert max(img.getpixel((WC.WIDTH - 20, 30))) < 70  # dark ground
    for y in (0, img.height // 2, img.height - 1):  # the spine runs the whole height
        assert img.getpixel((4, y)) == cards.PEN
    assert max(img.getpixel((WC.WIDTH - 20, img.height - 5))) < 70  # nothing drawn past the footer


def test_long_text_stays_inside_the_margins():
    long = "Extremely Long Name " * 12
    card = WC.member_spec(replace(FULL, top_games=((long, 9 * HOUR),), house=long), long, long, long,
                          {PAL: long, BUDDY: long})
    img = decode(WC.render(card)).convert("RGB")
    # The strip right of the content edge holds only the page (ground and dot grid), never ink.
    strip = img.crop((WC.RIGHT + 6, 0, WC.WIDTH, img.height))
    assert max(max(px) for px in strip.getdata()) < 70


def test_server_card_renders():
    img = decode(WC.render(WC.server_spec(SERVER, "Chill Gaming", NAMES_BY_ID)))
    assert img.width == 1080 and 900 < img.height < 1800
    empty = decode(WC.render(WC.server_spec(W.ServerSummary(2026), "Chill Gaming")))
    assert empty.height < 450


def test_wrap_and_fit_helpers():
    font = cards.load_font(26)
    lines = WC._wrap("word " * 60, font, 400, 2)
    assert len(lines) == 2 and lines[-1].endswith("…") and all(font.getlength(line) <= 400 for line in lines)
    assert WC._wrap("short", font, 400) == ["short"] and WC._wrap("", font, 400) == []
    text, _ = WC._fitted("🎮🎮", 28, 300, True, "Member")
    assert text == "Member"
