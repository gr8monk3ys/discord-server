"""Listing images in the Field Notebook style: a 960x540 banner (Disboard, top.gg,
discord.me) and a 1920x1080 hero (Discovery cover, social posts).

Night ground with a faint dot grid and grain, one forest-ink pen (#42A979) for the spine,
the rule and the invite pill, IBM Plex Sans from bot/assets/fonts. Drawn from scratch
except the server icon (assets/server-icon.png, from make_icon.py), which sits on the right.

    python assets/listing/make_banner.py   # writes banner.png and hero.png next to this file
"""

import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FONT = ROOT / "bot" / "assets" / "fonts" / "IBMPlexSans-Variable.ttf"
ICON = ROOT / "assets" / "server-icon.png"

NAME = "Lorenzo's Server"
TAGLINE = ("Chill 18+ gamers.", "No tryhards, no toxicity.")
LABEL = "DISCORD SERVER · 18+ · PACIFIC TIME"
GAMES = "Valorant · Call of Duty · Fortnite · Minecraft · Apex · CS2 · League · GTA Online · Roblox"
INVITE = "discord.gg/C25hy9Ca9W"

# Field Notebook, night side (site/style.css, theme/field-notebook.theme.css)
PAGE = (17, 19, 23)
DOTS = (37, 40, 46)
HAIRLINE = (41, 44, 50)
INK = (250, 250, 250)
INK_MUTED = (171, 176, 186)
INK_FAINT = (118, 124, 135)
PEN = (0x42, 0xA9, 0x79)
PEN_TEXT = (11, 26, 18)

SIZES = {"banner.png": (960, 540), "hero.png": (1920, 1080)}


def font(size: float, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), max(1, round(size)))
    f.set_variation_by_name(weight)
    return f


def spaced(draw, xy, text, f, fill, tracking):
    """Text with extra letter spacing (the site's mono wall labels, in Plex Sans)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch) + tracking
    return x


def page(w: int, h: int, u: float) -> Image.Image:
    img = Image.new("RGB", (w, h), PAGE)
    d = ImageDraw.Draw(img)
    step = max(12, round(24 * u))
    dot = max(1, round(1.5 * u))
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            d.rectangle((x, y, x + dot - 1, y + dot - 1), fill=DOTS)
    # grain: seeded so the output is the same every run
    rng = random.Random(7)
    noise = Image.frombytes("L", (w, h), bytes(128 + int((rng.random() - 0.5) * 14) for _ in range(w * h)))
    img = ImageChops.add(img, Image.merge("RGB", [noise] * 3), scale=1.0, offset=-128)
    # a soft pool of pen-coloured light behind the icon
    glow = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(glow)
    cx, cy, r = w * 0.79, h * 0.47, h * 0.62
    for i in range(40, 0, -1):
        rr = r * i / 40
        gd.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=int(26 * (1 - i / 40)))
    tint = Image.new("RGB", (w, h), PEN)
    img = Image.composite(tint, img, glow)
    return img


def circle_mask(size: int, scale: int = 4) -> Image.Image:
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def paste_icon(img: Image.Image, cx: int, cy: int, size: int, ring: int) -> None:
    outer = size + 2 * ring
    img.paste(Image.new("RGB", (outer, outer), PEN), (cx - outer // 2, cy - outer // 2), circle_mask(outer))
    gap = size + max(2, ring // 2)
    img.paste(Image.new("RGB", (gap, gap), PAGE), (cx - gap // 2, cy - gap // 2), circle_mask(gap))
    icon = Image.open(ICON).convert("RGB").resize((size, size), Image.LANCZOS)
    img.paste(icon, (cx - size // 2, cy - size // 2), circle_mask(size))


def render(w: int, h: int) -> Image.Image:
    u = h / 540  # layout unit: everything is drawn for 540 px tall and scaled
    img = page(w, h, u)
    d = ImageDraw.Draw(img)
    s = lambda v: round(v * u)  # noqa: E731

    d.rectangle((0, 0, s(10) - 1, h), fill=PEN)  # the pen spine
    left = s(64)

    # wall label + short rule
    label_font = font(13 * u, "SemiBold")
    spaced(d, (left, s(64)), LABEL, label_font, INK_FAINT, 2.2 * u)
    d.rectangle((left, s(92), left + s(44), s(92) + max(2, s(3)) - 1), fill=PEN)

    # the name, shrunk to fit the left column
    column = w * 0.60 - left
    size = 76
    while font(size * u, "Bold").getlength(NAME) > column and size > 40:
        size -= 2
    name_font = font(size * u, "Bold")
    d.text((left, s(176)), NAME, font=name_font, fill=INK, anchor="ls")

    # tagline: the second half in the pen, like the site's <em>
    tag_font = font(30 * u, "Medium")
    d.text((left, s(238)), TAGLINE[0], font=tag_font, fill=INK_MUTED, anchor="ls")
    d.text((left, s(278)), TAGLINE[1], font=tag_font, fill=PEN, anchor="ls")

    # hairline, then the games in two short lines
    d.rectangle((left, s(318), left + column, s(318) + max(1, s(1)) - 1), fill=HAIRLINE)
    games_font = font(16 * u, "Regular")
    words, lines, line = GAMES.split(" · "), [], ""
    for g in words:
        trial = f"{line} · {g}" if line else g
        if games_font.getlength(trial) > column and line:
            lines.append(line)
            line = g
        else:
            line = trial
    lines.append(line)
    for i, text in enumerate(lines[:2]):
        d.text((left, s(352 + 26 * i)), text, font=games_font, fill=INK_MUTED, anchor="ls")

    # invite pill
    pill_font = font(18 * u, "SemiBold")
    pad_x, pill_h, top = s(20), s(44), s(432)
    pill_w = round(pill_font.getlength(INVITE)) + 2 * pad_x
    d.rounded_rectangle((left, top, left + pill_w, top + pill_h), radius=pill_h // 2, fill=PEN)
    d.text((left + pad_x, top + pill_h / 2), INVITE, font=pill_font, fill=PEN_TEXT, anchor="lm")
    d.text((left + pill_w + s(18), top + pill_h / 2), "Squad up tonight.", font=font(16 * u), fill=INK_FAINT,
           anchor="lm")

    # the icon on the right
    paste_icon(img, round(w * 0.79), round(h * 0.47), s(250), max(3, s(5)))
    return img


def main() -> None:
    for name, (w, h) in SIZES.items():
        out = HERE / name
        render(w, h).save(out, optimize=True)
        print(out)


if __name__ == "__main__":
    main()
