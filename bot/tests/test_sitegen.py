"""The landing page's games list is generated from server/layout.py GAMES."""

import re
from pathlib import Path

import pytest

import config
from logic import sitegen

SITE = Path(__file__).resolve().parents[2] / "site"


def test_render_games_lists_each_game_in_order():
    html = sitegen.render_games([("🎯", "valorant", "Valorant"), ("🧱", "roblox", "Roblox")])
    assert html.index("Valorant") < html.index("Roblox")
    assert html.count("<li") == 2
    assert "🎯" in html and 'aria-hidden="true"' in html


def test_render_games_escapes_html():
    html = sitegen.render_games([("<b>", "x", "Tom & Jerry <3")])
    assert "Tom &amp; Jerry &lt;3" in html
    assert "<b>" not in html


def test_replace_block_swaps_only_the_marked_region():
    page = "a\n<!-- games:start -->\nold\n<!-- games:end -->\nb"
    out = sitegen.replace_block(page, "games", "NEW")
    assert out == "a\n<!-- games:start -->\nNEW\n<!-- games:end -->\nb"


def test_replace_block_is_idempotent():
    page = "<!-- games:start --><!-- games:end -->"
    once = sitegen.replace_block(page, "games", "X")
    assert sitegen.replace_block(once, "games", "X") == once


def test_replace_block_requires_markers():
    with pytest.raises(ValueError):
        sitegen.replace_block("no markers here", "games", "X")


def test_extract_block_round_trips():
    page = sitegen.replace_block("<!-- games:start --><!-- games:end -->", "games", "BODY")
    assert sitegen.extract_block(page, "games") == "BODY"


def test_valid_invite_code():
    assert sitegen.valid_invite_code("chill-gamers")
    assert sitegen.valid_invite_code("AbC123")
    assert not sitegen.valid_invite_code("REPLACE_ME")
    assert not sitegen.valid_invite_code("")
    assert not sitegen.valid_invite_code("https://discord.gg/abc")
    assert not sitegen.valid_invite_code("a b")


# ------------------------------------------------------------ the real site


def test_site_games_match_layout():
    """Run `python -m logic.sitegen` from bot/ after editing layout.GAMES."""
    page = (SITE / "index.html").read_text(encoding="utf-8")
    games = [(g.emoji, g.channel, g.role) for g in config.GAMES]
    assert sitegen.extract_block(page, "games").strip() == sitegen.render_games(games).strip()


def test_site_invite_code_lives_only_in_config_js():
    cfg = (SITE / "config.js").read_text(encoding="utf-8")
    assert re.search(r'INVITE_CODE\s*[:=]\s*"[^"]+"', cfg)
    for name in ("index.html", "app.js", "style.css"):
        text = (SITE / name).read_text(encoding="utf-8")
        assert "discord.gg/" not in text.replace("discord.gg/${", "")


def test_site_has_no_trackers_or_ids():
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".js", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{17,20}\b", text), f"snowflake-like ID in {path.name}"
        for bad in ("googletagmanager", "google-analytics", "facebook.net", "hotjar", "plausible"):
            assert bad not in text, f"{bad} in {path.name}"


def test_site_invite_code_is_placeholder_or_valid():
    cfg = (SITE / "config.js").read_text(encoding="utf-8")
    code = re.search(r'INVITE_CODE\s*:\s*"([^"]*)"', cfg).group(1)
    assert code == "REPLACE_ME" or sitegen.valid_invite_code(code)


def test_site_join_buttons_are_wired_by_app_js():
    page = (SITE / "index.html").read_text(encoding="utf-8")
    assert page.count("data-join") >= 3
    assert '<script src="config.js"' in page and '<script src="app.js"' in page
    assert page.index('src="config.js"') < page.index('src="app.js"')
    js = (SITE / "app.js").read_text(encoding="utf-8")
    assert "with_counts=true" in js and "approximate_presence_count" in js


def test_site_assets_referenced_exist():
    page = (SITE / "index.html").read_text(encoding="utf-8")
    for src in set(re.findall(r'(?:src|href)="((?:img/)[^"]+)"', page)):
        assert (SITE / src).is_file(), src


