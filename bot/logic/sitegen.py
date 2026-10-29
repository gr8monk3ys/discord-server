"""Landing page (site/) helpers: the games list is generated from layout.GAMES and the
rules list from the rules embed in layout.POSTS.

The site is a Next.js app; it renders both lists from JSON data files that this module
writes. Pure functions; the only I/O is the `python -m logic.sitegen` entry point (run from
bot/), which rewrites site/src/data/generated/games.json and rules.json, and
`python -m logic.sitegen --og`, which rebuilds the 1200x630 social card
site/public/img/og.png from assets/listing/hero.png.
tests/test_sitegen.py fails when the data files drift from layout.py.
"""

import json
import re

_INVITE_RE = re.compile(r"[A-Za-z0-9-]{2,32}")

OG_SIZE = (1200, 630)


def games_data(games) -> list[str]:
    """games: iterable of (emoji, channel, role) tuples, as in layout.GAMES. The page shows the
    names only: the emoji belong to Discord's channel list, and on a web page they render
    differently on every system. React escapes the names when it renders them."""
    return [role for _emoji, _channel, role in games]


_RULE_RE = re.compile(r"^`(\d+)`\s+(.+)$")
_LEAD_RE = re.compile(r"^(.+?[.?!])\s+(.+)$")


def _rule_segments(text: str) -> list:
    """One rule from the Discord embed as page segments: {#clips} and {@LFG} become plain #clips
    and @LFG (they are links only inside Discord), ||spoiler|| loses its bars and `code` becomes
    {"code": "..."}; everything else is a plain string, rendered as text (never as HTML)."""
    text = re.sub(r"\{([#@])([^}]+)\}", r"\1\2", text)
    text = text.replace("||", "")
    parts = re.split(r"`([^`]+)`", text)
    return [{"code": p} if i % 2 else p for i, p in enumerate(parts) if p]


def rules_data(description: str) -> list[dict]:
    """description: layout.POSTS["rules"]["description"], one "`01`  rule" per line. The first
    sentence of each rule is its lead (set in bold); the page numbers the list itself, as the
    embed does. Each rule is {"lead": segments, "body": segments}."""
    items = []
    for line in description.splitlines():
        m = _RULE_RE.match(line.strip())
        if not m:
            continue
        rule = m.group(2).strip()
        lead = _LEAD_RE.match(rule)
        if lead:
            items.append({"lead": _rule_segments(lead.group(1)), "body": _rule_segments(lead.group(2))})
        else:
            items.append({"lead": _rule_segments(rule), "body": []})
    return items


def dump_json(data) -> str:
    """The data files' exact text: two-space indent, UTF-8 kept, one trailing newline."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def valid_invite_code(code: str) -> bool:
    """A bare invite code (not a URL), and not the REPLACE_ME placeholder."""
    return bool(code) and code != "REPLACE_ME" and bool(_INVITE_RE.fullmatch(code))


def parse_csp(value: str) -> dict[str, list[str]]:
    """Content-Security-Policy header -> {directive: [sources]} (lower-cased names,
    first occurrence wins, as browsers do). site/src/data/security.json holds the policy
    as that same mapping; the site joins it into the header."""
    out: dict[str, list[str]] = {}
    for part in value.split(";"):
        bits = part.split()
        if bits and bits[0].lower() not in out:
            out[bits[0].lower()] = bits[1:]
    return out


def csp_allows(sources: list[str], url: str, self_origin: bool = True) -> bool:
    """Whether a source list admits `url` (an absolute https URL or a same-origin
    path). Handles 'self', scheme-only and host sources with an optional path
    prefix; enough for the site's own policy, not a full CSP engine."""
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
    dst = root / "site" / "public" / "img" / "og.png"
    with Image.open(src) as im:
        im = im.convert("RGB")
        card = im.crop(cover_box(*im.size, *OG_SIZE)).resize(OG_SIZE, Image.LANCZOS)
    card.save(dst, optimize=True)
    print("site/public/img/og.png written", card.size)


if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if "--og" in sys.argv[1:]:
        _write_og(root)
        raise SystemExit(0)

    import config

    out = root / "site" / "src" / "data" / "generated"
    files = {
        "games.json": dump_json(games_data((g.emoji, g.channel, g.role) for g in config.GAMES)),
        "rules.json": dump_json(rules_data(config.layout.POSTS["rules"]["description"])),
    }
    for name, text in files.items():
        path = out / name
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != text:
            path.write_text(text, encoding="utf-8", newline="\n")
        print(f"site/src/data/generated/{name}:", "updated" if old != text else "already current")
