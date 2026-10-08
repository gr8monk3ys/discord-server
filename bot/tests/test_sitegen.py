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
    for name in ("index.html", "404.html", "app.js", "theme.js", "style.css", "vercel.json"):
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
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="/?((?:img/)?[\w.-]+\.(?:png|css|js))"', page)
        assert refs, name
        for src in set(refs):
            assert (SITE / src).is_file(), f"{name}: {src}"


# ------------------------------------------------------- social cards / SEO

CANONICAL = "https://discord.lscaturchio.xyz/"


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


def test_canonical_is_one_origin_everywhere():
    """GitHub Pages is a mirror: no absolute URL on the page points at it."""
    page = _page()
    assert "github.io" not in page
    urls = set(re.findall(r'"(https://discord\.lscaturchio\.xyz[^"]*)"', page))
    assert urls and all(u.startswith(CANONICAL) for u in urls)


# ------------------------------------------------- lscaturchio.xyz design system

def _css():
    return (SITE / "style.css").read_text(encoding="utf-8")


def _vars(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def _rule(css: str, selector: str) -> str:
    i = css.index(selector + " {")
    return css[i:css.index("}", i)]


THEME_TOKENS = {"--page", "--card", "--tint", "--ink", "--ink-muted", "--hairline", "--pen",
                "--pen-text"}


def test_both_themes_define_the_same_tokens():
    css = _css()
    paper = _vars(_rule(css, ":root"))
    media = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{(.*?)\n\}", css, re.S)
    assert media, "night theme follows prefers-color-scheme"
    system_night = _vars(_rule(media.group(1), ':root:not([data-theme="light"])'))
    forced_night = _vars(_rule(css, ':root[data-theme="dark"]'))
    assert THEME_TOKENS <= set(paper)
    assert THEME_TOKENS <= set(system_night)
    assert system_night == forced_night  # the toggle and the OS give the same night
    # the house values from lscaturchio.xyz DESIGN.md
    assert paper["--page"].lower() == "#f9f8f5" and paper["--pen"].lower() == "#184e35"
    assert forced_night["--page"].lower() == "#111317"
    assert forced_night["--pen"].lower() == "#42a979"


def test_house_type_and_motion_rules():
    css = _css()
    for family in ("Fraunces", "Instrument Sans", "IBM Plex Mono"):
        assert family in css, family
    assert "letter-spacing: 0.16em" in css  # the wall label
    assert ":focus-visible { outline: 2px solid var(--pen); outline-offset: 2px; }" in css
    reduce = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", css, re.S)
    assert reduce and "transition-duration: 0.01ms" in reduce.group(1)
    # smooth scrolling only for readers who did not ask for less motion
    assert "@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }" in css
    assert css.count("scroll-behavior: smooth") == 1


def test_theme_toggle_is_wired_and_storage_is_guarded():
    js = (SITE / "theme.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in js and "data-theme" in js
    for m in re.finditer(r"localStorage", js):  # every access sits inside a try block
        before = js[:m.start()]
        assert before.rfind("try") > before.rfind("}"), "unguarded localStorage"
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        head = page[:page.index("</head>")]
        assert re.search(r'<script src="/?theme\.js"></script>', head), name  # no defer: no flash
        assert "data-theme-toggle" in page and 'aria-label="Toggle theme"' in page, name
        assert '<meta name="color-scheme" content="light dark">' in page, name


def test_header_links_back_to_lscaturchio():
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        header = re.search(r'<header class="top">.*?</header>', page, re.S).group(0)
        assert re.search(r'<a class="byline" href="https://lscaturchio\.xyz"[^>]*>by Lorenzo', header), name


def test_404_page():
    page = (SITE / "404.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, follow">' in page
    assert "404" in page and "<title>" in page
    # served for missing URLs at any depth, so local links are root-absolute
    for ref in re.findall(r'(?:src|href)="([^"#]+)"', page):
        assert ref.startswith(("/", "https://")), ref


# -------------------------------------------------------------- Vercel deploy

def _vercel():
    import json

    return json.loads((SITE / "vercel.json").read_text(encoding="utf-8"))


def _headers_for(cfg, path):
    """Headers Vercel attaches to `path` (our sources are plain regexes, valid path-to-regexp)."""
    out = {}
    for rule in cfg["headers"]:
        if re.fullmatch(rule["source"], path):
            for h in rule["headers"]:
                assert h["key"] not in out, f"{h['key']} set twice for {path}"
                out[h["key"]] = h["value"]
    return out


def test_vercel_json_is_a_static_no_build_config():
    cfg = _vercel()
    assert cfg["cleanUrls"] is True
    assert cfg.get("framework") is None and cfg.get("installCommand") == ""
    assert not cfg.get("buildCommand")
    assert not (SITE / "package.json").exists()


def test_vercel_security_headers():
    h = _headers_for(_vercel(), "/")
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in h["Permissions-Policy"] and "geolocation=()" in h["Permissions-Policy"]
    csp = sitegen.parse_csp(h["Content-Security-Policy"])
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'"]
    assert csp["connect-src"] == ["https://discord.com"]
    assert "https://fonts.googleapis.com" in csp["style-src"]
    assert "https://fonts.gstatic.com" in csp["font-src"]
    assert csp["frame-ancestors"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert "unsafe" not in h["Content-Security-Policy"]


def test_vercel_cache_headers():
    cfg = _vercel()
    for path in ("/img/og.png", "/img/server-icon.png"):
        cc = _headers_for(cfg, path)["Cache-Control"]
        assert int(re.search(r"max-age=(\d+)", cc).group(1)) >= 7 * 86400, path
    for path in ("/", "/404", "/index.html", "/style.css", "/config.js"):
        h = _headers_for(cfg, path)
        assert "max-age=0" in h["Cache-Control"], path
        assert "Content-Security-Policy" in h, path


def test_pages_obey_the_csp():
    """Everything the pages load is allowed by the policy; no inline code or styles."""
    csp = sitegen.parse_csp(_headers_for(_vercel(), "/")["Content-Security-Policy"])
    js = (SITE / "app.js").read_text(encoding="utf-8")
    fetched = re.findall(r'"(https://[^"]+)', js)
    assert fetched
    for url in fetched:
        assert sitegen.csp_allows(csp["connect-src"], url), url
    for name in ("index.html", "404.html"):
        page = (SITE / name).read_text(encoding="utf-8")
        assert "<style" not in page and " style=" not in page, name
        assert not re.search(r"\son[a-z]+=", page), f"inline handler in {name}"
        for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S):
            if 'type="application/ld+json"' in attrs:
                continue  # a data block, never executed
            src = re.search(r'src="([^"]+)"', attrs)
            assert src and not body.strip(), f"inline script in {name}"
            assert sitegen.csp_allows(csp["script-src"], src.group(1)), src.group(1)
        for href in re.findall(r'<link rel="stylesheet" href="([^"]+)"', page):
            assert sitegen.csp_allows(csp["style-src"], href), href
        for src in re.findall(r'<img [^>]*src="([^"]+)"', page):
            assert sitegen.csp_allows(csp["img-src"], src), src


def test_parse_csp_and_csp_allows():
    csp = sitegen.parse_csp("default-src 'self'; connect-src https://discord.com;; IMG-SRC 'self' data:")
    assert csp == {"default-src": ["'self'"], "connect-src": ["https://discord.com"],
                   "img-src": ["'self'", "data:"]}
    assert sitegen.csp_allows(csp["connect-src"], "https://discord.com/api/v10/invites/x")
    assert not sitegen.csp_allows(csp["connect-src"], "https://discord.com.evil.test/x")
    assert not sitegen.csp_allows(csp["connect-src"], "style.css")
    assert sitegen.csp_allows(csp["img-src"], "img/a.png")
    assert sitegen.csp_allows(csp["img-src"], "data:image/png;base64,AAAA")


def test_pages_workflow_rewrites_404_for_the_mirror():
    wf = (SITE.parent / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "site/404.html" in wf and "GITHUB_REPOSITORY" in wf
    assert "path: site" in wf
