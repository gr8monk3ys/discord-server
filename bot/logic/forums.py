"""Forums module rules: the guides and setups forums. Pure: no Discord calls, no database.

- `kind_of` says which of the two forums a channel name is.
- `decide` says what a post that has survived a day earns: a small one-time reward when the
  content clears a minimum bar, the author's account is established and they are under the
  weekly cap. Paying late (not at creation) means a post that was deleted or locked by a mod
  in the meantime never pays, so there is no claim to take back.
- `fresh_line` is the one-line "fresh in the forums" summary for the weekly recap.
"""

import re
from dataclasses import dataclass

import discord

import config
from logic import quests as Q
from logic import textfilter

DAY = 24 * 60 * 60
WEEK = 7 * DAY

GUIDES, SETUPS = "guides", "setups"
FORUMS = {GUIDES: config.GUIDES_FORUM, SETUPS: config.SETUPS_FORUM}
REACTIONS = {GUIDES: "👍", SETUPS: "🔥"}  # the same emoji layout.py sets as each forum's default reaction

REWARD = 30  # coins per post: under a day of chat coins, so not worth farming
WEEKLY_CAP = 2  # paid posts per member in any 7 days
SURVIVE = DAY  # a post is judged (and paid) once it has been up this long
GIVE_UP = 7 * DAY  # a post that still can't be judged this long after SURVIVE is dropped
REASON = "forum_post"

# The minimum-content bar. Links don't count toward length, and the distinct-word count stops
# "aaaa…" or one sentence pasted ten times from clearing it.
MIN_GUIDE_CHARS = 200
MIN_GUIDE_WORDS = 25
MIN_SETUP_CHARS = 150  # a setup needs a picture, or this much description
MIN_SETUP_WORDS = 20
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# What happens to a post: the `state` column of forum_posts.
PENDING, PAID, UNPAID, SKIPPED, DELETED = "pending", "paid", "unpaid", "skipped", "deleted"
LISTED = (PENDING, PAID, UNPAID)  # states the recap line may mention

MAX_TITLE = 80
_URL = re.compile(r"\S+://\S+|\bwww\.\S+", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]{2,}")


def kind_of(channel_name: str | None) -> str | None:
    """GUIDES or SETUPS if `channel_name` is one of the two forums (emoji and case ignored)."""
    if not channel_name:
        return None
    target = config.slug(channel_name)
    return next((kind for kind, name in FORUMS.items() if config.slug(name) == target), None)


def is_image(filename: str | None, content_type: str | None) -> bool:
    if content_type:
        return content_type.lower().startswith("image/")
    return bool(filename) and filename.lower().endswith(IMAGE_EXTENSIONS)


def measure(content: str | None) -> tuple[int, int]:
    """(characters, distinct words) of `content`, ignoring links and extra whitespace."""
    text = " ".join(_URL.sub(" ", content or "").split())
    return len(text), len({w.casefold() for w in _WORD.findall(text)})


def qualifies(kind: str, content: str | None, images: int) -> bool:
    """The minimum-content bar: an empty or one-line post earns nothing."""
    chars, words = measure(content)
    if kind == GUIDES:
        return chars >= MIN_GUIDE_CHARS and words >= MIN_GUIDE_WORDS
    if kind == SETUPS:
        return images > 0 or (chars >= MIN_SETUP_CHARS and words >= MIN_SETUP_WORDS)
    return False


def decide(kind: str, content: str | None, images: int, *, author_id: int, created_at: int,
           locked: bool, paid_this_week: int) -> str:
    """PAID, UNPAID or SKIPPED for a post that is still up after SURVIVE.

    SKIPPED: a mod locked it, or it is too thin. UNPAID: a fine post that earns nothing
    because the account was under quests.MIN_ACCOUNT_DAYS old when it was posted (alts) or
    the author already had WEEKLY_CAP paid posts in the last 7 days."""
    if locked or not qualifies(kind, content, images):
        return SKIPPED
    if not Q.established(author_id, created_at) or paid_this_week >= WEEKLY_CAP:
        return UNPAID
    return PAID


def ref(thread_id: int) -> str:
    """The ledger ref: one payout per post, whatever happens."""
    return f"forum:{thread_id}"


def show_title(title: str | None) -> str | None:
    """A member's post title made safe for the bot to repeat, or None if it shouldn't be:
    the text filter refuses invites, links and slurs (bots skip AutoMod)."""
    title = " ".join((title or "").split())
    if not title:
        return None
    ok, _ = textfilter.check(title, allow_links=False)
    if not ok:
        return None
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE - 1].rstrip() + "…"
    return discord.utils.escape_mentions(discord.utils.escape_markdown(title))


@dataclass(frozen=True)
class Post:
    kind: str
    thread_id: int
    title: str
    created_at: int


def _count(n: int, word: str) -> str:
    return f"{n} new {word}" + ("" if n == 1 else "s")


def fresh_line(posts: list[Post]) -> str | None:
    """"Fresh in the forums: 2 new guides and 1 new setup. Latest: **Title** (<#id>)", or None
    when there was nothing. The newest post whose title passes the text filter is the one
    linked; a post with a refused title is counted but never named or linked."""
    if not posts:
        return None
    counts = [_count(n, word) for kind, word in ((GUIDES, "guide"), (SETUPS, "setup"))
              if (n := sum(p.kind == kind for p in posts))]
    if not counts:
        return None
    line = f"Fresh in the forums: {' and '.join(counts)}."
    for post in sorted(posts, key=lambda p: (-p.created_at, p.thread_id)):
        title = show_title(post.title)
        if title is not None:
            return f"{line} Latest: **{title}** (<#{post.thread_id}>)"
    return line
