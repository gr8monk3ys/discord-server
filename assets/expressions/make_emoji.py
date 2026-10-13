"""Custom emoji pack for the server, in the Field Notebook style of the server icon and the
bot avatars (../make_icon.py, ../make_bot_avatars.py).

    python make_emoji.py        # writes emoji/<name>.png (128x128, transparent corners)

One construction for every emoji: a night-paper stamp (a rounded square with the sand hairline
on its edge) carrying one flat mark in one ink. The mark is either a drawn glyph or a word of
one to three characters set in Bahnschrift Bold, whose width axis is narrowed just enough for
the word to fill the stamp. The stamp is what makes the set work on both Discord themes: the
mark always sits on night paper, so pen green keeps the same contrast on a white chat as on a
dark one.

The ink says what kind of emoji it is:

    PEN    wins, hype and the server's own things
    INK    neutral words and looks (afk, brb, ez, sus)
    MUTED  losses, salt and bad connections

Emoji are shown at 22 to 48 px, so every mark is one idea whose main strokes are at least 8
units thick on the 128-unit grid; anything thinner (the bell's ding, the skull's teeth) is a
detail the mark still reads without. Everything is drawn at 8x and downsampled with LANCZOS;
there is no noise and nothing random, so a re-run writes the same pixels. The paper grain of
the icon is left out on purpose: it cannot be seen at these sizes.

To check a change by eye, look at it at 32 and 22 px on both a dark (49, 51, 56) and a white
background, as contact-sheet.png does for the set as committed.
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).with_name("emoji")

SIZE = 128  # the grid every coordinate below is in, and the size of the PNG
SS = 8  # draw at 8x and scale down, so curves and slanted strokes come out smooth
C = SIZE / 2

# Field Notebook, dark side (site/style.css)
NIGHT, PEN, HAIRLINE = (17, 19, 23), (66, 169, 121), (41, 44, 50)
INK, MUTED = (250, 250, 250), (171, 176, 186)

RADIUS = 28  # corner radius of the stamp
EDGE = 2.5  # hairline on its edge

# The one lettering face. A variable font: weight stays at Bold, width is fitted per word.
FONT = Path("C:/Windows/Fonts/bahnschrift.ttf")
BOLD = 700
WIDTHS = (100, 92, 84, 75)  # widest first; 75 is the font's Condensed


def font(size, width):
    f = ImageFont.truetype(str(FONT), max(1, round(size)))
    f.set_variation_by_axes([BOLD, width])
    return f


# ---------------------------------------------------------------- the stamp
class Stamp:
    """Night paper to draw on, in grid units. finish() cuts the stamp out of it."""

    def __init__(self):
        self.img = Image.new("RGB", (SIZE * SS, SIZE * SS), NIGHT)
        self.d = ImageDraw.Draw(self.img)

    def line(self, points, width, ink=PEN):
        """A stroke with round caps and joins."""
        pts = [(x * SS, y * SS) for x, y in points]
        self.d.line(pts, fill=ink, width=round(width * SS), joint="curve")
        r = width * SS / 2
        for x, y in pts:
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=ink)

    def arc(self, cx, cy, r, start, end, width, ink=PEN):
        """Part of a circle as a round-capped stroke; angles in degrees, clockwise from 3 o'clock."""
        n = max(8, int(abs(end - start) / 3))
        angles = [math.radians(start + (end - start) * i / n) for i in range(n + 1)]
        self.line([(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles], width, ink)

    def poly(self, points, ink=PEN):
        self.d.polygon([(x * SS, y * SS) for x, y in points], fill=ink)

    def disc(self, cx, cy, rx, ink=PEN, ry=None):
        ry = rx if ry is None else ry
        self.d.ellipse([(cx - rx) * SS, (cy - ry) * SS, (cx + rx) * SS, (cy + ry) * SS], fill=ink)

    def box(self, x0, y0, x1, y1, radius=0, ink=PEN):
        self.d.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], radius=radius * SS, fill=ink)

    def pie(self, x0, y0, x1, y1, start, end, ink=PEN):
        self.d.pieslice([x0 * SS, y0 * SS, x1 * SS, y1 * SS], start, end, fill=ink)

    def turn(self, degrees, cx=C, cy=C, shift=(0, 0)):
        """Rotate what has been drawn so far, clockwise, about (cx, cy), then move it by `shift`."""
        self.img = self.img.rotate(-degrees, resample=Image.BICUBIC, center=(cx * SS, cy * SS),
                                   translate=(shift[0] * SS, shift[1] * SS), fillcolor=NIGHT)
        self.d = ImageDraw.Draw(self.img)

    def word(self, text, ink=PEN, cap=64, max_width=98):
        """`text` centred on the stamp, `cap` units tall, in the widest width that fits."""
        ref = 400
        for width in WIDTHS:
            x0, y0, x1, y1 = font(ref, width).getbbox(text)
            k = cap * SS / (y1 - y0)
            if (x1 - x0) * k <= max_width * SS:
                break
        else:  # too long even condensed: give up height instead
            k = max_width * SS / (x1 - x0)
        f = font(ref * k, width)
        x0, y0, x1, y1 = f.getbbox(text)
        full = SIZE * SS
        self.d.text(((full - (x1 - x0)) / 2 - x0, (full - (y1 - y0)) / 2 - y0), text, font=f, fill=ink)
        return self

    def finish(self):
        full = SIZE * SS
        frame = [0, 0, full - 1, full - 1]
        self.d.rounded_rectangle(frame, radius=RADIUS * SS, outline=HAIRLINE, width=round(EDGE * SS))
        mask = Image.new("L", (full, full), 0)
        ImageDraw.Draw(mask).rounded_rectangle(frame, radius=RADIUS * SS, fill=255)
        # Colour and coverage are scaled separately, so no dark or light fringe can form on the edge.
        out = self.img.resize((SIZE, SIZE), Image.LANCZOS).convert("RGBA")
        out.putalpha(mask.resize((SIZE, SIZE), Image.LANCZOS))
        return out