# ------------------------------------------------------- social cards / SEO

CANONICAL = "https://gr8monk3ys.github.io/discord-server/"


def test_meta_tags_reads_property_and_name_in_any_attribute_order():
    page = (
        '<meta property="og:title" content="A &amp; B">'
        '<meta content="Big" name="twitter:card">'
        "<meta charset=\"utf-8\">"
    )
    assert sitegen.meta_tags(page) == {"og:title": "A & B", "twitter:card": "Big"}


def test_cover_box_crops_the_long_side_centred():
    # 1920x1080 into 1200x630 (wider): keep full width, trim top and bottom equally
    left, top, right, bottom = sitegen.cover_box(1920, 1080, 1200, 630)
    assert (left, right) == (0, 1920)
    assert abs((right - left) / (bottom - top) - 1200 / 630) < 0.01
    assert abs(top - (1080 - bottom)) <= 1
    # a square target from a wide source keeps the full height and trims the sides
    assert sitegen.cover_box(1920, 1080, 1080, 1080) == (420, 0, 1500, 1080)


def test_cover_box_same_ratio_is_the_whole_image():
    assert sitegen.cover_box(2400, 1260, 1200, 630) == (0, 0, 2400, 1260)


def _page():
    return (SITE / "index.html").read_text(encoding="utf-8")


def test_site_has_canonical_url():
    assert f'<link rel="canonical" href="{CANONICAL}">' in _page()


def test_site_has_open_graph_and_twitter_cards():
    meta = sitegen.meta_tags(_page())
    for key in ("og:title", "og:description", "og:image", "og:url", "og:type", "og:site_name",
                "og:image:alt", "twitter:card", "twitter:title", "twitter:description",
                "twitter:image", "twitter:image:alt", "description"):
        assert meta.get(key, "").strip(), key
    assert meta["og:url"] == CANONICAL
    assert meta["og:image"] == CANONICAL + "img/og.png"  # crawlers need an absolute URL
    assert meta["twitter:image"] == meta["og:image"]
    assert meta["twitter:card"] == "summary_large_image"
    assert (meta["og:image:width"], meta["og:image:height"]) == ("1200", "630")
    assert len(meta["og:description"]) <= 200 and len(meta["og:title"]) <= 70


def test_og_image_exists_and_is_1200x630():
    pil = pytest.importorskip("PIL.Image")
    path = SITE / "img" / "og.png"
    assert path.is_file()
    with pil.open(path) as im:
        assert im.size == (1200, 630)
    assert path.stat().st_size < 1_000_000  # unfurlers skip huge images


def test_site_json_ld_is_minimal_organization_and_website():
    import json

    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', _page(), re.S)
    assert len(blocks) == 1
    data = json.loads(blocks[0])
    types = {node["@type"]: node for node in data["@graph"]}
    assert set(types) == {"Organization", "WebSite"}
    assert types["WebSite"]["url"] == CANONICAL and types["Organization"]["url"] == CANONICAL
    assert types["Organization"]["logo"].startswith(CANONICAL + "img/")
    assert (SITE / types["Organization"]["logo"][len(CANONICAL):]).is_file()


def test_site_feature_cards_cover_the_current_server():
    features = re.search(r'<section id="features".*?</section>', _page(), re.S).group(0)
    cards = re.findall(r"<li\b", features)
    assert 9 <= len(cards) <= 12
    for want in ("Levels", "rank card", "tournament", "Weekly challenges", "Daily Word",
                 "Starter quest", "Creator spotlight", "Self roles", "Partners"):
        assert want.lower() in features.lower(), want


def test_site_has_this_week_strip_and_updated_faq():
    page = _page()
    week = re.search(r'<section id="week".*?</section>', page, re.S)
    assert week, "This week on the server strip"
    assert "This week on the server" in week.group(0)
    faq = re.search(r'<section id="faq".*?</section>', page, re.S).group(0).lower()
    for want in ("level", "roles", "partner"):
        assert want in faq, want
    assert '<p class="counts" id="counts"' in page  # live counts stay
