"""Levels: XP from chat and voice, the MEE6-style curve, reward roles, ranking, the
level-up announcement limiter and the 900x260 rank card. Pure: no Discord, no database.
render_rank_card is CPU-bound, so callers run it in a thread.

The card reuses the welcome card's Field Notebook pieces (fonts, colours, name cleaning,
avatar handling) from logic.cards, so both look like one set."""

from __future__ import annotations

import functools
import io
from collections.abc import Iterable

import discord
from PIL import Image, ImageDraw

import config
from logic import cards as C
from names import slug

# ---------------------------------------------------------------- rules
MESSAGE_XP = (15, 25)  # inclusive
MESSAGE_COOLDOWN = 60  # seconds between XP-earning messages per member
MIN_MESSAGE_CHARS = 3
VOICE_XP_PER_MINUTE = 10
LEVEL_ROLES: list[tuple[int, str]] = sorted(config.LEVEL_ROLES)


def xp_to_next(level: int) -> int:
    """XP needed to go from `level` to `level + 1` (MEE6's curve)."""
    return 5 * level * level + 50 * level + 100


def total_for_level(level: int) -> int:
    """Total XP at which `level` starts."""
    return sum(xp_to_next(i) for i in range(max(level, 0)))


def level_for(total_xp: int) -> int:
    level, remaining = 0, max(total_xp, 0)
    while remaining >= xp_to_next(level):
        remaining -= xp_to_next(level)
        level += 1
    return level


def progress(total_xp: int) -> tuple[int, int, int]:
    """(level, XP into this level, XP this level needs in all)."""
    level = level_for(total_xp)
    return level, max(total_xp, 0) - total_for_level(level), xp_to_next(level)


def fraction(into: int, needed: int) -> float:
    if needed <= 0:
        return 0.0
    return min(max(into / needed, 0.0), 1.0)


def message_xp(rng) -> int:
    return rng.randint(*MESSAGE_XP)


def long_enough(content: str | None) -> bool:
    return len((content or "").strip()) >= MIN_MESSAGE_CHARS


def off_cooldown(last_msg_at: int, now: int) -> bool:
    return not last_msg_at or now - last_msg_at >= MESSAGE_COOLDOWN


def voice_credit(total_counted: int, already_counted: int) -> tuple[int, int]:
    """(XP to add, new voice_seconds_counted). Only whole new minutes are credited, so the
    leftover seconds carry to the next sweep. If the total went down (sessions deleted by
    /privacy, or an open session closed a little earlier than last seen), the baseline
    follows it and no XP moves."""
    if total_counted < already_counted:
        return 0, max(total_counted, 0)
    minutes = (total_counted - already_counted) // 60
    return minutes * VOICE_XP_PER_MINUTE, already_counted + minutes * 60


# ---------------------------------------------------------------- roles
def reward_role(level: int) -> str | None:
    """The role for the highest threshold reached, or None below the first."""
    best = None
    for threshold, name in LEVEL_ROLES:
        if level >= threshold:
            best = name
    return best


def role_changes(level: int, held_names: Iterable[str]) -> tuple[str | None, list[str]]:
    """(role name to add or None, held level-role names to remove) so a member ends up
    with exactly the reward role for `level`. Names match ignoring emoji and case."""
    want = reward_role(level)
    level_slugs = {slug(name) for _, name in LEVEL_ROLES}
    held = [h for h in held_names if slug(h) in level_slugs]
    has_wanted = want is not None and any(slug(h) == slug(want) for h in held)
    remove = sorted(h for h in held if want is None or slug(h) != slug(want))
    return (None if has_wanted else want), remove


# ---------------------------------------------------------------- ranking
def rank_position(scores: dict[int, int], user_id: int) -> int | None:
    """Competition rank (1, 2, 2, 4) among members with XP; None if unranked."""
    mine = scores.get(user_id, 0)
    if mine <= 0:
        return None
    return 1 + sum(1 for uid, xp in scores.items() if xp > mine and uid != user_id)


def top(scores: dict[int, int], n: int = 10) -> list[tuple[int, int, int]]:
    """[(rank, user_id, xp)] by XP desc, then user id."""
    ordered = sorted(((u, x) for u, x in scores.items() if x > 0), key=lambda ux: (-ux[1], ux[0]))
    out: list[tuple[int, int, int]] = []
    for i, (uid, xp) in enumerate(ordered[:n]):
        rank = out[-1][0] if out and out[-1][2] == xp else i + 1
        out.append((rank, uid, xp))
    return out


# ---------------------------------------------------------------- announcing
def level_up_text(mention: str, level: int, role_name: str | None = None) -> str:
    text = f"{mention} reached **level {level}**"
    if role_name:
        text += f" and is now **{discord.utils.escape_markdown(role_name)}**"
    return text + ". GG!"


class Limiter:
    """Keeps level-up posts from spamming: at most one per member every `member_gap`
    seconds, one per channel every `channel_gap`, and `per_minute` in all. In memory:
    after a restart the worst case is one extra post."""

    def __init__(self, member_gap: int = 300, channel_gap: int = 20, per_minute: int = 5):
        self.member_gap, self.channel_gap, self.per_minute = member_gap, channel_gap, per_minute
        self.members: dict[int, int] = {}
        self.channels: dict[int, int] = {}
        self.recent: list[int] = []

    def allow(self, user_id: int, channel_id: int, now: int) -> bool:
        self.recent = [t for t in self.recent if now - t < 60]
        if len(self.recent) >= self.per_minute:
            return False
        if user_id in self.members and now - self.members[user_id] < self.member_gap:
            return False
        if channel_id in self.channels and now - self.channels[channel_id] < self.channel_gap:
            return False
        self.members[user_id] = self.channels[channel_id] = now
        self.recent.append(now)
        if len(self.members) > 5000:
            self.members = {u: t for u, t in self.members.items() if now - t < self.member_gap}
        return True