# ---------------------------------------------------------------- shapes
def teardrop(cx, cy, half_w, half_h, lean=0.0, n=120):
    """A drop with its point up: round at the bottom, the point pulled `lean` units sideways."""
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        up = (1 + math.cos(t)) / 2  # 1 at the point, 0 at the bottom
        x = half_w * math.sin(t) * math.sin(t / 2) ** 1.5 / 0.69  # 0.69 = the curve's widest value
        pts.append((cx + x + lean * up * up, cy - half_h * math.cos(t)))
    return pts


def curve(p0, c, p1, n=24):
    """A quadratic Bezier from p0 to p1 with control point c."""
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1])
            for t in (i / n for i in range(n + 1))]


def blade(s, base_x, tip, half_w=8, base_y=100):
    """A blade of grass: straight up out of the ground, then bending over to its tip."""
    knee = tip[1] + (base_y - tip[1]) * 0.3
    left = curve((base_x - half_w, base_y), (base_x - half_w, knee), tip)
    right = curve(tip, (base_x + half_w, knee), (base_x + half_w, base_y))
    s.poly(left + right)


# ---------------------------------------------------------------- words
def gg():
    return Stamp().word("GG")


def ez():
    return Stamp().word("EZ", INK)


def lfg():
    return Stamp().word("LFG")


def take_w():
    return Stamp().word("W", cap=76)


def take_l():
    return Stamp().word("L", MUTED, cap=76)


def ff():
    return Stamp().word("FF", MUTED)


def afk():
    return Stamp().word("AFK", INK)


def brb():
    return Stamp().word("BRB", INK)


def clutch():
    return Stamp().word("1v5")


# ---------------------------------------------------------------- glyphs
def mvp():
    """A trophy cup."""
    s = Stamp()
    for cx, start, end in ((40, 90, 270), (88, -90, 90)):  # the handles
        s.arc(cx, 42, 13, start, end, 8)
    s.box(36, 22, 92, 33, radius=3)  # rim
    s.pie(40, -14, 88, 74, 0, 180)  # bowl
    s.box(58, 70, 70, 90)  # stem
    s.box(38, 88, 90, 102, radius=4)  # foot
    return s


def rip():
    """A skull."""
    s = Stamp()
    s.disc(C, 54, 39, MUTED, ry=35)  # cranium
    s.box(42, 68, 86, 108, radius=9, ink=MUTED)  # jaw
    for cx in (47, 81):
        s.disc(cx, 60, 11, NIGHT, ry=12)
    s.poly([(64, 73), (58, 84), (70, 84)], NIGHT)  # nose
    for x in (54.5, 64, 73.5):  # the gaps between the teeth
        s.line([(x, 97), (x, 110)], 4.5, NIGHT)
    return s


def fire():
    """A flame with a hollow heart."""
    s = Stamp()
    s.poly(teardrop(40, 76, 22, 30, lean=-8))  # the lick on the left
    s.poly(teardrop(66, 62, 36, 50, lean=12))
    s.poly(teardrop(66, 84, 15, 20, lean=4), NIGHT)
    return s


