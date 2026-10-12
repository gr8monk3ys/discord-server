"""Wrapped cards: what a card says (`member_spec`, `server_spec`: plain data, easy to test)
and drawing it as one tall PNG in the Field Notebook look (`render`). Pure: no Discord, no
network. `render` is CPU-bound, so callers run it in a thread.

The card is 1080 px wide and as tall as its content: header (avatar, name, the persona
title), up to six stat tiles, then sections (top games with bars, teammates, highlights).
Sections with nothing to say are left out, and a member with almost no data gets a short
card with a kind note instead of a grid of zeros.

Names of members, games, houses and the server are other people's text. They pass
logic/textfilter.py here, and logic/cards.py's cleaning when drawn (compatibility forms
folded, control and bidi characters dropped, anything the font can't draw dropped, long
text cut with an ellipsis); if nothing is left a neutral fallback is drawn. The output is
deterministic (no grain), so the same spec always gives the same bytes."""

from __future__ import annotations

import functools
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from PIL import Image, ImageDraw

from logic import cards as C
from logic import textfilter
from logic import wrapped as W

WIDTH = 1080
MAX_HEIGHT = 2600  # a full card is about 1,700; this is only a ceiling
LEFT, RIGHT = 72, WIDTH - 72
TOP = 64
AVATAR = 148
TEXT_X = LEFT + AVATAR + 36
TILE_HEIGHT, TILE_GAP = 148, 16
TILE_FILL = (30, 33, 39)
TRACK = (34, 37, 43)
ROW_HEIGHT = {"bars": 70, "people": 62, "facts": 54}
MAX_TILES = 6
TILE_ROWS = {0: (), 1: (1,), 2: (2,), 3: (3,), 4: (2, 2), 5: (3, 2), 6: (3, 3)}
MEMBER_FALLBACK = "Member"
GAME_FALLBACK = "A game"
FOOTER = "Front Desk  ·  /wrapped"
FOOTER_PRIVACY = "only what /privacy lets us count"


# ---------------------------------------------------------------- what a card says
@dataclass(frozen=True)
class Tile:
    value: str
    label: str


@dataclass(frozen=True)
class Row:
    left: str
    right: str = ""
    share: float | None = None  # bars: 0..1 of the longest bar
    fallback: str = ""  # drawn if nothing in `left` is drawable
    names: tuple[str, ...] = ()  # facts: member names drawn before `right`, each cut to fit


@dataclass(frozen=True)
class Section:
    title: str
    kind: str  # "bars", "people" or "facts"
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class CardSpec:
    label: tuple[str, ...]  # joined with a dot: ("Wrapped 2026", server name)
    name: str | None
    alt_name: str | None  # used when nothing in `name` is drawable
    fallback_name: str
    title: str
    line: str
    tiles: tuple[Tile, ...] = ()
    sections: tuple[Section, ...] = ()
    notes: tuple[str, ...] = ()  # the short card's sentences
    footer: str = FOOTER
    footer_right: str = FOOTER_PRIVACY


def compact(n: int) -> str:
    """A number that fits a tile: 12,345 / 123k / 1.2M."""
    if n < 100_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def screened(text: str | None) -> str | None:
    """`text` if it may be shown on a public card, else None (the fallback is drawn)."""
    if not text:
        return None
    ok, _ = textfilter.check(text)
    return text if ok else None


def _tiles(candidates) -> tuple[Tile, ...]:
    return tuple(Tile(value, label) for show, value, label in candidates if show)[:MAX_TILES]


def _game_rows(top_games) -> tuple[Row, ...]:
    longest = top_games[0][1] if top_games else 0
    return tuple(Row(screened(game) or "", f"{W.fmt_hours(secs)} h", secs / longest if longest else 0.0,
                     GAME_FALLBACK) for game, secs in top_games)


def _day_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{W.MONTHS[d.month - 1][:3]} {d.day}"


def _joined(parts) -> str:
    return ", ".join(p for p in parts if p)


