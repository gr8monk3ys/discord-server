"""Landing page (site/) helpers: the games list is generated from layout.GAMES.

Pure string functions; the only I/O is the `python -m logic.sitegen` entry point
(run from bot/), which rewrites the games block in site/index.html in place, and
`python -m logic.sitegen --og`, which rebuilds the 1200x630 social card
site/img/og.png from assets/listing/hero.png.
tests/test_sitegen.py fails when the page drifts from layout.py.
"""

import html
import re

_INVITE_RE = re.compile(r"[A-Za-z0-9-]{2,32}")
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z:-]+)="([^"]*)"')

OG_SIZE = (1200, 630)


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


def meta_tags(page: str) -> dict[str, str]:
    """{property-or-name: content} for every <meta> with both; attribute order doesn't matter."""
    out: dict[str, str] = {}
    for tag in _META_RE.findall(page):
        attrs = dict(_ATTR_RE.findall(tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            out[key] = html.unescape(attrs["content"])
    return out


def parse_csp(value: str) -> dict[str, list[str]]:
    """Content-Security-Policy header -> {directive: [sources]} (lower-cased names,
    first occurrence wins, as browsers do)."""
    out: dict[str, list[str]] = {}
    for part in value.split(";"):
        bits = part.split()
        if bits and bits[0].lower() not in out:
            out[bits[0].lower()] = bits[1:]
    return out


def csp_allows(sources: list[str], url: str, self_origin: bool = True) -> bool:
    """Whether a source list admits `url` (an absolute https URL or a same-origin
    path). Handles 'self', scheme-only and host sources with an optional path
    prefix; enough for the static site's own policy, not a full CSP engine."""
    if "*" in sources:
        return True
    if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):  # relative or root path
        return self_origin and "'self'" in sources
    scheme = url.split(":", 1)[0].lower() + ":"
    if scheme in (s.lower() for s in sources):
        return True
    for src in sources:
        if src.startswith("'") or "://" not in src:
            continue
        if url == src or url.startswith(src.rstrip("/") + "/") or url.startswith(src + "?"):
            return True
    return False


def cover_box(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """Centred crop box (left, top, right, bottom) of the source with the target's aspect
    ratio, like CSS object-fit: cover. Scale the box to (dst_w, dst_h) afterwards."""
    if src_w * dst_h > dst_w * src_h:  # source is wider: trim the sides
        w = round(src_h * dst_w / dst_h)
        left = (src_w - w) // 2
        return left, 0, left + w, src_h
    h = round(src_w * dst_h / dst_w)  # source is taller (or equal): trim top and bottom
    top = (src_h - h) // 2
    return 0, top, src_w, top + h


def _write_og(root) -> None:  # pragma: no cover - Pillow I/O
    from PIL import Image

    src = root / "assets" / "listing" / "hero.png"
    dst = root / "site" / "img" / "og.png"
    with Image.open(src) as im:
        im = im.convert("RGB")
        card = im.crop(cover_box(*im.size, *OG_SIZE)).resize(OG_SIZE, Image.LANCZOS)
    card.save(dst, optimize=True)
    print("site/img/og.png written", card.size)


if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if "--og" in sys.argv[1:]:
        _write_og(root)
        raise SystemExit(0)

    import config

    path = root / "site" / "index.html"
    games = [(g.emoji, g.channel, g.role) for g in config.GAMES]
    page = path.read_text(encoding="utf-8")
    new = replace_block(page, "games", render_games(games))
    path.write_text(new, encoding="utf-8", newline="\n")
    print("site/index.html games:", "updated" if new != page else "already current")