def salty():
    """A salt shaker tipped over, pouring."""
    s = Stamp()
    s.pie(44, 28, 84, 68, 180, 360, MUTED)  # the domed cap
    s.box(44, 46, 84, 53, ink=MUTED)
    for x, y in ((55, 41), (64, 36), (73, 41)):  # its holes
        s.disc(x, y, 3.2, NIGHT)
    s.box(42, 56, 86, 96, radius=10, ink=MUTED)  # body
    s.turn(135, C, 62, shift=(-12, -12))
    for x, y in ((88, 86), (79, 100), (99, 101), (91, 114)):  # grains
        s.disc(x, y, 5.5, MUTED)
    return s


def sus():
    """Side-eye: two half-lidded eyes looking away, one brow up."""
    s = Stamp()
    for cx in (39, 89):
        s.disc(cx, 70, 21, INK, ry=18)
        s.box(cx - 24, 50, cx + 24, 63, ink=NIGHT)  # the lid
        s.disc(cx + 11, 74, 9.5, NIGHT)
    s.line([(70, 44), (108, 33)], 9, INK)  # the raised brow
    s.line([(48, 106), (80, 103)], 8, INK)  # unconvinced mouth
    return s


def star(cx, cy, r, inner=0.48):
    """A five-pointed star, one point up."""
    return [(cx + (r if i % 2 == 0 else r * inner) * math.sin(math.pi * i / 5),
             cy - (r if i % 2 == 0 else r * inner) * math.cos(math.pi * i / 5)) for i in range(10)]


def hype():
    """Star-struck: stars for eyes and a wide open grin."""
    s = Stamp()
    for cx in (40, 88):
        s.poly(star(cx, 46, 20))
    s.pie(30, 40, 98, 108, 0, 180)
    return s


def sad():
    """A face with a frown and one tear."""
    s = Stamp()
    s.line([(22, 46), (50, 34)], 9, MUTED)
    s.line([(78, 34), (106, 46)], 9, MUTED)
    for cx in (40, 88):
        s.disc(cx, 62, 10, MUTED)
    s.arc(C, 119, 26, 228, 312, 10, MUTED)
    s.poly(teardrop(105, 87, 8.5, 14), MUTED)
    return s


def headshot():
    """A crosshair."""
    s = Stamp()
    s.arc(C, C, 30, 0, 360, 10)
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        s.line([(C + 19 * dx, C + 19 * dy), (C + 50 * dx, C + 50 * dy)], 10)
    s.disc(C, C, 7)
    return s


def lag():
    """Signal bars with only the first one lit."""
    s = Stamp()
    for i in range(4):
        x, top = 20 + i * 24, 82 - i * 18
        s.box(x, top, x + 17, 104, radius=4, ink=MUTED)
        if i:  # the empty ones are outlines
            s.box(x + 4.5, top + 4.5, x + 12.5, 99.5, radius=1, ink=NIGHT)
    return s


def touchgrass():
    """A tuft of grass."""
    s = Stamp()
    s.box(14, 98, 114, 109, radius=5.5)  # the ground
    for base_x, tip in ((30, (15, 60)), (46, (37, 36)), (64, (69, 20)), (82, (94, 40)), (98, (114, 62))):
        blade(s, base_x, tip)
    return s


def frontdesk():
    """The front-desk bell, as on the bot's avatar."""
    s = Stamp()
    s.pie(20, 42, 96, 132, 180, 360)  # dome
    s.box(10, 92, 106, 105, radius=6.5)  # the plate it sits on
    s.box(54, 32, 62, 46, radius=2)  # plunger
    s.disc(58, 29, 7.5)
    for a, b in (((91, 38), (98, 26)), ((101, 53), (113, 47)), ((104, 69), (116, 69))):
        s.line([a, b], 7.5)  # ding
    return s


# Discord names need 2+ chars, so the one-letter ones are take_w / take_l.
EMOJI = {
    "gg": gg, "ez": ez, "lfg": lfg, "take_w": take_w, "take_l": take_l,
    "clutch": clutch, "rip": rip, "ff": ff, "afk": afk, "brb": brb,
    "mvp": mvp, "fire": fire, "salty": salty, "sus": sus, "hype": hype,
    "sad": sad, "headshot": headshot, "lag": lag, "touchgrass": touchgrass,
    "frontdesk": frontdesk,
}


def main():
    if not FONT.exists():
        sys.exit(f"{FONT} is missing: the lettering needs Bahnschrift, which ships with Windows 10 and 11.")
    OUT.mkdir(exist_ok=True)
    for name, fn in EMOJI.items():
        path = OUT / f"{name}.png"
        fn().finish().save(path, optimize=True)
        print(f"{path.name:<16} {path.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
