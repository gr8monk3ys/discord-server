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
