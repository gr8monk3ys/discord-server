"""Landing page (site/) helpers: the games list is generated from layout.GAMES.

Pure string functions; the only I/O is the `python -m logic.sitegen` entry point
(run from bot/), which rewrites the games block in site/index.html in place.
tests/test_sitegen.py fails when the page drifts from layout.py.
"""

import html
import re

_INVITE_RE = re.compile(r"[A-Za-z0-9-]{2,32}")


def render_games(games) -> str:
    """games: iterable of (emoji, channel, role) tuples, as in layout.GAMES."""
    items = [
        f'<li class="game"><span class="game-emoji" aria-hidden="true">{html.escape(emoji)}</span>'
        f"<span>{html.escape(role)}</span></li>"
        for emoji, _channel, role in games
    ]
    return "\n".join(items)


def _markers(name: str) -> tuple[str, str]:
    return f"<!-- {name}:start -->", f"<!-- {name}:end -->"


def _span(page: str, name: str) -> tuple[int, int]:
    start, end = _markers(name)
    i = page.find(start)
    j = page.find(end, i + len(start)) if i >= 0 else -1
    if i < 0 or j < 0:
        raise ValueError(f"missing {start} / {end} markers")
    return i + len(start), j


def extract_block(page: str, name: str) -> str:
    i, j = _span(page, name)
    return page[i:j].strip("\n")


def replace_block(page: str, name: str, body: str) -> str:
    i, j = _span(page, name)
    return page[:i] + "\n" + body + "\n" + page[j:]


def valid_invite_code(code: str) -> bool:
    """A bare invite code (not a URL), and not the REPLACE_ME placeholder."""
    return bool(code) and code != "REPLACE_ME" and bool(_INVITE_RE.fullmatch(code))


if __name__ == "__main__":  # pragma: no cover
    from pathlib import Path

    import config

    path = Path(__file__).resolve().parents[2] / "site" / "index.html"
    games = [(g.emoji, g.channel, g.role) for g in config.GAMES]
    page = path.read_text(encoding="utf-8")
    new = replace_block(page, "games", render_games(games))
    path.write_text(new, encoding="utf-8", newline="\n")
    print("site/index.html games:", "updated" if new != page else "already current")
