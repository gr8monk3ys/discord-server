"""Welcome cards: who gets one, and drawing the 1100x400 PNG in the Field Notebook look
(forest-green pen on a dark, grainy page). Pure: no Discord, no network; render_card is
CPU-bound, so callers run it in a thread.

Names can be anything Discord allows, so the text is cleaned before drawing: compatibility
forms fold to plain letters (fullwidth, "math bold"), control and format characters go
(including bidi overrides), and anything the chosen font can't draw (emoji, scripts we ship
no font for) is dropped. If nothing drawable is left, the card just says "Welcome"."""

from __future__ import annotations

import functools
import io
import logging
import unicodedata
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, features

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1100, 400
AVATAR_SIZE = 220
AVATAR_CENTER = (215, 200)
TEXT_X = 380
TEXT_RIGHT = WIDTH - 70
NAME_BASELINE = 262  # names sit on one baseline whatever their script or size
MAX_AVATAR_PIXELS = 4096 * 4096  # refuse anything absurd before decoding it

# Field Notebook, night side (theme/field-notebook.theme.css)
GROUND = (22, 24, 29)  # night-card
DOTS = (40, 43, 50)
HAIRLINE = (41, 44, 50)
INK = (250, 250, 250)
INK_MUTED = (171, 176, 186)
INK_FAINT = (118, 124, 135)
PEN = (0x42, 0xA9, 0x79)  # the one pen
PEN_TEXT = (11, 26, 18)

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
FONTS = {  # script -> file; each ships with its OFL licence next to it
    "latin": "IBMPlexSans-Variable.ttf",
    "arabic": "NotoSansArabic-Variable.ttf",
    "hebrew": "NotoSansHebrew-Variable.ttf",
}
REMOVE_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
ELLIPSIS = "…"


# ---------------------------------------------------------------- rules
def card_key(user_id: int) -> str:
    """meta key marking that this member got their card (once per member, ever)."""
    return f"card:{user_id}"


def should_card(is_bot: bool, already_done: bool) -> bool:
    return not is_bot and not already_done


def member_label(count: int | None) -> str | None:
    if not count or count < 1:
        return None
    return f"Member #{count:,}"


# ---------------------------------------------------------------- text
def clean_text(text: str | None, drawable: Callable[[str], bool]) -> str:
    """Fold, strip control/format characters and anything `drawable` rejects, collapse spaces."""
    if not text:
        return ""
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) in REMOVE_CATEGORIES:
            continue
        elif drawable(ch):
            out.append(ch)
    return " ".join("".join(out).split())


def fit_text(text: str, measure: Callable[[str], float], max_width: float) -> str:
    """`text`, or its longest prefix plus an ellipsis that fits in `max_width`."""
    if max_width <= 0:
        return ""
    if measure(text) <= max_width:
        return text
    for end in range(len(text) - 1, 0, -1):
        candidate = text[:end].rstrip() + ELLIPSIS
        if candidate != ELLIPSIS and measure(candidate) <= max_width:
            return candidate
    return ""


def initial(name: str | None) -> str:
    for ch in name or "":
        if ch.isalnum():
            return ch.upper()
    return "?"


# ---------------------------------------------------------------- fonts
@functools.lru_cache(maxsize=64)
def load_font(size: int, bold: bool = False, script: str = "latin"):
    """The shipped font for `script` at `size`, or Pillow's default font if it's missing."""
    path = FONT_DIR / FONTS.get(script, FONTS["latin"])
    try:
        engine = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
        font = ImageFont.truetype(str(path), size, layout_engine=engine)
        try:
            font.set_variation_by_name("Bold" if bold else "Regular")
        except Exception:  # not a variable font, or no such instance: keep its default
            pass
        return font
    except Exception:
        log.warning("cards: couldn't load %s; using Pillow's default font", path.name)
        return ImageFont.load_default(size)


def _glyph(font, ch: str) -> bytes | None:
    try:
        left, top, right, bottom = font.getbbox(ch)
    except Exception:
        return None
    if right <= left or bottom <= top:
        return b""
    img = Image.new("L", (right - left, bottom - top))
    ImageDraw.Draw(img).text((-left, -top), ch, font=font, fill=255)
    return bytes(img.tobytes()) + f"{img.size}".encode()


_NOTDEF = "\U000F0000"  # private-use plane: no font we ship maps it, so it draws .notdef
_draw_cache: dict[tuple[int, str], bool] = {}


def can_draw(font, ch: str) -> bool:
    """Whether `font` has a real glyph for `ch` (not the .notdef box)."""
    if ch.isspace():
        return True
    key = (id(font), ch)
    if key not in _draw_cache:
        glyph = _glyph(font, ch)
        if glyph is None:
            ok = False
        elif glyph == b"":
            # Invisible: fine for combining marks, useless for anything else.
            ok = unicodedata.category(ch).startswith("M")
        else:
            ok = glyph != _glyph(font, _NOTDEF)
        if len(_draw_cache) > 20_000:
            _draw_cache.clear()
        _draw_cache[key] = ok
    return _draw_cache[key]


def pick_script(text: str, size: int, bold: bool = True) -> str:
    """The shipped script font that can draw the most of `text` (Latin wins ties)."""
    best, best_score = "latin", -1
    for script in FONTS:
        font = load_font(size, bold, script)
        score = sum(1 for ch in text if not ch.isspace() and can_draw(font, ch))
        if score > best_score:
            best, best_score = script, score
    return best


