"""Hall of fame rules: who counts as a star, which messages qualify, what goes in
the board post. Pure: no Discord, no database, so it's all unit-tested."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from logic import quests as Q

STAR = "⭐"
THRESHOLD = 3  # distinct stars (not the author's, not bots') to land in the hall
TEXT_LIMIT = 1000
DAY = 24 * 60 * 60
MAX_AGE = 14 * DAY  # older messages are never newly posted
PENDING = 0  # board_message_id while the post is being sent (claimed, not yet known)
# A claim this old was never finished (crash/restart mid-send, or the final UPDATE
# failed): the sender is gone, so the claim is recovered instead of blocking forever.
CLAIM_TIMEOUT = 10 * 60
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_star(emoji) -> bool:
    """The unicode ⭐, given as a str (Reaction.emoji) or a PartialEmoji (payloads).
    Custom emoji never count, even one named ⭐."""
    if emoji is None:
        return False
    if isinstance(emoji, str):
        name = emoji
    else:
        if getattr(emoji, "id", None) is not None:
            return False
        name = getattr(emoji, "name", None) or ""
    return name.replace("️", "") == STAR


def count_stars(reactors: Iterable, author_id: int, now: int | None = None) -> int:
    """Distinct people who starred: not the author, not bots, and (given `now`) not accounts
    younger than quests.MIN_ACCOUNT_DAYS, since a hall-of-fame post earns coins."""
    return len({r.id for r in reactors if not r.bot and r.id != author_id
                and (now is None or Q.established(r.id, now))})


def reaches(stars: int) -> bool:
    return stars >= THRESHOLD


@dataclass(frozen=True)
class Board:
    """The starboard row for a message: its hall post and the count it shows."""
    board_message_id: int | None
    stars: int
    at: int = 0  # when the row was claimed


def stale_claim(board: Board | None, now_ts: float) -> bool:
    """A PENDING claim whose sender must be gone: safe to recover."""
    return (board is not None and board.board_message_id == PENDING
            and now_ts - board.at > CLAIM_TIMEOUT)


class Action(Enum):
    NONE = "none"
    POST = "post"
    UPDATE = "update"


def plan(board: Board | None, stars: int) -> Action:
    """Post once at the threshold; after that only ever update the count (the post
    stays even if stars drop below the threshold)."""
    if board is None or board.board_message_id is None:
        return Action.POST if reaches(stars) else Action.NONE
    if board.board_message_id == PENDING:
        return Action.NONE  # another update is posting it right now
    return Action.UPDATE if board.stars != stars else Action.NONE


def skip_channel(in_hall: bool, in_staff: bool, nsfw: bool) -> bool:
    return in_hall or in_staff or nsfw


def too_old(created_ts: float, now_ts: float) -> bool:
    return now_ts - created_ts > MAX_AGE


def text(content: str | None, limit: int = TEXT_LIMIT) -> str | None:
    """The message text for the board post, or None if there isn't any."""
    if not content or not content.strip():
        return None
    content = content.strip()
    if len(content) > limit:
        content = content[: limit - 1] + "…"
    return content


def _is_image_attachment(a) -> bool:
    content_type = getattr(a, "content_type", None)
    if content_type:
        return content_type.startswith("image/")
    return (getattr(a, "filename", "") or "").lower().endswith(IMAGE_EXTENSIONS)


def _url(obj) -> str | None:
    return getattr(obj, "url", None) if obj is not None else None


def pick_image(attachments: Iterable, embeds: Iterable) -> str | None:
    """First image attachment, else the first image from an embed (an image/gif
    link Discord expanded, or a rich embed's big image)."""
    for a in attachments:
        if _is_image_attachment(a):
            return a.url
    for e in embeds:
        kind = getattr(e, "type", None)
        if kind == "image":
            url = getattr(e, "url", None) or _url(getattr(e, "thumbnail", None))
        elif kind == "gifv":
            url = _url(getattr(e, "thumbnail", None))
        else:
            url = _url(getattr(e, "image", None))
        if url:
            return url
    return None


def footer(channel_name: str, stars: int) -> str:
    return f"#{channel_name} · {STAR} {stars}"
