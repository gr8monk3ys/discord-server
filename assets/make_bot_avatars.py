"""Avatars for Front Desk and its webhooks, in the server icon's Field Notebook style: one
forest-ink glyph on night paper inside the sand hairline ring (see make_icon.py).

  front-desk.png  a desk bell: the bot itself
  watchdog.png    a pulse line: the watchdog webhook that reports restarts in the mod log
  appeals.png     an envelope: the webhook the ban-appeal form posts through
"""
import random
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
SS = 3  # draw at 3x and scale down, so curves and slanted strokes come out smooth
NIGHT, PEN, HAIRLINE = (17, 19, 23), (66, 169, 121), (41, 44, 50)
OUT = Path(__file__).with_name("bots")


def s(*values):
    return [v * SS for v in values]


def line(d, points, width):
    """A stroke with round caps and joins."""
    pts = [(x * SS, y * SS) for x, y in points]
    d.line(pts, fill=PEN, width=width * SS, joint="curve")
    r = width * SS / 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=PEN)


def bell(d):
    d.pieslice(s(292, 372, 732, 892), 180, 360, fill=PEN)  # the dome
    d.rounded_rectangle(s(232, 648, 792, 708), radius=30 * SS, fill=PEN)  # the plate it sits on
    d.rectangle(s(292, 620, 732, 632), fill=NIGHT)  # a paper-thin gap between dome and plate
    d.rounded_rectangle(s(490, 318, 534, 384), radius=10 * SS, fill=PEN)  # the plunger
    d.ellipse(s(474, 286, 550, 362), fill=PEN)
    for a, b in (((700, 330), (742, 270)), ((752, 398), (822, 362)), ((772, 478), (848, 478))):
        line(d, [a, b], 18)  # ding


def pulse(d):
    line(d, [(236, 520), (392, 520), (446, 372), (528, 668), (592, 440), (636, 520), (788, 520)], 34)


def envelope(d):
    d.rounded_rectangle(s(252, 352, 772, 692), radius=34 * SS, outline=PEN, width=30 * SS)
    line(d, [(282, 392), (512, 560), (742, 392)], 30)


def make(name, glyph):
    img = Image.new("RGB", (SIZE * SS, SIZE * SS), NIGHT)
    d = ImageDraw.Draw(img)
    inset = 96
    d.ellipse(s(inset, inset, SIZE - inset, SIZE - inset), outline=HAIRLINE, width=6 * SS)
    glyph(d)
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    # the paper grain: the same 3.5% noise as the server icon and the site
    rng = random.Random(7)
    px = img.load()
    for y in range(SIZE):
        for x in range(SIZE):
            n = int((rng.random() - 0.5) * 255 * 0.07)
            r, g, b = px[x, y]
            px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))
    OUT.mkdir(exist_ok=True)
    out = OUT / f"{name}.png"
    img.save(out)
    print(out)


if __name__ == "__main__":
    make("front-desk", bell)
    make("watchdog", pulse)
    make("appeals", envelope)