# ---------------------------------------------------------------- rank card
CARD_WIDTH, CARD_HEIGHT = 900, 260
CARD_AVATAR = 168
CARD_AVATAR_CENTER = (138, 130)
TEXT_X = 262
TEXT_RIGHT = CARD_WIDTH - 52
NAME_BASELINE = 118
BAR_TOP, BAR_BOTTOM = 158, 184
BAR_LEFT, BAR_RIGHT = TEXT_X, TEXT_RIGHT
PEN = C.PEN
BAR_TRACK = (34, 37, 43)


def _page() -> Image.Image:
    """The welcome card's dark page with its dot grid, at rank-card size."""
    page = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), C.GROUND)
    draw = ImageDraw.Draw(page)
    for y in range(20, CARD_HEIGHT, 24):
        for x in range(36, CARD_WIDTH, 24):
            draw.point((x, y), fill=C.DOTS)
            draw.point((x + 1, y), fill=C.DOTS)
    return page


def _shown_name(name: str | None, alt_name: str | None, size: int) -> str:
    for candidate in (name, alt_name):
        script = C.pick_script(candidate or "", size)
        cleaned = C.clean_text(candidate, functools.partial(C.can_draw, C.load_font(size, True, script)))
        if cleaned:
            return cleaned
    return ""


def _fit_name(name: str, width: float):
    script = C.pick_script(name, 48)
    for size in (48, 42, 36):
        font = C.load_font(size, True, script)
        if font.getlength(name) <= width:
            return name, font
    font = C.load_font(36, True, script)
    return C.fit_text(name, font.getlength, width), font


def _rounded_bar(draw: ImageDraw.ImageDraw, box, fill) -> None:
    x0, y0, x1, y1 = box
    if x1 - x0 < 1:
        return
    radius = min((y1 - y0) // 2, max((x1 - x0) // 2, 0))
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render_rank_card(name: str | None, alt_name: str | None, avatar: bytes | None, *, level: int,
                     rank: int | None, into: int, needed: int, total: int) -> bytes:
    """The rank card as PNG bytes: avatar, name, level, rank position and the XP bar."""
    page = _page()
    draw = ImageDraw.Draw(page)
    draw.rectangle((0, 0, 7, CARD_HEIGHT), fill=PEN)  # the pen spine

    shown = _shown_name(name, alt_name, 48)
    face = C.decode_avatar(avatar, CARD_AVATAR) or C.initial_avatar(C.initial(shown), CARD_AVATAR)
    cx, cy = CARD_AVATAR_CENTER
    ring = CARD_AVATAR + 12
    page.paste(Image.new("RGB", (ring, ring), PEN), (cx - ring // 2, cy - ring // 2), C._supersampled_circle(ring))
    gap = CARD_AVATAR + 4
    page.paste(Image.new("RGB", (gap, gap), C.GROUND), (cx - gap // 2, cy - gap // 2), C._supersampled_circle(gap))
    page.paste(face.convert("RGB"), (cx - CARD_AVATAR // 2, cy - CARD_AVATAR // 2),
               C._supersampled_circle(CARD_AVATAR))

    # Right side, top: LEVEL n in the pen; left of it the label.
    level_font = C.load_font(40, True)
    level_text = f"LEVEL {level}"
    level_w = level_font.getlength(level_text)
    draw.text((TEXT_RIGHT, 30), level_text, font=level_font, fill=PEN, anchor="ra")
    label_font = C.load_font(18, True)
    label = "RANK " + (f"#{rank:,}" if rank else "—")
    label = C.fit_text(label, label_font.getlength, TEXT_RIGHT - TEXT_X - level_w - 24)
    draw.text((TEXT_X, 40), label, font=label_font, fill=C.INK_FAINT)
    draw.rectangle((TEXT_X, 66, TEXT_X + 36, 68), fill=PEN)

    # Name, big.
    name_width = TEXT_RIGHT - TEXT_X
    if shown:
        text, font = _fit_name(shown, name_width)
        draw.text((TEXT_X - 2, NAME_BASELINE), text, font=font, fill=C.INK, anchor="ls")
    else:
        draw.text((TEXT_X - 2, NAME_BASELINE), "Member", font=C.load_font(48, True), fill=C.INK, anchor="ls")

    # XP bar.
    _rounded_bar(draw, (BAR_LEFT, BAR_TOP, BAR_RIGHT, BAR_BOTTOM), BAR_TRACK)
    filled = int((BAR_RIGHT - BAR_LEFT) * fraction(into, needed))
    if filled >= BAR_BOTTOM - BAR_TOP:  # a sliver narrower than the bar is tall would look odd
        _rounded_bar(draw, (BAR_LEFT, BAR_TOP, BAR_LEFT + filled, BAR_BOTTOM), PEN)

    small = C.load_font(20)
    draw.text((TEXT_X, 200), f"{max(into, 0):,} / {needed:,} XP", font=small, fill=C.INK_MUTED)
    draw.text((TEXT_RIGHT, 200), f"{max(total, 0):,} XP total", font=small, fill=C.INK_FAINT, anchor="ra")

    buf = io.BytesIO()
    page.save(buf, "PNG", optimize=True)
    return buf.getvalue()