# ---------------------------------------------------------------- drawing
def _supersampled_circle(size: int, scale: int = 4) -> Image.Image:
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def decode_avatar(data: bytes | None, size: int = AVATAR_SIZE) -> Image.Image | None:
    """A square RGBA crop of the avatar, or None if it isn't a usable image."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_AVATAR_PIXELS:
                return None
            img.seek(0)  # first frame of an animated avatar
            img.load()
            rgba = img.convert("RGBA")
    except Exception:
        return None
    rgba = ImageOps.fit(rgba, (size, size), Image.Resampling.LANCZOS)
    flat = Image.new("RGBA", (size, size), GROUND + (255,))  # transparent avatars sit on the page
    flat.alpha_composite(rgba)
    return flat


def initial_avatar(letter: str, size: int = AVATAR_SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), PEN + (255,))
    font = load_font(int(size * 0.46), True, pick_script(letter, int(size * 0.46)))
    if not can_draw(font, letter):
        letter = "?"
    ImageDraw.Draw(img).text((size / 2, size / 2), letter, font=font, fill=PEN_TEXT, anchor="mm")
    return img


def _page() -> Image.Image:
    """Dark page with a faint dot grid and generated grain."""
    page = Image.new("RGB", (WIDTH, HEIGHT), GROUND)
    draw = ImageDraw.Draw(page)
    for y in range(20, HEIGHT, 24):
        for x in range(36, WIDTH, 24):
            draw.point((x, y), fill=DOTS)
            draw.point((x + 1, y), fill=DOTS)
    try:
        # Grain: noise centred on 128, squashed to about +-6 levels, added around the ground.
        noise = Image.effect_noise((WIDTH, HEIGHT), 24).point(lambda v: 128 + (v - 128) // 4)
        page = ImageChops.add(page, Image.merge("RGB", [noise] * 3), scale=1.0, offset=-128)
    except Exception:  # grain is decoration
        pass
    # Soft vignette towards the right so the text block reads as the lit part of the page.
    shade = Image.linear_gradient("L").rotate(90).resize((WIDTH, HEIGHT)).point(lambda v: v // 14)
    page = ImageChops.subtract(page, Image.merge("RGB", [shade] * 3))
    return page


def _fit_name(name: str) -> tuple[str, object]:
    """(text, font) for the big name line: shrink first, then truncate."""
    width = TEXT_RIGHT - TEXT_X
    script = pick_script(name, 72)
    for size in (72, 64, 56, 50):
        font = load_font(size, True, script)
        if font.getlength(name) <= width:
            return name, font
    font = load_font(50, True, script)
    return fit_text(name, font.getlength, width), font


def render_card(name: str | None, member_count: int | None, server_name: str | None,
                avatar: bytes | None, alt_name: str | None = None) -> bytes:
    """The welcome card as PNG bytes. `alt_name` (e.g. the username) is used if nothing in
    `name` is drawable."""
    page = _page()
    draw = ImageDraw.Draw(page)

    # The pen: a spine down the left edge and a short rule under the label.
    draw.rectangle((0, 0, 9, HEIGHT), fill=PEN)

    # Avatar, ringed in the pen colour.
    face = decode_avatar(avatar)
    shown = None
    for candidate in (name, alt_name):
        script = pick_script(candidate or "", 72)
        cleaned = clean_text(candidate, functools.partial(can_draw, load_font(72, True, script)))
        if cleaned:
            shown = cleaned
            break
    if face is None:
        face = initial_avatar(initial(shown))
    cx, cy = AVATAR_CENTER
    ring = AVATAR_SIZE + 16
    ring_img = Image.new("RGBA", (ring, ring), PEN + (255,))
    page.paste(ring_img, (cx - ring // 2, cy - ring // 2), _supersampled_circle(ring))
    gap = AVATAR_SIZE + 6
    page.paste(Image.new("RGB", (gap, gap), GROUND), (cx - gap // 2, cy - gap // 2), _supersampled_circle(gap))
    page.paste(face.convert("RGB"), (cx - AVATAR_SIZE // 2, cy - AVATAR_SIZE // 2),
               _supersampled_circle(AVATAR_SIZE))

    # Label: NEW ARRIVAL · SERVER
    label_font = load_font(22, True)
    server = clean_text(server_name, functools.partial(can_draw, label_font)).upper()
    label = "NEW ARRIVAL" + (f"  ·  {server}" if server else "")
    label = fit_text(label, label_font.getlength, TEXT_RIGHT - TEXT_X)
    draw.text((TEXT_X, 92), label, font=label_font, fill=INK_FAINT)
    draw.rectangle((TEXT_X, 128, TEXT_X + 44, 131), fill=PEN)

    # "Welcome," then the name, big.
    if shown:
        draw.text((TEXT_X, 148), "Welcome,", font=load_font(36), fill=INK_MUTED)
        text, font = _fit_name(shown)
        draw.text((TEXT_X - 3, NAME_BASELINE), text, font=font, fill=INK, anchor="ls")
    else:
        draw.text((TEXT_X - 3, NAME_BASELINE), "Welcome", font=load_font(72, True), fill=INK, anchor="ls")

    # Member number in the pen, with a hairline above it.
    draw.line((TEXT_X, 300, TEXT_RIGHT, 300), fill=HAIRLINE, width=2)
    number = member_label(member_count)
    if number:
        draw.text((TEXT_X, 316), number, font=load_font(30, True), fill=PEN)

    buf = io.BytesIO()
    page.save(buf, "PNG", optimize=True)
    return buf.getvalue()