def _teammate_rows(summary: W.Summary, names: Mapping[int, str]) -> tuple[Row, ...]:
    rows = []
    for mate in summary.teammates:
        if mate.user_id not in names:
            continue  # left since the summary was made: never show a bare id
        detail = " · ".join(p for p in (
            f"{W.fmt_hours(mate.voice_seconds)} h in voice" if mate.voice_seconds >= 360 else "",
            W.plural(mate.squads, "squad") if mate.squads else "") if p)
        rows.append(Row(screened(names[mate.user_id]) or "", detail, fallback=MEMBER_FALLBACK))
    return tuple(rows) if len(rows) >= W.MIN_TEAMMATES else ()


def _highlights(s: W.Summary) -> tuple[Row, ...]:
    rows = []
    if s.busiest_month:
        detail = _joined((W.plural(s.busiest_month_messages, "message") if s.busiest_month_messages else "",
                          f"{W.fmt_hours(s.busiest_month_voice)} h in voice"
                          if s.busiest_month_voice >= 360 else ""))
        rows.append(Row("Busiest month", f"{W.MONTHS[s.busiest_month - 1]}" + (f" · {detail}" if detail else "")))
    if s.busiest_day:
        rows.append(Row("Busiest day", f"{_day_label(s.busiest_day[0])} · {W.plural(s.busiest_day[1], 'message')}"))
    if s.daily_streak >= 2 and s.daily_streak >= s.chat_streak:
        rows.append(Row("Longest /daily streak", W.plural(s.daily_streak, "day")))
    elif s.chat_streak >= 3:
        rows.append(Row("Longest chat streak", f"{W.plural(s.chat_streak, 'day')} in a row"))
    if s.biggest_payout:
        coins, reason = s.biggest_payout
        rows.append(Row("Biggest payout", f"{coins:,} coins · {W.label_for(reason)}"))
    if s.rarest_badge:
        key, holders = s.rarest_badge
        held = "only you so far" if holders <= 1 else f"held by {holders:,}"
        rows.append(Row("Rarest badge", f"{W.badge_name(key)} · {held}"))
    if s.squads:
        rows.append(Row("Squads", _joined((f"{s.squads_hosted:,} hosted" if s.squads_hosted else "",
                                           f"{s.squads_joined:,} joined" if s.squads_joined else "",
                                           W.plural(s.matches, "match", "matches") if s.matches else ""))))
    if s.gamenights:
        rows.append(Row("Game nights hosted", f"{s.gamenights:,}"))
    if s.tournaments_entered or s.tournaments_won:
        rows.append(Row("Tournaments", _joined((f"{s.tournaments_entered:,} entered" if s.tournaments_entered else "",
                                                f"{s.tournaments_won:,} won" if s.tournaments_won else ""))))
    if s.clips or s.clip_week_wins:
        rows.append(Row("Clips", _joined((f"{s.clips:,} posted" if s.clips else "",
                                          f"{s.clip_week_wins:,} Clip of the Week" if s.clip_week_wins else ""))))
    if s.hall_of_fame:
        rows.append(Row("Hall of fame", W.plural(s.hall_of_fame, "message")))
    if s.quests or s.event_drops:
        rows.append(Row("Quests and challenges", _joined((
            f"{s.quests:,} claimed" if s.quests else "",
            W.plural(s.event_drops, "event drop") if s.event_drops else ""))))
    if s.words_solved:
        rows.append(Row("Daily Word", f"{s.words_solved:,} solved"))
    if s.levels_gained and s.level:
        rows.append(Row("Level", f"now level {s.level:,}"))
    if s.joined_month:
        rows.append(Row("Joined", f"in {W.MONTHS[s.joined_month - 1]}"))
    if screened(s.house):
        rows.append(Row("House", s.house))
    return tuple(rows)


