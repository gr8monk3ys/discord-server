"""Welcome-card rules and rendering (logic.cards). Pure: no Discord, no network."""

import io

import pytest
from PIL import Image

from logic import cards


def decode(png: bytes) -> Image.Image:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    img.load()
    return img


def avatar_png(color=(200, 80, 60), size=256) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- rules
def test_card_key_is_per_user():
    assert cards.card_key(42) == "card:42"


@pytest.mark.parametrize("is_bot, done, expected", [
    (False, False, True), (True, False, False), (False, True, False), (True, True, False)])
def test_should_card(is_bot, done, expected):
    assert cards.should_card(is_bot, done) is expected


def test_member_label():
    assert cards.member_label(1) == "Member #1"
    assert cards.member_label(1234) == "Member #1,234"
    assert cards.member_label(None) is None
    assert cards.member_label(0) is None


# ---------------------------------------------------------------- cleaning
def ascii_only(ch: str) -> bool:
    return ch.isascii()


def test_clean_keeps_drawable_text():
    assert cards.clean_text("Alex", ascii_only) == "Alex"


def test_clean_strips_undrawable_and_control_characters():
    assert cards.clean_text("🎮 Alex 🎮", ascii_only) == "Alex"
    assert cards.clean_text("A​l‮ex\n", ascii_only) == "Alex"
    assert cards.clean_text("  lots   of   space ", ascii_only) == "lots of space"


def test_clean_returns_empty_when_nothing_drawable():
    assert cards.clean_text("🎮🎮", ascii_only) == ""
    assert cards.clean_text("", ascii_only) == ""
    assert cards.clean_text(None, ascii_only) == ""


def test_clean_normalises_compatibility_forms():
    # Fullwidth and "math bold" letters fold to plain ones the font can draw.
    assert cards.clean_text("Ａｌｅｘ", ascii_only) == "Alex"
    assert cards.clean_text("𝐀𝐥𝐞𝐱", ascii_only) == "Alex"


def test_fit_truncates_with_ellipsis():
    measure = len  # one unit per character
    assert cards.fit_text("Alex", measure, 10) == "Alex"
    out = cards.fit_text("a" * 50, measure, 10)
    assert out.endswith("…") and measure(out) <= 10
    assert cards.fit_text("abc def ghi", measure, 8) == "abc def…"  # trailing space trimmed
    assert cards.fit_text("abcdef", measure, 0) == ""


def test_initial():
    assert cards.initial("alex") == "A"
    assert cards.initial("  _x") == "X"
    assert cards.initial("") == "?"
    assert cards.initial("!!!") == "?"


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
def test_render_is_a_valid_png_of_the_right_size(name):
    png = cards.render_card(name, 1234, "Chill Gaming", avatar_png())
    img = decode(png)
    assert img.size == (cards.WIDTH, cards.HEIGHT) == (1100, 400)
    assert img.format == "PNG"


def test_render_without_avatar_uses_initial_circle():
    png = cards.render_card("Alex", 7, "Chill Gaming", None)
    img = decode(png).convert("RGB")
    assert img.size == (1100, 400)
    # The fallback circle is accent-coloured around the avatar's centre.
    x, y = cards.AVATAR_CENTER
    r, g, b = img.getpixel((x - cards.AVATAR_SIZE // 2 + 20, y))
    assert g > r and g > b


@pytest.mark.parametrize("bad", [b"", b"not an image", b"\x89PNG\r\n\x1a\n broken"])
def test_render_survives_a_broken_avatar(bad):
    img = decode(cards.render_card("Alex", 7, "Chill Gaming", bad))
    assert img.size == (1100, 400)


def test_render_uses_the_avatar_when_given():
    img = decode(cards.render_card("Alex", 7, "Chill Gaming", avatar_png((250, 0, 0)))).convert("RGB")
    x, y = cards.AVATAR_CENTER
    r, g, b = img.getpixel((x, y))
    assert r > 200 and g < 60 and b < 60


def test_render_handles_odd_avatars():
    # Transparent, tiny and non-square avatars all render.
    for im in (Image.new("RGBA", (64, 64), (0, 0, 0, 0)), Image.new("RGB", (8, 8), "white"),
               Image.new("P", (300, 120))):
        buf = io.BytesIO()
        im.save(buf, "PNG")
        assert decode(cards.render_card("Alex", 2, "Server", buf.getvalue())).size == (1100, 400)


def test_render_without_member_count_or_server_name():
    assert decode(cards.render_card("Alex", None, "", None)).size == (1100, 400)


def test_render_is_dark_with_an_accent():
    img = decode(cards.render_card("Alex", 7, "Chill Gaming", None)).convert("RGB")
    r, g, b = img.getpixel((cards.WIDTH - 40, cards.HEIGHT // 2))
    assert max(r, g, b) < 70  # dark ground
    colors = {img.getpixel((x, 200)) for x in range(0, 12)}
    assert any(abs(c[0] - 0x42) < 25 and abs(c[1] - 0xA9) < 25 and abs(c[2] - 0x79) < 25 for c in colors)


def test_fonts_load_or_fall_back():
    font = cards.load_font(40, bold=True)
    assert font is not None
    assert cards.can_draw(font, "A")
