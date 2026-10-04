"""Custom emoji pack for the server, drawn from scratch with Pillow + numpy.

    python make_emoji.py        # writes emoji/<name>.png (128x128, transparent)

Sticker style: flat fills from the Field Notebook palette (plus a few accent
colours), a thick night-ink outline around every shape and a thin sand rim
around the whole silhouette so it reads on both dark and light Discord themes.
Lettering is a hand-made 5x7 block font defined below, so no font files are
used or shipped. Everything is drawn at 4x and downsampled for anti-aliasing.
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

OUT = Path(__file__).with_name("emoji")
SIZE = 128
S = 4 * SIZE  # working canvas
C = S // 2

# Field Notebook palette (server/layout.py) + a few accents
NIGHT = (17, 19, 23)
FOREST = (66, 169, 121)
MOSS = (57, 127, 94)
SAND = (226, 219, 213)
MUTED = (171, 176, 186)
SLATE = (96, 105, 118)
RED = (229, 83, 75)
AMBER = (242, 177, 52)
EMBER = (240, 122, 58)
SKY = (91, 164, 230)
PURPLE = (156, 124, 232)
BROWN = (140, 98, 64)
WHITE = (246, 244, 240)
SKIN = (247, 200, 96)  # face yellow, warmer than AMBER

INK_W = 14  # outline around each shape (3.5 px at 128)
RIM_W = 9  # sand rim around the whole emoji

# ---------------------------------------------------------------- block font
GLYPHS = {
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "I": ["###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "V": ["#...#", "#...#", "#...#", "#...#", ".#.#.", ".#.#.", "..#.."],
    "1": [".#.", "##.", ".#.", ".#.", ".#.", ".#.", "###"],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "z": ["####", "..#.", ".#..", "####"],  # small z for afk snores
}


def text_width(text, cell):
    cols = sum(len(GLYPHS[ch][0]) for ch in text) + (len(text) - 1)
    return cols * cell


def text_mask(text, cx, cy, cell):
    """Block letters centred on (cx, cy)."""
    m = new_mask()
    d = ImageDraw.Draw(m)
    x = cx - text_width(text, cell) / 2
    rows = len(GLYPHS[text[0]])
    y0 = cy - rows * cell / 2
    for ch in text:
        g = GLYPHS[ch]
        for r, row in enumerate(g):
            for c, px in enumerate(row):
                if px == "#":
                    d.rectangle([x + c * cell, y0 + r * cell, x + (c + 1) * cell - 1, y0 + (r + 1) * cell - 1], fill=255)
        x += (len(g[0]) + 1) * cell
    return m


# ---------------------------------------------------------------- helpers
def new_mask():
    return Image.new("L", (S, S), 0)


def _disk(r):
    return [(dx, dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1) if dx * dx + dy * dy <= r * r]


def dilate(mask, r):
    if r <= 0:
        return mask
    a = np.asarray(mask) > 127
    out = np.zeros_like(a)
    p = np.pad(a, r)
    for dx, dy in _disk(r):
        out |= p[r + dy : r + dy + S, r + dx : r + dx + S]
    return Image.fromarray((out * 255).astype(np.uint8))


class Art:
    def __init__(self):
        self.img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    def stamp(self, mask, fill, ink=INK_W):
        """Paint mask in fill colour with an ink outline of width `ink`."""
        if ink:
            self.img.paste(NIGHT + (255,), (0, 0), dilate(mask, ink))
        self.img.paste(fill + (255,), (0, 0), mask)

    def shape(self, fn, fill, ink=INK_W):
        m = new_mask()
        fn(ImageDraw.Draw(m))
        self.stamp(m, fill, ink)
        return m

    def text(self, text, cx, cy, cell, fill, ink=INK_W - 4):
        self.stamp(text_mask(text, cx, cy, cell), fill, ink)

    def finish(self):
        alpha = self.img.getchannel("A")
        rim = dilate(alpha, RIM_W)
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        out.paste(SAND + (255,), (0, 0), rim)
        out.alpha_composite(self.img)
        return out.resize((SIZE, SIZE), Image.LANCZOS)


M = 40  # margin kept clear for outline + rim


def badge(a, fill, top=110, bottom=402, radius=70):
    a.shape(lambda d: d.rounded_rectangle([M, top, S - M, bottom], radius=radius, fill=255), fill)


def text_badge(text, fill, text_fill=SAND, cell=None):
    a = Art()
    badge(a, fill)
    cell = cell or min(34, int((S - 2 * M - 70) / (text_width(text, 1))))
    a.text(text, C, C, cell, text_fill, ink=8)
    return a


def eye(a, cx, cy, rx, ry, px, py, pr):
    """White eye with a pupil offset by (px, py)."""
    a.shape(lambda d: d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255), WHITE, ink=8)
    a.shape(lambda d: d.ellipse([cx + px - pr, cy + py - pr, cx + px + pr, cy + py + pr], fill=255), NIGHT, ink=0)


def face(a, fill=SKIN):
    a.shape(lambda d: d.ellipse([M + 6, M + 6, S - M - 6, S - M - 6], fill=255), fill)


def thick_line(a, pts, width, fill=NIGHT, ink=0):
    def fn(d):
        d.line(pts, fill=255, width=width, joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=255)

    a.shape(fn, fill, ink=ink)


def arc_pts(cx, cy, r, a0, a1, n=24):
    return [(cx + r * math.cos(math.radians(t)), cy + r * math.sin(math.radians(t)))
            for t in np.linspace(a0, a1, n)]


# ---------------------------------------------------------------- the pack
def gg():
    return text_badge("GG", FOREST)


def ez():
    return text_badge("EZ", SKY, WHITE)


def lfg():
    return text_badge("LFG", EMBER)


def w():
    a = Art()
    a.text("W", C, C, 62, FOREST, ink=INK_W)
    return a


def l():
    a = Art()
    a.text("L", C + 10, C, 62, RED, ink=INK_W)
    return a


def clutch():
    a = Art()
    shield = [(C, M + 4), (S - M - 20, 110), (S - M - 40, 300), (C, S - M - 4), (M + 40, 300), (M + 20, 110)]
    a.shape(lambda d: d.polygon(shield, fill=255), AMBER)
    a.text("1V5", C, 236, 18, NIGHT, ink=0)
    return a


def rip():
    a = Art()
    a.shape(lambda d: d.rounded_rectangle([110, 70, 402, 520], radius=140, fill=255), MUTED)
    a.text("RIP", C, 215, 17, NIGHT, ink=0)
    a.shape(lambda d: d.rounded_rectangle([M, 392, S - M, 468], radius=34, fill=255), FOREST)
    return a


def ff():
    a = Art()
    a.shape(lambda d: d.rounded_rectangle([100, 50, 132, 470], radius=14, fill=255), BROWN)
    top = [(132 + t, 80 + 22 * math.sin(t / 60)) for t in range(0, 341, 10)]
    bot = [(132 + t, 300 + 22 * math.sin(t / 60)) for t in range(340, -1, -10)]
    a.shape(lambda d: d.polygon(top + bot, fill=255), WHITE)
    a.text("FF", 300, 192, 20, NIGHT, ink=0)
    return a


def afk():
    a = text_badge("AFK", PURPLE, cell=22)
    a.text("z", 404, 96, 14, SAND, ink=6)
    a.text("z", 452, 52, 9, SAND, ink=5)
    return a


def brb():
    return text_badge("BRB", SLATE, SAND, cell=22)


def mvp():
    a = Art()
    crown = [(96, 250), (80, 90), (176, 170), (C, 60), (336, 170), (432, 90), (416, 250)]
    a.shape(lambda d: d.polygon(crown, fill=255), AMBER)
    for x, y in ((80, 90), (C, 60), (432, 90)):
        a.shape(lambda d, x=x, y=y: d.ellipse([x - 22, y - 22, x + 22, y + 22], fill=255), RED, ink=8)
    a.shape(lambda d: d.rounded_rectangle([M, 270, S - M, 450], radius=40, fill=255), FOREST)
    a.text("MVP", C, 360, 18, SAND, ink=8)
    return a


# Outline of a flame with two side licks, as (x, y) on the 512 canvas.
FLAME = [(106, 330), (110, 240), (124, 150), (178, 214), (214, 130), (276, 40),
         (322, 150), (352, 212), (398, 118), (420, 220), (406, 330)]


def smooth(pts, rounds=4):
    """Chaikin corner cutting on a closed polygon."""
    for _ in range(rounds):
        out = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
            out += [(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1), (0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)]
        pts = out
    return pts


def fire():
    a = Art()
    base = FLAME + arc_pts(C, 330, 150, 0, 180)[1:-1]
    outline = smooth(base)

    def scaled(k):
        ox, oy = C, 470  # shrink toward the base of the flame
        return [(ox + (x - ox) * k, oy + (y - oy) * k) for x, y in outline]

    a.shape(lambda d: d.polygon(outline, fill=255), RED)
    a.shape(lambda d: d.polygon(scaled(0.66), fill=255), EMBER, ink=0)
    a.shape(lambda d: d.polygon(scaled(0.36), fill=255), AMBER, ink=0)
    return a


def salty():
    a = Art()
    a.shape(lambda d: d.rounded_rectangle([140, 150, 372, 470], radius=50, fill=255), WHITE)
    a.shape(lambda d: d.rounded_rectangle([150, 70, 362, 170], radius=40, fill=255), MUTED)
    for x in (206, C, 306):
        a.shape(lambda d, x=x: d.ellipse([x - 11, 98, x + 11, 120], fill=255), NIGHT, ink=0)
    # grumpy face on the shaker
    thick_line(a, [(180, 248), (236, 274)], 16)
    thick_line(a, [(332, 248), (276, 274)], 16)
    a.shape(lambda d: d.ellipse([200, 290, 232, 322], fill=255), NIGHT, ink=0)
    a.shape(lambda d: d.ellipse([280, 290, 312, 322], fill=255), NIGHT, ink=0)
    thick_line(a, arc_pts(C, 420, 46, 200, 340), 14)
    for x, y in ((80, 120), (430, 70), (420, 200), (66, 260)):
        a.shape(lambda d, x=x, y=y: d.rectangle([x - 12, y - 12, x + 12, y + 12], fill=255), WHITE, ink=7)
    return a


def sus():
    a = Art()
    face(a)
    eye(a, 186, 220, 58, 44, 30, 6, 22)
    eye(a, 330, 220, 58, 44, 30, 6, 22)
    thick_line(a, [(130, 150), (230, 160)], 18)  # flat brow
    thick_line(a, [(282, 140), (330, 118), (382, 132)], 18)  # raised brow
    thick_line(a, [(200, 360), (330, 340)], 18)  # flat, doubtful mouth
    return a


def hype():
    a = Art()
    face(a)
    eye(a, 182, 196, 46, 56, 0, -8, 18)
    eye(a, 330, 196, 46, 56, 0, -8, 18)
    thick_line(a, arc_pts(182, 150, 60, 215, 325), 16)
    thick_line(a, arc_pts(330, 150, 60, 215, 325), 16)
    a.shape(lambda d: d.ellipse([186, 286, 326, 446], fill=255), NIGHT, ink=0)
    a.shape(lambda d: d.chord([206, 360, 306, 440], 0, 180, fill=255), RED, ink=0)
    return a


def sad():
    a = Art()
    face(a, (160, 196, 230))
    for cx in (180, 332):
        a.shape(lambda d, cx=cx: d.ellipse([cx - 46, 196, cx + 46, 266], fill=255), WHITE, ink=8)
        a.shape(lambda d, cx=cx: d.ellipse([cx - 20, 226, cx + 20, 266], fill=255), NIGHT, ink=0)
        a.shape(lambda d, cx=cx: d.rectangle([cx - 60, 180, cx + 60, 226], fill=255), (160, 196, 230), ink=0)  # heavy lids
        thick_line(a, [(cx - 48, 226), (cx + 48, 226)], 12)
    thick_line(a, [(130, 170), (220, 140)], 16)
    thick_line(a, [(382, 170), (292, 140)], 16)
    thick_line(a, arc_pts(C, 430, 70, 220, 320), 16)  # frown
    a.shape(lambda d: d.polygon([(332, 270), (304, 340), (360, 340)], fill=255) or d.ellipse([304, 316, 360, 372], fill=255), SKY, ink=7)
    return a


def headshot():
    a = Art()

    def ring(d):
        d.ellipse([M + 30, M + 30, S - M - 30, S - M - 30], fill=255)
        d.ellipse([M + 74, M + 74, S - M - 74, S - M - 74], fill=0)
        for box in ([C - 18, M, C + 18, 190], [C - 18, 322, C + 18, S - M],
                    [M, C - 18, 190, C + 18], [322, C - 18, S - M, C + 18]):
            d.rectangle(box, fill=255)

    a.shape(ring, RED)
    a.shape(lambda d: d.ellipse([C - 26, C - 26, C + 26, C + 26], fill=255), RED, ink=10)
    return a


def lag():
    a = Art()
    cx, cy = C - 20, 410
    for i, r in enumerate((260, 186, 112)):
        fill = MUTED if i < 2 else RED
        a.shape(lambda d, r=r: (d.pieslice([cx - r, cy - r, cx + r, cy + r], 225, 315, fill=255),
                                d.pieslice([cx - r + 46, cy - r + 46, cx + r - 46, cy + r - 46], 0, 360, fill=0)), fill)
    a.shape(lambda d: d.ellipse([cx - 34, cy - 34, cx + 34, cy + 34], fill=255), RED)
    a.shape(lambda d: d.ellipse([360, 250, 466, 356], fill=255), AMBER)
    a.shape(lambda d: (d.rounded_rectangle([404, 268, 422, 318], radius=8, fill=255),
                       d.ellipse([403, 324, 423, 344], fill=255)), NIGHT, ink=0)
    return a


def touchgrass():
    a = Art()
    a.shape(lambda d: d.chord([M, 330, S - M, 560], 180, 360, fill=255), BROWN)
    blades = []
    for i, x in enumerate(range(80, 440, 34)):
        h = 200 + 60 * math.sin(i * 1.7) + (40 if 3 < i < 8 else 0)
        lean = 30 * math.sin(i * 2.3)
        blades.append([(x - 20, 380), (x + lean, 380 - h), (x + 20, 380)])
    a.shape(lambda d: [d.polygon(b, fill=255) for b in blades], FOREST)
    a.shape(lambda d: [d.polygon(b, fill=255) for b in blades[1::3]], MOSS, ink=0)
    fx, fy = 360, 150
    for k in range(5):
        t = k * 2 * math.pi / 5
        px, py = fx + 34 * math.cos(t), fy + 34 * math.sin(t)
        a.shape(lambda d, px=px, py=py: d.ellipse([px - 26, py - 26, px + 26, py + 26], fill=255), WHITE, ink=7)
    a.shape(lambda d: d.ellipse([fx - 22, fy - 22, fx + 22, fy + 22], fill=255), AMBER, ink=7)
    return a


def frontdesk():
    """Server mascot: the front-desk service bell, with a face."""
    a = Art()
    a.shape(lambda d: d.rounded_rectangle([C - 26, 92, C + 26, 150], radius=14, fill=255), SLATE)
    a.shape(lambda d: d.ellipse([C - 50, 64, C + 50, 110], fill=255), MUTED)
    a.shape(lambda d: d.chord([86, 140, 426, 480], 180, 360, fill=255), FOREST)
    a.shape(lambda d: d.chord([126, 180, 300, 360], 200, 250, fill=255), (120, 200, 160), ink=0)  # shine
    a.shape(lambda d: d.rounded_rectangle([M, 312, S - M, 380], radius=24, fill=255), SAND)
    a.shape(lambda d: d.rounded_rectangle([M + 30, 380, S - M - 30, 430], radius=16, fill=255), SLATE)
    for x in (206, 306):
        a.shape(lambda d, x=x: d.ellipse([x - 16, 226, x + 16, 262], fill=255), NIGHT, ink=0)
    thick_line(a, arc_pts(C, 256, 34, 30, 150), 12)
    for ang in (205, 335):
        x0, y0 = C + 210 * math.cos(math.radians(ang)), 150 + 210 * math.sin(math.radians(ang))
        x1, y1 = C + 250 * math.cos(math.radians(ang)), 150 + 250 * math.sin(math.radians(ang))
        thick_line(a, [(x0, y0), (x1, y1)], 18, AMBER, ink=7)
    return a


# Discord names need 2+ chars, so the one-letter ones are take_w / take_l.
EMOJI = {
    "gg": gg, "ez": ez, "lfg": lfg, "take_w": w, "take_l": l,
    "clutch": clutch, "rip": rip, "ff": ff, "afk": afk, "brb": brb,
    "mvp": mvp, "fire": fire, "salty": salty, "sus": sus, "hype": hype,
    "sad": sad, "headshot": headshot, "lag": lag, "touchgrass": touchgrass,
    "frontdesk": frontdesk,
}


def main():
    OUT.mkdir(exist_ok=True)
    for name, fn in EMOJI.items():
        path = OUT / f"{name}.png"
        fn().finish().save(path, optimize=True)
        print(f"{path.name:<16} {path.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