def _short_facts(s: W.Summary) -> str:
    return " · ".join(p for p in (
        W.plural(s.messages, "message") if s.messages else "",
        f"{W.fmt_hours(s.voice_seconds)} h in voice" if s.voice_seconds >= 360 else "",
        W.plural(len(s.badges), "badge") if s.badges else "",
        f"{s.coins_earned:,} coins earned" if s.coins_earned else "",
        f"joined in {W.MONTHS[s.joined_month - 1]}" if s.joined_month else "") if p)


def member_spec(summary: W.Summary, name: str | None, alt_name: str | None, server_name: str | None,
                names: Mapping[int, str] | None = None) -> CardSpec:
    """What a member's card says. `names`: display names of the teammates who are still here."""
    s = summary
    who = W.persona(s)
    label = (f"Wrapped {s.year}", screened(server_name) or "")
    base = dict(label=label, name=screened(name), alt_name=screened(alt_name), fallback_name=MEMBER_FALLBACK,
                title=who.title, line=who.line)
    if s.sparse:
        facts = _short_facts(s)
        opener = ("You're new to the page, and already part of it. Glad you're here." if s.joined_month
                  else "A quiet year on the page, and that's fine. Glad you're here.")
        notes = (opener,
                 *((facts,) if facts else ()),
                 f"Here's to more in {s.year + 1}.")
        return CardSpec(**base, notes=notes)

    if s.levels_gained:
        level = (True, f"+{s.levels_gained:,}", "levels gained")
    else:
        level = (bool(s.level), f"{s.level or 0:,}", "level reached")
    tiles = _tiles((
        (s.voice_seconds >= 360, W.fmt_hours(s.voice_seconds), "hours in voice"),
        (s.messages > 0, compact(s.messages), "messages"),
        (s.game_seconds >= 360, W.fmt_hours(s.game_seconds), "hours in game"),
        (s.coins_earned > 0, compact(s.coins_earned), "coins earned"),
        level,
        (bool(s.badges), f"{len(s.badges):,}", "badges earned"),
        (s.squads > 0, f"{s.squads:,}", "squads and matches"),
        (s.active_days > 0, f"{s.active_days:,}", "days in chat"),
    ))
    sections = []
    if s.top_games:
        sections.append(Section("Top games", "bars", _game_rows(s.top_games)))
    mates = _teammate_rows(s, names or {})
    if mates:
        sections.append(Section("Favourite teammates", "people", mates))
    notes = _highlights(s)
    if notes:
        sections.append(Section("Highlights", "facts", notes))
    return CardSpec(**base, tiles=tiles, sections=tuple(sections))


def server_spec(summary: W.ServerSummary, server_name: str | None,
                names: Mapping[int, str] | None = None) -> CardSpec:
    """What the server-wide card says: totals, top games and shout-outs. A shout-out is
    only drawn when every winner's name is in `names` (still here, tracking on)."""
    s, names = summary, names or {}
    line = (f"{W.plural(s.active_members, 'member')} left a mark on the page this year."
            if s.active_members else "The first pages are still being written.")
    tiles = _tiles((
        (s.messages > 0, compact(s.messages), "messages"),
        (s.voice_seconds >= 360, W.fmt_hours(s.voice_seconds), "hours in voice"),
        (s.game_seconds >= 360, W.fmt_hours(s.game_seconds), "hours in game"),
        (s.squads > 0, f"{s.squads:,}", "squads and matches"),
        (s.new_members > 0, f"{s.new_members:,}", "new members"),
        (s.badges > 0, f"{s.badges:,}", "badges earned"),
        (s.coins_earned > 0, compact(s.coins_earned), "coins earned"),
    ))
    sections = []
    if s.top_games:
        sections.append(Section("Top games", "bars", _game_rows(s.top_games)))
    shouts = []
    for sup in s.superlatives:
        if not sup.user_ids or any(uid not in names for uid in sup.user_ids):
            continue
        shouts.append(Row(sup.title, sup.detail, fallback=MEMBER_FALLBACK,
                          names=tuple(screened(names[uid]) or "" for uid in sup.user_ids)))
    if shouts:
        sections.append(Section("Shout-outs", "facts", tuple(shouts)))
    also = []
    if s.busiest_month:
        also.append(Row("Busiest month", W.MONTHS[s.busiest_month - 1]))
    if s.gamenights:
        also.append(Row("Game nights", f"{s.gamenights:,}"))
    if s.tournaments:
        also.append(Row("Tournaments", f"{s.tournaments:,}"))
    if s.clips:
        also.append(Row("Clips posted", f"{s.clips:,}"))
    if also:
        sections.append(Section("Also this year", "facts", tuple(also)))
    return CardSpec(label=(f"Wrapped {s.year}", "The whole server"), name=screened(server_name), alt_name=None,
                    fallback_name="The server", title="The year on the page", line=line, tiles=tiles,
                    sections=tuple(sections), footer="Front Desk  ·  /wrapped for your own card",
                    footer_right="totals only, plus a few shout-outs")


# ---------------------------------------------------------------- drawing
def _clean(text: str | None, size: int, bold: bool = False) -> tuple[str, object]:
    """(drawable text, the font to draw it with). The font is the shipped script that
    covers the most of the text."""
    script = C.pick_script(text or "", size, bold)
    font = C.load_font(size, bold, script)
    return C.clean_text(text, functools.partial(C.can_draw, font)), font


def _fitted(text: str | None, size: int, width: float, bold: bool = False, fallback: str = "") -> tuple[str, object]:
    cleaned, font = _clean(text, size, bold)
    if not cleaned:
        cleaned, font = _clean(fallback, size, bold)
    return C.fit_text(cleaned, font.getlength, width), font


def _wrap(text: str, font, width: float, max_lines: int = 3) -> list[str]:
    """Greedy word wrap; the last allowed line is cut with an ellipsis if text is left over."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or font.getlength(trial) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [C.fit_text(line, font.getlength, width) for line in lines]


def _page() -> Image.Image:
    """The dark notebook page with its dot grid (no grain: the output must be repeatable)."""
    page = Image.new("RGB", (WIDTH, MAX_HEIGHT), C.GROUND)
    draw = ImageDraw.Draw(page)
    for y in range(20, MAX_HEIGHT, 24):
        for x in range(36, WIDTH, 24):
            draw.point((x, y), fill=C.DOTS)
            draw.point((x + 1, y), fill=C.DOTS)
    draw.rectangle((0, 0, 9, MAX_HEIGHT), fill=C.PEN)  # the pen spine
    return page


def _shown_name(spec: CardSpec, size: int) -> str:
    for candidate in (spec.name, spec.alt_name, spec.fallback_name):
        cleaned, _ = _clean(candidate, size, True)
        if cleaned:
            return cleaned
    return MEMBER_FALLBACK


def _header(page: Image.Image, draw: ImageDraw.ImageDraw, spec: CardSpec, avatar: bytes | None, y: int) -> int:
    shown = _shown_name(spec, 60)
    face = C.decode_avatar(avatar, AVATAR) or C.initial_avatar(C.initial(shown), AVATAR)
    ring, gap = AVATAR + 12, AVATAR + 4
    cx, cy = LEFT + AVATAR // 2, y + AVATAR // 2
    page.paste(Image.new("RGB", (ring, ring), C.PEN), (cx - ring // 2, cy - ring // 2), C._supersampled_circle(ring))
    page.paste(Image.new("RGB", (gap, gap), C.GROUND), (cx - gap // 2, cy - gap // 2), C._supersampled_circle(gap))
    page.paste(face.convert("RGB"), (cx - AVATAR // 2, cy - AVATAR // 2), C._supersampled_circle(AVATAR))

    width = RIGHT - TEXT_X
    parts = [p for p in (_clean(part, 20, True)[0].upper() for part in spec.label) if p]
    label_font = C.load_font(20, True)
    draw.text((TEXT_X, y + 2), C.fit_text("  ·  ".join(parts), label_font.getlength, width),
              font=label_font, fill=C.INK_FAINT)
    draw.rectangle((TEXT_X, y + 36, TEXT_X + 44, y + 38), fill=C.PEN)

    script = C.pick_script(shown, 60)
    font = C.load_font(44, True, script)
    text = C.fit_text(shown, font.getlength, width)
    for size in (60, 52, 44):
        candidate = C.load_font(size, True, script)
        if candidate.getlength(shown) <= width:
            text, font = shown, candidate
            break
    draw.text((TEXT_X - 2, y + 104), text, font=font, fill=C.INK, anchor="ls")

    title, title_font = _fitted(spec.title, 30, width, True)
    draw.text((TEXT_X, y + 116), title, font=title_font, fill=C.PEN)
    y += AVATAR + 30

    line_font = C.load_font(26)
    for text in _wrap(spec.line, line_font, RIGHT - LEFT, 2):
        draw.text((LEFT, y), text, font=line_font, fill=C.INK_MUTED)
        y += 36
    return y + 24


def _notes(draw: ImageDraw.ImageDraw, spec: CardSpec, y: int) -> int:
    for i, note in enumerate(spec.notes):
        font = C.load_font(28, bold=i == 0)
        fill = C.INK if i == 0 else C.INK_MUTED
        for text in _wrap(note, font, RIGHT - LEFT, 3):
            draw.text((LEFT, y), text, font=font, fill=fill)
            y += 40
        y += 14
    return y + 10


def _tiles_block(draw: ImageDraw.ImageDraw, tiles: tuple[Tile, ...], y: int) -> int:
    tiles = tiles[:MAX_TILES]
    done = 0
    for per_row in TILE_ROWS[len(tiles)]:
        width = (RIGHT - LEFT - TILE_GAP * (per_row - 1)) / per_row
        for i, tile in enumerate(tiles[done:done + per_row]):
            x = LEFT + i * (width + TILE_GAP)
            draw.rounded_rectangle((x, y, x + width, y + TILE_HEIGHT), radius=14, fill=TILE_FILL)
            inner = width - 48
            value_font = C.load_font(40, True)
            for size in (58, 50, 40):
                value_font = C.load_font(size, True)
                if value_font.getlength(tile.value) <= inner:
                    break
            draw.text((x + 24, y + 84), C.fit_text(tile.value, value_font.getlength, inner),
                      font=value_font, fill=C.INK, anchor="ls")
            label_font = C.load_font(19, True)
            draw.text((x + 24, y + 102), C.fit_text(tile.label.upper(), label_font.getlength, inner),
                      font=label_font, fill=C.INK_FAINT)
        done += per_row
        y += TILE_HEIGHT + TILE_GAP
    return y + 24 if tiles else y


def _section_title(draw: ImageDraw.ImageDraw, title: str, y: int) -> int:
    font = C.load_font(20, True)
    draw.text((LEFT, y), title.upper(), font=font, fill=C.INK_FAINT)
    draw.rectangle((LEFT, y + 32, LEFT + 44, y + 34), fill=C.PEN)
    return y + 54


def _right_text(draw: ImageDraw.ImageDraw, text: str, y: int, size: int, max_width: float, fill,
                bold: bool = False) -> float:
    """Draw right-aligned text; returns the width it took."""
    if not text:
        return 0.0
    fitted, font = _fitted(text, size, max_width, bold)
    draw.text((RIGHT, y), fitted, font=font, fill=fill, anchor="ra")
    return font.getlength(fitted)


def _bars(draw: ImageDraw.ImageDraw, rows: tuple[Row, ...], y: int) -> int:
    for i, row in enumerate(rows):
        number_font = C.load_font(24, True)
        draw.text((LEFT, y + 2), f"{i + 1}", font=number_font, fill=C.INK_FAINT)
        x = LEFT + 40
        taken = _right_text(draw, row.right, y + 3, 24, 220, C.INK_MUTED)
        name, font = _fitted(row.left, 28, RIGHT - x - taken - 28, True, row.fallback)
        draw.text((x, y), name, font=font, fill=C.INK)
        draw.rounded_rectangle((x, y + 44, RIGHT, y + 50), radius=3, fill=TRACK)
        filled = int((RIGHT - x) * max(0.0, min(row.share or 0.0, 1.0)))
        if filled >= 6:
            draw.rounded_rectangle((x, y + 44, x + filled, y + 50), radius=3, fill=C.PEN)
        y += ROW_HEIGHT["bars"]
    return y


def _people(page: Image.Image, draw: ImageDraw.ImageDraw, rows: tuple[Row, ...], y: int) -> int:
    dot = 44
    for row in rows:
        shown = _clean(row.left, 28, True)[0] or row.fallback
        page.paste(C.initial_avatar(C.initial(shown), dot).convert("RGB"), (LEFT, y), C._supersampled_circle(dot))
        x = LEFT + dot + 20
        taken = _right_text(draw, row.right, y + 9, 22, 420, C.INK_MUTED)
        name, font = _fitted(row.left, 28, RIGHT - x - taken - 28, True, row.fallback)
        draw.text((x, y + 4), name, font=font, fill=C.INK)
        y += ROW_HEIGHT["people"]
    return y


def _facts(draw: ImageDraw.ImageDraw, rows: tuple[Row, ...], y: int) -> int:
    for i, row in enumerate(rows):
        if i:
            draw.line((LEFT, y - 9, RIGHT, y - 9), fill=C.HAIRLINE, width=1)
        label, font = _fitted(row.left, 24, 330, False, row.fallback)
        draw.text((LEFT, y + 2), label, font=font, fill=C.INK_MUTED)
        room = RIGHT - LEFT - font.getlength(label) - 32
        if row.names:
            # Names are cut to fit; the number after them is always shown whole.
            taken = _right_text(draw, row.right, y + 3, 23, room / 2, C.INK_MUTED)
            x = RIGHT - taken - 16
            each = (room - taken - 16 - 30 * (len(row.names) - 1)) / len(row.names)
            for j, name in enumerate(reversed(row.names)):
                if j:
                    amp = C.load_font(23)
                    draw.text((x - 8, y + 3), "&", font=amp, fill=C.INK_MUTED, anchor="ra")
                    x -= 30
                text, name_font = _fitted(name, 25, each, True, row.fallback)
                draw.text((x, y + 1), text, font=name_font, fill=C.INK, anchor="ra")
                x -= name_font.getlength(text)
        else:
            value, value_font = _fitted(row.right, 25, room, True, row.fallback)
            draw.text((RIGHT, y + 1), value, font=value_font, fill=C.INK, anchor="ra")
        y += ROW_HEIGHT["facts"]
    return y


def render(spec: CardSpec, avatar: bytes | None = None) -> bytes:
    """The card as PNG bytes: WIDTH wide, as tall as its content."""
    page = _page()
    draw = ImageDraw.Draw(page)
    y = _header(page, draw, spec, avatar, TOP)
    if spec.notes:
        y = _notes(draw, spec, y)
    y = _tiles_block(draw, spec.tiles, y)
    for section in spec.sections:
        if not section.rows:
            continue
        y = _section_title(draw, section.title, y)
        if section.kind == "bars":
            y = _bars(draw, section.rows, y)
        elif section.kind == "people":
            y = _people(page, draw, section.rows, y)
        else:
            y = _facts(draw, section.rows, y)
        y += 30

    draw.line((LEFT, y, RIGHT, y), fill=C.HAIRLINE, width=2)
    small = C.load_font(19)
    right = C.fit_text(spec.footer_right, small.getlength, 420)
    draw.text((RIGHT, y + 18), right, font=small, fill=C.INK_FAINT, anchor="ra")
    draw.text((LEFT, y + 18), C.fit_text(spec.footer, small.getlength, RIGHT - LEFT - small.getlength(right) - 32),
              font=small, fill=C.INK_FAINT)
    height = min(y + 18 + 26 + 40, MAX_HEIGHT)

    buf = io.BytesIO()
    page.crop((0, 0, WIDTH, height)).save(buf, "PNG", optimize=True)
    return buf.getvalue()
